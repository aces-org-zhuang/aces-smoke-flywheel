#!/usr/bin/env python
"""断言密度账本：防止"AI 把用例改绿"的最直接指标。

口径（写在代码里，避免事后解释）：
  密度 = 一个测试方法体内的断言语句数 / 该方法的有效代码行数
  断言语句 = self.assert*、assert 关键字、self.check*、expected_code= 断言参数

为什么要它：
  工业案例里 AI 修用例最常见的两种作弊是"弱化断言"和"删用例"。
  两者都会让密度下降，而通过率上升。密度是唯一和"通过率"反着走的指标，
  所以它能当刹车。

用法:
  python tools/assertion_density.py --repo <SUT> --out 5.evidence/ledger/assertion_density.csv
"""

from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path

CLASS_RE = re.compile(r"^class\s+(\w+)\(")
DEF_RE = re.compile(r"^(\s+)def\s+(test_\w+)\(")
ASSERT_RE = re.compile(r"self\.assert\w+|(?<![\w.])assert\s|\bexpected_code\s*=")


def scan_file(path: Path, module: str) -> list[dict]:
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    cls = None
    rows: list[dict] = []
    cur = None
    indent = 0
    body: list[str] = []

    def flush():
        nonlocal body, cur, indent
        if cur is None:
            return
        code_lines = [
            ln
            for ln in body
            if ln.strip() and not ln.strip().startswith(("#", '"""', "'''"))
        ]
        asserts = sum(len(ASSERT_RE.findall(ln)) for ln in body)
        rows.append(
            {
                "test": f"{module}.{cls}.{cur}",
                "file": str(path),
                "assertions": asserts,
                "code_lines": len(code_lines),
                "density": round(asserts / len(code_lines), 4) if code_lines else 0.0,
            }
        )
        body = []

    for line in lines:
        m = CLASS_RE.match(line)
        if m:
            flush()
            cur = None
            cls = m.group(1)
            continue
        d = DEF_RE.match(line)
        if d:
            flush()
            cur = d.group(2)
            indent = len(d.group(1))
            body = []
            continue
        if cur is not None:
            stripped = line.strip()
            if stripped and (len(line) - len(line.lstrip())) <= indent and not stripped.startswith((")", "]", "}")):
                flush()
                cur = None
                continue
            body.append(line)
    flush()
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--out", default="5.evidence/ledger/assertion_density.csv")
    ap.add_argument("--glob", default="test*.py")
    args = ap.parse_args()

    root = Path(args.repo) / "src/backend/InvenTree"
    rows: list[dict] = []
    for path in sorted(root.rglob(args.glob)):
        rel = path.relative_to(root).with_suffix("")
        rows.extend(scan_file(path, ".".join(rel.parts)))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=["test", "file", "assertions", "code_lines", "density"]
        )
        writer.writeheader()
        writer.writerows(rows)

    total_asserts = sum(r["assertions"] for r in rows)
    total_lines = sum(r["code_lines"] for r in rows)
    zero_assert = [r for r in rows if r["assertions"] == 0]
    print(
        f"KS_LEDGER\trows={len(rows)}\tassertions={total_asserts}\tcode_lines={total_lines}\t"
        f"density={round(total_asserts / total_lines, 4) if total_lines else 0}\tzero_assert_tests={len(zero_assert)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
