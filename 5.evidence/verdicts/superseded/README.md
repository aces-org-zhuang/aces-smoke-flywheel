# 归档：首轮变异体的分类结论

这两个文件是第一轮变异（清单 v1）的分类产物，结论全部为 `unknown`。

归档而不是删除的原因：它们记录了"变异体被归因判据漏掉"这件事本身。
第一轮的 S2 只用失败栈，而 `assertRaises` 类失败的调用栈指向测试文件、不指向生产代码，
所以三个注入缺陷全部落到 S4。这条缺陷的说明与修正见：

- `docs/mutation-equivalence.md`（等价变异体的处置）
- `4.gates/classify-failure/classify.py` 文件头第 3 条说明（S2b 增量归因）
- `5.evidence/verdicts/mutation.json`（修正后的结论：3/3 killed，每条 product_defect=1）

同时归档的还有 v1 清单里两个**等价变异体**（M2-over-allocation / M3-build-double-complete）——
它们因为第二道守卫兜底而没有改变行为，`mutations.yaml` 里保留了完整说明。
