#!/usr/bin/env python
"""0.ssot 生成器：把"被测系统的事实"从源码/规范里抽出来，形成唯一事实源。

原则：
  1. 只做投影，不做推断。端点来自 openapi.yaml（drf-spectacular 从真实 URLconf 生成），
     消费方期望来自前端源码里的枚举声明，依赖边来自真实 import。
  2. 每个产物都带 commit 绑定；commit 变了但 ssot 没重算，gate 必须拒跑。
  3. 不写"估计值"。所有计数都是本次实际遍历得到的。

用法:
  python tools/emit_ssot.py --repo D:/wbeval/repos/inventree --out 0.ssot
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("需要 PyYAML：请使用 inventree 虚拟环境里的 python 运行本脚本")


# ---------------------------------------------------------------- helpers


API_PREFIX = "/api"


def normalize_path(path: str, *, consumer: bool = False) -> str:
    """把后端 {id} 与前端 :id 归一到同一个比较口径。

    后端 openapi 的路径带 /api 前缀，前端 ApiEndpoints 的模板是相对 /api 的；
    两者必须落到同一坐标系，否则合同矩阵全量假阴性（第一版就踩了这个坑，见 commit 记录）。
    """
    p = path.strip()
    p = p.split("?")[0]
    if not p.startswith("/"):
        p = "/" + p
    p = re.sub(r"\{[^}]+\}", "{}", p)
    p = re.sub(r":[A-Za-z_][A-Za-z0-9_]*", "{}", p)
    if len(p) > 1 and p.endswith("/"):
        p = p[:-1]
    if not consumer and (p == API_PREFIX or p.startswith(API_PREFIX + "/")):
        p = p[len(API_PREFIX) :] or "/"
    return p


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


# ---------------------------------------------------------------- openapi


HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


def parse_openapi(openapi_path: Path) -> tuple[list[dict], str]:
    spec = yaml.safe_load(openapi_path.read_text(encoding="utf-8"))
    items: list[dict] = []
    for path, node in (spec.get("paths") or {}).items():
        if not isinstance(node, dict):
            continue
        for method, op in node.items():
            if method.lower() not in HTTP_METHODS or not isinstance(op, dict):
                continue
            security = op.get("security", spec.get("security"))
            items.append(
                {
                    "path": path,
                    "normalized": normalize_path(path),
                    "method": method.upper(),
                    "operation_id": op.get("operationId"),
                    "tags": op.get("tags") or [],
                    "secured": bool(security),
                    "deprecated": bool(op.get("deprecated", False)),
                }
            )
    items.sort(key=lambda x: (x["normalized"], x["method"]))
    return items, spec.get("openapi", "unknown")


# ---------------------------------------------------------------- consumers


ENUM_ENTRY = re.compile(r"^\s*([A-Za-z0-9_]+)\s*=\s*'([^']*)'\s*,?\s*$", re.M)


def parse_frontend_consumers(endpoints_tsx: Path) -> list[dict]:
    text = endpoints_tsx.read_text(encoding="utf-8", errors="ignore")
    out = []
    for name, template in ENUM_ENTRY.findall(text):
            out.append(
                {
                    "enum": name,
                    "template": template,
                    "normalized": normalize_path(template, consumer=True),
                }
            )
    out.sort(key=lambda x: x["enum"])
    return out


# ---------------------------------------------------------------- dep graph


IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_][A-Za-z0-9_]*)", re.M)


def parse_dependency_graph(backend_root: Path) -> dict:
    apps = [
        d.name
        for d in sorted(backend_root.iterdir())
        if d.is_dir()
        and (d / "models.py").exists()
        and (d / "__init__.py").exists()
    ]
    app_set = set(apps)
    edges = set()
    for app in apps:
        for py in (backend_root / app).rglob("*.py"):
            if "migrations" in py.parts:
                continue
            try:
                text = py.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for mod in IMPORT_RE.findall(text):
                if mod in app_set and mod != app:
                    edges.add((app, mod))
    return {
        "nodes": apps,
        "edges": [{"from": a, "to": b} for a, b in sorted(edges)],
        "node_count": len(apps),
        "edge_count": len(edges),
    }


# ---------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="被测仓根目录")
    ap.add_argument("--out", default="0.ssot")
    ap.add_argument("--openapi", default="0.ssot/openapi.yaml")
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    openapi_path = (Path.cwd() / args.openapi).resolve()
    if not openapi_path.exists():
        sys.exit(f"缺少 {openapi_path}，先执行 manage.py spectacular 导出规范")

    commit = git(repo, "rev-parse", "HEAD")
    commit_date = git(repo, "log", "-1", "--format=%cI")

    endpoints, spec_version = parse_openapi(openapi_path)
    (out / "endpoints.json").write_text(
        json.dumps(
            {
                "source": str(openapi_path.name),
                "openapi_version": spec_version,
                "commit": commit,
                "count": len(endpoints),
                "items": endpoints,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    tsx = repo / "src/frontend/lib/enums/ApiEndpoints.tsx"
    consumers = parse_frontend_consumers(tsx) if tsx.exists() else []
    (out / "consumers.json").write_text(
        json.dumps(
            {
                "source": "src/frontend/lib/enums/ApiEndpoints.tsx",
                "commit": commit,
                "count": len(consumers),
                "items": consumers,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    backend_index: dict[str, list[dict]] = {}
    for item in endpoints:
        backend_index.setdefault(item["normalized"], []).append(item)

    matched, unmatched_consumer = [], []
    for c in consumers:
        hit = backend_index.get(c["normalized"])
        if hit:
            matched.append(
                {
                    "enum": c["enum"],
                    "template": c["template"],
                    "methods": sorted({h["method"] for h in hit}),
                    "operation_ids": [h["operation_id"] for h in hit],
                }
            )
        else:
            unmatched_consumer.append(c)

    consumer_norm = {c["normalized"] for c in consumers}
    unclaimed_backend = [e for e in endpoints if e["normalized"] not in consumer_norm]

    matrix = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "commit": commit,
        "provider": "InvenTree backend (Django REST Framework)",
        "consumer": "InvenTree frontend (React/TypeScript ApiEndpoints enum)",
        "totals": {
            "backend_operations": len(endpoints),
            "consumer_declarations": len(consumers),
            "matched": len(matched),
            "unmatched_consumer": len(unmatched_consumer),
            "backend_unclaimed": len(unclaimed_backend),
        },
        "matched": matched,
        "unmatched_consumer": unmatched_consumer,
        "backend_unclaimed_sample": [
            {"path": e["path"], "method": e["method"], "tags": e["tags"]}
            for e in unclaimed_backend[:40]
        ],
    }
    (out / "contract_matrix.json").write_text(
        json.dumps(matrix, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    depgraph = parse_dependency_graph(repo / "src/backend/InvenTree")
    depgraph["commit"] = commit
    (out / "dependency_graph.json").write_text(
        json.dumps(depgraph, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    version = {
        "commit": commit,
        "commit_date": commit_date,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "openapi_sha256": sha256_file(openapi_path),
        "counts": {
            "endpoints": len(endpoints),
            "consumers": len(consumers),
            "backend_apps": depgraph["node_count"],
            "dependency_edges": depgraph["edge_count"],
        },
    }
    (out / "schema_version.json").write_text(
        json.dumps(version, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(json.dumps(version, ensure_ascii=False, indent=2))
    print(
        f"\nL0 合同矩阵: 后端 {len(endpoints)} 个操作 / 前端 {len(consumers)} 条声明 / "
        f"命中 {len(matched)} / 前端未命中 {len(unmatched_consumer)} / 后端未被引用 {len(unclaimed_backend)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
