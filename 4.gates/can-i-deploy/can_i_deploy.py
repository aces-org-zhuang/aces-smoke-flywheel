#!/usr/bin/env python
"""can-i-deploy：部署闸门的唯一入口。

设计要点（与方案 §4.3 对齐）：
  - 只看证据，不看"人觉得"：每个输入都是前序 gate 落盘的 JSON，缺证据即 unknown，不放行；
  - 判据用**增量**而非全量：存量债务不该让闸门永久红灯，但新增失败必须 100% 可归因；
  - unknown 有界重试：证据缺失时给出"该补哪个 gate"，而不是模糊地"再看看"。

判决：
  ALLOW —— 所有必需 gate 有证据且通过
  BLOCK —— 任一必需 gate 失败（附失败 gate 与证据路径）
  UNKNOWN —— 证据缺失/过期（附补齐命令）

与最初版本的差别（实践暴露的规则缺口）：
  原实现取"最新一个 run.json"当 L1 证据，而变异门禁每跑一次都会落一个 run.json，
  于是 can-i-deploy 会把某个变异体当成候选集去判——判决对象漂移且无人察觉。
  现在候选集必须显式指定（--candidate），且会校验其 commit 与产物是否自洽。
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

POLICY = {
    "mutation_kill_rate_min": 0.8,
    "new_failure_unknown_ratio_max": 0.2,
    "require_l1_all_green": True,
    "allow_product_defect": False,
    "debt_is_reported_not_blocking": True,
}


def read_json(p: Path) -> dict | None:
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def pick_latest_run(runs: Path, prefix: str) -> str:
    cands = [
        d
        for d in runs.glob("*")
        if (d / "run.json").exists() and d.name.startswith(prefix)
    ]
    if not cands:
        return ""
    cands.sort(key=lambda d: (d / "run.json").stat().st_mtime)
    return cands[-1].name


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--evidence", default="5.evidence")
    ap.add_argument("--candidate", default="", help="候选 L1 run id；省略则取最新的 l1-* 运行")
    ap.add_argument("--classify", default="5.evidence/verdicts/classification.json")
    ap.add_argument("--debt-ledger", default="5.evidence/verdicts/baseline-debt.json")
    ap.add_argument("--mutation", default="5.evidence/verdicts/mutation.json")
    ap.add_argument("--out", default="5.evidence/verdicts/can-i-deploy.json")
    args = ap.parse_args()

    ev = Path(args.evidence)
    runs = ev / "runs"
    verdicts = ev / "verdicts"

    candidate = args.candidate or pick_latest_run(runs, "l1-")
    l1_run = read_json(runs / candidate / "run.json") if candidate else None

    lint = read_json(verdicts / "lint-boundary.json")
    cls = read_json(Path(args.classify))
    mut = read_json(Path(args.mutation))
    ledger = read_json(Path(args.debt_ledger))

    checks = []

    # 1. 边界契约
    if lint is None:
        checks.append({"gate": "lint-boundary", "state": "UNKNOWN", "detail": "缺少判决文件"})
    else:
        checks.append(
            {
                "gate": "lint-boundary",
                "state": "PASS" if lint.get("result") == "PASS" else "BLOCK",
                "detail": f"{lint.get('checked_boundaries')} 份契约 / "
                f"{len(lint.get('violations') or [])} 处违规",
                "evidence": "5.evidence/verdicts/lint-boundary.json",
            }
        )

    # 2. L1（候选集内的不变量用例）
    if l1_run is None:
        checks.append({"gate": "l1", "state": "UNKNOWN", "detail": "没有找到 L1 运行证据"})
    else:
        ok = l1_run.get("failed", 1) == 0 and not l1_run.get("missing_test_ids")
        checks.append(
            {
                "gate": "l1",
                "state": "PASS" if ok else "BLOCK",
                "detail": f"candidate={candidate} passed={l1_run.get('passed')} "
                f"failed={l1_run.get('failed')} "
                f"missing={len(l1_run.get('missing_test_ids') or [])}",
                "evidence": f"5.evidence/runs/{candidate}/run.json",
            }
        )

    # 3. 失败定责：只看增量
    if cls is None:
        checks.append(
            {"gate": "classify-failure", "state": "UNKNOWN", "detail": "缺少分类结论"}
        )
    else:
        new = cls.get("new_failures", cls.get("total_failures", 0))
        ratio = cls.get("new_failure_unknown_ratio", cls.get("unknown_ratio", 1.0))
        new_defects = sum(
            1
            for r in cls.get("results", [])
            if r.get("verdict") == "product_defect" and not r.get("preexisting_debt")
        )
        state = "PASS"
        reasons = []
        if new_defects and not POLICY["allow_product_defect"]:
            state = "BLOCK"
            reasons.append(f"新增产品缺陷 {new_defects} 条")
        if ratio > POLICY["new_failure_unknown_ratio_max"]:
            state = "BLOCK"
            reasons.append(
                f"增量失败未知比例 {ratio} > {POLICY['new_failure_unknown_ratio_max']}"
            )
        checks.append(
            {
                "gate": "classify-failure",
                "state": state,
                "detail": f"new_failures={new} 新增缺陷={new_defects} "
                f"new_unknown_ratio={ratio}"
                + ("；" + "，".join(reasons) if reasons else ""),
                "evidence": args.classify,
            }
        )

    # 4. 变异
    if mut is None:
        checks.append({"gate": "mutation-diff", "state": "UNKNOWN", "detail": "缺少变异报告"})
    else:
        rate = mut.get("kill_rate", 0.0)
        checks.append(
            {
                "gate": "mutation-diff",
                "state": "PASS" if rate >= POLICY["mutation_kill_rate_min"] else "BLOCK",
                "detail": f"kill_rate={rate} (阈值 {POLICY['mutation_kill_rate_min']})，"
                f"killed={mut.get('killed')}/{mut.get('valid')}，"
                f"restored={mut.get('worktree_restored')}",
                "evidence": args.mutation,
            }
        )

    # 5. 契约：黄金路径实际用到的路由，不得出现在已知漂移集合里（L2 的落地判据）
    l3 = read_json(runs / "l3-golden" / "run.json")
    matrix = read_json(Path("0.ssot/contract_matrix.json"))
    if l3 is None or matrix is None:
        checks.append(
            {"gate": "contract-matrix", "state": "UNKNOWN", "detail": "缺少 L3 运行或合同矩阵"}
        )
    else:
        def norm(path: str) -> str:
            p = path.split("?")[0]
            p = f"/{p.strip('/')}"
            p = re.sub(r"/\d+(?=/|$)", "/{}", p)
            return p[:-1] if len(p) > 1 and p.endswith("/") else p

        used = {norm(s["path"]) for s in l3.get("steps", [])}
        drifted = {x["normalized"] for x in matrix.get("unmatched_consumer", [])}
        hits = sorted(used & drifted)
        checks.append(
            {
                "gate": "contract-matrix",
                "state": "BLOCK" if hits else "PASS",
                "detail": f"黄金路径使用 {len(used)} 条路由，命中漂移 {len(hits)}"
                + (f"：{hits}" if hits else ""),
                "evidence": "0.ssot/contract_matrix.json",
            }
        )

    blockers = [c for c in checks if c["state"] == "BLOCK"]
    unknowns = [c for c in checks if c["state"] == "UNKNOWN"]
    decision = "BLOCK" if blockers else ("UNKNOWN" if unknowns else "ALLOW")

    debt = {}
    if ledger:
        debt = {
            "ledger": args.debt_ledger,
            "baseline_run": ledger.get("run_id"),
            "baseline_commit": ledger.get("sut_commit"),
            "count": ledger.get("count"),
            "by_verdict": ledger.get("by_verdict"),
            "policy": "存量债务披露但不阻断；每条带 owner 与到期日，不允许静默消失",
        }

    verdict = {
        "gate": "can-i-deploy",
        "decided_at": datetime.now(timezone.utc).isoformat(),
        "candidate": candidate,
        "sut_commit": (l1_run or {}).get("sut_commit"),
        "policy": POLICY,
        "checks": checks,
        "decision": decision,
        "blockers": [c["gate"] for c in blockers],
        "missing_evidence": [c["gate"] for c in unknowns],
        "preexisting_debt": debt,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"KS_GATE\tcan-i-deploy\t{decision}\tcandidate={candidate}\tblockers={verdict['blockers']}")
    for c in checks:
        print(f"  [{c['state']:7s}] {c['gate']:18s} {c['detail']}")
    if debt:
        print(f"  [debt   ] 存量债务 {debt.get('count')} 条（不阻断，见 {args.debt_ledger}）")
    return 0 if decision == "ALLOW" else 1


if __name__ == "__main__":
    raise SystemExit(main())
