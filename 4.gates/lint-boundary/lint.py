#!/usr/bin/env python
"""lint-boundary：把 §3.3 的"边界契约"变成机器可校验的门槛。

四件事（缺一不可，任一不过即拒绝合入）：
  1. 结构完整：每份 boundary 必须声明 sut / sut_mode / real / virtual / assert / forbid / timeout_s / owner / us
  2. 依赖显式：virtual 里每个替身必须给出来源（from_contract 或 declared_external），
     且 from_contract 指向的路径必须真实存在——禁止"口头塌缩"
  3. 用例可解析：boundary 对应不变量里的 tests[*] 必须在被测仓里真实存在，
     否则就是用例静默失效（改名/删除后没人发现）
  4. 禁令可执行：forbid 里的模式不得出现在 2.tests 的源码里；timeout_s 不得超层上限

输出：KS 行 + 机器可读 JSON 判决，退出码 0=通过 / 1=拒绝
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

LAYER_TIMEOUT = {"L0": 60, "L1": 180, "L2": 120, "L3": 600}
REQUIRED = [
    "id",
    "layer",
    "us",
    "owner",
    "sut",
    "sut_mode",
    "real",
    "virtual",
    "assert",
    "forbid",
    "timeout_s",
]
VALID_SUT_MODE = {"in-process", "live-http", "static"}


def load_yaml(p: Path):
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def index_sut_tests(root: Path) -> dict[str, Path]:
    """扫描被测仓，建立 <module>.<Class>.<method> -> 文件 的索引。"""
    index: dict[str, Path] = {}
    backend = root / "src/backend/InvenTree"
    if not backend.exists():
        return index
    cls_re = re.compile(r"^class\s+(\w+)\(")
    def_re = re.compile(r"^\s+def\s+(test_\w+)\(")
    for py in backend.rglob("test*.py"):
        rel = py.relative_to(backend).with_suffix("")
        parts = list(rel.parts)
        mod = ".".join(parts)
        cur = None
        for line in py.read_text(encoding="utf-8", errors="ignore").splitlines():
            m = cls_re.match(line)
            if m:
                cur = m.group(1)
            m2 = def_re.match(line)
            if m2 and cur:
                index[f"{mod}.{cur}.{m2.group(1)}"] = py
    return index


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", default="1.spec")
    ap.add_argument("--tests", default="2.tests")
    ap.add_argument("--env", default="3.env/target.yaml")
    ap.add_argument("--out", default="5.evidence/verdicts/lint-boundary.json")
    args = ap.parse_args()

    target = load_yaml(Path(args.env))
    sut_root = Path(target["runtime"]["sut_root"])
    sut_index = index_sut_tests(sut_root)

    invariants = load_yaml(Path(args.spec) / "services/invariants.yaml")["invariants"]
    inv_by_id = {i["id"]: i for i in invariants}

    violations: list[dict] = []
    checked = 0
    for bpath in sorted((Path(args.spec) / "boundaries").glob("*.boundary.yaml")):
        checked += 1
        b = load_yaml(bpath)
        where = bpath.name
        for key in REQUIRED:
            if key not in b:
                violations.append(
                    {"rule": "R1-schema", "where": where, "detail": f"缺少字段 {key}"}
                )
        if b.get("sut_mode") not in VALID_SUT_MODE:
            violations.append(
                {
                    "rule": "R1-schema",
                    "where": where,
                    "detail": f"sut_mode={b.get('sut_mode')} 不在 {sorted(VALID_SUT_MODE)}",
                }
            )

        # R2 依赖显式
        for v in b.get("virtual") or []:
            if not isinstance(v, dict):
                violations.append(
                    {"rule": "R2-virtual", "where": where, "detail": "virtual 条目必须是映射"}
                )
                continue
            src = v.get("from_contract")
            if v.get("declared_external"):
                continue
            if not src:
                violations.append(
                    {
                        "rule": "R2-virtual",
                        "where": where,
                        "detail": f"替身 {v.get('name')} 没有 from_contract 也没有 declared_external",
                    }
                )
                continue
            if not Path(src).exists():
                violations.append(
                    {
                        "rule": "R2-virtual",
                        "where": where,
                        "detail": f"{v.get('name')} 的 from_contract 路径不存在: {src}",
                    }
                )
        if not b.get("real"):
            violations.append(
                {"rule": "R2-real", "where": where, "detail": "real 为空：没有真实依赖就不是服务级冒烟"}
            )

        # R3 用例可解析
        inv = inv_by_id.get(b.get("id"))
        if not inv:
            violations.append(
                {"rule": "R3-trace", "where": where, "detail": f"不变量 {b.get('id')} 未在 invariants.yaml 登记"}
            )
        else:
            if inv.get("us") != b.get("us"):
                violations.append(
                    {
                        "rule": "R3-trace",
                        "where": where,
                        "detail": f"us 不一致: boundary={b.get('us')} invariants={inv.get('us')}",
                    }
                )
            for tid in inv.get("tests") or []:
                if tid not in sut_index:
                    violations.append(
                        {
                            "rule": "R3-tests",
                            "where": where,
                            "detail": f"用例在被测仓不存在: {tid}",
                        }
                    )

        # R4 禁令与时限
        limit = LAYER_TIMEOUT.get(str(b.get("layer")))
        if limit is None:
            violations.append({"rule": "R4-layer", "where": where, "detail": f"未知层 {b.get('layer')}"})
        elif int(b.get("timeout_s") or 0) > limit:
            violations.append(
                {
                    "rule": "R4-timeout",
                    "where": where,
                    "detail": f"timeout_s={b.get('timeout_s')} 超过 {b.get('layer')} 上限 {limit}s",
                }
            )
        scope = (b.get("assert") or {}).get("scope", "")
        if not scope or scope.strip() in {"all", "any", "*"}:
            violations.append(
                {"rule": "R4-assert", "where": where, "detail": f"assert.scope 必须具体，当前={scope!r}"}
            )

    # 禁令扫描：2.tests 下的实现不得包含边界里声明的禁止模式
    banned: dict[str, str] = {}
    for bpath in sorted((Path(args.spec) / "boundaries").glob("*.boundary.yaml")):
        b = load_yaml(bpath)
        for pat in b.get("forbid") or []:
            banned.setdefault(pat, bpath.name)
    scanned = 0
    for src in sorted(Path(args.tests).rglob("*.py")):
        text = src.read_text(encoding="utf-8", errors="ignore")
        scanned += 1
        for pat in banned:
            if pat in text:
                violations.append(
                    {
                        "rule": "R4-forbid",
                        "where": str(src),
                        "detail": f"命中禁止模式 {pat!r}（声明于 {banned[pat]}）",
                    }
                )

    verdict = {
        "gate": "lint-boundary",
        "sut_commit": target["target"]["commit"],
        "checked_boundaries": checked,
        "scanned_test_files": scanned,
        "sut_test_ids_indexed": len(sut_index),
        "violations": violations,
        "result": "PASS" if not violations else "BLOCK",
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"KS_GATE\tlint-boundary\t{verdict['result']}\t{len(violations)} violation(s)")
    for v in violations:
        print(f"  [{v['rule']}] {v['where']}: {v['detail']}")
    print(
        f"检查 {checked} 份边界契约 / 扫描 {scanned} 个用例文件 / 被测仓索引 {len(sut_index)} 个用例 ID"
    )
    return 0 if not violations else 1


if __name__ == "__main__":
    raise SystemExit(main())
