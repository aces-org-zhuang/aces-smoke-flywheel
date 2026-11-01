#!/usr/bin/env python
"""为每一个 SSOT 版本单独渲染一份"资产全景"，并且每一条资产都能点开看实际内容。

这里不写"改了什么"：每个版本各自成章，章节内部结构完全一致，独立可读——
一个版本里有哪些资产、各有多少、怎么分布、全量清单是什么、每一条的实际内容是什么。
版本之间只并列陈列，不做差异叙述。

"可点开"的口径（点开看到的是原文，不是摘要）：
  端点     → OpenAPI 里该操作的完整原文：description / parameters / requestBody /
             responses（含 schema 逐层展开）/ security / 原始 JSON
  schema   → components.schemas 里的完整定义：类型、必填、逐字段类型与说明、组合器、items
  消费方   → src/frontend/lib/enums/ApiEndpoints.tsx 的源码行（文件名 + 行号 + 原文 + 上下文）
  契约矩阵 → 命中项的全部 method 与 operationId（可继续下钻到操作）
  依赖边   → src/backend/InvenTree/<app> 里真正的那条 import 语句（文件 + 行号 + 原文）
  鉴权方案 → securitySchemes 的完整定义

数据来源：
  0.ssot/openapi.yaml（规范原文）
  0.ssot/history/<version>/{endpoints,consumers,contract_matrix,dependency_graph,
                            schema_version}.json
  已提交原件 0.ssot/（作为"现行版本"并列）

用法：
  python tools/build_ssot_atlas.py --repo <被测仓克隆> --out docs/ssot-atlas.html
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from build_report import CSS, esc  # noqa: E402  与报告页共用排版

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")
METHOD_SET = set(HTTP_METHODS)

CONSUMER_FILE = "src/frontend/lib/enums/ApiEndpoints.tsx"
ENUM_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*'([^']*)'\s*,?\s*$")
IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_][A-Za-z0-9_]*)")


# ---------------------------------------------------------------- 基础工具


def jload(p: Path):
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def bar(count: int, biggest: int, width: int = 120) -> str:
    w = 0 if not biggest else max(2, round(count / biggest * width))
    return (
        f'<span style="display:inline-block;height:7px;width:{w}px;'
        'background:#1a56db;border-radius:2px;vertical-align:middle"></span>'
    )


def attr(v) -> str:
    """写进 HTML 属性值：引号必须转义，否则属性会被截断。"""
    return (
        str(v)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace(chr(10), " ")
        .replace(chr(9), " ")
    )


def short(text, n: int = 90) -> str:
    if not text:
        return ""
    t = " ".join(str(text).split())
    return t if len(t) <= n else t[: n - 1] + "…"


# ---------------------------------------------------------------- 规范资产


def load_spec(openapi: Path) -> dict:
    spec = yaml.safe_load(openapi.read_text(encoding="utf-8"))
    if not isinstance(spec, dict):
        sys.exit(f"{openapi} 解析后不是对象，规范可能为空")
    comp = spec.get("components") or {}
    schemas = comp.get("schemas") or {}
    sec = comp.get("securitySchemes") or {}
    paths = spec.get("paths") or {}

    ops = 0
    tag_counter = Counter()
    op_with_params = 0
    op_with_body = 0
    for node in paths.values():
        if not isinstance(node, dict):
            continue
        for m, op in node.items():
            if m.lower() in METHOD_SET and isinstance(op, dict):
                ops += 1
                for t in op.get("tags") or ["untagged"]:
                    tag_counter[t] += 1
                if op.get("parameters"):
                    op_with_params += 1
                if op.get("requestBody"):
                    op_with_body += 1

    # 自证：所有 $ref 都能在 components.schemas 里找到目标
    def refs(o, acc):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "$ref" and isinstance(v, str):
                    acc.append(v)
                else:
                    refs(v, acc)
        elif isinstance(o, list):
            for v in o:
                refs(v, acc)
        return acc

    all_refs = refs({"paths": paths, "schemas": schemas}, [])
    dangling = sorted({r for r in all_refs if r.split("/")[-1] not in schemas})

    schema_rows = []
    for name, s in schemas.items():
        props = s.get("properties") or {}
        schema_rows.append(
            {
                "name": name,
                "type": s.get("type") or ("allOf" if s.get("allOf") else "—"),
                "props": len(props),
                "required": len(s.get("required") or []),
                "desc": short(s.get("description"), 70),
            }
        )
    schema_rows.sort(key=lambda r: r["name"])

    return {
        "openapi": spec.get("openapi"),
        "paths": paths,
        "schemas": schemas,
        "security": sec,
        "servers": [s.get("url") for s in (spec.get("servers") or [])],
        "path_count": len(paths),
        "operation_count": ops,
        "op_with_params": op_with_params,
        "op_with_body": op_with_body,
        "tags": sorted(tag_counter.items(), key=lambda x: (-x[1], x[0])),
        "schema_rows": schema_rows,
        "ref_total": len(all_refs),
        "ref_dangling": dangling,
    }


def consumer_sources(repo: Path) -> dict:
    """把 216 条声明落到 ApiEndpoints.tsx 的具体行，带上下文。"""
    p = repo / CONSUMER_FILE
    if not p.exists():
        return {}
    lines = p.read_text(encoding="utf-8").split("\n")
    out = {}
    for i, line in enumerate(lines):
        m = ENUM_RE.match(line)
        if not m:
            continue
        ctx = []
        for j in range(max(0, i - 3), min(len(lines), i + 2)):
            ctx.append([j + 1, lines[j].rstrip()])
        out[m.group(1)] = {
            "f": CONSUMER_FILE,
            "l": i + 1,
            "raw": line.strip(),
            "ctx": ctx,
        }
    return out


def dep_sites(repo: Path, nodes: list[str]) -> dict:
    """依赖边落到真正的那条 import 语句，规则与 emit_ssot.parse_dependency_graph 一致。"""
    known = set(nodes)
    root = repo / "src/backend/InvenTree"
    out: dict[str, list] = defaultdict(list)
    if not root.exists():
        return {}
    for app in nodes:
        for py in (root / app).rglob("*.py"):
            if "migrations" in py.parts:
                continue
            try:
                text = py.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for i, line in enumerate(text.split("\n"), 1):
                m = IMPORT_RE.match(line)
                if not m:
                    continue
                target = m.group(1)
                if target not in known or target == app:
                    continue
                key = f"{app}->{target}"
                if len(out[key]) < 3:
                    out[key].append(
                        {
                            "f": str(py.relative_to(repo)).replace("\\", "/"),
                            "l": i,
                            "raw": line.strip(),
                        }
                    )
    return dict(out)


# ---------------------------------------------------------------- 版本资产


def load_version(label: str, kind: str, d: Path, openapi: Path, note: str) -> dict:
    ep = jload(d / "endpoints.json") or {}
    cs = jload(d / "consumers.json") or {}
    cm = jload(d / "contract_matrix.json") or {}
    dg = jload(d / "dependency_graph.json") or {}
    sv = jload(d / "schema_version.json") or {}
    items = ep.get("items", [])
    consumers = cs.get("items", [])
    matched_enums = {m["enum"] for m in cm.get("matched", [])}
    consumer_norm = {c["normalized"] for c in consumers}
    unclaimed = [e for e in items if e["normalized"] not in consumer_norm]

    files = {}
    for name in (
        "endpoints.json",
        "consumers.json",
        "contract_matrix.json",
        "dependency_graph.json",
        "schema_version.json",
    ):
        p = d / name
        if p.exists():
            files[name] = {"bytes": p.stat().st_size, "sha256": sha256_file(p)[:16]}

    return {
        "label": label,
        "kind": kind,
        "note": note,
        "dir": str(d.relative_to(ROOT)),
        "commit": sv.get("commit"),
        "openapi_sha256": sv.get("openapi_sha256"),
        "openapi_bytes": openapi.stat().st_size,
        "generated_at": sv.get("generated_at"),
        "counts": {
            "operations": len(items),
            "paths": len({e["path"] for e in items}),
            "consumers": len(consumers),
            "matched": len(cm.get("matched", [])),
            "unmatched": len(cm.get("unmatched_consumer", [])),
            "unclaimed": len(unclaimed),
            "apps": dg.get("node_count"),
            "edges": dg.get("edge_count"),
        },
        "files": files,
        "endpoints": items,
        "consumers": consumers,
        "matched_enums": matched_enums,
        "matrix": cm,
        "depgraph": dg,
        "unclaimed": unclaimed,
    }


def m_badge(method: str) -> str:
    return f'<span class="mm m-{method.lower()}">{esc(method)}</span>'


def render_version(v: dict, spec: dict, sources: dict, sites: dict) -> str:
    items = v["endpoints"]
    by_tag = Counter()
    by_method = Counter()
    by_domain = Counter()
    secured = Counter()
    deprecated = 0
    for e in items:
        for t in e["tags"] or ["untagged"]:
            by_tag[t] += 1
        by_method[e["method"]] += 1
        seg = [s for s in e["normalized"].strip("/").split("/") if s and not s.startswith("{")]
        by_domain[seg[0] if seg else "(root)"] += 1
        secured["需鉴权" if e["secured"] else "无鉴权"] += 1
        deprecated += 1 if e["deprecated"] else 0

    groups: dict[str, list[dict]] = defaultdict(list)
    for e in items:
        groups[(e["tags"] or ["untagged"])[0]].append(e)

    tag_rows = "".join(
        f"<tr><td><code>{esc(t)}</code></td><td>{c}</td><td>{bar(c, by_tag.most_common(1)[0][1])}</td></tr>"
        for t, c in sorted(by_tag.items(), key=lambda x: (-x[1], x[0]))
    )
    method_rows = "".join(
        f"<tr><td>{m_badge(m)}</td><td>{c}</td><td>{bar(c, by_method.most_common(1)[0][1])}</td></tr>"
        for m, c in sorted(by_method.items(), key=lambda x: (-x[1], x[0]))
    )
    domain_rows = "".join(
        f"<tr><td><code>{esc(d)}</code></td><td>{c}</td><td>{bar(c, by_domain.most_common(1)[0][1])}</td></tr>"
        for d, c in sorted(by_domain.items(), key=lambda x: (-x[1], x[0]))
    )

    # ---- D 端点全量清单：每行可点开规格原文
    group_blocks = []
    for tag, rows in sorted(groups.items(), key=lambda x: (-len(x[1]), x[0])):
        body = "".join(
            f'<tr class="row" data-k="ep" data-x="{attr(e["method"] + " " + e["path"])}" '
            f'data-norm="{attr(e["normalized"])}" data-c="4" '
            f'data-s="{attr((e["method"] + " " + e["path"] + " " + (e.get("operation_id") or "") + " " + " ".join(e["tags"])).lower())}">'
            f'<td>{m_badge(e["method"])}</td><td><code>{esc(e["path"])}</code></td>'
            f'<td class="opid">{esc(e.get("operation_id") or "-")}</td>'
            f"<td class=\"ctr\">{'✓' if e['secured'] else '—'}</td></tr>"
            for e in sorted(rows, key=lambda x: (x["path"], x["method"]))
        )
        group_blocks.append(
            f"""<details><summary><strong>{esc(tag)}</strong> · {len(rows)} 个操作</summary>
      <table class="lst"><tr><th style="width:74px">方法</th><th>路径</th><th style="width:30%">operationId</th><th style="width:60px">鉴权</th></tr>{body}</table>
      </details>"""
        )

    # ---- B 数据模型资产：可点开逐字段定义
    schema_rows = "".join(
        f'<tr class="row" data-k="sch" data-x="{attr(r["name"])}" data-c="4" '
        f'data-s="{attr(r["name"].lower() + " " + (r["desc"] or "").lower())}">'
        f'<td><code>{esc(r["name"])}</code></td><td class="small">{esc(r["type"])}</td>'
        f'<td class="num">{r["props"]}</td><td class="small">{esc(r["desc"] or "—")}</td></tr>'
        for r in spec["schema_rows"]
    )
    sec_rows = "".join(
        f'<tr class="row" data-k="sec" data-x="{attr(name)}" data-c="3" data-s="{attr(name.lower())}">'
        f'<td><code>{esc(name)}</code></td><td class="small">{esc(str(d.get("type") or "—"))}</td>'
        f'<td class="small">{esc(short(d.get("description"), 80) or "—")}</td></tr>'
        for name, d in sorted(spec["security"].items())
    )

    # ---- E 消费方声明：可点开前端源码行
    consumer_rows = "".join(
        f'<tr class="row" data-k="con" data-x="{attr(c["enum"])}" data-c="4" '
        f'data-st="{attr("命中" if c["enum"] in v["matched_enums"] else "未命中")}" '
        f'data-s="{attr((c["enum"] + " " + c["template"] + " " + c["normalized"]).lower())}">'
        f'<td><code>{esc(c["enum"])}</code></td><td><code>{esc(c["template"])}</code></td>'
        f'<td><code>{esc(c["normalized"])}</code></td>'
        f'<td class="ctr">{"命中" if c["enum"] in v["matched_enums"] else "未命中"}</td></tr>'
        for c in v["consumers"]
    )

    # ---- F 契约矩阵：命中可下钻到操作，未命中可下钻到源码，未被引用可下钻到规格
    matched_rows = "".join(
        f'<tr class="row" data-k="mat" data-x="{attr(m["enum"])}" data-c="4" '
        f'data-s="{attr((m["enum"] + " " + m["template"] + " " + " ".join(m["methods"])).lower())}">'
        f'<td><code>{esc(m["enum"])}</code></td><td><code>{esc(m["template"])}</code></td>'
        f'<td class="small">{esc(" ".join(m["methods"]))}</td><td class="num">{len(m["operation_ids"])}</td></tr>'
        for m in v["matrix"].get("matched", [])
    )
    unmatched_rows = "".join(
        f'<tr class="row" data-k="con" data-x="{attr(u["enum"])}" data-c="3" data-st="未命中" '
        f'data-s="{attr((u["enum"] + " " + u["template"] + " " + u["normalized"]).lower())}">'
        f'<td><code>{esc(u["enum"])}</code></td><td><code>{esc(u["template"])}</code></td>'
        f'<td><code>{esc(u["normalized"])}</code></td></tr>'
        for u in v["matrix"].get("unmatched_consumer", [])
    )
    unclaimed_rows = "".join(
        f'<tr class="row" data-k="ep" data-x="{attr(e["method"] + " " + e["path"])}" '
        f'data-norm="{attr(e["normalized"])}" data-c="3" '
        f'data-s="{attr((e["method"] + " " + e["path"] + " " + " ".join(e["tags"])).lower())}">'
        f'<td>{m_badge(e["method"])}</td><td><code>{esc(e["path"])}</code></td>'
        f'<td class="small">{esc(",".join(e["tags"]) or "-")}</td></tr>'
        for e in sorted(v["unclaimed"], key=lambda x: (x["path"], x["method"]))
    )

    nodes = v["depgraph"].get("nodes", [])
    out_edges: dict[str, list[str]] = {n: [] for n in nodes}
    for e in v["depgraph"].get("edges", []):
        out_edges.setdefault(e["from"], []).append(e["to"])
    dep_rows = "".join(
        f'<tr class="row" data-k="dep" data-x="{attr(n)}" data-c="3" data-s="{attr(n.lower())}">'
        f'<td><code>{esc(n)}</code></td><td class="num">{len(out_edges.get(n, []))}</td>'
        f'<td class="small">{esc(", ".join(sorted(out_edges.get(n, []))) or "—")}</td></tr>'
        for n in nodes
    )

    repo_url = "https://github.com/aces-org-zhuang/aces-smoke-flywheel/blob/main"
    file_rows = "".join(
        f'<tr><td><a href="{repo_url}/{v["dir"]}/{k}"><code>{esc(k)}</code></a></td>'
        f'<td class="num">{val["bytes"]:,} B</td>'
        f'<td><code>{esc(val["sha256"])}…</code></td></tr>'
        for k, val in v["files"].items()
    )

    with_src = sum(1 for c in v["consumers"] if c["enum"] in sources)
    edge_keys = [f"{a}->{b}" for a in nodes for b in out_edges.get(a, [])]
    with_site = sum(1 for k in edge_keys if k in sites)

    return f"""
<section class="ver" id="{esc(v['id'])}">
  <div class="vhead">
    <div>
      <div class="kicker">SSOT 版本资产全景</div>
      <h2>{esc(v['label'])}</h2>
      <p class="small" style="margin:2px 0 0">{esc(v['note'])}</p>
    </div>
    <div class="vbatch"><span class="pill mute">{esc(v['kind'])}</span></div>
  </div>

  <div class="grid g4">
    <div class="card kpi"><div class="n">{v['counts']['operations']}</div><div class="l">API 操作</div></div>
    <div class="card kpi"><div class="n">{v['counts']['paths']}</div><div class="l">路径（path）</div></div>
    <div class="card kpi"><div class="n">{v['counts']['consumers']}</div><div class="l">前端声明</div></div>
    <div class="card kpi"><div class="n">{len(spec['schemas'])}</div><div class="l">数据模型（schema）</div></div>
  </div>

  <h3 id="{esc(v['id'])}-a">A. 版本指纹</h3>
  <table>
    <tr><th style="width:22%">项</th><th>值</th></tr>
    <tr><td>目录</td><td><code>{esc(v['dir'])}</code></td></tr>
    <tr><td>绑定 commit</td><td><code>{esc(str(v['commit']))}</code></td></tr>
    <tr><td>openapi 指纹</td><td><code>{esc(str(v['openapi_sha256']))[:24]}…</code> · {v['openapi_bytes']:,} 字节</td></tr>
    <tr><td>生成时间</td><td>{esc(str(v['generated_at']))}</td></tr>
  </table>
  <table><tr><th>产物（点名字看版本库里的原文）</th><th style="width:130px">字节</th><th>sha256 前 16 位</th></tr>{file_rows}</table>

  <h3 id="{esc(v['id'])}-b">B. 规格资产（OpenAPI {esc(str(spec['openapi']))}）</h3>
  <div class="grid g4">
    <div class="card kpi"><div class="n">{spec['path_count']}</div><div class="l">paths 节点</div></div>
    <div class="card kpi"><div class="n">{spec['operation_count']}</div><div class="l">操作</div></div>
    <div class="card kpi"><div class="n">{len(spec['schemas'])}</div><div class="l">schema</div></div>
    <div class="card kpi"><div class="n">{len(spec['security'])}</div><div class="l">鉴权方案</div></div>
  </div>
  <p class="small">服务地址：{esc('、'.join(str(x) for x in spec['servers']) or '—')} ·
  带参数的操作 {spec['op_with_params']} 个 · 带请求体的操作 {spec['op_with_body']} 个 ·
  <code>$ref</code> 引用 {spec['ref_total']:,} 处，解析失败 {len(spec['ref_dangling'])} 处</p>

  <h3 style="margin-top:18px">鉴权方案（{len(spec['security'])} 条，点开看定义）</h3>
  <table class="lst"><tr><th style="width:26%">方案</th><th style="width:16%">类型</th><th>说明</th></tr>{sec_rows}</table>

  <h3>数据模型（{len(spec['schemas'])} 条，点开看逐字段定义）</h3>
  <table class="lst"><tr><th style="width:34%">schema</th><th style="width:14%">类型</th><th style="width:80px">字段数</th><th>说明</th></tr>{schema_rows}</table>

  <h3 id="{esc(v['id'])}-c">C. 端点资产分布</h3>
  <div class="grid g2">
    <div class="card">
      <strong>按方法</strong>
      <table><tr><th>方法</th><th>数量</th><th></th></tr>{method_rows}</table>
      <p class="small">鉴权：{esc(' / '.join(f'{k} {c}' for k, c in secured.items()))} · 标记废弃 {deprecated} 个</p>
    </div>
    <div class="card">
      <strong>按资源域（路径首段）</strong>
      <table><tr><th>域</th><th>数量</th><th></th></tr>{domain_rows}</table>
    </div>
  </div>

  <h3 style="margin-top:22px">按操作标签（{len(by_tag)} 个）</h3>
  <table><tr><th>tag</th><th style="width:90px">操作数</th><th></th></tr>{tag_rows}</table>

  <h3 id="{esc(v['id'])}-d">D. 端点全量清单（{v['counts']['operations']} 条，按标签分组，点开看规格原文）</h3>
  {''.join(group_blocks)}

  <h3 id="{esc(v['id'])}-e">E. 前端消费方声明（{v['counts']['consumers']} 条，点开看源码行 · 可定位 {with_src} 条）</h3>
  <table class="lst"><tr><th style="width:26%">枚举名</th><th style="width:34%">模板</th><th>归一化</th><th style="width:80px">契约状态</th></tr>{consumer_rows}</table>

  <h3 id="{esc(v['id'])}-f">F. 契约矩阵（这一版的三段资产）</h3>
  <div class="grid g3">
    <div class="card kpi"><div class="n">{v['counts']['matched']}</div><div class="l">命中（声明 ↔ 操作）</div></div>
    <div class="card kpi"><div class="n">{v['counts']['unmatched']}</div><div class="l">前端声明未命中</div></div>
    <div class="card kpi"><div class="n">{v['counts']['unclaimed']}</div><div class="l">后端操作未被声明引用</div></div>
  </div>
  <details><summary><strong>命中清单</strong> · {v['counts']['matched']} 条（点开看全部 method 与 operationId）</summary>
    <table class="lst"><tr><th style="width:30%">声明</th><th style="width:30%">模板</th><th>可用的方法</th><th style="width:90px">operation 数</th></tr>{matched_rows}</table>
  </details>
  <details><summary><strong>未命中清单</strong> · {v['counts']['unmatched']} 条（点开看源码行）</summary>
    <table class="lst"><tr><th style="width:30%">声明</th><th style="width:40%">模板</th><th>归一化</th></tr>{unmatched_rows}</table>
  </details>
  <details><summary><strong>未被引用的后端操作</strong> · {v['counts']['unclaimed']} 条（点开看规格原文）</summary>
    <table class="lst"><tr><th style="width:74px">方法</th><th>路径</th><th style="width:30%">标签</th></tr>{unclaimed_rows}</table>
  </details>

  <h3 id="{esc(v['id'])}-g">G. 依赖图资产（{v['counts']['apps']} 个 app / {v['counts']['edges']} 条 import 边 · 可定位 {with_site} 条）</h3>
  <table class="lst"><tr><th style="width:26%">app</th><th style="width:90px">出边数</th><th>依赖的 app（点开看 import 语句）</th></tr>{dep_rows}</table>
</section>
"""


# ---------------------------------------------------------------- 页面外壳

ATLAS_CSS = """
  .ver { margin:56px 0 0; }
  .vhead { display:flex; justify-content:space-between; align-items:flex-start; gap:20px;
           border-top:3px solid var(--ink); padding-top:16px; }
  .ver h2 { margin:6px 0 4px; border-top:none; padding-top:0; }
  .kpi .n { font-variant-numeric:tabular-nums; }

  body.js .ver { display:none; }
  body.js .ver.active { display:block; }

  .bar { position:sticky; top:0; z-index:40; background:rgba(247,248,250,.96);
         backdrop-filter:blur(8px); border-bottom:1px solid var(--line);
         padding:10px 0; margin:16px 0 0; display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
  .tabs { display:flex; gap:6px; flex-wrap:wrap; }
  .tab { font:inherit; font-size:12.5px; padding:5px 12px; border:1px solid var(--line);
         border-radius:999px; background:#fff; color:var(--ink2); cursor:pointer; }
  .tab[aria-selected="true"] { background:var(--ink); border-color:var(--ink); color:#fff; font-weight:600; }
  .bar input[type=search] { flex:1 1 220px; min-width:180px; font:inherit; font-size:13px;
         padding:6px 10px; border:1px solid var(--line); border-radius:8px; background:#fff; }
  .bar .btn { font:inherit; font-size:12.5px; padding:5px 10px; border:1px solid var(--line);
         border-radius:8px; background:#fff; color:var(--ink2); cursor:pointer; }
  .bar .cnt { font-size:12.5px; color:var(--mute); font-variant-numeric:tabular-nums; }

  table.lst td { padding:6px 10px; }
  table.lst code, .detail code { word-break:break-word; }
  .detail table td { word-break:break-word; }
  tr.row { cursor:pointer; }
  tr.row:hover td { background:#eef3fd; }
  tr.row.open td { background:#e8effc; }
  tr.row.open td:first-child { box-shadow:inset 3px 0 0 var(--brand); }
  tr.drow > td { background:#fbfcfe; padding:0; border-bottom:1px solid var(--line); }
  .detail { padding:14px 18px 18px; border-left:3px solid var(--brand); background:#fbfcfe; }
  .detail h4 { font-size:13.5px; margin:16px 0 6px; color:var(--ink); }
  .detail h4:first-of-type { margin-top:8px; }
  .detail .dhead { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
  .detail .dhead .opid { font-family:ui-monospace,Consolas,monospace; font-size:12.5px; color:var(--mute); }
  .detail .dmeta { display:flex; gap:14px; flex-wrap:wrap; font-size:12.5px; color:var(--mute); margin-top:6px; }
  .detail .ddesc { font-size:13px; color:var(--ink2); margin:8px 0 0; }
  .detail table { margin:8px 0 4px; font-size:12.8px; }
  .detail th, .detail td { padding:5px 8px; }
  .detail pre { margin:8px 0 0; max-height:420px; }
  .detail .raw { font-size:12px; }

  .mm { display:inline-block; min-width:52px; text-align:center; padding:1px 7px; border-radius:4px;
        font-family:ui-monospace,Consolas,monospace; font-size:11.5px; font-weight:700;
        letter-spacing:.04em; background:#eef1f5; color:var(--ink2); }
  .m-get { background:#e7f0fd; color:#12459e; }
  .m-post { background:#e6f5ea; color:#0f6b3c; }
  .m-put, .m-patch { background:#fdf3e0; color:#8a5a00; }
  .m-delete { background:#fdeceb; color:#a01c12; }
  td.num { text-align:right; font-variant-numeric:tabular-nums; }
  td.ctr { text-align:center; color:var(--mute); }
  td.opid { font-family:ui-monospace,Consolas,monospace; font-size:12px; color:var(--mute); }

  .chips { display:flex; gap:6px; flex-wrap:wrap; margin:6px 0; }
  .chip { font-size:11.5px; padding:1px 7px; border-radius:4px; background:#eef1f5; color:var(--ink2);
          font-family:ui-monospace,Consolas,monospace; }
  .chip.req { background:#fdecea; color:var(--bad); font-weight:600; }
  .schbox { border:1px solid var(--line); border-radius:8px; padding:10px 12px; margin:10px 0; background:#fff; }
  .schtitle { font-family:ui-monospace,Consolas,monospace; font-size:12.8px; font-weight:600; }
  .schtitle .small { font-family:inherit; font-weight:400; margin-left:8px; }
  .nested { border-left:2px solid var(--line); padding-left:12px; margin:8px 0 0; }
  .refline { font-size:12px; color:var(--mute); margin:6px 0 2px; }
  .sdesc { font-size:12.5px; color:var(--ink2); margin:4px 0; }
  .comb { font-size:12px; color:var(--mute); margin:8px 0 2px; }
  .srcnote { font-size:12px; color:var(--mute); }
  pre.code { background:#11161d; color:#e6edf3; padding:12px 14px; border-radius:8px;
             overflow-x:auto; font-size:12.2px; line-height:1.6; }
  pre.code .ln { color:#6a7684; user-select:none; padding-right:12px; }
  pre.code .hit { background:#1d2a3a; display:inline-block; width:100%; }
  pre.code .kw { color:#8ab4f8; }

  .modalwrap { position:fixed; inset:0; background:rgba(12,17,24,.55); z-index:99;
               display:none; align-items:flex-start; justify-content:center; padding:40px 20px; overflow:auto; }
  .modalwrap.on { display:flex; }
  .modal { background:#fff; border-radius:12px; max-width:980px; width:100%;
           box-shadow:0 20px 60px rgba(0,0,0,.28); }
  .modal .mhead { display:flex; justify-content:space-between; align-items:center; gap:12px;
                  padding:14px 18px; border-bottom:1px solid var(--line); position:sticky; top:0;
                  background:#fff; border-radius:12px 12px 0 0; }
  .modal .mhead .t { font-weight:600; font-size:14px; }
  .modal .mbody { padding:16px 18px 22px; }
  .modal .mbody table { margin:8px 0; }
  .modal .x { border:1px solid var(--line); background:#fff; border-radius:6px; cursor:pointer;
              font:inherit; font-size:12.5px; padding:3px 9px; }
  .hint { font-size:12.5px; color:var(--mute); }
  .hide { display:none !important; }
"""

ATLAS_JS = r"""
(function () {
  var node = document.getElementById('atlas-data');
  if (!node) return;
  var D = JSON.parse(node.textContent);
  var SPEC = D.spec, PATHS = SPEC.paths, SCH = SPEC.schemas, SEC = SPEC.securitySchemes;
  var METH = ['get', 'post', 'put', 'patch', 'delete', 'head', 'options'];
  var CUR = 'V3';

  document.body.classList.add('js');

  // operationId -> "METHOD path"，让契约矩阵能下钻到具体操作
  var OPID = {};
  Object.keys(PATHS).forEach(function (p) {
    var nd = PATHS[p];
    if (!nd || typeof nd !== 'object') return;
    METH.forEach(function (m) {
      var op = nd[m];
      if (op && op.operationId) OPID[op.operationId] = m.toUpperCase() + ' ' + p;
    });
  });

  function esc(s) {
    return String(s === undefined || s === null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function refName(r) { return String(r).replace('#/components/schemas/', ''); }
  function schLink(n) { return '<a href="#" data-k="sch" data-x="' + esc(n) + '"><code>' + esc(n) + '</code></a>'; }
  function opLink(k) {
    return '<a href="#" data-k="ep" data-x="' + esc(k) + '"><code>' + esc(k) + '</code></a>';
  }
  function badge(m) {
    return '<span class="mm m-' + esc(String(m).toLowerCase()) + '">' + esc(m) + '</span>';
  }

  function typeOf(s) {
    if (!s) return '—';
    if (s.$ref) return schLink(refName(s.$ref));
    var t = s.type || (s.oneOf ? 'oneOf' : s.anyOf ? 'anyOf' : s.allOf ? 'allOf' : '—');
    if (s.type === 'array' && s.items) t = 'array&lt;' + typeOf(s.items) + '&gt;';
    else if (s.format) t += ' · ' + s.format;
    if (s.enum) t += ' ∈ ' + s.enum.slice(0, 8).map(function (x) { return JSON.stringify(x); }).join('|') +
      (s.enum.length > 8 ? '…' : '');
    if (s.nullable) t += ' | null';
    return esc(t);
  }

  function chips(s) {
    var out = [];
    if (s.type) out.push('type: ' + s.type);
    if (s.format) out.push('format: ' + s.format);
    if (s.default !== undefined) out.push('default: ' + JSON.stringify(s.default));
    if (s.nullable) out.push('nullable');
    if (s.readOnly) out.push('readOnly');
    if (s.minimum !== undefined || s.maximum !== undefined) {
      out.push('range: ' + (s.minimum === undefined ? '-∞' : s.minimum) + '..' + (s.maximum === undefined ? '∞' : s.maximum));
    }
    if (s.maxLength !== undefined) out.push('maxLength: ' + s.maxLength);
    if (s.minItems !== undefined) out.push('minItems: ' + s.minItems);
    if (s.deprecated) out.push('deprecated');
    return out.length ? '<div class="chips">' + out.map(function (x) {
      return '<span class="chip">' + esc(x) + '</span>';
    }).join('') + '</div>' : '';
  }

  function schemaBody(s, depth, seen) {
    if (!s) return '';
    depth = depth || 0;
    seen = seen || [];
    if (s.$ref) {
      var n = refName(s.$ref);
      if (seen.indexOf(n) >= 0 || depth >= 3) {
        return '<div class="refline">↳ ' + schLink(n) + ' <span class="hint">（' +
          (seen.indexOf(n) >= 0 ? '递归引用' : '层级较深') + '，点击单独展开）</span></div>';
      }
      var t = SCH[n];
      if (!t) return '<div class="refline">↳ <code>' + esc(n) + '</code> <span class="hint">（规范中不存在）</span></div>';
      return '<div class="schbox"><div class="schtitle">' + schLink(n) + '</div>' +
        schemaBody(t, depth + 1, seen.concat([n])) + '</div>';
    }
    var out = '';
    out += chips(s);
    if (s.description) out += '<div class="sdesc">' + esc(s.description) + '</div>';
    ['allOf', 'oneOf', 'anyOf'].forEach(function (key) {
      if (!s[key]) return;
      out += '<div class="comb">' + key + ' · ' + s[key].length + ' 项</div>';
      out += s[key].map(function (x) { return schemaBody(x, depth + 1, seen.slice()); }).join('');
    });
    if (s.properties) {
      var req = s.required || [];
      var rows = Object.keys(s.properties).map(function (k) {
        var v = s.properties[k] || {};
        var desc = v.description || (v.items && v.items.description) || '';
        return '<tr><td><code>' + esc(k) + '</code></td><td>' + typeOf(v) + '</td><td>' +
          (req.indexOf(k) >= 0 ? '<span class="chip req">必填</span>' : '') + '</td><td class="small">' +
          esc(desc) + '</td></tr>';
      }).join('');
      out += '<table class="props"><tr><th style="width:28%">字段</th><th style="width:26%">类型</th>' +
        '<th style="width:64px"></th><th>说明</th></tr>' + rows + '</table>';
    }
    if (s.items) {
      out += '<div class="comb">items（数组元素）</div>' + schemaBody(s.items, depth + 1, seen.slice());
    }
    if (s.additionalProperties && typeof s.additionalProperties === 'object') {
      out += '<div class="comb">additionalProperties</div>' +
        schemaBody(s.additionalProperties, depth + 1, seen.slice());
    }
    return out;
  }

  function paramTable(list) {
    if (!list || !list.length) return '<p class="hint">无参数</p>';
    var rows = list.map(function (p) {
      var s = p.schema || {};
      var def = s.default !== undefined ? JSON.stringify(s.default) : '—';
      return '<tr><td><code>' + esc(p.name) + '</code></td><td>' + esc(p.in) + '</td><td>' + typeOf(s) +
        '</td><td>' + (p.required ? '是' : '否') + '</td><td class="small">' + esc(def) +
        '</td><td class="small">' + esc(p.description || (s.description || '')) + '</td></tr>';
    }).join('');
    return '<table><tr><th style="width:26%">名称</th><th style="width:70px">位置</th><th style="width:20%">类型</th>' +
      '<th style="width:56px">必填</th><th style="width:90px">默认</th><th>说明</th></tr>' + rows + '</table>';
  }

  function contentBlock(content) {
    if (!content) return '';
    var out = '';
    Object.keys(content).forEach(function (ct) {
      var media = content[ct] || {};
      out += '<div class="comb">' + esc(ct) + '</div>';
      if (media.schema) out += schemaBody(media.schema, 0, []);
      else if (media.type) out += '<div class="hint">类型 ' + esc(media.type) + '</div>';
    });
    return out;
  }

  function endpointDetail(key, meta) {
    var i = key.indexOf(' ');
    var m = key.slice(0, i).toLowerCase(), p = key.slice(i + 1);
    var nd = PATHS[p];
    if (!nd) {
      return '<p>规范里找不到路径 <code>' + esc(p) + '</code>。这条来自该版本端点集，但当前规范无此 path——本身就是一处需要核查的漂移。</p>';
    }
    var op = nd[m];
    if (!op) {
      return '<p>路径 <code>' + esc(p) + '</code> 存在，但没有 <b>' + esc(m.toUpperCase()) +
        '</b> 操作。该路径提供了：' + METH.filter(function (x) { return nd[x]; }).map(function (x) {
          return badge(x.toUpperCase());
        }).join(' ') + '</p>';
    }
    var params = (nd.parameters || []).concat(op.parameters || []);
    var out = '<div class="dhead">' + badge(m.toUpperCase()) + '<code>' + esc(p) + '</code>' +
      '<span class="opid">' + esc(op.operationId || '—') + '</span></div>';
    out += '<div class="dmeta">';
    out += '<span>标签：' + esc((op.tags || []).join(', ') || '—') + '</span>';
    if (meta && meta.norm) out += '<span>本版归一化：<code>' + esc(meta.norm) + '</code></span>';
    if (op.deprecated) out += '<span class="pill bad">已标记废弃</span>';
    out += '</div>';
    if (op.description) out += '<p class="ddesc">' + esc(op.description) + '</p>';

    out += '<h4>鉴权</h4>';
    if (op.security && op.security.length) {
      out += '<div class="chips">' + op.security.map(function (s) {
        return Object.keys(s).map(function (k) {
          var scopes = (s[k] || []).length ? '（' + s[k].join(', ') + '）' : '';
          return '<span class="chip">' + esc(k) + esc(scopes) + '</span>';
        }).join('');
      }).join('') + '</div>';
    } else if (op.security && op.security.length === 0) {
      out += '<p class="hint">显式声明为空 = 无需鉴权</p>';
    } else {
      out += '<p class="hint">操作未声明，沿用规范级默认</p>';
    }

    out += '<h4>参数（' + params.length + '）</h4>' + paramTable(params);

    out += '<h4>请求体</h4>';
    if (op.requestBody) {
      if (op.requestBody.description) out += '<p class="ddesc">' + esc(op.requestBody.description) + '</p>';
      if (op.requestBody.required) out += '<p class="hint">该请求体为必填</p>';
      out += contentBlock(op.requestBody.content);
    } else {
      out += '<p class="hint">无请求体</p>';
    }

    out += '<h4>响应（' + Object.keys(op.responses || {}).length + '）</h4>';
    out += Object.keys(op.responses || {}).sort().map(function (code) {
      var r = op.responses[code] || {};
      return '<div class="schbox"><div class="schtitle">' + esc(code) + ' <span class="small">' +
        esc(r.description || '') + '</span></div>' + contentBlock(r.content) + '</div>';
    }).join('');

    out += '<details><summary class="hint">规格原文（该操作的完整 JSON，未加工）</summary><pre class="raw">' +
      esc(JSON.stringify(op, null, 2)) + '</pre></details>';
    if (nd.parameters) {
      out += '<details><summary class="hint">路径级公共参数（path 节点上的 parameters）</summary><pre class="raw">' +
        esc(JSON.stringify(nd.parameters, null, 2)) + '</pre></details>';
    }
    return out;
  }

  function consumerDetail(name) {
    var s = D.sources[name];
    var out = '<div class="dhead"><code>' + esc(name) + '</code>' +
      '<span class="opid">' + esc(s ? s.f + ' : ' + s.l : '源码中未定位') + '</span></div>';
    if (!s) {
      out += '<p class="hint">该声明在 <code>' + esc(D.meta.consumerFile) + '</code> 里没有找到对应行。</p>';
      return out;
    }
    out += '<h4>源码原文</h4>';
    out += '<pre class="code">' + s.ctx.map(function (row) {
      var no = row[0], text = row[1];
      var hit = no === s.l;
      return '<span class="' + (hit ? 'hit' : '') + '"><span class="ln">' + no + '</span>' + esc(text) + '</span>';
    }).join('\n') + '</pre>';
    return out;
  }

  function matchedDetail(name) {
    var all = D.matrices[CUR] || {};
    var m = all[name];
    var out = '<div class="dhead"><code>' + esc(name) + '</code><span class="opid">本版：' + esc(CUR) + '</span></div>';
    if (!m) { out += '<p class="hint">该版本里这条声明没有命中记录。</p>'; return out; }
    out += '<h4>模板</h4><p><code>' + esc(m.template) + '</code></p>';
    out += '<h4>命中的操作（' + (m.operation_ids || []).length + '）</h4>';
    out += '<table><tr><th style="width:34%">operationId</th><th>操作</th></tr>' +
      (m.operation_ids || []).map(function (id) {
        var key = OPID[id];
        return '<tr><td><code>' + esc(id) + '</code></td><td>' +
          (key ? opLink(key) : '<span class="hint">当前规范中无此 operationId</span>') + '</td></tr>';
      }).join('') + '</table>';
    out += '<h4>可用方法</h4><div class="chips">' + (m.methods || []).map(function (x) {
      return badge(x);
    }).join('') + '</div>';
    return out;
  }

  function depDetail(app) {
    var edges = D.sites[app] || {};
    var keys = Object.keys(edges);
    var out = '<div class="dhead"><code>' + esc(app) + '</code><span class="opid">' +
      keys.length + ' 个下游 app</span></div>';
    if (!keys.length) {
      out += '<p class="hint">该 app 没有出边：不 import 任何其它 app。</p>';
      return out;
    }
    keys.sort().forEach(function (k) {
      var to = k.split('->')[1];
      out += '<h4>→ ' + esc(to) + '</h4>';
      out += '<table><tr><th style="width:52%">文件</th><th style="width:60px">行</th><th>语句</th></tr>' +
        edges[k].map(function (s) {
          return '<tr><td><code>' + esc(s.f) + '</code></td><td class="num">' + s.l +
            '</td><td class="small"><code>' + esc(s.raw) + '</code></td></tr>';
        }).join('') + '</table>';
    });
    return out;
  }

  function secDetail(name) {
    var d = SEC[name];
    if (!d) return '<p class="hint">规范中无此鉴权方案。</p>';
    var out = '<div class="dhead"><code>' + esc(name) + '</code>' + badge(String(d.type || '').toUpperCase()) + '</div>';
    if (d.description) out += '<p class="ddesc">' + esc(d.description) + '</p>';
    out += '<pre class="raw">' + esc(JSON.stringify(d, null, 2)) + '</pre>';
    return out;
  }

  function schemaDetail(name) {
    var s = SCH[name];
    if (!s) return '<p class="hint">规范中无此 schema：<code>' + esc(name) + '</code></p>';
    var out = '<div class="dhead"><code>' + esc(name) + '</code></div>';
    out += '<div class="schbox">' + schemaBody(s, 0, [name]) + '</div>';
    out += '<details><summary class="hint">规格原文（该 schema 的完整 JSON，未加工）</summary><pre class="raw">' +
      esc(JSON.stringify(s, null, 2)) + '</pre></details>';
    return out;
  }

  function detailFor(kind, key, tr) {
    if (kind === 'ep') return endpointDetail(key, { norm: tr ? tr.dataset.norm : '' });
    if (kind === 'sch') return schemaDetail(key);
    if (kind === 'con') return consumerDetail(key);
    if (kind === 'mat') return matchedDetail(key);
    if (kind === 'dep') return depDetail(key);
    if (kind === 'sec') return secDetail(key);
    return '<p class="hint">未知资产类型</p>';
  }

  // ---------- 行内展开
  function toggleRow(tr) {
    var nx = tr.nextElementSibling;
    if (nx && nx.classList.contains('drow')) {
      nx.parentNode.removeChild(nx);
      tr.classList.remove('open');
      return;
    }
    var drow = document.createElement('tr');
    drow.className = 'drow';
    drow.innerHTML = '<td colspan="' + (tr.dataset.c || 4) + '"><div class="detail">' +
      detailFor(tr.dataset.k, tr.dataset.x, tr) + '</div></td>';
    tr.parentNode.insertBefore(drow, tr.nextSibling);
    tr.classList.add('open');
  }

  // ---------- 弹层（用于资产之间的下钻）
  var stack = [];
  var wrap = document.createElement('div');
  wrap.className = 'modalwrap';
  wrap.innerHTML = '<div class="modal"><div class="mhead"><span class="t"></span>' +
    '<span><button class="x" data-act="back">← 返回</button> ' +
    '<button class="x" data-act="close">关闭</button></span></div><div class="mbody"></div></div>';
  document.body.appendChild(wrap);

  function renderModal() {
    var cur = stack[stack.length - 1];
    if (!cur) { wrap.classList.remove('on'); return; }
    wrap.querySelector('.t').innerHTML = cur.title;
    wrap.querySelector('.mbody').innerHTML = detailFor(cur.k, cur.x, null);
    wrap.querySelector('[data-act=back]').style.display = stack.length > 1 ? '' : 'none';
    wrap.classList.add('on');
    wrap.scrollTop = 0;
  }
  function push(k, x) {
    var title = k === 'ep' ? (badge(x.split(' ')[0]) + ' <code>' + esc(x.split(' ').slice(1).join(' ')) + '</code>')
      : k === 'sch' ? ('schema <code>' + esc(x) + '</code>')
        : k === 'con' ? ('声明 <code>' + esc(x) + '</code>')
          : k === 'dep' ? ('app <code>' + esc(x) + '</code>')
            : k === 'sec' ? ('鉴权方案 <code>' + esc(x) + '</code>') : esc(x);
    stack.push({ k: k, x: x, title: title });
    renderModal();
  }
  wrap.addEventListener('click', function (ev) {
    var act = ev.target.getAttribute && ev.target.getAttribute('data-act');
    if (act === 'close') { stack = []; wrap.classList.remove('on'); return; }
    if (act === 'back') { stack.pop(); renderModal(); return; }
    if (ev.target === wrap) { stack = []; wrap.classList.remove('on'); }
  });
  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape') { stack = []; wrap.classList.remove('on'); }
  });

  // ---------- 统一事件委托
  document.addEventListener('click', function (ev) {
    var a = ev.target.closest('a[data-k]');
    if (a) {
      ev.preventDefault();
      push(a.dataset.k, a.dataset.x);
      return;
    }
    var tr = ev.target.closest('tr.row');
    if (tr && tr.closest('.ver')) { toggleRow(tr); }
  });

  // ---------- 版本切换
  var tabs = Array.prototype.slice.call(document.querySelectorAll('.tab'));
  var verses = Array.prototype.slice.call(document.querySelectorAll('.ver'));
  function activate(id) {
    CUR = id;
    verses.forEach(function (s) { s.classList.toggle('active', s.id === id); });
    tabs.forEach(function (t) { t.setAttribute('aria-selected', String(t.dataset.v === id)); });
    applyFilter();
    window.scrollTo(0, 0);
  }
  tabs.forEach(function (t) { t.addEventListener('click', function () { activate(t.dataset.v); }); });
  if (tabs.length) {
    // 默认落在"已提交原件"这一版：V0 是空章，开局不看空页
    activate(tabs[tabs.length - 1].dataset.v);
  }

  // ---------- 过滤
  var q = document.getElementById('q');
  var cnt = document.getElementById('cnt');
  function applyFilter() {
    var v = (q && q.value || '').trim().toLowerCase();
    var active = document.querySelector('.ver.active');
    if (!active) return;
    var rows = active.querySelectorAll('tr.row');
    var shown = 0, total = rows.length;
    rows.forEach(function (tr) {
      var hit = !v || (tr.dataset.s || '').indexOf(v) >= 0;
      tr.classList.toggle('hide', !hit);
      if (hit) shown++;
      var nx = tr.nextElementSibling;
      if (nx && nx.classList.contains('drow')) {
        if (!hit) { nx.parentNode.removeChild(nx); tr.classList.remove('open'); }
      }
    });
    if (cnt) cnt.textContent = v ? ('筛选 ' + shown + ' / ' + total + ' 条资产') : (total + ' 条资产可点开');
  }
  if (q) q.addEventListener('input', applyFilter);
  applyFilter();

  // ---------- 全部展开 / 收起（只作用于当前版本的分组）
  document.querySelectorAll('[data-act=toggle-all]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var active = document.querySelector('.ver.active');
      if (!active) return;
      var ds = active.querySelectorAll('details');
      var open = btn.dataset.state !== 'open';
      ds.forEach(function (d) { d.open = open; });
      btn.dataset.state = open ? 'open' : 'closed';
      btn.textContent = open ? '收起分组' : '展开分组';
    });
  });
})();
"""


def build_payload(spec: dict, sources: dict, sites: dict, matrices: dict) -> dict:
    """把点开要看的原文一次性嵌进页面：规格全文 + 声明源码 + 依赖语句 + 各版契约矩阵。

    只嵌一次：三个版本的端点/schema 都指向同一份规范，重复嵌会把页面撑到不可用的体积。
    契约矩阵必须按版本分开——命中集合本身就是版本差异所在（V1 命中 0，V2/V3 命中 190）。
    """
    return {
        "spec": {
            "openapi": spec["openapi"],
            "servers": spec["servers"],
            "paths": spec["paths"],
            "schemas": spec["schemas"],
            "securitySchemes": spec["security"],
        },
        "sources": sources,
        "sites": sites,
        "matrices": matrices,
        "meta": {"consumerFile": CONSUMER_FILE},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="D:/wbeval/repos/inventree", help="被测仓克隆（读前端枚举与后端 import）")
    ap.add_argument("--out", default="docs/ssot-atlas.html")
    args = ap.parse_args()

    openapi = ROOT / "0.ssot/openapi.yaml"
    if not openapi.exists():
        sys.exit("缺少 0.ssot/openapi.yaml")
    spec = load_spec(openapi)
    repo = Path(args.repo)

    sources = consumer_sources(repo) if repo.exists() else {}
    depgraph = jload(ROOT / "0.ssot/dependency_graph.json") or {}
    sites_flat = dep_sites(repo, depgraph.get("nodes", [])) if repo.exists() else {}
    sites: dict[str, dict] = defaultdict(dict)
    for k, v in sites_flat.items():
        app = k.split("->")[0]
        sites[app][k] = v

    v0 = {
        "label": "V0 · 空事实源",
        "kind": "失败态",
        "note": (jload(ROOT / "0.ssot/history/G0-export-failed/record.json") or {}).get("summary")
        or "spectacular 因未迁移以退出码 0 写出 0 字节规范，此版本没有任何 SSOT 资产可投影。",
    }

    v1 = load_version(
        "V1 · 第一版投影",
        "重放快照",
        ROOT / "0.ssot/history/G1-naive-axis",
        openapi,
        "后端路径保留 /api 前缀，前端模板相对 /api；产物按此坐标系投影。",
    )
    v2 = load_version(
        "V2 · 现行投影",
        "重放快照",
        ROOT / "0.ssot/history/G2-aligned-axis",
        openapi,
        "后端剥离 /api 前缀后与前端模板同坐标系；本快照与已提交原件逐字段相等（忽略时间戳）。",
    )
    v3 = load_version(
        "V3 · 已提交原件",
        "版本库原件",
        ROOT / "0.ssot",
        openapi,
        "0.ssot/ 在交付仓中的实际内容，绑定 commit d96174cd，是全链路的唯一事实源。",
    )
    for i, v in enumerate((v1, v2, v3), start=1):
        v["id"] = f"V{i}"

    # 自证：端点集里的 path 必须都能在规范里查到，否则"点开"就是空壳
    paths = set(spec["paths"])
    per_version_resolvable = {}
    for v in (v1, v2, v3):
        ok = sum(1 for e in v["endpoints"] if e["path"] in paths)
        per_version_resolvable[v["id"]] = (ok, len(v["endpoints"]))

    matrices = {
        v["id"]: {
            m["enum"]: {
                "template": m["template"],
                "methods": m["methods"],
                "operation_ids": m["operation_ids"],
            }
            for m in v["matrix"].get("matched", [])
        }
        for v in (v1, v2, v3)
    }
    payload = build_payload(spec, sources, sites, matrices)
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    blob = blob.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")

    chapters = [
        f"""<section class="ver" id="V0">
  <div class="vhead"><div><div class="kicker">SSOT 版本资产全景</div>
  <h2>{esc(v0['label'])}</h2><p class="small" style="margin:2px 0 0">{esc(v0['note'])}</p></div>
  <div class="vbatch"><span class="pill bad">失败态</span></div></div>
  <table><tr><th>资产类别</th><th style="width:120px">数量</th></tr>
    <tr><td>API 操作</td><td class="num">0</td></tr><tr><td>路径</td><td class="num">0</td></tr>
    <tr><td>前端声明</td><td class="num">0</td></tr><tr><td>schema</td><td class="num">0</td></tr>
    <tr><td>依赖边</td><td class="num">0</td></tr></table>
  <p class="small">这一版没有资产清单可列：规范写盘 0 字节，投影未开始。保留此章是为了让"版本序列"从原点开始，而不是从第一个能跑通的版本开始。</p>
</section>"""
    ]
    for v in (v1, v2, v3):
        chapters.append(render_version(v, spec, sources, sites))

    tab_items = [("V0", v0["label"])] + [(v["id"], v["label"]) for v in (v1, v2, v3)]
    tabs = "".join(
        f'<button class="tab" role="tab" data-v="{esc(i)}">{esc(lbl)}</button>'
        for i, lbl in tab_items
    )

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SSOT 版本资产全景</title>
<style>{CSS}{ATLAS_CSS}</style>
</head>
<body>
<div class="wrap">

<header class="top">
  <div class="kicker">Smoke Flywheel · SSOT Asset Atlas</div>
  <h1>每个 SSOT 版本的资产全景</h1>
  <p class="lead">一章一个版本。每章独立回答同一组问题：这一版里有哪些资产、各有多少、
  怎么分布、全量清单是什么，以及<strong>每一条资产的实际内容</strong>。</p>
  <p class="small">点开任意一行即可看原文，不是摘要：端点 → OpenAPI 里该操作的完整定义（参数 / 请求体 /
  响应 / 鉴权 / 原始 JSON）；数据模型 → 逐字段定义并可继续下钻；前端声明 → <code>{esc(CONSUMER_FILE)}</code>
  的具体行与上下文；依赖边 → 后端那条真实的 <code>import</code> 语句；鉴权方案 → 完整定义。
  资产之间互相引用处（schema 的 <code>$ref</code>、契约矩阵的 operationId）可直接跳转。</p>
</header>

<div class="bar">
  <div class="tabs" role="tablist">{tabs}</div>
  <input type="search" id="q" placeholder="在当前版本内筛资产：路径 / operationId / 声明名 / app">
  <button class="btn" data-act="toggle-all" data-state="closed">展开分组</button>
  <span class="cnt" id="cnt"></span>
</div>

{''.join(chapters)}

<footer>
  由 <code>tools/build_ssot_atlas.py</code> 从各版本目录的产物与规范原文渲染，数字与清单均在构建时现算 ·
  页面内嵌完整规范副本（{len(blob):,} 字节 JSON），可离线点开每一条资产 ·
  生成于 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} ·
  仓：<a href="https://github.com/aces-org-zhuang/aces-smoke-flywheel">aces-org-zhuang/aces-smoke-flywheel</a>
</footer>
</div>

<script id="atlas-data" type="application/json">{blob}</script>
<script>{ATLAS_JS}</script>
</body>
</html>
"""
    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")

    print(f"KS_ATLAS\t{out}\t{out.stat().st_size:,} bytes")
    print(f"  内嵌数据\t{len(blob):,} bytes（完整规范 paths+schemas+securitySchemes、声明源码、依赖语料）")
    print(f"  声明源码定位\t{len(sources)} 条")
    edge_total = sum(len(v) for v in sites.values())
    print(f"  依赖边定位\t{edge_total} 个 app 有出边语料")
    for v in (v1, v2, v3):
        c = v["counts"]
        ok, tot = per_version_resolvable[v["id"]]
        print(
            f"  {v['label']:16s} ops={c['operations']:4d} paths={c['paths']:4d} "
            f"decl={c['consumers']:4d} hit={c['matched']:4d} miss={c['unmatched']:3d} "
            f"unclaimed={c['unclaimed']:4d} apps={c['apps']} edges={c['edges']} "
            f"可解析端点={ok}/{tot}"
        )
    print(f"  $ref 自证\t{spec['ref_total']:,} 处引用，悬空 {len(spec['ref_dangling'])} 处")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
