# 等价变异体：为什么第一轮的 M2/M3 "存活" 不算断言缺口

这份说明保留第一轮的失败结论，因为它是本轮方法论修正的直接来源。

## 现象

第一轮清单（`mutations.yaml` 的 v1）注入了三个缺陷：

| 变异体 | 注入方式 | 结果 |
|---|---|---|
| M1-bom-self-reference | 注释掉 `self.sub_part.check_add_to_bom(...)` | **killed** |
| M2-over-allocation | `if self.quantity > self.item.quantity:` → `* 10` | survived |
| M3-build-double-complete | `if self.incomplete_count > 0:` → `if False and ...` | survived |

按"存活 = 断言缺口"的直觉，这会被报成"超分配和结单守卫没有被测到"。

## 查证

**M2。** `SalesOrderAllocation.clean` 里有两道独立的超分配校验：

- `order/models.py:3286` —— 单行分配量不得超过库存；
- `order/models.py:3303` —— 累计分配量（build + sales + transfer + 本次）不得超过库存。

把第一道放宽十倍，第二道仍然会把同一笔分配拦下来。**可观测行为没有改变。**

而且分配接口只走模型 `clean()`，序列化器不做数量校验，所以不存在"改成校验另一处"的绕法——
这道防线是真正的纵深防御。

**M3。** `build/models.py:1033` 的守卫抛
`Cannot complete build order with incomplete outputs`，
但 `build/serializers.py:799` 在 API 层已经先抛了一次
`Build order has incomplete outputs`。API 用例走到序列化器就被挡住，模型层的第二道守卫永远轮不到它。

同文件 `build/models.py:665` 还有第三处 `incomplete_count > 0`，只 `return False`，也不改变异常行为。

## 结论

**变异体存活只说明"行为没变"，不说明"用例没断言"。**
把等价变异体当作断言缺口上报，是把工具的无能粉饰成代码的问题。

## 处置

两个锚点都换掉，改用没有第二道守卫兜底的位置：

| 新变异体 | 锚点 | 无兜底的依据 |
|---|---|---|
| M2-trackable-fractional-quantity | `part/models.py` `if self.quantity != int(self.quantity):` | 序列化器不做整数校验，模型 `clean()` 是唯一入口 |
| M3-order-reference-pattern | `InvenTree/models.py` `if not InvenTree.format.validate_string(value, pattern):` | 所有单据（SO/PO/BO…）共用的唯一编号模式守卫 |

重跑结果 3/3 全部被杀灭，见 `5.evidence/verdicts/mutation.json`。

## 遗留的可操作结论

"某处校验有第二道守卫"这件事本身应当被登记，否则将来两个守卫同时被误删也无人察觉。
本轮未做（属于 L4 探索层的职责），在此留白而不是硬凑。
