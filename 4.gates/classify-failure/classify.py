#!/usr/bin/env python
"""classify-failure：确定性失败分类器（顺序不可逆）。

规则来自方案 §3.4 约束 A，落到本仓的五条可执行判据：

  S0 声明式环境排除 —— 失败文本自己指认了缺失的前置条件，且该前置条件已在
                       target.yaml#environment_deviations 里声明并被探针验证过
  S1 重跑确认       —— 同 commit 重跑两次均过 → flaky 候选（隔离区，不计入通过率）
  S2 归属判定       —— 两条并列证据，任一成立即归属产品缺陷：
                       a) 改动面：失败栈涉及的生产文件出现在 baseline..HEAD 的改动集里
                       b) 增量：该用例在基线运行里是通过的、在候选里失败，且候选确实有改动
                       → 产品缺陷（用例冻结，禁止改用例）
  S3 契约违背       —— 失败发生在 API 层，且 0.ssot 合同矩阵显示该路由存在漂移
                       → 契约违背（阻断 + 通知双方）
  S4 其余           —— 未知，进隔离区，必须人工签字（owner + 7 天到期）

判据全部可复算：每条结论都带 evidence 引用（commit / 文件 / 行 / 合同矩阵键）。
LLM 不参与定责——它只能对已定责的"用例陈旧"提修复提案。

两个易错点，这里刻意写死：
  1. S0 的例外表**只能来自 target.yaml**，不能写在代码里，否则等于给"看着像环境问题"
     开后门。代码里不出现任何具体包名/主机名。
  2. S2 必须用 baseline..HEAD 的**改动集**判定，不能用"文件最后一次提交的 sha"。
     基线自身 HEAD==baseline 时后者会把历史文件全部误判成产品缺陷；实测该写法在本仓
     返回空串，规则静默失效——最坏的一种：看起来跑过了，其实什么都没判。
  3. 但只用失败栈也不够。assertRaises / assertEqual 这类失败的调用栈指向**测试文件**，
     生产代码根本不在栈里，于是"按栈归属"对最常见的一类缺陷永久失效。实测三次注入缺陷
     全部落到 S4 unknown。所以 S2 必须再补一条不依赖栈的判据：基线通过 + 候选失败 + 有改动。
     这条判据的证据是两份 JUnit（基线 passed 记录 + 候选 failed 记录），同样可复算。

存量债务（preexisting debt）不是一个"原因"，而是一个状态：
  基线里已经失败的用例，在候选运行里再失败，不应该被重复定责，也不应该让闸门永久红灯。
  所以它记在 preexisting_debt 字段上，verdict 仍然记录真实原因。
  闸门看的是 new_failure_unknown_ratio（增量失败里的未知比例），
  unknown_ratio（全量比例）保留在报告里用于披露，不参与放行判定。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "tools"))
from junit_lib import parse_junit_dir, source_files_in_traceback  # noqa: E402

HTTP_STATUS = re.compile(r"\b(4\d{2}|5\d{2})\b")


def git(repo: Path, *args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(repo), *args], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except subprocess.CalledProcessError:
        return ""


def build_env_rules(target: dict) -> list[dict]:
    """把 target.yaml 里的偏差声明编译成可执行规则。

    虚拟化声明与 environment_deviations 合并成一类：两者都是"被测环境与上游 CI 的差异"，
    对分类器的语义完全一样——可解释的环境排除项。
    """
    rules: list[dict] = []
    rt = target.get("runtime", {})

    for v in rt.get("virtualization", []) or []:
        tokens = [v.get("package", "")] + list(v.get("match_message") or [])
        rules.append(
            {
                "rule": f"S0-virtualized:{v.get('package')}",
                "match_message": [t for t in tokens if t],
                "match_test_prefix": [],
                "declared_in": "3.env/target.yaml#runtime.virtualization",
                "reason": (v.get("reason") or "").strip().splitlines()[0]
                if v.get("reason")
                else "",
            }
        )

    for d in rt.get("environment_deviations", []) or []:
        rules.append(
            {
                "rule": f"S0-env:{d['id']}",
                "match_message": [t for t in (d.get("match_message") or []) if t],
                "match_test_prefix": [t for t in (d.get("match_test_prefix") or []) if t],
                "declared_in": "3.env/target.yaml#runtime.environment_deviations",
                "reason": (d.get("change") or "").strip(),
                "probe": (d.get("probe") or "").strip(),
                "prefix_reason": (d.get("prefix_reason") or "").strip(),
            }
        )
    return rules


def match_env_rule(rules: list[dict], test_id: str, blob: str) -> dict | None:
    for r in rules:
        for token in r["match_message"]:
            if token.lower() in blob.lower():
                return {**r, "matched_on": "message", "token": token}
        for prefix in r["match_test_prefix"]:
            if test_id.startswith(prefix):
                return {**r, "matched_on": "test_prefix", "token": prefix}
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--env", default="3.env/target.yaml")
    ap.add_argument("--matrix", default="0.ssot/contract_matrix.json")
    ap.add_argument("--out", default="5.evidence/verdicts/classification.json")
    ap.add_argument("--debt-ledger", default="", help="基线债务台账；命中者标记为存量失败")
    ap.add_argument("--baseline-run", default="5.evidence/runs/baseline", help="基线运行目录，用于增量归属")
    ap.add_argument("--write-ledger", default="", help="把本次分类结果写成基线债务台账")
    ap.add_argument("--quarantine-days", type=int, default=7)
    args = ap.parse_args()

    target = yaml.safe_load(Path(args.env).read_text(encoding="utf-8"))
    sut_root = Path(target["runtime"]["sut_root"])
    baseline_commit = target["target"]["commit"]
    run_dir = Path(args.run_dir)

    run_meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    cases = parse_junit_dir(run_dir / "junit")
    failures = [c for c in cases if c["status"] in {"failed", "error"}]

    rerun = {}
    rerun_file = run_dir / "rerun.json"
    if rerun_file.exists():
        rerun = json.loads(rerun_file.read_text(encoding="utf-8"))

    matrix = {}
    if Path(args.matrix).exists():
        matrix = json.loads(Path(args.matrix).read_text(encoding="utf-8"))
    drifted = {x["normalized"] for x in matrix.get("unmatched_consumer", [])}

    ledger_tests: set[str] = set()
    ledger_meta: dict = {}
    if args.debt_ledger and Path(args.debt_ledger).exists():
        ledger = json.loads(Path(args.debt_ledger).read_text(encoding="utf-8"))
        ledger_tests = {t["test"] for t in ledger.get("tests", [])}
        ledger_meta = {
            "run_id": ledger.get("run_id"),
            "sut_commit": ledger.get("sut_commit"),
            "count": len(ledger_tests),
            "path": args.debt_ledger,
        }

    # S2 的判据之一：baseline..HEAD 之间真正被改过的文件集合
    changed_out = git(sut_root, "diff", "--name-only", f"{baseline_commit}..HEAD")
    changed_files = {line.strip() for line in changed_out.splitlines() if line.strip()}
    head_commit = git(sut_root, "rev-parse", "HEAD")

    # S2 的判据之二：基线里哪些用例是绿的。这是"候选引入"的直接反证材料。
    baseline_passed: set[str] = set()
    baseline_run = Path(args.baseline_run)
    if (baseline_run / "junit").exists():
        for c in parse_junit_dir(baseline_run / "junit"):
            if c["status"] == "passed":
                baseline_passed.add(f"{c['classname']}.{c['name']}")
    baseline_available = bool(baseline_passed)

    env_rules = build_env_rules(target)
    due = (datetime.now(timezone.utc) + timedelta(days=args.quarantine_days)).date().isoformat()
    results = []
    for c in failures:
        test_id = f"{c['classname']}.{c['name']}"
        srcs = source_files_in_traceback(c["detail"], sut_root)
        blob = f"{c['message']} {c['detail']}"
        verdict, evidence = "unknown", []

        # S0 声明式环境排除
        hit = match_env_rule(env_rules, test_id, blob)
        if hit:
            verdict = "environment_excluded"
            evidence.append(
                {
                    "rule": hit["rule"],
                    "matched_on": hit["matched_on"],
                    "token": hit["token"],
                    "declared_in": hit["declared_in"],
                    "reason": hit["reason"],
                    "probe": hit.get("probe", ""),
                    "prefix_reason": hit.get("prefix_reason", ""),
                    "detail": "失败信息直接命中已声明并验证过的环境偏差，属环境性排除项",
                }
            )

        # S1 重跑
        passes = rerun.get("passed_twice", [])
        if verdict == "unknown" and any(
            test_id.endswith(p.split(".")[-1]) for p in passes
        ):
            verdict = "flaky_candidate"
            evidence.append({"rule": "S1-rerun", "detail": "同 commit 重跑两次均通过"})

        # S2 归属：只认 baseline..HEAD 改动集里的文件
        if verdict == "unknown":
            for src in srcs[:4]:
                try:
                    rel = Path(src["file"]).resolve().relative_to(sut_root.resolve()).as_posix()
                except ValueError:
                    continue
                if rel in changed_files:
                    verdict = "product_defect"
                    evidence.append(
                        {
                            "rule": "S2-attribution",
                            "file": rel,
                            "line": src["line"],
                            "in_diff": f"{baseline_commit[:12]}..{(head_commit or '')[:12]}",
                            "detail": "失败栈所在生产文件属于本次候选改动，缺陷由候选引入",
                        }
                    )
                    break

        # S2b 增量归属：不依赖调用栈。基线绿 → 候选红，且候选确有改动，则缺陷由候选引入。
        # 这条是为 assertRaises/assertEqual 类失败准备的：它们的栈只到测试文件。
        if verdict == "unknown" and changed_files and test_id in baseline_passed:
            verdict = "product_defect"
            evidence.append(
                {
                    "rule": "S2-delta-attribution",
                    "baseline_run": str(baseline_run),
                    "baseline_state": "passed",
                    "candidate_state": c["status"],
                    "diff_in": f"{baseline_commit[:12]}..{(head_commit or '')[:12]}",
                    "diff_files": sorted(changed_files)[:10],
                    "detail": "该用例在基线运行中通过、在候选中失败，且候选存在改动集 → 由候选引入",
                }
            )

        # S3 契约
        if verdict == "unknown":
            msg = f"{c['message']} {c['detail']}"
            statuses = set(HTTP_STATUS.findall(msg))
            if statuses & {"400", "404", "405", "409", "422"}:
                found = next((p for p in drifted if p and p in msg), None)
                if found:
                    verdict = "contract_violation"
                    evidence.append(
                        {
                            "rule": "S3-contract",
                            "route": found,
                            "http": sorted(statuses),
                            "detail": "请求命中合同矩阵中已知的前端/后端漂移路由",
                        }
                    )

        if verdict == "unknown":
            evidence.append({"rule": "S4-unknown", "detail": "四条判据均未命中，进隔离区"})

        results.append(
            {
                "test": test_id,
                "status": c["status"],
                "message": c["message"][:300],
                "verdict": verdict,
                "preexisting_debt": test_id in ledger_tests,
                "evidence": evidence,
                "owner": "team-quarantine" if verdict == "unknown" else "team-sut",
                "quarantine_due": due if verdict == "unknown" else None,
                "source_files": srcs[:4],
            }
        )

    total = len(results)
    new = [r for r in results if not r["preexisting_debt"]]
    unknown_all = sum(1 for r in results if r["verdict"] == "unknown")
    unknown_new = sum(1 for r in new if r["verdict"] == "unknown")
    by_verdict = {
        k: sum(1 for r in results if r["verdict"] == k)
        for k in [
            "product_defect",
            "flaky_candidate",
            "contract_violation",
            "environment_excluded",
            "case_stale",
            "unknown",
        ]
    }
    summary = {
        "run_id": run_meta.get("run_id"),
        "sut_commit": run_meta.get("sut_commit"),
        "sut_dirty": run_meta.get("sut_dirty"),
        "baseline_commit": baseline_commit,
        "head_commit": head_commit,
        "diff_files": sorted(changed_files),
        "baseline_passed_count": len(baseline_passed),
        "baseline_available": baseline_available,
        "classified_at": datetime.now(timezone.utc).isoformat(),
        "total_failures": total,
        "new_failures": len(new),
        "preexisting_debt": total - len(new),
        "debt_ledger": ledger_meta,
        "by_verdict": by_verdict,
        "unknown_ratio": round(unknown_all / total, 4) if total else 0.0,
        "new_failure_unknown_ratio": round(unknown_new / len(new), 4) if new else 0.0,
        "unknown_ratio_limit": 0.2,
        "ratio_semantics": {
            "unknown_ratio": "全量失败中的未知比例，仅用于披露存量债务质量",
            "new_failure_unknown_ratio": "增量失败中的未知比例，can-i-deploy 的实际判据",
        },
        "rule_set": [
            "S0-declared-environment-deviation",
            "S1-rerun",
            "S2-attribution-on-diff",
            "S2-delta-attribution",
            "S3-contract",
            "S4-unknown",
        ],
        "env_rules": [
            {k: r[k] for k in ("rule", "declared_in", "reason", "match_message", "match_test_prefix")}
            for r in env_rules
        ],
        "results": results,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.write_ledger:
        ledger = {
            "note": "基线债务台账：由基线运行的分类结论生成，候选运行据此区分新增失败与存量失败",
            "run_id": summary["run_id"],
            "sut_commit": summary["sut_commit"],
            "generated_at": summary["classified_at"],
            "count": total,
            "by_verdict": by_verdict,
            "tests": [
                {
                    "test": r["test"],
                    "verdict": r["verdict"],
                    "owner": r["owner"],
                    "quarantine_due": r["quarantine_due"],
                    "message": r["message"][:200],
                }
                for r in results
            ],
        }
        lp = Path(args.write_ledger)
        lp.parent.mkdir(parents=True, exist_ok=True)
        lp.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"KS_LEDGER\t{lp}\tcount={total}")

    print(
        f"KS_GATE\tclassify-failure\t"
        f"{'PASS' if summary['new_failure_unknown_ratio'] <= 0.2 else 'WARN'}\t"
        f"failures={total}\tnew={len(new)}\t"
        f"unknown_ratio={summary['unknown_ratio']}\t"
        f"new_failure_unknown_ratio={summary['new_failure_unknown_ratio']}"
    )
    for k, v in by_verdict.items():
        if v:
            print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
