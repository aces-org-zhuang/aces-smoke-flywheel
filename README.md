# aces-smoke-flywheel

把"冒烟飞轮"从方案落成可执行资产的仓。**这不是一套测试框架，是一套判决机制**：
用例不是资产，可追溯的契约才是；证据不是日志，能复算的判定才是。

## 这次实践测的是谁

| 项 | 值 |
|---|---|
| 被测仓 | https://github.com/inventree/InvenTree |
| 提交 | `d96174cddbc2566fea53b3fe39b5d4aa5d30fbf1`（2026-10-30） |
| 规模（实测） | 275,140 行源码 / 1,304 文件（Python 176,773 + TypeScript 97,175） |
| 前后端分离 | Django REST（617 个 API 操作）+ React SPA（216 条消费方声明） |
| 上游用例 | 92 个测试文件 / 1,886 个用例函数（本次实跑收集 2,018 个 test case） |
| 领域 | 电子元件 / BOM / 库存 / 生产工单（PCB 装配链），**不是**原理图编辑器 |

域正对照仓：`LibrePCB/LibrePCB`（375,122 行 C++，真 EDA，但桌面单体、无前后端分离），
见 `docs/librepcb-control.md`——它是"域对了但架构不对"的反例，用来锁住口径。

## 六层结构

```
0.ssot/          唯一事实源：openapi.yaml / endpoints.json / contract_matrix.json
                 / dependency_graph.json / schema_version.json（与 commit 绑定）
1.spec/          人写的规格：invariants.yaml（9 条不变量）、boundaries/*.boundary.yaml
2.tests/         可执行用例：L3 黄金路径（真实 HTTP），L1 直接复用上游成品用例
3.env/           运行底座：target.yaml（被测对象声明）、virtual/（声明式替身）、states/
4.gates/         门禁即代码：lint-boundary / classify-failure / mutation-diff / can-i-deploy
5.evidence/      证据仓：runs/（每次运行的 junit+stdout）、verdicts/、ledger/
```

## 一次完整转动的六步

```bash
# 0) 生成 SSOT（必须与 commit 绑定）
python tools/emit_ssot.py --repo <SUT> --out 0.ssot

# 1) 边界契约 lint（四类违规直接拒绝）
python 4.gates/lint-boundary/lint.py

# 2) L1：跑 9 条不变量指向的上游用例
python tools/run_l1.py --run-id l1-<ts>

# 3) 失败定责（确定性分类器，LLM 不参与）
python 4.gates/classify-failure/classify.py --run-dir 5.evidence/runs/l1-<ts>

# 4) 变异：注入真实缺陷，看用例能不能抓
python 4.gates/mutation-diff/run_mutation.py

# 5) 部署闸门：只看证据
python 4.gates/can-i-deploy/can_i_deploy.py
```

## 三条纪律（写进代码，不靠自觉）

1. **SSOT 与 commit 绑定**：`0.ssot/schema_version.json` 里的 commit 与 SUT 当前 HEAD 不一致时，
   gate 拒绝出结论——防止"用旧模型测新代码"。
2. **AI 只提案，不定责**：失败先由 `classify-failure` 四条判据定责；
   只有"用例陈旧"才允许生成修复提案，且必须过 G1 断言不弱化 / G2 不删用例 / G3 杀死变异体。
3. **证据只追加**：`5.evidence/` 不覆写。任何结论都必须能从原始 junit + 源码 commit 复算出来。

## 环境声明（不隐藏，不美化）

- 数据库用 sqlite3（上游 CI 用 PostgreSQL）。原因：本机 Docker daemon 未运行。
- `weasyprint` 被声明式虚拟化（缺 GTK3/libgobject）。原因：上游 `report/models.py`
  在 import 期 `sys.exit(1)`，不替换则整套服务起不来。替身**只满足导入期依赖**，
  任何真实 PDF 渲染调用都会抛错——不会静默通过。受影响的 `report.*` 用例已从 L1 选择中排除。
- 上述两项都写进 `3.env/target.yaml` 的 `virtualization` 与 `database_note`，并进入报告首页。
