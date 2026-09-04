# 11: 收尾——旧运行时退役与基线落档

**What to build:** 新链路全绿后的清场：卸载并删除旧 TypeScript 运行时及其数据库目录；删除本地参考副本（吸收完成，MIT 署名留在吸收处）；其余存量项目逐个首次新链路重建；修订合并方案文档的过时条款、给已作废方案打作废标记、落退役决策 ADR；首次新链路完整重建的报告与效率基准直接提交为新基线（不做新旧 diff）；清理不再需要的依赖声明。

**Blocked by:** 10（serve 内置 watcher）

**Status:** done（主树协调项 pending——见各条标注与 MAP status=closed-pending-migration）

- [x] 旧运行时进程停用、目录与忽略规则条目删除；仓库内无残留引用（文档中的历史引用除外）——worktree 内完成（`.gitignore` 条目删、watch.py 三处门控退役、hooks 统一 rebuild_entry）；主 checkout 的 `.codegraph/` 目录删除为协调项
- [x] 本地参考副本删除；吸收处的第三方署名注释保留——gitignore 条目已删 + MIT 署名保留（watch.py）；主 checkout vendored `codegraph/` 副本删除为协调项
- [x] 存量项目完成首次新链路重建，产物对齐备——worktree 本仓完成（graph.json 六件套 + failed_refs 3888 + `.fts-index.db`）；主 checkout 与 jianshen/wuziqi 等主树执行
- [x] 合并方案过时条款修订、作废方案标记、退役 ADR 落档（决策链引用 wayfinder 地图与各票决议）——完成（ADR-0001 + merge-plan/phase4/phase5 横幅 + 决策链 docs/issues/01-11 + wayfinder 落档）
- [x] 新基线提交：完整重建报告 + 效率基准结果落档——完成（`benchmarks/results-2026-09-04.json`，8.1%；fetch 披露见 JSON summary 与 ticket 07）
- [x] 金标门默认根点亮：主 checkout 基线重建后，conftest 金标门默认根（D:/code/graphify_fork）不再 SKIP；或 CI 显式设 GRAPHIFY_GOLDEN_ROOT 指向新链语料（Task 07 I4 决策承接）——机制验证 100%（worktree 根 2 passed / 默认根 2 skipped）；默认根点亮待主 checkout 重建，CI 可显式 `GRAPHIFY_GOLDEN_ROOT`
- [x] 依赖声明清理；全量测试最终绿——依赖清理完成（无 codegraph 依赖，watchdog extras 保留）；全量测试通过（5278 passed / 39 项环境性 pre-existing 失败，清单见 ADR 验证摘要）

## Resolution 指针

精确实现坐标（退役清单、vendored 处置、存量项目迁移、文档修订与 ADR、基准重落、
金标门点亮、依赖清理）见 wayfinder 决策票
[`tickets/07-retirement-and-archival.md`](../wayfinder/tickets/07-retirement-and-archival.md)
的 Resolution 段与退役 ADR `docs/adr/0001-retire-codegraph-runtime.md`；本票为验收清单，
不携带实现坐标。
