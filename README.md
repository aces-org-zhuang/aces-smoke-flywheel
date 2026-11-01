# aces-smoke-flywheel

把"冒烟飞轮"从方案落成可执行资产的仓。**这不是一套测试框架，是一套判决机制**：
用例不是资产，可追溯的契约才是；证据不是日志，能复算的判定才是。

报告页：`docs/report.html`（由 `tools/build_report.py` 从 `5.evidence/` 渲染，页面数字全部可复算）。

## 这次实践测的是谁

| 项 | 值 |
|---|---|
| 被测仓 | https://github.com/inventree/InvenTree |
| 提交 | `d96174cddbc2566fea53b3fe39b5d4aa5d30fbf1`（2026-10-30） |
| 规模（实测） | 275,140 行源码 / 1,304 文件（Python 176,773 + TypeScript 97,175） |
| 前后端分离 | Django REST（617 个 API 操作）+ React SPA（216 条消费方声明） |
| 上游用例 | 92 个测试文件 / 1,886 个用例函数（本次实跑 JUnit 落盘 2,020 条用例记录，终端汇总行报 `Ran 2018 tests`，以 JUnit 为准） |
| 领域 | 电子元件 / BOM / 库存 / 生产工单（PCB 装配链），**不是**原理图编辑器 |

域正对照仓：`LibrePCB/LibrePCB`（375,122 行 C++，真 EDA，但桌面单体、无前后端分离），
见 `docs/librepcb-control.md`——它是"域对了但架构不对"的反例，用来锁住口径。

## 六层结构

```
0.ssot/          唯一事实源：openapi.yaml / endpoints.json / contract_matrix.json
                 / dependency_graph.json / schema_version.json（与 commit 绑定）
                 history/ 演进快照：G1 轴错位 / G2 轴对齐，逐状态可重放对照
1.spec/          人写的规格：invariants.yaml（9 条不变量）、boundaries/*.boundary.yaml
2.tests/         可执行用例：L3 黄金路径（真实 HTTP），L1 直接复用上游成品用例
3.env/           运行底座：target.yaml（被测对象 + 环境偏差声明）、virtual/、states/
4.gates/         门禁即代码：verify-ssot / lint-boundary / classify-failure
                 / mutation-diff / can-i-deploy
                 check-atlas：资产全景页的点击验证（每条资产点开是不是原文）
5.evidence/      证据仓：runs/（每次运行的 junit+stdout）、verdicts/、ledger/
docs/            方法对照（method.md）、域对照、变异等价性说明、
                 报告页（report.html）、版本资产全景（ssot-atlas.html）、演进全景（ssot-evolution.html）

资产全景（ssot-atlas.html）是"能点开的"版本：一章一个 SSOT 版本，章内每一条资产都能展开看原文——
  端点 617 条 → 该操作的完整 OpenAPI 定义（参数 / 请求体 / 响应 / 鉴权 / 原始 JSON）
  数据模型 423 条 → 逐字段类型与必填，$ref 可继续下钻
  前端声明 216 条 → ApiEndpoints.tsx 的具体行与上下文
  依赖边 129 条 → 后端那条真实的 import 语句
  契约矩阵 529 条 → 命中的全部 method 与 operationId，可跳回端点
页面内嵌完整规范副本，离线可查；点开是否真出原文由 4.gates/check-atlas 断言（40 条断言）。
```

## 一次完整转动的八步

```bash
# 0) 生成 SSOT（必须与 commit 绑定）
python tools/emit_ssot.py --repo <SUT> --out 0.ssot

# 1) SSOT 冻结校验（七条判据；绑定不成立则后面全部作废）
python 4.gates/verify-ssot/verify.py --sut-root <SUT>

# 2) 边界契约 lint（四类违规直接拒绝）
python 4.gates/lint-boundary/lint.py

# 3) L1：跑 9 条不变量指向的上游用例
python tools/run_l1.py --run-id l1-run-01

# 4) 基线全量 + 建账（存量债务台账，每条带 owner 与到期日）
python 4.gates/classify-failure/classify.py \
       --run-dir 5.evidence/runs/baseline \
       --out 5.evidence/verdicts/classification-baseline.json \
       --write-ledger 5.evidence/verdicts/baseline-debt.json

# 5) 候选定责（增量口径：只看新增失败）
python 4.gates/classify-failure/classify.py \
       --run-dir 5.evidence/runs/l1-run-01 \
       --out 5.evidence/verdicts/classification.json \
       --debt-ledger 5.evidence/verdicts/baseline-debt.json

# 6) 变异：注入真实缺陷，看用例能不能抓
python 4.gates/mutation-diff/run_mutation.py

# 7) 部署闸门：只看证据，候选集必须显式指定
python 4.gates/can-i-deploy/can_i_deploy.py --candidate l1-run-01
```

