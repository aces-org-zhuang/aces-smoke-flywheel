#!/usr/bin/env python
"""mutation-diff：只对"本次改动行"注入缺陷，验证用例到底能不能抓住。

与方案 §3.5 的对应关系：
  - 只对改动行做变异（PR 增量），不跑全量；
  - 每个变异体都绑定"应该杀死它的用例"（expect_killed_by）；
  - 被杀死 = 至少一条目标用例变红；存活 = 全绿（这是断言缺口的直接证据）；
  - 每个变异体跑完立即 git checkout 还原，工作区必须回到干净态。

注入的是"人写得出来的真实缺陷"，而不是自动算子的随机翻转：
  注释掉校验、放宽边界、跳过守卫——这三种形态在真实 PR 里最常见。

用法:
  python 4.gates/mutation-diff/run_mutation.py --baseline-commit <sha>
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "tools"))
from junit_lib import parse_junit_dir  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default="4.gates/mutation-diff/mutations.yaml")
    ap.add_argument("--env", default="3.env/target.yaml")
    ap.add_argument("--suite", default="1.spec/services/invariants.yaml")
    ap.add_argument("--out", default="5.evidence/verdicts/mutation.json")
    args = ap.parse_args()

    target = yaml.safe_load((ROOT / args.env).read_text(encoding="utf-8"))
    sut_root = Path(target["runtime"]["sut_root"])
    manifest = yaml.safe_load((ROOT / args.manifest).read_text(encoding="utf-8"))

    invariants = yaml.safe_load((ROOT / args.suite).read_text(encoding="utf-8"))["invariants"]
    all_l1 = [t for inv in invariants for t in inv["tests"]]

    baseline_commit = git(sut_root, "rev-parse", "HEAD")
    dirty = git(sut_root, "status", "--porcelain")
    if dirty:
        print("KS_GATE\tmutation-diff\tABORT\tworktree 不干净，变异结果不可信")
        print(dirty[:800])
        return 2

    results = []
    for mut in manifest["mutations"]:
        path = sut_root / mut["file"]
        original = path.read_text(encoding="utf-8")
        occurrences = original.count(mut["find"])
        nth = int(mut.get("nth", 1))
        if occurrences < nth:
            results.append(
                {
                    "id": mut["id"],
                    "status": "invalid",
                    "detail": f"锚点只出现 {occurrences} 次，要求至少 {nth} 次",
                }
            )
            continue

        # 只替换第 nth 处，并记录被改的行号——证据要能指到具体一行
        idx = -1
        for _ in range(nth):
            idx = original.index(mut["find"], idx + 1)
        mutated = original[:idx] + mut["replace"] + original[idx + len(mut["find"]) :]
        mutated_line = original[:idx].count("\n") + 1
        path.write_text(mutated, encoding="utf-8")
        # 缺陷必须成为一次"提交"，否则 git blame 永远看不到它，
        # 分类器 S2（归属判定）也就无法证明自己有效。
        subprocess.run(["git", "-C", str(sut_root), "add", "-A"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(sut_root),
                "-c",
                "user.name=flywheel-mutant",
                "-c",
                "user.email=mutant@aces.local",
                "commit",
                "-q",
                "-m",
                f"MUTANT {mut['id']}: {mut['rationale'].strip().splitlines()[0][:60]}",
            ],
            check=True,
        )
        mutant_sha = git(sut_root, "rev-parse", "HEAD")
        run_id = f"mutation-{mut['id']}"
        selected = list(dict.fromkeys(mut["expect_killed_by"] + all_l1))
        started = time.time()
        run_dir = ROOT / "5.evidence/runs" / run_id
        # 先清空本轮目录：否则 runner 崩掉时会把上一轮残留的 JUnit 当成本轮结果，
        # 门禁就会给出"killed"这种最危险的假绿。
        if run_dir.exists():
            shutil.rmtree(run_dir)
        child_env = dict(os.environ)
        child_env["FLYWHEEL_PYTHON"] = sys.executable
        try:
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "tools/run_l1.py"),
                    "--run-id",
                    run_id,
                    "--tests-override",
                    ",".join(selected),
                ],
                cwd=str(ROOT),
                env=child_env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            duration = round(time.time() - started, 2)
            run_meta_path = run_dir / "run.json"
            if not run_meta_path.exists():
                results.append(
                    {
                        "id": mut["id"],
                        "status": "invalid",
                        "detail": "runner 未产出 run.json（进程未能启动或被环境打断），本轮结论不可用",
                        "duration_s": duration,
                    }
                )
                continue
            run_meta = json.loads(run_meta_path.read_text(encoding="utf-8"))
            if run_meta.get("collected", 0) == 0:
                results.append(
                    {
                        "id": mut["id"],
                        "status": "invalid",
                        "detail": f"runner 收集到 0 个用例（exit={run_meta.get('exit_code')}），本轮结论不可用",
                        "duration_s": duration,
                    }
                )
                continue

            cases = parse_junit_dir(run_dir / "junit")
            failed_names = {
                f"{c['classname']}.{c['name']}"
                for c in cases
                if c["status"] in {"failed", "error"}
            }
            targets = mut["expect_killed_by"]
            killed_by = [
                t
                for t in targets
                if any(f == t or f.endswith(t.split(".", 1)[-1]) for f in failed_names)
            ]
            collateral = [
                f
                for f in failed_names
                if not any(f.endswith(t.split(".", 1)[-1]) for t in targets)
            ]

            # 分类必须发生在缺陷提交仍是 HEAD 的时候：
            # S2 的两条判据都依赖 baseline..HEAD 的改动集，一旦先 reset，
            # 改动集为空，注入的缺陷就会被判成 unknown —— 门禁看起来跑了，其实没归因。
            verdict_file = ROOT / "5.evidence/verdicts" / f"classification-{mut['id']}.json"
            classify_env = dict(os.environ)
            classify_env.pop("PYTHONHOME", None)
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "4.gates/classify-failure/classify.py"),
                    "--run-dir",
                    str(run_dir),
                    "--out",
                    str(verdict_file),
                    "--debt-ledger",
                    str(ROOT / "5.evidence/verdicts/baseline-debt.json"),
                    "--baseline-run",
                    str(ROOT / "5.evidence/runs/baseline"),
                ],
                cwd=str(ROOT),
                env=classify_env,
                capture_output=True,
                text=True,
            )
            head_during_classify = git(sut_root, "rev-parse", "HEAD")
            classification = {}
            if verdict_file.exists():
                blob = json.loads(verdict_file.read_text(encoding="utf-8"))
                classification = {
                    "by_verdict": blob.get("by_verdict"),
                    "unknown_ratio": blob.get("unknown_ratio"),
                    "new_failure_unknown_ratio": blob.get("new_failure_unknown_ratio"),
                    "diff_files": blob.get("diff_files"),
                }
        finally:
            subprocess.run(
                ["git", "-C", str(sut_root), "reset", "--hard", baseline_commit],
                check=False,
                capture_output=True,
            )

        results.append(
            {
                "id": mut["id"],
                "mutant_commit": mutant_sha[:12],
                "mutated_line": mutated_line,
                "anchor_occurrences": occurrences,
                "classification": classification,
                "classified_at_head": head_during_classify[:12],
                "file": mut["file"],
                "rationale": mut["rationale"],
                "mutant": mut["replace"].strip()[:120],
                "status": "killed" if killed_by else "survived",
                "killed_by": killed_by,
                "collateral_failures": sorted(collateral)[:6],
                "targeted": targets,
                "duration_s": duration,
                "run_dir": str(run_dir.relative_to(ROOT)),
            }
        )

    restored = git(sut_root, "status", "--porcelain")
    killed = sum(1 for r in results if r.get("status") == "killed")
    valid = sum(1 for r in results if r.get("status") in {"killed", "survived"})
    report = {
        "gate": "mutation-diff",
        "sut_commit": target["target"]["commit"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "manual PR-diff mutants (3), each bound to expected killer tests",
        "mutants": results,
        "killed": killed,
        "valid": valid,
        "kill_rate": round(killed / valid, 4) if valid else 0.0,
        "worktree_restored": not restored,
    }
    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        f"KS_GATE\tmutation-diff\t{'PASS' if valid and killed == valid else 'BLOCK'}\t"
        f"killed={killed}/{valid}\tworktree_restored={not restored}"
    )
    for r in results:
        print(f"  {r['id']}: {r.get('status')}  killed_by={r.get('killed_by')}")
    return 0 if valid and killed == valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
