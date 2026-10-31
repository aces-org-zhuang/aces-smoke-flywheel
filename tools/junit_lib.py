"""JUnit XML 读取：把测试结果变成可判定的结构化事实。

只做两件事：读结果、抽失败栈里的"被测源码文件"。
不做猜测——抽不到源码文件时，返回空列表，由分类器走"未知"分支。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

TRACEBACK_FILE = re.compile(r'File "([^"]+\.py)", line (\d+)')


def parse_junit_dir(junit_dir: Path) -> list[dict]:
    cases: list[dict] = []
    if not junit_dir.exists():
        return cases
    for xml in sorted(junit_dir.glob("*.xml")):
        try:
            root = ET.parse(xml).getroot()
        except ET.ParseError:
            continue
        suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
        for suite in suites:
            for tc in suite.findall("testcase"):
                status = "passed"
                message = ""
                detail = ""
                for tag in ("failure", "error", "skipped"):
                    node = tc.find(tag)
                    if node is not None:
                        status = {
                            "failure": "failed",
                            "error": "error",
                            "skipped": "skipped",
                        }[tag]
                        message = (node.get("message") or "").strip()
                        detail = (node.text or "").strip()
                        break
                cases.append(
                    {
                        "classname": tc.get("classname") or "",
                        "name": tc.get("name") or "",
                        "time": float(tc.get("time") or 0),
                        "status": status,
                        "message": message,
                        "detail": detail,
                        "file": str(xml),
                    }
                )
    return cases


def source_files_in_traceback(detail: str, sut_root: Path) -> list[dict]:
    """从失败栈里找出属于被测仓的生产代码位置（排除 test*.py 与第三方包）。"""
    out: list[dict] = []
    root = str(sut_root).replace("\\", "/").lower()
    for path, line in TRACEBACK_FILE.findall(detail or ""):
        norm = path.replace("\\", "/")
        if not norm.lower().startswith(root):
            continue
        name = norm.rsplit("/", 1)[-1]
        if name.startswith("test") or "/tests/" in norm:
            continue
        out.append({"file": norm, "line": int(line)})
    seen, unique = set(), []
    for item in out:
        key = (item["file"], item["line"])
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique
