#!/usr/bin/env python
"""为每一个 SSOT 版本单独渲染一份"资产全景"。

这里不写"改了什么"：每个版本各自成章，章节内部结构完全一致，独立可读——
一个版本里有哪些资产、各有多少、怎么分布、全量清单是什么。
版本之间只并列陈列，不做差异叙述。

数据全部来自该版本目录下的产物，构建时现算：
  0.ssot/history/<version>/{openapi 引用, endpoints, consumers, contract_matrix,
                            dependency_graph, schema_version}.json
  以及已提交的原件 0.ssot/（作为"现行版本"，与其中一个快照章节并列）

用法：
  python tools/build_ssot_atlas.py --out docs/ssot-atlas.html
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from build_report import CSS, esc  # noqa: E402  与报告页共用排版

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


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


def spec_assets(openapi: Path) -> dict:
    spec = yaml.safe_load(openapi.read_text(encoding="utf-8"))
    comp = spec.get("components") or {}
    schemas = sorted((comp.get("schemas") or {}).keys())
    ops = 0
    tag_counter = Counter()
    for _p, node in (spec.get("paths") or {}).items():
        if not isinstance(node, dict):
            continue
        for m, op in node.items():
            if m.lower() in HTTP_METHODS and isinstance(op, dict):
                ops += 1
                for t in op.get("tags") or ["untagged"]:
                    tag_counter[t] += 1
    return {
        "openapi": spec.get("openapi"),
        "paths": len(spec.get("paths") or {}),
        "operations": ops,
        "schemas": schemas,
        "security_schemes": sorted((comp.get("securitySchemes") or {}).keys()),
        "servers": [s.get("url") for s in (spec.get("servers") or [])],
        "tags": sorted(tag_counter.items(), key=lambda x: (-x[1], x[0])),
        "declared_tags": [t.get("name") for t in (spec.get("tags") or [])],
    }


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


def render_version(v: dict, spec: dict) -> str:
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

    # 端点清单按 tag 分组
    groups: dict[str, list[dict]] = defaultdict(list)
    for e in items:
        groups[(e["tags"] or ["untagged"])[0]].append(e)

    tag_rows = "".join(
        f"<tr><td><code>{esc(t)}</code></td><td>{c}</td><td>{bar(c, by_tag.most_common(1)[0][1])}</td></tr>"
        for t, c in sorted(by_tag.items(), key=lambda x: (-x[1], x[0]))
    )
    method_rows = "".join(
        f"<tr><td><code>{esc(m)}</code></td><td>{c}</td><td>{bar(c, by_method.most_common(1)[0][1])}</td></tr>"
        for m, c in sorted(by_method.items(), key=lambda x: (-x[1], x[0]))
    )
    domain_rows = "".join(
        f"<tr><td><code>{esc(d)}</code></td><td>{c}</td><td>{bar(c, by_domain.most_common(1)[0][1])}</td></tr>"
        for d, c in sorted(by_domain.items(), key=lambda x: (-x[1], x[0]))
    )

    group_blocks = []
    for tag, rows in sorted(groups.items(), key=lambda x: (-len(x[1]), x[0])):
        body = "".join(
            f"<tr><td><code>{esc(e['method'])}</code></td><td><code>{esc(e['path'])}</code></td>"
            f"<td>{esc(e.get('operation_id') or '-')}</td>"
            f"<td>{'✓' if e['secured'] else '—'}</td><td>{'是' if e['deprecated'] else ''}</td></tr>"
            for e in sorted(rows, key=lambda x: (x["path"], x["method"]))
        )
        group_blocks.append(
            f"""<details><summary><strong>{esc(tag)}</strong> · {len(rows)} 个操作</summary>
      <table><tr><th>方法</th><th>路径</th><th>operationId</th><th>鉴权</th><th>废弃</th></tr>{body}</table>
      </details>"""
        )

    consumer_rows = "".join(
        f"<tr><td><code>{esc(c['enum'])}</code></td><td><code>{esc(c['template'])}</code></td>"
        f"<td><code>{esc(c['normalized'])}</code></td>"
        f"<td>{'命中' if c['enum'] in v['matched_enums'] else '未命中'}</td></tr>"
        for c in v["consumers"]
    )

    matched_rows = "".join(
        f"<tr><td><code>{esc(m['enum'])}</code></td><td><code>{esc(m['template'])}</code></td>"
        f"<td>{esc(','.join(m['methods']))}</td><td>{len(m['operation_ids'])}</td></tr>"
        for m in v["matrix"].get("matched", [])
    )
    unmatched_rows = "".join(
        f"<tr><td><code>{esc(u['enum'])}</code></td><td><code>{esc(u['template'])}</code></td>"
        f"<td><code>{esc(u['normalized'])}</code></td></tr>"
        for u in v["matrix"].get("unmatched_consumer", [])
    )
    unclaimed_rows = "".join(
        f"<tr><td><code>{esc(e['method'])}</code></td><td><code>{esc(e['path'])}</code></td>"
        f"<td>{esc(','.join(e['tags']) or '-')}</td></tr>"
        for e in sorted(v["unclaimed"], key=lambda x: (x["path"], x["method"]))
    )

    nodes = v["depgraph"].get("nodes", [])
    out_edges: dict[str, list[str]] = {n: [] for n in nodes}
    for e in v["depgraph"].get("edges", []):
        out_edges.setdefault(e["from"], []).append(e["to"])
    dep_rows = "".join(
        f"<tr><td><code>{esc(n)}</code></td><td>{len(out_edges.get(n, []))}</td>"
        f"<td>{esc(', '.join(sorted(out_edges.get(n, []))) or '—')}</td></tr>"
        for n in nodes
    )

    file_rows = "".join(
        f"<tr><td><code>{esc(k)}</code></td><td>{val['bytes']:,} B</td>"
        f"<td><code>{esc(val['sha256'])}…</code></td></tr>"
        for k, val in v["files"].items()
    )
    schema_sample = spec["schemas"]
    schema_text = "、".join(esc(s) for s in schema_sample)

    return f"""
