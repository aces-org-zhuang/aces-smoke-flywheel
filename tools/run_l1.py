#!/usr/bin/env python
"""L1 运行器：按 1.spec/services/invariants.yaml 选中"成品用例"，跑完落证据。

为什么不让 AI 生成用例：
  这 9 条不变量指向的都是被测仓里已存在、已被上游 CI 长期跑的用例。
  飞轮要解决的是"选谁跑、跑完信不信、坏了怎么判责"，不是"再多写一堆断言"。

用法:
  python tools/run_l1.py --run-id l1-20261031 --out 5.evidence/runs
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
from junit_lib import parse_junit_dir  # noqa: E402

PY = os.environ.get("FLYWHEEL_PYTHON")
SECRET = os.environ.get("FLYWHEEL_SECRET_KEY", "devkey-devkey-devkey-devkey-devkey")


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--out", default="5.evidence/runs")
    ap.add_argument("--spec", default="1.spec/services/invariants.yaml")
    ap.add_argument("--env", default="3.env/target.yaml")
    ap.add_argument("--only", default="", help="只跑匹配的不变量 id（逗号分隔）")
    ap.add_argument("--tests-override", default="", help="直接指定 Django 用例 ID（变异测试用）")
    args = ap.parse_args()

    target = yaml.safe_load(Path(args.env).read_text(encoding="utf-8"))
    rt = target["runtime"]
    sut_root = Path(rt["sut_root"])
    workdir = Path(rt["workdir"])
    commit = git(sut_root, "rev-parse", "HEAD")
    dirty = git(sut_root, "status", "--porcelain")

    run_dir = Path(args.out) / args.run_id
    junit_dir = run_dir / "junit"
    junit_dir.mkdir(parents=True, exist_ok=True)

    invs = yaml.safe_load(Path(args.spec).read_text(encoding="utf-8"))["invariants"]
    if args.only:
        wanted = {x.strip() for x in args.only.split(",") if x.strip()}
        invs = [i for i in invs if i["id"] in wanted]

    if args.tests_override:
        selected = [t for t in args.tests_override.split(",") if t.strip()]
        id_to_inv = {}
    else:
        selected = []
        id_to_inv = {}
        for inv in invs:
            for tid in inv["tests"]:
                selected.append(tid)
                id_to_inv[tid] = inv["id"]

    env = dict(os.environ)
    env.pop("PYTHONHOME", None)
    env["PYTHONPATH"] = ";".join(
        [str(Path("3.env/virtual").resolve()), str(Path("tools").resolve())]
    )
    env.update({k: str(v) for k, v in rt["env"].items()})
    env["INVENTREE_SECRET_KEY"] = SECRET
    env["TEST_RUNNER"] = "django_junit_runner.FlywheelXMLRunner"
    env["FLYWHEEL_JUNIT_DIR"] = str(junit_dir.resolve())

    started = time.time()
    cmd = [
        PY,
        "manage.py",
        "test",
        *selected,
        "--exclude-tag",
        "migration_test",
        "--verbosity",
        "1",
    ]
    proc = subprocess.run(
        cmd, cwd=str(workdir), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    duration = round(time.time() - started, 2)
    (run_dir / "stdout.log").write_text(
        (proc.stdout or "") + "\n--- stderr ---\n" + (proc.stderr or ""), encoding="utf-8"
    )

    cases = parse_junit_dir(junit_dir)
    tested = {f"{c['classname']}.{c['name']}" for c in cases}
    by_inv: dict[str, dict] = {}
    for tid in selected:
        inv = id_to_inv.get(tid, "override")
        by_inv.setdefault(inv, {"total": 0, "failed": 0, "missing": 0})
        by_inv[inv]["total"] += 1
        if tid not in tested and not any(t.endswith(tid.split(".")[-1]) for t in tested):
            by_inv[inv]["missing"] += 1
    for c in cases:
        full = f"{c['classname']}.{c['name']}"
        inv = next((i for t, i in id_to_inv.items() if full.endswith(t.split(".", 1)[-1])), None)
        if inv and c["status"] in {"failed", "error"}:
            by_inv[inv]["failed"] += 1

    failures = [c for c in cases if c["status"] in {"failed", "error"}]
    missing = [
        tid
        for tid in selected
        if not any(c["classname"] + "." + c["name"] == tid for c in cases)
        and not any(c["name"] == tid.split(".")[-1] for c in cases)
    ]

    evidence = {
        "run_id": args.run_id,
        "layer": "L1",
        "sut_commit": commit,
        "sut_dirty": bool(dirty),
        "sut_dirty_files": dirty.splitlines() if dirty else [],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "duration_s": duration,
        "exit_code": proc.returncode,
        "selected": len(selected),
        "collected": len(cases),
        "passed": sum(1 for c in cases if c["status"] == "passed"),
        "failed": len(failures),
        "skipped": sum(1 for c in cases if c["status"] == "skipped"),
        "missing_test_ids": missing,
        "per_invariant": by_inv,
        "failures": [
            {
                "test": f"{c['classname']}.{c['name']}",
                "status": c["status"],
                "message": c["message"][:400],
                "detail_head": c["detail"][:1200],
            }
            for c in failures
        ],
        "command": " ".join(cmd),
    }
    (run_dir / "run.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(
        f"KS_RUN\t{args.run_id}\tcollected={len(cases)}\tpassed={evidence['passed']}\t"
        f"failed={evidence['failed']}\tmissing={len(missing)}\tduration={duration}s"
    )
    for f in failures[:8]:
        print(f"  FAIL {f['classname']}.{f['name']}: {f['message'][:120]}")
    for tid in missing[:5]:
        print(f"  MISSING {tid}")
    return 0 if proc.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
