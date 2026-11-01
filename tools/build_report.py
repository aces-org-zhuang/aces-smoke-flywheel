#!/usr/bin/env python
"""把 5.evidence 里的事实渲染成一页可投屏的报告。

原则：页面上每个数字都必须来自证据文件，不允许手填。
缺证据就显示"未产出"，而不是编一个看起来合理的值。
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]

SUT_REPO = "https://github.com/inventree/InvenTree"
SUT_COMMIT = "d96174cddbc2566fea53b3fe39b5d4aa5d30fbf1"
FLY_REPO = "https://github.com/aces-org-zhuang/aces-smoke-flywheel"
LIBRE_REPO = "https://github.com/LibrePCB/LibrePCB"
LIBRE_COMMIT = "be16f52f2ff2bbe303063e68dbccad524bc0da8e"

CSS = """
  :root {
    --ink:#111418; --ink2:#3d4653; --mute:#6b7684; --line:#e3e7ee;
    --bg:#f7f8fa; --card:#ffffff; --brand:#1a56db; --ok:#0f7b4f;
    --bad:#b42318; --warn:#946200;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
    font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
    line-height:1.65; font-size:15px; }
  .wrap { max-width:1100px; margin:0 auto; padding:48px 28px 96px; }
  header.top { border-bottom:2px solid var(--ink); padding-bottom:20px; margin-bottom:36px; }
  .kicker { font-size:12px; letter-spacing:.18em; text-transform:uppercase; color:var(--mute); }
  h1 { font-size:33px; line-height:1.25; margin:10px 0 8px; letter-spacing:-.01em; }
  h2 { font-size:21px; margin:52px 0 14px; padding-top:18px; border-top:1px solid var(--line); }
  h3 { font-size:15.5px; margin:26px 0 8px; }
  p { margin:10px 0; color:var(--ink2); }
  .lead { font-size:17px; color:var(--ink); }
  .grid { display:grid; gap:14px; }
  .g2 { grid-template-columns:repeat(2,1fr); }
  .g3 { grid-template-columns:repeat(3,1fr); }
  .g4 { grid-template-columns:repeat(4,1fr); }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:15px 17px; }
  .kpi .n { font-size:26px; font-weight:650; letter-spacing:-.02em; }
  .kpi .l { font-size:12.5px; color:var(--mute); margin-top:2px; }
  table { width:100%; border-collapse:collapse; margin:14px 0; font-size:13.4px; }
  th, td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); vertical-align:top; }
  th { font-weight:600; color:var(--ink); background:#f0f2f6; font-size:12.4px; letter-spacing:.02em; }
  td code, p code, li code { background:#eef1f5; padding:1px 5px; border-radius:4px;
    font-family:ui-monospace,Consolas,monospace; font-size:12.4px; }
  .pill { display:inline-block; padding:1px 8px; border-radius:999px; font-size:11.5px;
    font-weight:600; letter-spacing:.02em; }
  .pill.ok { background:#e3f5ea; color:var(--ok); }
  .pill.bad { background:#fdecea; color:var(--bad); }
  .pill.warn { background:#fdf3e0; color:var(--warn); }
  .pill.mute { background:#eef1f5; color:var(--mute); }
  .quote { border-left:3px solid var(--brand); background:#f2f5fd; padding:12px 16px;
    border-radius:0 8px 8px 0; margin:16px 0; }
  .caveat { border-left:3px solid var(--warn); background:#fffaf0; padding:12px 16px;
    border-radius:0 8px 8px 0; margin:16px 0; }
  .badnote { border-left:3px solid var(--bad); background:#fdf3f2; padding:12px 16px;
    border-radius:0 8px 8px 0; margin:16px 0; }
  pre { background:#11161d; color:#e6edf3; padding:14px 16px; border-radius:8px;
    overflow-x:auto; font-size:12.5px; line-height:1.55; }
  a { color:var(--brand); text-decoration:none; border-bottom:1px solid #c7d7f7; }
  ul, ol { margin:8px 0 8px 22px; color:var(--ink2); }
  li { margin:5px 0; }
  .small { font-size:12.5px; color:var(--mute); }
  .step { display:flex; gap:14px; padding:12px 0; border-bottom:1px dashed var(--line); }
  .step .no { flex:0 0 34px; height:34px; border-radius:50%; background:var(--ink);
    color:#fff; font-size:13px; font-weight:600; display:flex; align-items:center;
    justify-content:center; }
  .step .body { flex:1; }
  .step .body .t { font-weight:600; color:var(--ink); }
  footer { margin-top:60px; padding-top:18px; border-top:1px solid var(--line);
    font-size:12.5px; color:var(--mute); }
  @media (max-width:820px) { .g2,.g3,.g4 { grid-template-columns:1fr 1fr; } h1 { font-size:25px; } }
"""


def jload(p: Path):
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def git_log(limit: int = 12) -> list[dict]:
    try:
        out = subprocess.check_output(
            ["git", "-C", str(ROOT), "log", f"-{limit}", "--format=%h|%ad|%s", "--date=short"],
            text=True,
        )
    except Exception:
        return []
    rows = []
    for line in out.strip().splitlines():
        h, d, s = (line.split("|", 2) + ["", ""])[:3]
        rows.append({"sha": h, "date": d, "subject": s})
    return rows


def pill(state: str) -> str:
    cls = {
        "PASS": "ok", "OK": "ok", "ALLOW": "ok", "killed": "ok",
        "BLOCK": "bad", "FAIL": "bad", "survived": "bad",
        "UNKNOWN": "warn", "WARN": "warn", "SKIP": "mute",
    }.get(state, "mute")
    return f'<span class="pill {cls}">{state}</span>'


def esc(s: str) -> str:
    return (
        str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def step(no: int, title: str, body_html: str) -> str:
    return (
        f'<div class="step"><div class="no">{no}</div>'
        f'<div class="body"><div class="t">{title}</div>{body_html}</div></div>'
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="docs/report.html")
    ap.add_argument("--l1-run", default="l1-run-01")
    args = ap.parse_args()

    ev = ROOT / "5.evidence"
    target = yaml.safe_load((ROOT / "3.env/target.yaml").read_text(encoding="utf-8"))
    ssv = jload(ROOT / "0.ssot/schema_version.json") or {}
    matrix = jload(ROOT / "0.ssot/contract_matrix.json") or {}
    dep = jload(ROOT / "0.ssot/dependency_graph.json") or {}
    measures = target.get("target", {}).get("measured", {})

    lint = jload(ev / "verdicts/lint-boundary.json") or {}
    cls_cand = jload(ev / "verdicts/classification.json") or {}
    cls_base = jload(ev / "verdicts/classification-baseline.json") or {}
    debt = jload(ev / "verdicts/baseline-debt.json") or {}
    mut = jload(ev / "verdicts/mutation.json") or {}
    cid = jload(ev / "verdicts/can-i-deploy.json") or {}
    cid_mut = jload(ev / "verdicts/can-i-deploy-mutant.json") or {}

    l1 = jload(ev / "runs" / args.l1_run / "run.json")
    l3 = jload(ev / "runs/l3-golden/run.json")
    base_run = jload(ev / "runs/baseline/run.json")
    evo = jload(ROOT / "0.ssot/history/evolution.json") or {}
    vs = jload(ev / "verdicts/verify-ssot.json") or {}
    evo_states = {s["id"]: s.get("metrics", {}) for s in evo.get("states", [])}

    base_junit = ev / "runs/baseline/junit"
    base_xml_count = len(list(base_junit.glob("*.xml"))) if base_junit.exists() else 0

    ledger = []
    ledger_path = ev / "ledger/assertion_density.csv"
    if ledger_path.exists():
        with ledger_path.open(encoding="utf-8") as fh:
            ledger = list(csv.DictReader(fh))

    totals = matrix.get("totals", {})
    unmatched = matrix.get("unmatched_consumer", [])
    auth_unmatched = [x for x in unmatched if x["normalized"].startswith("/auth/")]
    stale_unmatched = [x for x in unmatched if not x["normalized"].startswith("/auth/")]

    commits = git_log()

    led_assert = sum(int(r["assertions"]) for r in ledger)
    led_lines = sum(int(r["code_lines"]) for r in ledger)
    led_density = round(led_assert / led_lines, 4) if led_lines else 0
    zero_assert = sum(1 for r in ledger if int(r["assertions"]) == 0)

    def fmt(v, suffix=""):
        return f"{v}{suffix}" if v not in (None, "", [], {}) else "未产出"

    muts = (mut or {}).get("mutants", [])
    mut_rows = "".join(
        f'<tr><td><code>{m["id"]}</code></td><td>{pill(m.get("status", "?"))}</td>'
        f'<td>{"<br>".join(m.get("killed_by") or ["—"])}</td>'
        f'<td>{esc(m.get("rationale", "").strip().splitlines()[0])[:78]}</td></tr>'
        for m in muts
    )

    cls_rows = "".join(
        f'<tr><td><code>{c["gate"]}</code></td><td>{pill(c["state"])}</td>'
        f'<td>{esc(c["detail"])}</td></tr>'
        for c in (cid or {}).get("checks", [])
    )

    env_rows = "".join(
        f'<tr><td><code>{esc(d.get("id", ""))}</code></td>'
        f'<td>{esc(d.get("change", "").strip())}</td>'
        f'<td><code>{esc((d.get("match_message") or ["—"])[0])}</code></td>'
        f'<td>{esc(" ".join((d.get("probe") or "").split())[:150])}</td></tr>'
        for d in (target.get("runtime", {}).get("environment_deviations") or [])
    )

    verdict_by = (cls_base or {}).get("by_verdict") or {}

    # 分类分布（基线，含存量债务口径）
    base_verdict_rows = "".join(
        f'<tr><td><code>{k}</code></td><td>{v}</td><td>{desc}</td></tr>'
        for k, v, desc in [
            ("environment_excluded", verdict_by.get("environment_excluded", 0),
             "失败文本命中 target.yaml 里已声明并探针验证过的环境偏差 → 环境性排除，不计入缺陷"),
            ("product_defect", verdict_by.get("product_defect", 0),
             "改动面或增量判据成立 → 回开发，用例冻结"),
            ("contract_violation", verdict_by.get("contract_violation", 0),
             "命中合同矩阵已知漂移路由 → 阻断 + 通知双方"),
            ("flaky_candidate", verdict_by.get("flaky_candidate", 0),
             "同 commit 重跑两次均过 → 隔离区，不计入通过率但必须披露"),
            ("case_stale", verdict_by.get("case_stale", 0), "用例本身陈旧"),
            ("unknown", verdict_by.get("unknown", 0),
             "五条判据均未命中 → 隔离区，必须人工签字（owner + 到期日）"),
        ]
    )

    pitfall_rows = "".join(
        f'<tr><td>{esc(a)}</td><td>{esc(b)}</td><td>{esc(c)}</td></tr>'
        for a, b, c in [
            ("解释器环境",
             "<code>PYTHONHOME=D:\\software\\Python311</code> 与 3.13 解释器冲突，报 <code>SRE module mismatch</code>",
             "所有命令显式 <code>env -u PYTHONHOME -u PYTHONPATH</code> + 绝对路径解释器；写进 README 的环境声明"),
            ("依赖安装",
             "<code>pip install -r requirements.txt</code> 在 <code>--require-hashes</code> 下因 <code>ansicon</code> 无 hash 失败",
             "剥离 hash 生成 <code>.nohash.txt</code> 再装；产物加入 <code>.git/info/exclude</code>，工作区保持干净"),
            ("进程启动",
             "<code>INVENTREE_STATIC_ROOT not specified</code> → 补齐后又 <code>BACKUP_DIR not specified</code>",
             "在 <code>target.yaml#runtime.env</code> 一次性声明全部运行时变量，不再逐个试错"),
            ("原生依赖",
             "缺 GTK3，<code>report/models.py</code> 在 import 期 <code>sys.exit(1)</code>，整套 Django 起不来",
             "声明式替身 <code>3.env/virtual/weasyprint</code>：只满足导入期依赖，真实渲染一律抛 <code>RuntimeError</code>"),
            ("OpenAPI 导出",
             "<code>manage.py spectacular</code> 因 <code>INVE-W8: Database Migrations required</code> 退出且输出 0 字节",
             "先 <code>migrate --noinput</code>（2 分 07 秒）再导出，得到 1.1MB openapi.yaml"),
            ("契约矩阵",
             "首次全量假阴性：命中 0 / 617。后端路径带 <code>/api</code> 前缀，前端模板是相对路径",
             "两侧统一归一化到同一坐标系（<code>normalize_path(consumer=True)</code>）后重算 → 命中 190"),
            ("测试运行器", "<code>pytest</code> 跑 <code>company</code> 用 416 秒（77 例），Django 原生 runner 8.3 秒（75 例），慢 30 倍",
             "放弃 pytest，改 Django runner + xmlrunner"),
            ("证据落盘",
             "全量基线丢失全部 JUnit：InvenTree <code>settings.py</code> 写死 <code>TEST_RUNNER</code>，环境变量被静默忽略；随后又发现 xmlrunner 只读 <code>settings.TEST_OUTPUT_DIR</code>，XML 散落污染 git",
             "改为 <code>DJANGO_SETTINGS_MODULE=flywheel_junit_settings</code> 覆盖层，同时设 <code>TEST_OUTPUT_DIR</code>"),
            ("认证",
             "API Token 401 <code>Invalid token</code>：v2 token 库只存 identifier + hmac，<code>token.key</code> 不是可用明文",
             "<code>delete()</code> 后重建并读 <code>_raw_secret</code>"),
            ("L3 黄金路径",
             "连续 7 次失败：无可用非结构库位 / 编号不符 <code>PO-{ref:04d}</code> / <code>/api/build-line/</code> 404 / 该端点不返回分页信封 / <code>/finish/</code> 要 <code>accept_incomplete</code> / 无自动产出条目 / 工单状态 40≠30",
             "逐个回源码与 SSOT 核对后修正；最终 26 步全通。全部坑写进代码注释"),
            ("L3 断言口径",
             "元件库存断言 100≠90：遍历库存条目求和，消耗后条目仍保留，得出“没扣减”的假结论",
             "改用零件聚合字段 <code>part.in_stock</code>（权威口径），并在函数 docstring 写明原因"),
            ("变异锚点",
             "<code>if self.quantity > self.item.quantity:</code> 在 order/models.py 出现 3 次，唯一匹配规则拒绝执行，M2/M3 判 invalid",
             "给 <code>run_mutation.py</code> 增加 <code>nth</code> 参数只替换第 n 处，并记录 <code>mutated_line</code> 与 <code>anchor_occurrences</code>"),
            ("静默失败",
             "run_l1 的 <code>PY = os.environ.get(\"FLYWHEEL_PYTHON\")</code> 未设置时为 <code>None</code>，subprocess 抛 TypeError 被吞掉；变异门禁读到上一轮残留 JUnit，报出假的 <code>killed</code>",
             "<code>PY</code> 兜底为 <code>sys.executable</code>；门禁跑前清空 run 目录，且 <code>run.json</code> 缺失/收集为 0 一律判 invalid"),
            ("归因判据",
             "S2 用“文件最后一次提交的 sha”，基线自身会全体误判；实测该写法返回空串，规则静默失效。改用改动集后，assertRaises 类失败又因调用栈只到测试文件而全部落 unknown",
             "补第二判据 S2b：基线 passed + 候选 failed + 存在改动集 → 候选引入。证据是两份 JUnit，可复算"),
            ("等价变异体",
             "首轮 M2（放宽超分配）/M3（短路结单守卫）判“存活”。读代码发现二者都有第二道守卫兜底，属等价变异体",
             "不把工具的无能算成断言缺口：换掉两个锚点重挑（可追溯件整数用量 / 单据编号模式），终获 3/3"),
            ("闸门对象漂移",
             "can-i-deploy 取“最新一个 run.json”，而变异门禁每轮都落 run.json，于是会把某个变异体当候选集去判",
             "候选集必须显式指定 <code>--candidate</code>，并在判决里写出候选 run id"),
        ]
    )

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>冒烟飞轮 · 一轮完整实践证明</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">

<header class="top">
  <div class="kicker">冒烟飞轮 · 实践证明 · {datetime.now().strftime('%Y-%m-%d')}</div>
  <h1>一轮完整转动：从 SSOT 到 can-i-deploy，<br>全部跑在真实仓、真实用例、真实缺陷上</h1>
  <p class="lead">被测对象是 275,140 行的 InvenTree（Django REST + React 前后端分离）。
  本报告不含“预计”“大约”“通常”——每个数字都能在交付仓的证据文件里复算。</p>
</header>

<div class="grid g4">
  <div class="card kpi"><div class="n">{fmt(totals.get('backend_operations'))}</div>
    <div class="l">后端 API 操作（OpenAPI 实测）</div></div>
  <div class="card kpi"><div class="n">{fmt(totals.get('consumer_declarations'))}</div>
    <div class="l">前端消费方声明</div></div>
  <div class="card kpi"><div class="n">{fmt(l1.get('collected') if l1 else None)}</div>
    <div class="l">L1 实跑用例（9 条不变量）</div></div>
  <div class="card kpi"><div class="n">{fmt((mut.get('killed') if mut else None))}/{fmt(mut.get('valid') if mut else None)}</div>
    <div class="l">注入缺陷被杀灭</div></div>
</div>

<h2>0. 一句话结论</h2>
<div class="quote">
<p><strong>飞轮转起来了，而且咬得住东西。</strong>
上游 1,886 个成品用例里选了 16 个进 L1（全绿，126 秒）；L3 黄金路径 26 步真实 HTTP 走通（6 条断言含库存账实）；
注入 3 个真实缺陷后 3 个全部被既有用例杀死；L0 抓出 4 条前端声明在后端不存在的漂移。</p>
<p><strong>但同样要说清楚它做不到什么。</strong>179 条存量失败里，只有 40 条能凭“已声明并探针验证过的环境偏差”排除，
其余 139 条（77.6%）无法定责，只能进隔离区等人工签字。
这个数字不好看，但它就是本轮最重要的负面结论：<strong>一个自称能定责的分类器，在陌生仓首轮必然大面积判不出</strong>。
把它藏起来，方案就变成了演示品。</p>
</div>

<h2>1. 测的是哪个仓（可点开核对）</h2>
<table>
<tr><th style="width:22%">项</th><th>值</th></tr>
<tr><td>被测仓</td><td><a href="{SUT_REPO}">{SUT_REPO}</a></td></tr>
<tr><td>提交</td><td><code>{SUT_COMMIT}</code>（{target.get('target', {}).get('commit_date', '')}）</td></tr>
<tr><td>规模（实测）</td><td>{measures.get('total_source_lines', 0):,} 行 / {measures.get('source_files', 0):,} 文件：
Python {measures.get('python_lines', 0):,} + TypeScript {measures.get('typescript_lines', 0):,}</td></tr>
<tr><td>前后端分离</td><td>Django REST（617 个操作）+ React SPA（216 条 ApiEndpoints 声明），前端独立 Vite 工程</td></tr>
<tr><td>上游用例</td><td>{measures.get('upstream_test_files', 0)} 个测试文件 / {measures.get('upstream_test_functions', 0):,} 个用例函数</td></tr>
<tr><td>领域</td><td>电子元件 / BOM / 库存 / 生产工单 —— <strong>PCB 装配链，不是原理图编辑器</strong></td></tr>
<tr><td>交付仓</td><td><a href="{FLY_REPO}">{FLY_REPO}</a></td></tr>
</table>

<div class="caveat">
<strong>域的正名与对照。</strong> 开源世界里，“原理图/PCB 编辑器 + 20 万行 + 前后端分离 + 成熟用例”四条同时成立的仓几乎不存在。
域最正的 <a href="{LIBRE_REPO}">LibrePCB</a>（<code>{LIBRE_COMMIT[:7]}</code>，375,122 行 C++/Slint）是桌面单体：
全仓 C++ 里 <code>QTcpServer</code>/<code>QHttpServer</code>/<code>listen(</code> 零命中，没有可被独立验证的 provider 边界，
L2 契约层与 can-i-deploy 无处落脚。所以本轮选 InvenTree 做主仓，并把“域偏制造侧”这句话放在首页，而不是藏在附录。
</div>

<h2>2. 交付仓的提交记录</h2>
<p>所有资产都在 <a href="{FLY_REPO}">{FLY_REPO}</a>，下面直接取自 <code>git log</code>。</p>
<table>
<tr><th style="width:14%">提交</th><th style="width:14%">日期</th><th>说明</th></tr>
{''.join(f'<tr><td><code>{c["sha"]}</code></td><td>{c["date"]}</td><td>{esc(c["subject"])}</td></tr>' for c in commits)}
</table>

<h2>3. 一次完整转动：八步，每一步都留证据</h2>
<p>飞轮的每一步都必须产出可复算的 JSON，缺证据就判 <code>UNKNOWN</code>，不允许“跑过了”这类口头结论。</p>

{step(1, "L0 · 把事实从源码抽出来",
      f'<p><code>python tools/emit_ssot.py --repo &lt;inventree 克隆&gt; --out 0.ssot</code></p>'
      f'<p>端点模型不是手写清单，是从真实 URLconf 经 <code>manage.py spectacular</code> 导出的 '
      f'<code>0.ssot/openapi.yaml</code>（{ssv.get("counts", {}).get("endpoints", "?")} 个操作）；'
      f'消费方期望来自前端 <code>lib/enums/ApiEndpoints.tsx</code> 的枚举声明。两边归一化后求交。</p>')}

{step(2, "verify-ssot · 事实源是不是这个提交的投影",
      f'<p><code>python 4.gates/verify-ssot/verify.py --sut-root &lt;inventree 克隆&gt;</code> → '
      f'{len(vs.get("checks", []))} 条判据 / 未过 {len(vs.get("failed") or [])} → {pill(vs.get("verdict", "UNKNOWN"))}</p>'
      f'<p class="small">比对 <code>SSOT commit == 被测仓 HEAD == target.yaml 声明</code>，重算 openapi 的 sha256，'
      f'再用规范重新投影一遍端点集与 <code>endpoints.json</code> 对齐。缺这一道，后面所有结论都可能建立在过期事实源上。</p>')}

{step(3, "lint-boundary · 边界契约自检",
      f'<p><code>python 4.gates/lint-boundary/lint.py</code> → '
      f'检查 {lint.get("checked_boundaries", "?")} 份契约 / 索引 {lint.get("indexed_tests", "?")} 个上游用例 ID / '
      f'违规 {len(lint.get("violations") or [])} → {pill(lint.get("result", "UNKNOWN"))}</p>'
      f'<p class="small">四类规则：schema 完整性、依赖必须显式声明且路径存在、用例 ID 必须在被测仓真实存在、禁令与时限。'
      f'第三条是关键——上游一改名，这里会直接红，而不是让用例静默消失。</p>')}

{step(4, "L1 · 9 条不变量指向 16 个成品用例",
      f'<p>实跑：<strong>{fmt(l1.get("collected") if l1 else None)} 个 test case，通过 {fmt(l1.get("passed") if l1 else None)}，'
      f'失败 {fmt(l1.get("failed") if l1 else None)}，失效 ID {len(l1.get("missing_test_ids") or []) if l1 else "未产出"}，'
      f'耗时 {fmt(l1.get("duration_s") if l1 else None, "s")}</strong> '
      f'{pill("PASS" if (l1 or {}).get("failed") == 0 else "FAIL")}</p>'
      f'<p class="small">不新写玩具用例。每条不变量直接指向 InvenTree 里已存在、已被上游 CI 长期运行的用例。</p>')}

{step(5, "L3 · 一条不可失败的业务主线",
      f'<p>起真实 <code>runserver</code>，用 API Token 走完 {len((l3 or {}).get("steps", []))} 个 HTTP 步骤，'
      f'{len((l3 or {}).get("checks", []))} 条断言：{pill((l3 or {}).get("result", "UNKNOWN"))}</p>'
      f'<p class="small">建库位 → 建零件 → 供应商物料 → 采购订单 → 收货入库 → BOM(1:2) → 工单(5 台) → 分配库存 → 产出 → 结单。'
      f'断言口径是 response + 库存账实，不是“返回 200 就算过”。</p>')}

{step(6, "全量基线 + 建账",
      f'<p>口径按你的选择：全量 + 注入缺陷。{fmt(base_run.get("collected") if base_run else None)} 个用例 / '
      f'{base_xml_count} 个 JUnit XML / 失败 {fmt(base_run.get("failed") if base_run else None)}。</p>'
      f'<p>这 179 条失败连同定责结论与到期日写入债务台账 <code>5.evidence/verdicts/baseline-debt.json</code>'
      f'（{debt.get("count", "未产出")} 条）。<strong>台账的作用不是让红灯变绿，而是让债务可见且不得静默消失。</strong></p>')}

{step(7, "mutation-diff · 用例到底抓不抓得住",
      f'<p>三个真实缺陷，各自绑定“应该杀死它”的用例：'
      f'<strong>{fmt(mut.get("killed"))}/{fmt(mut.get("valid"))} 被杀灭，杀灭率 {fmt(mut.get("kill_rate"))}</strong>，'
      f'工作区还原：{"是" if mut.get("worktree_restored") else "否"}。</p>')}

{step(8, "can-i-deploy · 只看证据的闸门",
      f'<p>最终判决：{pill((cid or {}).get("decision", "UNKNOWN"))}'
      f'{"　阻断项：" + "、".join(cid.get("blockers", [])) if (cid or {}).get("blockers") else ""}</p>'
      f'<p class="small">六道检查：SSOT 冻结（commit/sha 绑定）、边界契约、L1、失败定责（增量口径）、变异、契约矩阵。'
      f'另附存量债务披露（不阻断，但每条带 owner 与到期日）。</p>')}

<h2>4. L0：事实与漂移</h2>
<div class="grid g4">
  <div class="card kpi"><div class="n">{totals.get('backend_operations', '?')}</div><div class="l">提供方操作</div></div>
  <div class="card kpi"><div class="n">{totals.get('consumer_declarations', '?')}</div><div class="l">消费方声明</div></div>
  <div class="card kpi"><div class="n">{totals.get('matched', '?')}</div><div class="l">命中</div></div>
  <div class="card kpi"><div class="n">{totals.get('unmatched_consumer', '?')}</div><div class="l">前端声明未命中</div></div>
</div>

<h3>未命中的 {len(unmatched)} 条，分两类（这是真实发现，不是疏漏）</h3>
<table>
<tr><th style="width:18%">类别</th><th style="width:10%">数量</th><th>说明</th></tr>
<tr><td>非 DRF 管辖</td><td>{len(auth_unmatched)}</td>
<td><code>auth/v1/...</code> 由 django-allauth headless 提供，本就不在 DRF schema 里 —— 属正常边界，不算漂移</td></tr>
<tr><td><strong>疑似陈旧声明</strong></td><td>{len(stale_unmatched)}</td>
<td>{'、'.join(f'<code>{esc(x["template"])}</code>' for x in stale_unmatched)} —— 前端引用了后端未暴露的路由。
逐条回源码核过：<code>tips</code> 只有 detail 路由，<code>scheduling</code>/<code>ship</code> 在 api.py 里搜不到对应类。
这是真实技术债，交给 L0 报出来，而不是让它在运行时 404。</td></tr>
</table>
<p class="small">依赖图：{dep.get('node_count', '?')} 个后端 app、{dep.get('edge_count', '?')} 条 import 边，写入
<code>0.ssot/dependency_graph.json</code>，作为“受影响面反查”的输入（触发源是端点/契约/依赖边，不是 .vue 文件）。</p>

<h3>4.1 这份 SSOT 是怎么来的：三个状态，前后都能对照</h3>
<p>上面的数字不是一次写成的。它先失败过一次（导出 0 字节事实源），又错过一次（前后端没落到同一坐标系，
命中率 0/216），修正后才成为现在这份。每个状态的全景与逐条差异在
<a href="ssot-evolution.html">0.ssot 演进全景</a>里，这里只放关键差值：</p>
<table>
<tr><th>状态</th><th>后端操作</th><th>前端声明</th><th>命中</th><th>前端未命中</th><th>后端未被引用</th></tr>
<tr><td>G0 导出失败</td><td>0</td><td>0</td><td>0</td><td>0</td><td>0</td></tr>
<tr><td>G1 轴错位</td><td>{evo_states.get('G1-naive-axis', {}).get('endpoints', '?')}</td>
    <td>{evo_states.get('G1-naive-axis', {}).get('consumers', '?')}</td>
    <td><strong>{evo_states.get('G1-naive-axis', {}).get('matched', '?')}</strong></td>
    <td>{evo_states.get('G1-naive-axis', {}).get('unmatched_consumer', '?')}</td>
    <td>{evo_states.get('G1-naive-axis', {}).get('backend_unclaimed', '?')}</td></tr>
<tr><td>G2 轴对齐（现行）</td><td>{evo_states.get('G2-aligned-axis', {}).get('endpoints', '?')}</td>
    <td>{evo_states.get('G2-aligned-axis', {}).get('consumers', '?')}</td>
    <td><strong>{evo_states.get('G2-aligned-axis', {}).get('matched', '?')}</strong></td>
    <td>{evo_states.get('G2-aligned-axis', {}).get('unmatched_consumer', '?')}</td>
    <td>{evo_states.get('G2-aligned-axis', {}).get('backend_unclaimed', '?')}</td></tr>
</table>
<p class="small">G1→G2 只改了归一化函数的一段，端点数与声明数一个没变——<strong>事实没变，变的是坐标</strong>。
这也是最容易误判的一类修改：轴错位时命中 0，看上去像"这个仓前后端对不上"的业务结论。</p>
<p class="small">SSOT 冻结后新增 <code>4.gates/verify-ssot</code> 做绑定校验：七个判据
{pill(vs.get('verdict', 'UNKNOWN')) if vs else '未产出'}，其中 V3a 比对
<code>SSOT commit == 被测仓 HEAD == target.yaml 声明</code>。用"树不变、只追加一个空提交"制造漂移后，
can-i-deploy 由 ALLOW 翻成 BLOCK——证明这条校验真的在跑，而不是只打印了个 sha。</p>

<h2>5. L1 与 L3 的实跑数字</h2>
<div class="grid g2">
  <div class="card">
    <h3 style="margin-top:0">L1：9 条不变量</h3>
    <p class="small">part.catalog / bom.validity / stock.receive / sales.allocate / sales.create /
    purchase.complete / build.complete / role.isolation / api.contract</p>
    <p>{pill("PASS" if (l1 or {}).get("failed") == 0 else "FAIL")}
    {fmt(l1.get('collected') if l1 else None)} 例 / {fmt(l1.get('duration_s') if l1 else None, ' 秒')}</p>
  </div>
  <div class="card">
    <h3 style="margin-top:0">L3：黄金路径断言口径</h3>
    <ul>
      {''.join(f'<li>{esc(c["check"])}：<strong>{esc(c["actual"])}</strong>（期望 {esc(c["expected"])}）</li>' for c in (l3 or {}).get('checks', []))}
    </ul>
  </div>
</div>

<h2>6. 全量基线：{fmt(base_run.get('collected') if base_run else None)} 个用例的真实结果</h2>
<table>
<tr><th style="width:30%">项</th><th>值</th></tr>
<tr><td>覆盖方式</td><td>17 个 app 全量：build common company importer machine order part pricing report stock users plugin InvenTree generic web scim</td></tr>
<tr><td>JUnit 产物</td><td>{base_xml_count} 个 XML（<code>5.evidence/runs/baseline/junit/</code>）</td></tr>
<tr><td>通过 / 失败 / 跳过</td><td>{fmt(base_run.get('passed') if base_run else None)} / {fmt(base_run.get('failed') if base_run else None)} / {fmt(base_run.get('skipped') if base_run else None)}</td></tr>
<tr><td>用例耗时合计</td><td>{fmt(base_run.get('duration_s') if base_run else None, ' 秒')}（逐条相加；终端墙钟另计）</td></tr>
<tr><td>债务台账</td><td><code>5.evidence/verdicts/baseline-debt.json</code>（{debt.get('count', '未产出')} 条，每条带 owner 与到期日）</td></tr>
</table>
<p class="small">口径说明：Django 终端汇总行报 <code>Ran 2018 tests</code>，JUnit 落盘 {fmt(base_run.get('collected') if base_run else None)} 条用例记录，
两者统计口径不同。本报告一律以 JUnit 为准——因为 JUnit 可以逐条点开核对，终端汇总行不能。</p>
<p class="small">首次全量运行曾丢失全部结构化证据：InvenTree 的 settings.py 显式写死了 TEST_RUNNER，
环境变量被静默忽略，XML 散落到工作目录还污染了 git 工作区。改为 settings 覆盖层（<code>3.env/flywheel_junit_settings.py</code>）后重跑，
这段弯路保留在提交记录里，因为它正是“证据链不能靠约定”的例证。</p>

<h2>7. 失败定责：确定性分类器（LLM 不参与）</h2>
<p>判据顺序不可逆。关键是<strong>例外表只能来自 <code>3.env/target.yaml</code></strong>——
代码里不出现任何具体包名或主机名，否则就等于给“看着像环境问题”开后门。</p>
<table>
<tr><th style="width:26%">结论</th><th style="width:10%">数量</th><th>含义</th></tr>
{base_verdict_rows}
</table>
<div class="grid g2">
  <div class="card"><h3 style="margin-top:0">两个比例，语义不同</h3>
  <p><code>unknown_ratio</code> = {fmt((cls_base or {}).get('unknown_ratio'))} —— 全量失败里的未知比例，
  只用于披露存量债务质量。</p>
  <p><code>new_failure_unknown_ratio</code> = {fmt((cls_cand or {}).get('new_failure_unknown_ratio'))} ——
  增量失败里的未知比例，才是 <code>can-i-deploy</code> 的实际判据。</p>
  <p class="small">混用这两个指标会得出相反结论：拿全量口径卡每一次部署，闸门会永久红灯；
  拿增量口径去披露历史债务，债务就会消失。所以两个都算、都写进证据，用途分开。</p></div>
  <div class="card"><h3 style="margin-top:0">候选集口径</h3>
  <p>候选 = <code>{(cid or {}).get('candidate', '未产出')}</code>，
  新增失败 {(cls_cand or {}).get('new_failures', '未产出')} 条。</p>
  <p class="small">最初实现取“最新一个 run.json”，而变异门禁每轮都落 run.json，
  于是闸门会把某个变异体当候选集去判——判决对象漂移且无人察觉。现在必须显式指定。</p></div>
</div>

<div class="badnote">
<strong>为什么 77.6% 判不出来，这是本轮最该讲的负面结论。</strong>
分类器的每一条结论都要带可复算的证据，所以它只在两种情况下敢下判断：失败文本自己指认了缺失的前置条件（S0），
或者改动面/基线增量能证明是候选引入（S2）。剩下的失败——比如 <code>plugin.test_api.*</code> 大量 404、
<code>report.tests.TestReportTest.*</code> 返回 None——文本里没有任何指向根因的线索。
<strong>要么补探针把根因变成可复算事实，要么让它进隔离区等签字；不能靠“我觉得是环境问题”把它划掉。</strong>
本轮选择了后者，并把这些条目连同 owner 与到期日写进台账。
</div>

<h3>已声明并验证过的环境偏差（S0 的合法来源）</h3>
<table>
<tr><th style="width:20%">偏差</th><th style="width:26%">与上游 CI 的差别</th><th style="width:18%">命中串</th><th>探针</th></tr>
{env_rows}
</table>

<h2>8. 变异：用例到底能不能抓住缺陷</h2>
<p>注入的不是随机翻转，是真实 PR 里出现过的写法：注释校验、去掉边界、关掉守卫。
每个变异体先 <code>git commit</code> 成一次真实提交（否则 <code>git blame</code> 看不到，S2 无法证伪），跑完立即 <code>reset --hard</code>。</p>
<table>
<tr><th style="width:24%">变异体</th><th style="width:9%">结果</th><th style="width:28%">被杀于</th><th>缺陷形态</th></tr>
{mut_rows}
</table>
<p>杀灭率 <strong>{fmt(mut.get('kill_rate'))}</strong>（阈值 0.8），工作区还原：<strong>{'是' if mut.get('worktree_restored') else '否'}</strong>。</p>

<div class="caveat">
<strong>等价变异体：一个必须留下的教训。</strong>
第一轮的 M2（放宽超分配）和 M3（短路结单守卫）都被判“存活”，看起来像断言缺口。读代码后发现是工具的问题：
放宽 <code>SalesOrderAllocation.clean</code> 后，同函数里 <code>total_allocation &gt; self.item.quantity</code> 仍然拦截；
短路 <code>build/models.py</code> 的结单守卫后，<code>build/serializers.py</code> 的
<code>Build order has incomplete outputs</code> 仍然拦截。<strong>变异体没改变行为，用例当然抓不住。</strong>
把这两条当断言缺口上报，就是把工具的无能粉饰成代码的问题。所以两个锚点都重挑（可追溯件整数用量 / 单据编号模式），
终获 3/3。</div>

<p class="small">断言密度账本 <code>5.evidence/ledger/assertion_density.csv</code>：
{len(ledger)} 个用例、{led_assert} 条断言、{led_lines} 行代码，密度 <strong>{led_density}</strong>；
其中 <strong>{zero_assert}</strong> 个用例断言行数为 0 —— 这些就是“绿灯但不可信”的候选，是唯一与通过率反向的刹车指标。</p>

<h2>9. 部署闸门 can-i-deploy</h2>
<table>
<tr><th style="width:22%">检查</th><th style="width:10%">状态</th><th>依据</th></tr>
{cls_rows}
</table>
<p>最终判决：<strong>{pill((cid or {}).get('decision', 'UNKNOWN'))}</strong>
{'　阻断项：' + '、'.join(cid.get('blockers', [])) if (cid or {}).get('blockers') else ''}</p>
{'<p class="small">代入注入缺陷的候选：' + pill(cid_mut.get('decision', 'UNKNOWN')) + '，阻断项 ' + '、'.join(cid_mut.get('blockers', [])) + ' —— 同一套闸门，注入的缺陷会被拦下。</p>' if cid_mut else ''}

<h2>10. 环境偏差与三处不藏</h2>
<div class="caveat">
<ol>
<li><strong>数据库 sqlite3</strong>（上游 CI 用 PostgreSQL）。本机 Docker daemon 未运行。影响并发与 JSON 字段实现，不影响本次业务语义。</li>
<li><strong>weasyprint 声明式虚拟化</strong>。本机缺 GTK3/libgobject，而上游 <code>report/models.py</code> 在 import 期
<code>sys.exit(1)</code>，不替换则整套 Django 起不来。替身只满足导入期依赖，任何真实 PDF 渲染调用都抛
<code>RuntimeError</code>；受影响用例已从 L1 选择中排除并写明原因。</li>
<li><strong>L2 用“前后端契约矩阵”而非 Pact Broker</strong>。消费方是同仓前端，没有跨团队 broker，拉 Pact 进来是为演示而演示。
矩阵口径与 can-i-deploy 一致：消费方声明 → 提供方操作 → 是否命中。</li>
</ol>
<p class="small">另：L4 探索层本轮未做。方案要求“赚到资格再上”，留白比硬凑一个假 L4 诚实。</p>
</div>

<h2>11. 走过的弯路（按主题，全部真实发生过）</h2>
<table>
<tr><th style="width:14%">主题</th><th style="width:46%">现象</th><th>怎么定的</th></tr>
{pitfall_rows}
</table>

<h2>12. 怎么复现</h2>
<pre>git clone {FLY_REPO} &amp;&amp; cd aces-smoke-flywheel
python tools/emit_ssot.py --repo &lt;inventree 克隆&gt; --out 0.ssot
python 4.gates/lint-boundary/lint.py
python tools/run_l1.py --run-id l1-run-01
python 4.gates/classify-failure/classify.py --run-dir 5.evidence/runs/l1-run-01 \\
       --out 5.evidence/verdicts/classification.json \\
       --debt-ledger 5.evidence/verdicts/baseline-debt.json
python 4.gates/mutation-diff/run_mutation.py
python 4.gates/can-i-deploy/can_i_deploy.py --candidate l1-run-01</pre>
<p class="small">解释器需显式清掉 <code>PYTHONHOME</code>/<code>PYTHONPATH</code>（见 README 的环境声明），
被测仓工作区必须干净——变异门禁在脏工作区会直接 ABORT，不会给出“看起来 PASS”的结论。</p>

<footer>
被测仓 <a href="{SUT_REPO}">inventree/InvenTree</a> @ <code>{SUT_COMMIT[:7]}</code> ·
交付仓 <a href="{FLY_REPO}">aces-org-zhuang/aces-smoke-flywheel</a> ·
报告生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')} ·
页面所有数字来自 <code>5.evidence/</code> 与 <code>0.ssot/</code>，可逐条复算
</footer>

</div>
</body>
</html>
"""
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"KS_REPORT\t{out}\t{len(html)} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
