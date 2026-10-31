#!/usr/bin/env python
"""把一次 JUnit 目录"回收"成 run.json。

用途：全量基线是由 manage.py test 直接跑的（不是 run_l1.py 调的），
只有 XML，没有 run.json。分类器需要 run.json 的 commit 与脏标记，
所以这里从证据本身还原元数据——而不是事后手填一个"看起来对"的 JSON。
"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import yaml

import sys

sys.path.insert(0, str(Path(__file__).parent))
from junit_lib import parse_junit_dir  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--env", default="3.env/target.yaml")
    ap.add_argument("--layer", default="L0-L3-all")
    args = ap.parse_args()

    target = yaml.safe_load(Path(args.env).read_text(encoding="utf-8"))
    sut_root = Path(target["runtime"]["sut_root"])
    commit = subprocess.check_output(
        ["git", "-C", str(sut_root), "rev-parse", "HEAD"], text=True
    ).strip()
    dirty = subprocess.check_output(
        ["git", "-C", str(sut_root), "status", "--porcelain"], text=True
    ).strip()

    run_dir = Path(args.run_dir)
    cases = parse_junit_dir(run_dir / "junit")
    failures = [c for c in cases if c["status"] in {"failed", "error"}]

    meta = {
        "run_id": run_dir.name,
        "layer": args.layer,
        "source": "manage.py test（全量），由 tools/ingest_baseline.py 从 JUnit 回收元数据",
        "sut_commit": commit,
        "sut_dirty": bool(dirty),
        "sut_dirty_files": dirty.splitlines() if dirty else [],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "collected": len(cases),
        "passed": sum(1 for c in cases if c["status"] == "passed"),
        "failed": len(failures),
        "skipped": sum(1 for c in cases if c["status"] == "skipped"),
        "duration_s": round(sum(c["time"] for c in cases), 2),
        "missing_test_ids": [],
        "failures": [
            {
                "test": f"{c['classname']}.{c['name']}",
                "status": c["status"],
                "message": c["message"][:400],
            }
            for c in failures
        ],
    }
    (run_dir / "run.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"KS_INGEST\t{run_dir.name}\tcollected={meta['collected']}\tpassed={meta['passed']}\t"
        f"failed={meta['failed']}\tdirty={meta['sut_dirty']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
