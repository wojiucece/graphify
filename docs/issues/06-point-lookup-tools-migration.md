# 06: 点查类工具换源

**What to build:** serve 的符号名片工具（get_node 四档：无/签名/源码/源码+上下文）、新鲜度判定（freshness）、变更符号与热区工具（get_changed_symbols / get_hotspots）全部改从新链路取数：元数据点查走 FTS 缓存的元数据表，符号集合查询走 graph.json 内存索引（按源文件建索引）。源码切片从行范围升级为字节精确切片；旧运行时的消歧后缀回退查询逻辑删除（id 用原生形态）；freshness 从"数据库 WAL 时间戳 vs 事实层"平移为"缓存时间戳 vs 事实层"。

**Blocked by:** 05（FTS 缓存模块）

**Status:** ready-for-agent

- [ ] get_node 四档在新链路语义不变：签名档带新字段、源码档按字节精确切片
- [ ] 无消歧后缀的 id 直接命中；响应中不再出现后缀形态
- [ ] freshness 判定继续输出新鲜/陈旧 verdict（缓存落后于事实层时诚实标注）
- [ ] 变更符号/热区工具输出与旧链路等价（度数单位差异按决议诚实标注）
- [ ] 对应工具的既有 serve 测试在新数据源下全绿