<section class="ver" id="{esc(v['label'].replace(' ', '-'))}">
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

  <h3>A. 版本指纹</h3>
  <table>
    <tr><th style="width:22%">项</th><th>值</th></tr>
    <tr><td>目录</td><td><code>{esc(v['dir'])}</code></td></tr>
    <tr><td>绑定 commit</td><td><code>{esc(str(v['commit']))}</code></td></tr>
    <tr><td>openapi 指纹</td><td><code>{esc(str(v['openapi_sha256']))[:24]}…</code> · {v['openapi_bytes']:,} 字节</td></tr>
    <tr><td>生成时间</td><td>{esc(str(v['generated_at']))}</td></tr>
  </table>
  <table><tr><th>产物</th><th>字节</th><th>sha256 前 16 位</th></tr>{file_rows}</table>

  <h3>B. 规格资产（OpenAPI {esc(str(spec['openapi']))}）</h3>
  <div class="grid g4">
    <div class="card kpi"><div class="n">{spec['paths']}</div><div class="l">paths 节点</div></div>
    <div class="card kpi"><div class="n">{spec['operations']}</div><div class="l">操作</div></div>
    <div class="card kpi"><div class="n">{len(spec['schemas'])}</div><div class="l">schema</div></div>
    <div class="card kpi"><div class="n">{len(spec['security_schemes'])}</div><div class="l">鉴权方案</div></div>
  </div>
  <p class="small">鉴权方案：{esc('、'.join(spec['security_schemes']))} ·
  服务地址：{esc('、'.join(str(x) for x in spec['servers']))} ·
  操作标签 {len(spec['tags'])} 个（spec 顶层未声明 tag 列表，标签由各操作自带）</p>
  <details><summary><strong>数据模型清单</strong> · {len(spec['schemas'])} 个 schema 名称</summary>
    <p class="small" style="line-height:2">{schema_text}</p>
  </details>

  <h3>C. 端点资产分布</h3>
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
  <table><tr><th>tag</th><th>操作数</th><th></th></tr>{tag_rows}</table>

  <h3>D. 端点全量清单（{v['counts']['operations']} 条，按标签分组）</h3>
  {''.join(group_blocks)}

  <h3>E. 前端消费方声明全量清单（{v['counts']['consumers']} 条）</h3>
  <details open><summary>展开 / 收起</summary>
    <table><tr><th>枚举名</th><th>模板</th><th>归一化</th><th>契约状态</th></tr>{consumer_rows}</table>
  </details>

  <h3>F. 契约矩阵（这一版的三段资产）</h3>
  <div class="grid g3">
    <div class="card kpi"><div class="n">{v['counts']['matched']}</div><div class="l">命中（声明 ↔ 操作）</div></div>
    <div class="card kpi"><div class="n">{v['counts']['unmatched']}</div><div class="l">前端声明未命中</div></div>
    <div class="card kpi"><div class="n">{v['counts']['unclaimed']}</div><div class="l">后端操作未被声明引用</div></div>
  </div>
  <details><summary><strong>命中清单</strong> · {v['counts']['matched']} 条</summary>
    <table><tr><th>声明</th><th>模板</th><th>可用的方法</th><th>operation 数</th></tr>{matched_rows}</table>
  </details>
  <details><summary><strong>未命中清单</strong> · {v['counts']['unmatched']} 条</summary>
    <table><tr><th>声明</th><th>模板</th><th>归一化</th></tr>{unmatched_rows}</table>
  </details>
  <details><summary><strong>未被引用的后端操作</strong> · {v['counts']['unclaimed']} 条</summary>
    <table><tr><th>方法</th><th>路径</th><th>标签</th></tr>{unclaimed_rows}</table>
  </details>

  <h3>G. 依赖图资产（{v['counts']['apps']} 个 app / {v['counts']['edges']} 条 import 边）</h3>
  <table><tr><th>app</th><th>出边数</th><th>依赖的 app</th></tr>{dep_rows}</table>
