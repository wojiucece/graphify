# 08: dispatch 概念退役

**What to build:** 删除动态分发标注的旧实现（数据库元数据 JOIN 的双副本、响应中的 dispatch_candidate 字段、对应金标测试），其语义由边属性原生承载：多态 fanout 判断改为读边置信度（INFERRED/AMBIGUOUS 保留信号）与 04 产出的 resolved_by 属性。邻接与影响面工具（get_neighbors / blast_radius）的响应结构随之变化，工具描述诚实标注语义来源变化（"外部解析器判定"→"提取期原生推断"）。

**Blocked by:** 04（resolved_by 打点——语义承接需要它在）、05（FTS 缓存模块——邻接工具查询面已换源）

**Status:** ready-for-agent

- [ ] 旧分发标注实现与金标测试全部删除，无残留调用点
- [ ] 邻接/影响面工具响应不再含 dispatch_candidate 字段，描述含语义变化标注
- [ ] 边置信度与 resolved_by 可从响应中读出（多态 fanout 判断能力保留）
- [ ] 相关工具测试改写为新语义并通过

## Resolution 指针

精确实现坐标（dispatch 独立概念退役、语义由边属性 confidence + resolved_by 吸收、
`_edge_dispatch_info` 双副本与金标删除）见 wayfinder 决策票
[`tickets/06-tool-surface-migration.md`](../wayfinder/tickets/06-tool-surface-migration.md)
的 Resolution 段；本票为验收清单，不携带实现坐标。
