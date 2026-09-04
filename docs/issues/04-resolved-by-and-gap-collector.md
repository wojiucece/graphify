# 04: resolved_by 打点 + raw_calls 失败收集器

**What to build:** 两个提取层信号：(a) 跨文件调用解析成功时，各语言解析器按解析路径给边打 resolved_by 属性（类型注解驱动→qualified-name、实例方法→instance-method、启发式→fuzzy）；(b) 跨文件解析循环中解析失败的调用不再静默丢弃，收集为结构化失败引用信号（来源节点、被调名、行号、文件），作为知识缺口的原生来源。

**Blocked by:** None (can start immediately, 与 01 并行).

**Status:** ready-for-agent

- [ ] 类型注解驱动的调用边带 resolved_by="qualified-name"；实例方法边带 "instance-method"；启发式边带 "fuzzy"
- [ ] 解析失败引用以结构化形态（来源节点/被调名/行号/文件）可从提取产物取回
- [ ] 失败收集不改变既有解析行为与边集（只增信号，不改语义）
- [ ] 既有解析相关测试无回归

## Resolution 指针

精确实现坐标（resolved_by 三映射 static-type→qualified-name / instance-method /
heuristic→fuzzy、raw_calls 失败收集器）见 wayfinder 决策票
[`tickets/03-field-contract.md`](../wayfinder/tickets/03-field-contract.md)（resolved_by）与
[`tickets/06-tool-surface-migration.md`](../wayfinder/tickets/06-tool-surface-migration.md)
（gap 通道换源语义）的 Resolution 段；本票为验收清单，不携带实现坐标。
