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
        if occurrences != 1:
            results.append(
                {
                    "id": mut["id"],
                    "status": "invalid",
                    "detail": f"锚点出现 {occurrences} 次，必须恰好 1 次",
                }
            )
            continue

        path.write_text(original.replace(mut["find"], mut["replace"]), encoding="utf-8")
        run_id = f"mutation-{mut['id']}"
        selected = list(dict.fromkeys(mut["expect_killed_by"] + all_l1))
        started = time.time()
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
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        finally:
            path.write_text(original, encoding="utf-8")
            subprocess.run(["git", "-C", str(sut_root), "checkout", "--", mut["file"]], check=False)

        duration = round(time.time() - started, 2)
        run_dir = ROOT / "5.evidence/runs" / run_id
        cases = parse_junit_dir(run_dir / "junit")
        failed_names = {
            f"{c['classname']}.{c['name']}" for c in cases if c["status"] in {"failed", "error"}
        }
        targets = mut["expect_killed_by"]
        killed_by = [
            t
            for t in targets
            if any(f == t or f.endswith(t.split(".", 1)[-1]) for f in failed_names)
        ]
        collateral = [
            f for f in failed_names if not any(f.endswith(t.split(".", 1)[-1]) for t in targets)
        ]

        results.append(
            {
                "id": mut["id"],
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
