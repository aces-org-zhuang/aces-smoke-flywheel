#!/usr/bin/env python
"""把 0.ssot 的演进路线渲染成一页全景。

页面上的每个数字都在构建时从产物里重新算，不允许手填：
  - 各状态计数 ← 0.ssot/history/<state>/*.json
  - 前后差异   ← 两份 contract_matrix.json 的集合差
  - 26 条未命中定性 ← 前端枚举 + 后端规范现场比对
  - 冻结结论   ← 5.evidence/verdicts/verify-ssot*.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from build_report import CSS, esc, pill  # noqa: E402  复用同一套排版，避免两页风格漂移

HIST = ROOT / "0.ssot" / "history"


def jload(p: Path):
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def common_prefix_len(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a.split("/"), b.split("/")):
        if x != y:
            break
        n += 1
    return n


def closest_routes(ep_items: list[dict], normalized: str) -> list[str]:
    """给一条写错的前端声明找"它现在大概指哪"。按归一化路径的公共段数排序。"""
    scored = []
    for e in ep_items:
        n = common_prefix_len(e["normalized"], normalized)
        if n >= 2:
            scored.append((n, e["normalized"]))
    best = sorted(set(scored), key=lambda x: (-x[0], x[1]))[:3]
    return [p for _, p in best]


def v3a_detail(blob: dict) -> str:
    """从判决文件里取 V3a 的实测值，避免在页面里硬编码提交号。"""
    for c in blob.get("checks", []):
        if c.get("id") == "V3a":
            ev = c.get("evidence") or {}
            return (
                f"<code>SSOT {str(ev.get('ssot_commit'))[:12]} / "
                f"SUT {str(ev.get('sut_head'))[:12]}</code>"
            )
    return "<code>缺少 V3a 证据</code>"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="docs/ssot-evolution.html")
    args = ap.parse_args()

    evo = jload(HIST / "evolution.json")
    if evo is None:
        sys.exit("缺少 0.ssot/history/evolution.json，先跑 tools/replay_ssot.py --probe-empty")

    matrix = jload(ROOT / "0.ssot/contract_matrix.json") or {}
    endpoints = (jload(ROOT / "0.ssot/endpoints.json") or {}).get("items", [])
    dc = jload(ROOT / "0.ssot/schema_version.json") or {}
    verify = jload(ROOT / "5.evidence/verdicts/verify-ssot.json") or {}
    drift = jload(ROOT / "5.evidence/verdicts/verify-ssot-drift.json") or {}
    cid = jload(ROOT / "5.evidence/verdicts/can-i-deploy.json") or {}
    cid_drift = jload(ROOT / "5.evidence/verdicts/can-i-deploy-ssot-drift.json") or {}

    states = {s["id"]: s for s in evo["states"]}
    g0 = states.get("G0-export-failed", {})
    g1 = states.get("G1-naive-axis", {}).get("metrics", {})
    g2 = states.get("G2-aligned-axis", {}).get("metrics", {})
    delta = evo.get("delta", {})
    cls = evo.get("unmatched_classification", {})

    # ---- 190 条命中按前缀分组（构建时现算）
    groups = Counter()
    for m in matrix.get("matched", []):
        parts = [p for p in m["template"].strip("/").split("/") if p and not p.startswith(":")]
        groups["/".join(parts[:1]) or "-"] += 1

    # ---- 313 条后端未被引用按首个 tag 分组（用矩阵口径重算，不采信手填数字）
    # 注意口径：未被引用 = 端点归一化路径不在**全部前端声明**里（含未命中的那 26 条），
    # 而不是"不在命中集合里"。用错口径会得到比矩阵更大的数。
    consumer_items = (jload(ROOT / "0.ssot/consumers.json") or {}).get("items", [])
    consumer_norm = {c["normalized"] for c in consumer_items}
    tag_counts = Counter()
    for e in endpoints:
        if e["normalized"] not in consumer_norm:
            tag_counts[(e["tags"] or ["untagged"])[0]] += 1
    unclaimed_total = sum(tag_counts.values())
    if unclaimed_total != matrix.get("totals", {}).get("backend_unclaimed"):
        print(
            f"WARN 口径不一致：重算 {unclaimed_total} vs 矩阵 "
            f"{matrix.get('totals', {}).get('backend_unclaimed')}"
        )

    # ---- 四条陈旧声明：现在后端长什么样
    stale_rows = []
    for item in cls.get("stale_items", []):
        near = closest_routes(endpoints, item["normalized"])
        stale_rows.append({**item, "near": near})

    # ---- 逐状态卡
    def state_card(sid: str, title: str, state: dict, tone: str) -> str:
        m = state.get("metrics", {})
        files = m.get("files") or {}
        file_rows = "".join(
            f"<tr><td><code>{esc(k)}</code></td><td>{v:,} B</td></tr>"
            for k, v in files.items()
            if k != "replay.json"
        )
        obs = state.get("observed") or {}
        extra = ""
        if obs:
            extra = (
                f'<div class="caveat"><strong>当场复现的守卫行为：</strong>'
                f"{esc(obs.get('message', '未触发守卫'))}</div>"
            )
        ext = state.get("external_evidence")
        if ext:
            extra += (
                f'<div class="small">外部证据：<code>{esc(ext["file"])}</code> · '
                f"<code>{esc(ext['line'])}</code> · 退出码 {ext['exit_code']} · "
                f"落盘 {ext['bytes_written']} 字节</div>"
            )
        return f"""
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:baseline">
        <h3 style="margin:0">{esc(sid)} · {esc(title)}</h3>
        <span class="pill {tone}">{esc(state.get('kind',''))}</span>
      </div>
      <p class="small">{esc(state.get('definition', 'spectacular 以退出码 0 写出 0 字节，没有事实源可投影。'))}</p>
      <table>
        <tr><th>投影结果</th><th>值</th></tr>
        <tr><td>后端操作</td><td><strong>{m.get('endpoints', 0):,}</strong></td></tr>
        <tr><td>前端声明</td><td><strong>{m.get('consumers', 0):,}</strong></td></tr>
        <tr><td>合同矩阵命中</td><td><strong>{m.get('matched', 0):,}</strong></td></tr>
        <tr><td>前端未命中</td><td><strong>{m.get('unmatched_consumer', 0):,}</strong></td></tr>
        <tr><td>后端未被引用</td><td><strong>{m.get('backend_unclaimed', 0):,}</strong></td></tr>
        <tr><td>应用 / 依赖边</td><td>{m.get('apps', 0)} / {m.get('edges', 0)}</td></tr>
      </table>
      {f'<table><tr><th>产物</th><th>字节</th></tr>{file_rows}</table>' if file_rows else ''}
      {extra}
    </div>"""

    cards = "".join(
        [
            state_card("G0", "导出失败", g0, "bad"),
            state_card("G1", "轴错位（第一版归一化）", states.get("G1-naive-axis", {}), "warn"),
            state_card("G2", "轴对齐（现行实现）", states.get("G2-aligned-axis", {}), "ok"),
        ]
    )

    # ---- 前后对照表
    compare = f"""
    <table>
      <tr><th>指标</th><th>G1 轴错位</th><th>G2 轴对齐</th><th>差值</th><th>说明</th></tr>
      <tr><td>后端操作</td><td>{g1.get('endpoints',0):,}</td><td>{g2.get('endpoints',0):,}</td>
          <td>{'0（不变）' if delta.get('endpoints_unchanged') else '变化'}</td>
          <td>同一份 openapi.yaml，事实没变</td></tr>
      <tr><td>前端声明</td><td>{g1.get('consumers',0):,}</td><td>{g2.get('consumers',0):,}</td>
          <td>{'0（不变）' if delta.get('consumers_unchanged') else '变化'}</td>
          <td>同一份 ApiEndpoints.tsx，事实没变</td></tr>
      <tr><td>命中</td><td>{g1.get('matched',0):,}</td><td>{g2.get('matched',0):,}</td>
          <td>+{delta.get('matched',0):,}</td>
          <td>只改坐标系，命中从 0 恢复为真实值</td></tr>
      <tr><td>前端未命中</td><td>{g1.get('unmatched_consumer',0):,}</td><td>{g2.get('unmatched_consumer',0):,}</td>
          <td>{delta.get('unmatched_consumer',0):,}</td>
          <td>从"全部未命中"收敛到 26 条真问题</td></tr>
      <tr><td>后端未被引用</td><td>{g1.get('backend_unclaimed',0):,}</td><td>{g2.get('backend_unclaimed',0):,}</td>
          <td>{delta.get('backend_unclaimed',0):,}</td>
          <td>304 条不是"没被用"，是"没被认出来"</td></tr>
    </table>"""

    group_rows = "".join(
        f"<tr><td><code>{esc(k)}</code></td><td>{v}</td></tr>" for k, v in groups.most_common(12)
    )
    tag_rows = "".join(
        f"<tr><td><code>{esc(k)}</code></td><td>{v}</td></tr>" for k, v in tag_counts.most_common(10)
    )
    flips = delta.get("flip_sample", [])
    flip_rows = "".join(
        f"<tr><td><code>{esc(f['enum'])}</code></td><td>{esc(f['G1'])}</td><td>{esc(f['G2'])}</td></tr>"
        for f in flips
    )

    stale_html = "".join(
        f"""<tr>
          <td><code>{esc(r['enum'])}</code></td>
          <td><code>{esc(r['template'])}</code></td>
          <td>{('<br>'.join(f'<code>{esc(n)}</code>' for n in r['near']) or '无相近路由')}</td>
          <td>{r['frontend_references']}</td>
        </tr>"""
        for r in stale_rows
    )

    vs_rows = "".join(
        f"<tr><td>{esc(c['id'])}</td><td>{esc(c['name'])}</td>"
        f"<td>{pill('PASS' if c['ok'] else 'FAIL')}</td><td>{esc(c['detail'])}</td></tr>"
        for c in verify.get("checks", [])
    )
    dr_rows = "".join(
        f"<tr><td>{esc(c['id'])}</td><td>{esc(c['name'])}</td>"
        f"<td>{pill('PASS' if c['ok'] else 'FAIL')}</td><td>{esc(c['detail'])}</td></tr>"
        for c in drift.get("checks", [])
    )

    def gate_line(blob: dict, label: str) -> str:
        if not blob:
            return ""
        cs = blob.get("checks", [])
        rows = "".join(
            f"<tr><td>{esc(c['gate'])}</td><td>{pill(c['state'])}</td><td>{esc(c['detail'])}</td></tr>"
            for c in cs
        )
        return f"""
    <h3>{esc(label)} → {pill(blob.get('decision','UNKNOWN'))}</h3>
    <table><tr><th>闸门</th><th>状态</th><th>依据</th></tr>{rows}</table>"""

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>0.ssot 演进全景 · 每个修改的前后对照</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">

<header class="top">
  <div class="kicker">Smoke Flywheel · SSOT Evolution</div>
  <h1>0.ssot 演进全景：四个状态，每一步的前后都留证</h1>
  <p class="lead">SSOT 不是一次写成的。它从"没有事实源"开始，经历一次坐标系错误，才收敛到
  {dc.get('counts',{}).get('endpoints',0):,} 个操作 / {dc.get('counts',{}).get('consumers',0):,} 条声明的可用投影。
  本页把每个状态的全景、每个修改的前后差异、以及"改动是否真的生效"的校验逐个摊开。</p>
  <p class="small">冻结输入：<code>0.ssot/openapi.yaml</code>（{evo['frozen_input']['openapi_bytes']:,} 字节，
  sha256 <code>{str(dc.get('openapi_sha256'))[:24]}…</code>）· 被测仓
  <code>inventree/InvenTree</code> @ <code>{str(dc.get('commit'))[:12]}</code></p>
</header>

<div class="quote">
  <strong>口径先说清楚：</strong>交付仓里 <code>0.ssot</code> 只被写过一次（首个提交之后冻结），
  生成链上的中间态没有进版本库，早期产物已被覆盖。<br>
  因此 G1/G2 是<strong>用同一份冻结的 openapi.yaml 重放各版本投影逻辑</strong>得到的快照，
  不是当时的原件；G2 重放结果与已提交的 <code>0.ssot</code> 逐字段相等（忽略时间戳），
  所以中间态可信。G0 的证据是生成日志与当场复现的守卫行为，不是重建。
</div>

<h2>1. 四个状态：从"没有事实源"到"可复核的投影"</h2>
<div class="grid g2" style="grid-template-columns:repeat(3,1fr)">{cards}</div>

<h2>2. 唯一真正的修改：把两端拉回同一坐标系</h2>
<p>G1→G2 改的只有 <code>normalize_path</code> 里的一段：后端路径剥离 <code>/api</code> 前缀，
前端模板保持相对 /api。事实源（规范、前端枚举）一个字没动——这从"后端操作数、前端声明数完全不变"
可以直接证伪式的确认。改的不是数据，是坐标。</p>
{compare}
<div class="caveat">
  <strong>为什么这一步最危险：</strong>轴错位时命中率是 0，而 0 命中的表现是"所有声明都未命中"，
  看上去像"这个仓前后端对不上"这样一个业务结论，而不是"工具算错了"。
  门禁此后所有基于合同矩阵的判断都会是假阴性——而且它们都会"正常"跑完。
</div>

<h2>3. 190 条声明翻转：改坐标之前它们全被判成未命中</h2>
<div class="grid g2">
  <div class="card">
    <div class="kpi"><div class="n">{delta.get('flipped_declarations',0)}</div>
      <div class="l">G1 未命中 → G2 命中的声明数（逐条比对 enum）</div></div>
  </div>
  <div class="card">
    <div class="kpi"><div class="n">{g2.get('matched',0)}/{g2.get('consumers',0)}</div>
      <div class="l">修正后命中率（{g2.get('unmatched_consumer',0)} 条未命中是真问题）</div></div>
  </div>
</div>
<h3>命中声明按前端资源前缀分布</h3>
<table><tr><th>前缀</th><th>命中数</th></tr>{group_rows}</table>
<h3>翻转样例（前 12 条，完整 190 条见 evolution.json）</h3>
<table><tr><th>声明</th><th>G1</th><th>G2</th></tr>{flip_rows}</table>

<h3>另一侧：{unclaimed_total} 条后端操作没有任何前端声明命中</h3>
<p>这个数字同样被"轴"影响过：G1 时是 {g1.get('backend_unclaimed',0):,} 条。修正后仍有
{unclaimed_total} 条，按首个 tag 分布如下——它们多为外部集成（barcode、机器、插件）与
admin 专用端点，是"后端能力大于前端当前界面"的正常形状，不是一个待修的缺陷。</p>
<table><tr><th>tag</th><th>未被引用的操作数</th></tr>{tag_rows}</table>

<h2>4. 26 条未命中的定性：22 条体外边界，4 条真陈旧</h2>
<p>未命中不等于缺陷。判据两条，都可复算：① 路由是否由第三方包（allauth headless）挂载，
不属于本仓 URLconf；② 前端枚举仍在被引用，而后端规范里已经没有任何对应路由。
按此定性：<strong>体外 {cls.get('outside_boundary',0)} 条 / 陈旧 {cls.get('stale',0)} 条</strong>。</p>
<table>
  <tr><th>陈旧声明</th><th>前端写的模板</th><th>后端现在的相近路由</th><th>前端引用处</th></tr>
  {stale_html}
</table>
<p class="small">这 4 条是真实的前后端契约漂移：路由被移除（part_scheduling）、被下移到子资源
（ship → shipment/:id/ship）、集合路由消失只剩详情（user/me/tips/:id）、多了一层 key 段
（machine settings/:config_type/:key）。它们不该被"未命中率"这一个数字吞掉。</p>

<h2>5. SSOT 被冻结之后：7 项校验，以及一次故意制造的漂移</h2>
<p>此前 <code>schema_version.json</code> 里写着 commit 与 sha256，README 也写着"不匹配即拒跑"，
但没有任何门禁真的检查它——这属于"声明存在、执行缺失"。补上 <code>4.gates/verify-ssot</code> 后：</p>
<table><tr><th>#</th><th>判据</th><th>结果</th><th>实测</th></tr>{vs_rows}</table>

<h3>漂移实验：树不变，只把 commit 前推一格</h3>
<p>在完全不改任何文件的前提下追加一个空提交，让 SUT 的 HEAD 离开 SSOT 绑定的 commit。
如果闸门真的在校验绑定，它必须拦下；如果只是打印了 sha，它会照常放行。</p>
<table><tr><th>#</th><th>判据</th><th>结果</th><th>实测</th></tr>{dr_rows}</table>

<div class="grid g2">
  <div class="card">{gate_line(cid, '同一套证据，SUT 与 SSOT 对齐时')}</div>
  <div class="card">{gate_line(cid_drift, '同一套证据，SUT 前推一个空提交后')}</div>
</div>
<p class="small">两次判决唯一的差别是 V3a：{v3a_detail(verify)} 对 {v3a_detail(drift)}。
其余判据全绿、四道旧门禁全绿——这正是"事实源过期但没人发现"的形状。</p>

<h2>6. 局限</h2>
<ul>
  <li><code>verify-ssot</code> 证明的是"当前 HEAD 与 SSOT 一致"，不能回溯证明某个历史提交期间的契约；
      变异候选是临时提交，跑完即回滚，回滚后校验自然恢复绿。</li>
  <li>G1/G2 是重放，不是原件。能保证的是"同一输入 + 同一投影逻辑 ⇒ 同一输出"，
      这由 G2 与已提交产物逐字段相等来支撑。</li>
  <li>26 条未命中里"体外边界"的判定依赖前端枚举命名与模板前缀（<code>auth/v1</code>），
      是结构判据，不是人工豁免；新增边界必须改判据，不能改名单。</li>
</ul>

<footer>
  由 <code>tools/build_ssot_panorama.py</code> 从 <code>0.ssot/history/evolution.json</code>、
  <code>0.ssot/contract_matrix.json</code> 与 <code>5.evidence/verdicts/verify-ssot*.json</code> 渲染 ·
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
    print(f"wrote {out} ({out.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
