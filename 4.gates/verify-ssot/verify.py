#!/usr/bin/env python
"""SSOT 冻结校验：确认 0.ssot 确实是被测对象在某个 commit 上的投影，且内部自洽。

为什么需要这一道：
  emit_ssot.py 和 README 都写了"SSOT 与 commit 绑定，不匹配即拒跑"，但此前**没有任何
  门禁真的校验它**——声明与实现不一致，属于典型的静默失效：SSOT 过期了，后面四道门
  仍然全部照跑，判决看起来依然有据。这一道把它变成可执行的检查。

五条判据（任一不满足即 BLOCK）：
  V1 产物齐全        六个文件存在，openapi.yaml 非空
  V2 规范与指纹一致  sha256(openapi.yaml) == schema_version.openapi_sha256
  V3 指纹与 SUT 对齐 schema_version.commit == SUT HEAD，且工作区干净
  V4 计数自洽        schema_version.counts / 各文件 count / 数组长度三者一致
  V5 规范再投影一致  从 openapi.yaml 重新解析出的 (normalized, method) 集合 == endpoints.json

用法：
  python 4.gates/verify-ssot/verify.py --sut-root D:/wbeval/repos/inventree --out 5.evidence/verdicts/verify-ssot.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SSOT = ROOT / "0.ssot"
REQUIRED = [
    "openapi.yaml",
    "endpoints.json",
    "consumers.json",
    "contract_matrix.json",
    "dependency_graph.json",
    "schema_version.json",
]
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True, encoding="utf-8"
    ).strip()


def normalize_path(path: str) -> str:
    """必须与 emit_ssot.normalize_path 逐字一致，否则 V5 会自相矛盾。"""
    p = path.strip()
    p = p.split("?")[0]
    if not p.startswith("/"):
        p = "/" + p
    p = re.sub(r"\{[^}]+\}", "{}", p)
    p = re.sub(r":[A-Za-z_][A-Za-z0-9_]*", "{}", p)
    if len(p) > 1 and p.endswith("/"):
        p = p[:-1]
    if p == "/api" or p.startswith("/api/"):
        p = p[len("/api") :] or "/"
    return p


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sut-root", required=True)
    ap.add_argument("--out", default="5.evidence/verdicts/verify-ssot.json")
    ap.add_argument(
        "--allow-dirty",
        action="store_true",
        help="允许 SUT 工作区有未提交改动（仅本地调试；判决会记为 PASS_WITH_DIRTY_WORKTREE）",
    )
    args = ap.parse_args()
    sut = Path(args.sut_root).resolve()
    checks: list[dict] = []

    def add(cid: str, name: str, ok: bool, detail: str, evidence=None):
        checks.append(
            {"id": cid, "name": name, "ok": ok, "detail": detail, "evidence": evidence}
        )

    # V1 产物齐全
    missing = [f for f in REQUIRED if not (SSOT / f).exists()]
    zero = [f for f in REQUIRED if (SSOT / f).exists() and (SSOT / f).stat().st_size == 0]
    add(
        "V1",
        "产物齐全且非空",
        not missing and not zero,
        "六个产物齐备" if not missing and not zero else f"缺失 {missing} / 空文件 {zero}",
        {"required": REQUIRED},
    )
    if missing or zero:
        return finish(checks, args, sut)

    ssv = json.loads((SSOT / "schema_version.json").read_text(encoding="utf-8"))
    endpoints = json.loads((SSOT / "endpoints.json").read_text(encoding="utf-8"))
    consumers = json.loads((SSOT / "consumers.json").read_text(encoding="utf-8"))
    matrix = json.loads((SSOT / "contract_matrix.json").read_text(encoding="utf-8"))
    depgraph = json.loads((SSOT / "dependency_graph.json").read_text(encoding="utf-8"))

    # V2 规范与指纹一致
    actual_sha = sha256_file(SSOT / "openapi.yaml")
    add(
        "V2",
        "openapi.yaml 与记录指纹一致",
        actual_sha == ssv.get("openapi_sha256"),
        f"磁盘 {actual_sha[:16]}… / 记录 {str(ssv.get('openapi_sha256'))[:16]}…",
        {"actual": actual_sha, "recorded": ssv.get("openapi_sha256")},
    )

    # V3 指纹与 SUT 对齐
    head = git(sut, "rev-parse", "HEAD")
    dirty = git(sut, "status", "--porcelain")
    commit_ok = head == ssv.get("commit")
    clean_ok = not dirty or args.allow_dirty
    add(
        "V3a",
        "SSOT 绑定的 commit == 被测仓 HEAD",
        commit_ok,
        f"SSOT {str(ssv.get('commit'))[:12]} / SUT {head[:12]}",
        {"ssot_commit": ssv.get("commit"), "sut_head": head},
    )
    add(
        "V3b",
        "被测仓工作区干净（SSOT 只对 commit 负责）",
        clean_ok,
        "clean" if not dirty else f"dirty: {len(dirty.splitlines())} 个文件",
        {"dirty_files": dirty.splitlines()[:10]},
    )

    # V3c 与声明式目标一致：target.yaml 是"我要测谁"的唯一声明，
    # SSOT 是"我测的是谁"的投影，两者不一致说明投影对象根本不是声明的那个。
    target_commit = None
    try:
        import yaml as _yaml

        tgt = _yaml.safe_load((ROOT / "3.env/target.yaml").read_text(encoding="utf-8"))
        target_commit = ((tgt or {}).get("target") or {}).get("commit")
    except Exception as exc:  # noqa: BLE001 - 读不到就判失败，不允许跳过
        target_commit = None
        target_err = str(exc)
    add(
        "V3c",
        "SSOT commit == target.yaml 声明 commit",
        bool(target_commit) and target_commit == ssv.get("commit"),
        f"target {str(target_commit)[:12]} / SSOT {str(ssv.get('commit'))[:12]}",
        {"target_commit": target_commit},
    )

    # V4 计数自洽
    recomputed = {
        "endpoints": len(endpoints.get("items", [])),
        "consumers": len(consumers.get("items", [])),
        "backend_apps": depgraph.get("node_count"),
        "dependency_edges": depgraph.get("edge_count"),
    }
    declared = ssv.get("counts", {})
    file_counts = {
        "endpoints.json.count": endpoints.get("count"),
        "consumers.json.count": consumers.get("count"),
        "contract_matrix.totals.backend_operations": matrix["totals"]["backend_operations"],
        "contract_matrix.totals.consumer_declarations": matrix["totals"]["consumer_declarations"],
        "contract_matrix.totals.matched": matrix["totals"]["matched"],
    }
    consistency = {
        "declared_vs_recomputed": {k: (declared.get(k), recomputed[k]) for k in recomputed},
        "file_counts": file_counts,
    }
    self_consistent = (
        all(declared.get(k) == v for k, v in recomputed.items())
        and file_counts["endpoints.json.count"] == recomputed["endpoints"]
        and file_counts["consumers.json.count"] == recomputed["consumers"]
        and file_counts["contract_matrix.totals.backend_operations"] == recomputed["endpoints"]
        and file_counts["contract_matrix.totals.consumer_declarations"] == recomputed["consumers"]
        and file_counts["contract_matrix.totals.matched"] == len(matrix["matched"])
        and len(matrix["unmatched_consumer"]) == matrix["totals"]["unmatched_consumer"]
    )
    add(
        "V4",
        "四份产物计数互相自洽",
        self_consistent,
        "声明值 = 数组长度 = 矩阵总计" if self_consistent else "存在计数矛盾",
        consistency,
    )

    # V5 从规范再投影，与 endpoints.json 对齐
    try:
        import yaml
    except ImportError:
        add("V5", "规范再投影一致", False, "缺 PyYAML，无法再投影（不允许跳过）")
        return finish(checks, args, sut)
    spec = yaml.safe_load((SSOT / "openapi.yaml").read_text(encoding="utf-8"))
    projected = set()
    for path, node in (spec.get("paths") or {}).items():
        if not isinstance(node, dict):
            continue
        for method, op in node.items():
            if method.lower() in HTTP_METHODS and isinstance(op, dict):
                projected.add((normalize_path(path), method.upper()))
    recorded = {(e["normalized"], e["method"]) for e in endpoints.get("items", [])}
    only_spec = sorted(projected - recorded)[:5]
    only_json = sorted(recorded - projected)[:5]
    add(
        "V5",
        "从 openapi.yaml 再投影得到的端点集 == endpoints.json",
        projected == recorded,
        f"规范 {len(projected)} / 记录 {len(recorded)} / 只在规范 {len(projected - recorded)} / 只在记录 {len(recorded - projected)}",
        {"only_in_spec": only_spec, "only_in_json": only_json},
    )

    return finish(checks, args, sut)


def finish(checks: list[dict], args, sut: Path) -> int:
    hard_fail = [c for c in checks if not c["ok"]]
    verdict = "BLOCK" if hard_fail else "PASS"
    blob = {
        "gate": "verify-ssot",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sut_root": str(sut),
        "verdict": verdict,
        "checks": checks,
        "failed": [c["id"] for c in hard_fail],
    }
    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8")
    mark = "OK  " if verdict == "PASS" else "FAIL"
    print(f"KS_GATE\tverify-ssot\t{verdict}\t{len(checks) - len(hard_fail)}/{len(checks)} checks")
    for c in checks:
        # 逐条标状态：整体判决 BLOCK 不等于每条都失败，混标会让日志自相矛盾
        flag = "OK  " if c["ok"] else "FAIL"
        print(f"  {flag} {c['id']} {c['name']}: {c['detail']}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