## 分类器的五条判据（顺序不可逆）

| 规则 | 判据 | 证据 |
|---|---|---|
| S0 | 失败文本命中 `3.env/target.yaml` 里**已声明且探针验证过**的环境偏差 | 偏差声明 + 命中串 |
| S1 | 同 commit 重跑两次均过 → flaky | `rerun.json` |
| S2a | 失败栈里的生产文件出现在 `baseline..HEAD` 改动集 | `git diff --name-only` |
| S2b | 用例基线 passed、候选 failed，且候选有改动集 | 两份 JUnit |
| S3 | API 层失败且命中合同矩阵已知漂移路由 | `contract_matrix.json` |
| S4 | 其余 → unknown，进隔离区（owner + 到期日） | 无 |

两条纪律：

- **S0 的例外表只能来自 `target.yaml`**，代码里不出现任何具体包名/主机名。
  否则"看着像环境问题"就有了后门。
- **S2b 是为 `assertRaises` 类失败准备的**：这类失败的调用栈只到测试文件，
  生产代码根本不在栈里，只靠栈归属会对最常见的一类缺陷永久失效。

两个比例语义不同，混用会得出相反结论：

- `unknown_ratio`：全量失败里的未知比例，只用于披露存量债务质量；
- `new_failure_unknown_ratio`：增量失败里的未知比例，才是 `can-i-deploy` 的判据。

## 三条纪律（写进代码，不靠自觉）

1. **SSOT 与 commit 绑定**：`4.gates/verify-ssot/verify.py` 会比对
   `schema_version.commit == SUT HEAD == target.yaml 声明`，并重算 openapi 的 sha256、
   用规范重新投影一遍端点集与 `endpoints.json` 对齐。任一不符即 BLOCK。
   （这条以前只写在文档里、没有代码执行，属于"声明存在、执行缺失"，已补。）
2. **AI 只提案，不定责**：失败先由 `classify-failure` 判据定责；
   只有"用例陈旧"才允许生成修复提案，且必须过 G1 断言不弱化 / G2 不删用例 / G3 杀死变异体。
3. **证据只追加**：`5.evidence/` 不覆写。任何结论都必须能从原始 junit + 源码 commit 复算出来。

## 门禁自身的三个坑（已修，写在代码注释里）

- `run_l1.py` 的 `FLYWHEEL_PYTHON` 未设置时为 `None`，subprocess 抛错被吞，
  变异门禁会读到上一轮残留 JUnit 给出**假绿** → 兜底 `sys.executable`，跑前清空 run 目录，
  `run.json` 缺失或收集为 0 一律判 `invalid`。
- 分类必须在缺陷提交仍是 HEAD 时进行，否则 `baseline..HEAD` 为空，注入的缺陷会被判 `unknown`。
- `can-i-deploy` 不能取"最新一个 run.json"当候选——变异门禁每轮都落 run.json，会判错对象。

## 环境声明（不隐藏，不美化）

- 数据库用 sqlite3（上游 CI 用 PostgreSQL）。原因：本机 Docker daemon 未运行。
- `weasyprint` 被声明式虚拟化（缺 GTK3/libgobject）。原因：上游 `report/models.py`
  在 import 期 `sys.exit(1)`，不替换则整套服务起不来。替身**只满足导入期依赖**，
  任何真实 PDF 渲染调用都会抛错——不会静默通过。受影响的 `report.*` 用例已从 L1 选择中排除。
- Windows 路径分隔符、未编译翻译（仓内 0 个 `.mo`）、未安装 sample plugins、
  出网受限、文件锁等偏差，全部登记在 `3.env/target.yaml#runtime.environment_deviations`，
  每条都带可复算探针。
- 解释器需显式清掉 `PYTHONHOME`/`PYTHONPATH`（本机 `PYTHONHOME` 指向 3.11，会与 3.13 冲突）。
