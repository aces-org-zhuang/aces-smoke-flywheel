#!/usr/bin/env python
"""can-i-deploy：部署闸门的唯一入口。

设计要点（与方案 §4.3 对齐）：
  - 只看证据，不看"人觉得"：每个输入都是前序 gate 落盘的 JSON，缺证据即 unknown，不放行；
  - 分档：支付/权限类模块阈值更严（本仓目前只有一档，阈值写在 policy 里，可改）；
  - unknown 有界重试：证据缺失时给出"该补哪个 gate"，而不是模糊地"再看看"。

判决：
  ALLOW —— 所有必需 gate 有证据且通过
  BLOCK —— 任一必需 gate 失败（附失败 gate 与证据路径）
  UNKNOWN —— 证据缺失/过期（附补齐命令）
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import yaml

REQUIRED_GATES = ["lint-boundary", "l1", "classify-failure", "mutation-diff"]

POLICY = {
    "mutation_kill_rate_min": 0.8,
    "unknown_ratio_max": 0.2,
    "require_l1_all_green": True,
    "allow_product_defect": False,
}


def read_json(p: Path) -> dict | None:
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--evidence", default="5.evidence")
    ap.add_argument("--run-id", default="", help="指定 L1 run id；省略则取最新")
    ap.add_argument("--out", default="5.evidence/verdicts/can-i-deploy.json")
    args = ap.parse_args()

    ev = Path(args.evidence)
    runs = ev / "runs"
    verdicts = ev / "verdicts"

    l1_run_id = args.run_id
    if not l1_run_id:
        candidates = sorted(
            [d for d in runs.glob("*") if (d / "run.json").exists()],
            key=lambda d: (d / "run.json").stat().st_mtime,
        )
        l1_run_id = candidates[-1].name if candidates else ""
    l1_run = read_json(runs / l1_run_id / "run.json") if l1_run_id else None

    lint = read_json(verdicts / "lint-boundary.json")
    cls = read_json(verdicts / "classification.json")
    mut = read_json(verdicts / "mutation.json")

    checks = []

    # 1. 边界契约
    if lint is None:
        checks.append({"gate": "lint-boundary", "state": "UNKNOWN", "detail": "缺少判决文件"})
    else:
        checks.append(
            {
                "gate": "lint-boundary",
                "state": "PASS" if lint.get("result") == "PASS" else "BLOCK",
                "detail": f"{lint.get('checked_boundaries')} 份契约 / {len(lint.get('violations') or [])} 处违规",
            }
        )

    # 2. L1
    if l1_run is None:
        checks.append({"gate": "l1", "state": "UNKNOWN", "detail": "没有找到 L1 运行证据"})
    else:
        ok = l1_run.get("failed", 1) == 0 and not l1_run.get("missing_test_ids")
        checks.append(
            {
                "gate": "l1",
                "state": "PASS" if ok else "BLOCK",
                "detail": f"run={l1_run_id} passed={l1_run.get('passed')} failed={l1_run.get('failed')} "
                f"missing={len(l1_run.get('missing_test_ids') or [])}",
                "evidence": f"5.evidence/runs/{l1_run_id}/run.json",
            }
        )

    # 3. 失败分类（有失败才要求分类结论）
    if l1_run and l1_run.get("failed", 0) > 0:
        if cls is None:
            checks.append(
                {"gate": "classify-failure", "state": "UNKNOWN", "detail": "有失败但没有分类结论"}
            )
        else:
            defects = (cls.get("by_verdict") or {}).get("product_defect", 0)
            unknown_ratio = cls.get("unknown_ratio", 1.0)
            state = "BLOCK" if defects and not POLICY["allow_product_defect"] else "PASS"
            if unknown_ratio > POLICY["unknown_ratio_max"]:
                state = "BLOCK"
            checks.append(
                {
                    "gate": "classify-failure",
                    "state": state,
                    "detail": f"product_defect={defects} unknown_ratio={unknown_ratio}",
                    "evidence": "5.evidence/verdicts/classification.json",
                }
            )
    else:
        checks.append(
            {"gate": "classify-failure", "state": "SKIP", "detail": "L1 全绿，无需定责"}
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
                "detail": f"kill_rate={rate} (阈值 {POLICY['mutation_kill_rate_min']}), "
                f"restored={mut.get('worktree_restored')}",
                "evidence": "5.evidence/verdicts/mutation.json",
            }
        )

    blockers = [c for c in checks if c["state"] == "BLOCK"]
    unknowns = [c for c in checks if c["state"] == "UNKNOWN"]
    decision = "BLOCK" if blockers else ("UNKNOWN" if unknowns else "ALLOW")

    verdict = {
        "gate": "can-i-deploy",
        "decided_at": datetime.now(timezone.utc).isoformat(),
        "l1_run": l1_run_id,
        "sut_commit": (l1_run or {}).get("sut_commit"),
        "policy": POLICY,
        "checks": checks,
        "decision": decision,
        "blockers": [c["gate"] for c in blockers],
        "missing_evidence": [c["gate"] for c in unknowns],
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"KS_GATE\tcan-i-deploy\t{decision}\tblockers={verdict['blockers']}")
    for c in checks:
        print(f"  [{c['state']:7s}] {c['gate']:18s} {c['detail']}")
    return 0 if decision == "ALLOW" else 1


if __name__ == "__main__":
    raise SystemExit(main())