</section>
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="docs/ssot-atlas.html")
    args = ap.parse_args()

    openapi = ROOT / "0.ssot/openapi.yaml"
    if not openapi.exists():
        sys.exit("缺少 0.ssot/openapi.yaml")
    spec = spec_assets(openapi)

    versions = []
    g0_record = jload(ROOT / "0.ssot/history/G0-export-failed/record.json") or {}
    v0 = {
        "label": "V0 · 空事实源",
        "kind": "失败态",
        "note": g0_record.get("summary")
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
    committed = load_version(
        "V3 · 已提交原件",
        "版本库原件",
        ROOT / "0.ssot",
        openapi,
        "0.ssot/ 在交付仓中的实际内容，绑定 commit d96174cd，是全链路的唯一事实源。",
    )

    chapters = []
    if v0:
        chapters.append(
            f"""<section class="ver">
  <div class="vhead"><div><div class="kicker">SSOT 版本资产全景</div>
  <h2>{esc(v0['label'])}</h2><p class="small" style="margin:2px 0 0">{esc(v0['note'])}</p></div>
  <div class="vbatch"><span class="pill bad">失败态</span></div></div>
  <table><tr><th>资产类别</th><th>数量</th></tr>
    <tr><td>API 操作</td><td>0</td></tr><tr><td>路径</td><td>0</td></tr>
    <tr><td>前端声明</td><td>0</td></tr><tr><td>schema</td><td>0</td></tr>
    <tr><td>依赖边</td><td>0</td></tr></table>
  <p class="small">这一版没有资产清单可列：规范写盘 0 字节，投影未开始。保留此章是为了让"版本序列"从原点开始，而不是从第一个能跑通的版本开始。</p>
</section>"""
        )
    for v in (v1, v2, committed):
        chapters.append(render_version(v, spec))

    nav = "".join(
        f'<a class="navitem" href="#{esc(v["label"].replace(" ", "-"))}">{esc(v["label"])}</a>'
        for v in ([v0] if v0 else []) + [v1, v2, committed]
    )

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SSOT 版本资产全景</title>
<style>{CSS}
  .vhead {{ display:flex; justify-content:space-between; align-items:flex-start; gap:20px;
           border-top:3px solid var(--ink); padding-top:16px; margin-top:0; }}
  .ver {{ margin:64px 0 0; }}
  .ver h2 {{ margin:6px 0 4px; border-top:none; padding-top:0; }}
  .nav {{ position:sticky; top:0; z-index:5; background:rgba(247,248,250,.94);
          backdrop-filter:blur(6px); border-bottom:1px solid var(--line);
          padding:10px 0; margin:18px 0 0; display:flex; gap:8px; flex-wrap:wrap; }}
  .navitem {{ font-size:12.5px; padding:4px 10px; border:1px solid var(--line);
              border-radius:999px; background:#fff; border-bottom:1px solid var(--line); }}
  details {{ margin:10px 0; }}
  details > summary {{ cursor:pointer; font-size:13.5px; color:var(--brand); padding:6px 0; }}
  details[open] > summary {{ margin-bottom:6px; }}
  .kpi .n {{ font-variant-numeric:tabular-nums; }}
</style>
</head>
<body>
<div class="wrap">

<header class="top">
  <div class="kicker">Smoke Flywheel · SSOT Asset Atlas</div>
  <h1>每个 SSOT 版本的资产全景</h1>
  <p class="lead">一章一个版本。每章独立回答同一组问题：这一版里有哪些资产、各有多少、
  怎么分布、全量清单是什么。章节之间只并列，不做差异叙述。</p>
  <p class="small">资产口径：<code>0.ssot/</code> 六个产物（规范 / 端点 / 消费方声明 / 契约矩阵 /
  依赖图 / 版本元数据）。端点数按操作计（同一路径的不同方法各算一条），路径数按去重后的 path 计。</p>
</header>

<nav class="nav">{nav}</nav>

{''.join(chapters)}

<footer>
  由 <code>tools/build_ssot_atlas.py</code> 从各版本目录的产物渲染，数字与清单均在构建时现算 ·
  生成于 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} ·
  仓：<a href="https://github.com/aces-org-zhuang/aces-smoke-flywheel">aces-org-zhuang/aces-smoke-flywheel</a>
</footer>
</div>
</body>
</html>
"""
    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"KS_ATLAS\t{out}\t{out.stat().st_size:,} bytes")
    for v in [v1, v2, committed]:
        c = v["counts"]
        print(
            f"  {v['label']:16s} ops={c['operations']:4d} paths={c['paths']:4d} "
            f"decl={c['consumers']:4d} hit={c['matched']:4d} miss={c['unmatched']:3d} "
            f"unclaimed={c['unclaimed']:4d} apps={c['apps']} edges={c['edges']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
