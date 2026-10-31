#!/usr/bin/env python
"""classify-failure：确定性失败分类器（顺序不可逆）。

规则来自方案 §3.4 约束 A，落到本仓的四条可执行判据：

  S1 重跑确认   —— 同 commit 重跑两次均过 → flaky 候选（隔离区，不计入通过率）
  S2 归属判定   —— 失败栈涉及的"生产代码文件"，其最后一次提交 != 基线 commit
                   → 产品缺陷（用例冻结，禁止改用例）
  S3 契约违背   —— 失败发生在 API 层，且 0.ssot 合同矩阵显示该路由存在漂移
                   → 契约违背（阻断 + 通知双方）
  S4 其余       —— 未知，进隔离区，必须人工签字（owner + 7 天到期）

判据全部可复算：每条结论都带 evidence 引用（commit / 文件 / 行 / 合同矩阵键）。
LLM 不参与定责——它只能对已定责的"用例陈旧"提修复提案。
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


def last_commit_for(repo: Path, path: str) -> dict:
    out = git(repo, "log", "-1", "--format=%H|%cI|%s", "--", path)
    if not out:
        return {"commit": "", "date": "", "subject": ""}
    h, d, s = (out.split("|", 2) + ["", ""])[:3]
    return {"commit": h, "date": d, "subject": s}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--env", default="3.env/target.yaml")
    ap.add_argument("--matrix", default="0.ssot/contract_matrix.json")
    ap.add_argument("--out", default="5.evidence/verdicts/classification.json")
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

    due = (datetime.now(timezone.utc) + timedelta(days=args.quarantine_days)).date().isoformat()
    results = []
    for c in failures:
        test_id = f"{c['classname']}.{c['name']}"
        srcs = source_files_in_traceback(c["detail"], sut_root)
        verdict, evidence = "unknown", []

        # S1 重跑
        passes = rerun.get("passed_twice", [])
        if any(test_id.endswith(p.split(".")[-1]) for p in passes):
            verdict = "flaky_candidate"
            evidence.append({"rule": "S1-rerun", "detail": "同 commit 重跑两次均通过"})

        # S2 归属
        if verdict == "unknown":
            for src in srcs[:4]:
                rel = Path(src["file"]).relative_to(sut_root).as_posix()
                info = last_commit_for(Path(sut_root), rel)
                if info["commit"] and info["commit"] != baseline_commit:
                    verdict = "product_defect"
                    evidence.append(
                        {
                            "rule": "S2-attribution",
                            "file": rel,
                            "line": src["line"],
                            "last_commit": info["commit"][:12],
                            "last_commit_subject": info["subject"],
                            "baseline_commit": baseline_commit[:12],
                            "detail": "失败栈所在生产文件的最后一次改动不属于基线提交",
                        }
                    )
                    break

        # S3 契约
        if verdict == "unknown":
            msg = f"{c['message']} {c['detail']}"
            statuses = set(HTTP_STATUS.findall(msg))
            if statuses & {"400", "404", "405", "409", "422"}:
                hit = next((p for p in drifted if p and p in msg), None)
                if hit:
                    verdict = "contract_violation"
                    evidence.append(
                        {
                            "rule": "S3-contract",
                            "route": hit,
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
                "evidence": evidence,
                "owner": "team-quarantine" if verdict == "unknown" else "team-sut",
                "quarantine_due": due if verdict == "unknown" else None,
                "source_files": srcs[:4],
            }
        )

    total = len(results)
    unknown = sum(1 for r in results if r["verdict"] == "unknown")
    summary = {
        "run_id": run_meta.get("run_id"),
        "sut_commit": run_meta.get("sut_commit"),
        "sut_dirty": run_meta.get("sut_dirty"),
        "baseline_commit": baseline_commit,
        "classified_at": datetime.now(timezone.utc).isoformat(),
        "total_failures": total,
        "by_verdict": {
            k: sum(1 for r in results if r["verdict"] == k)
            for k in [
                "product_defect",
                "flaky_candidate",
                "contract_violation",
                "case_stale",
                "unknown",
            ]
        },
        "unknown_ratio": round(unknown / total, 4) if total else 0.0,
        "unknown_ratio_limit": 0.2,
        "rule_set": ["S1-rerun", "S2-attribution", "S3-contract", "S4-unknown"],
        "results": results,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        f"KS_GATE\tclassify-failure\t{'PASS' if summary['unknown_ratio'] <= 0.2 else 'WARN'}\t"
        f"failures={total}\tunknown_ratio={summary['unknown_ratio']}"
    )
    for k, v in summary["by_verdict"].items():
        if v:
            print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
