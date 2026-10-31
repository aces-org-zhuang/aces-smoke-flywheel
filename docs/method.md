# 方案条款 → 本仓实现 → 本次实测证据

| 方案条款 | 本仓实现 | 证据位置 |
|---|---|---|
| §3.2 分层 L0~L4（按约束定义） | L0=`0.ssot` 静态核对；L1=`1.spec/services/invariants.yaml`+上游用例；L2=`contract_matrix.json`（前后端契约矩阵）；L3=`2.tests/l3/golden_path.py`（真实 HTTP） | `5.evidence/runs/*` |
| §3.3 `boundary.yaml` 每用例一份 | `1.spec/boundaries/*.boundary.yaml`（9 份） | `4.gates/lint-boundary/lint.py` 的四项校验 |
| §3.3 "real 必须真起、virtual 必须有来源" | `lint.py` R2 规则：替身必须给 `from_contract` 且路径存在 | `5.evidence/verdicts/lint-boundary.json` |
| §3.4 约束 A：判定权归分类器 | `4.gates/classify-failure/classify.py` 四条判据 S1~S4 | `5.evidence/verdicts/classification.json` |
| §3.4 约束 B：三重门 G1/G2/G3 | G3 由 `mutation-diff` 落地（能杀死变异体）；G1/G2 见 `docs/ai-proposal-gates.md` | `5.evidence/verdicts/mutation.json` |
| §3.5 变异只对改动行、双阈值 | `4.gates/mutation-diff/mutations.yaml`（3 个真实缺陷，各绑定应杀它的用例） | 同上 |
| §4.1 资产 5 类 + SSOT | 六层目录 | 仓根 |
| §4.2 六步闭环 | README「一次完整转动的六步」 | — |
| §4.3 分层门禁 | `4.gates/can-i-deploy/can_i_deploy.py`（含 unknown 分支） | `5.evidence/verdicts/can-i-deploy.json` |
| §4.4 与七层设计链接驳 | `1.spec` 的 `us:` 字段与不变量一一对应，`lint.py` R3 校验 US↔不变量一致 | `lint-boundary.json` |

## 与方案的两处偏差（必须写在明面上）

1. **L2 契约层用的是"前后端契约矩阵"，不是 Pact Broker。**
   理由：InvenTree 的消费方是同仓前端（`ApiEndpoints.tsx`），没有跨团队 broker；
   拉 Pact 进来会变成"为了演示而演示"。矩阵的口径与 can-i-deploy 一致：
   消费方声明 → 提供方操作 → 是否命中。

2. **L4 探索层本次未做。**
   方案 §3.6 明确 L4 要"赚到资格再上"。本次只跑到 L3，L4 留白比硬凑一个假 L4 更诚实。

## 三条不可绕过的判据（写进代码）

- 用例指向的 test id 在被测仓不存在 → lint 直接 BLOCK（防止改名后的静默失效）
- 失败栈所在生产文件的最后改动不属于基线 commit → 判产品缺陷，用例冻结
- 变异体存活 → 说明"用例能过但不能测"，杀灭率不达标即 BLOCK 部署
