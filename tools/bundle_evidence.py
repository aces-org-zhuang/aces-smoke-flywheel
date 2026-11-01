#!/usr/bin/env python
"""把一次实践的原始证据打成一个可下载的包，供第三方复核复算。

包含：SSOT、规格、门禁代码、全部运行证据（JUnit + run.json + verdicts + ledger）、报告页。
不包含：被测仓源码（用 commit 号即可复现）、任何密钥。
"""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

INCLUDE = [
    "0.ssot",
    "1.spec",
    "2.tests",
    "3.env",
    "4.gates",
    "5.evidence",
    "docs",
    "tools",
    "README.md",
]

EXCLUDE_SUFFIX = {".pyc"}
# node_modules 是 check-atlas 的验证依赖（jsdom），不是证据；打进去会把包从数百 KB 撑到数十 MB
EXCLUDE_PARTS = {"__pycache__", ".git", "node_modules"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="docs/aces-smoke-flywheel-evidence.zip")
    ap.add_argument("--root", default=".")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    count = 0
    total = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in INCLUDE:
            p = root / item
            if not p.exists():
                continue
            files = [p] if p.is_file() else sorted(p.rglob("*"))
            for f in files:
                if not f.is_file():
                    continue
                if f.suffix in EXCLUDE_SUFFIX or EXCLUDE_PARTS & set(f.parts):
                    continue
                if f.name.endswith(".env") or f.name == "server.log":
                    continue
                zf.write(f, f.relative_to(root).as_posix())
                count += 1
                total += f.stat().st_size

    print(f"KS_BUNDLE\t{out}\tfiles={count}\traw_bytes={total}\tzip_bytes={out.stat().st_size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
