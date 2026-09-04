# 存量退役与归档（收尾）

label: wayfinder:task
status: closed
blocked-by: []

## Question

新链路（03 提取契约 / 04 watcher / 05 FTS 缓存 / 06 工具迁移）实施全绿后的收尾动作清单（task，时点前提 = 新链路验收通过）：

1. **本仓库 `.codegraph/` 退役**：codegraph TS 运行时卸载（watcher/daemon 停用）、目录删除（.gitignore 条目清理）。
2. **vendored `codegraph/` 副本处置**：v1.5.0 参考副本使命完成（03/05/06 已吸收其设计）——删除或归档；MIT 署名保留在吸收处代码注释。
3. **其他 6 个存量项目迁移**：各项目首次新链路 rebuild（extract cache 在，成本≈增量提取）；各自的 graphify-out/ 产物更新。
4. **文档修订与 ADR 落档**：
   - merge-plan v4.2 修订（"codegraph.db 事实源"条款 → 单一事实层模型）
   - phase5 v1.1 作废标记（作废说明指向本图）
   - 退役决策 ADR（难逆转 + 无上下文会惊讶 + 真实权衡三条件齐备；内容源 = MAP Decisions so far + 各票 Resolution）
   - README / how-it-works / skills 文档同步（若产物面有变化）
5. **基准重落**：GRAPH_REPORT 新基线提交（06 Q4：直接替换不 diff）；efficiency benchmark 改打新链路接口后重跑（13.9% 基准对照）。
6. **依赖清理**：pyproject codegraph 相关依赖移除（06 Q4）；check-custom.sh 登记全部新文件（graphify/codegraph_context.py 类比 Phase 4 惯例）。

## Resolution

（2026-09-04，实施票 11 落档）六项全落地：

1. **`.codegraph/` 退役**：`.gitignore` 移除 `.codegraph/` 与 vendored `codegraph/` 条目；
   `graphify/watch.py` 三处 `.codegraph/codegraph.db` 门控退役；hooks（sessionend/
   precompact）移除 `.codegraph` 门控统一路由 `rebuild_entry`。主 checkout 的 `.codegraph/`
   目录由主树迁移删除（worktree 隔离范围外，见退役 ADR §金标门点亮）。
2. **vendored `codegraph/` 副本**：v1.5.0 参考副本仅存在于主 checkout（gitignore 条目已删）；
   MIT 署名保留在吸收处代码注释（`graphify/watch.py` 移植 codegraph 防抖/退避处）。主树删除
   副本为协调项。
3. **存量项目首次新链路重建**：worktree 本仓已完成（rebuild_entry 首次 rebuild，graph.json
   含六件套契约 + failed_refs 3888 条 + `.fts-index.db` 投影）；主 checkout 与 jianshen/
   wuziqi 等存量项目迁移在主树执行（协调项，步骤见退役 ADR）。
4. **文档修订与 ADR**：merge-plan v4.2 加已取代横幅 + 事实源条款作废说明；phase5 v1.1
   作废标记；phase4 v1.15 已取代标记；退役 ADR = `docs/adr/0001-retire-codegraph-runtime.md`。
5. **基准重落**：efficiency benchmark 换源 `.fts-index.db` 重跑，结果 JSON 落档
   `benchmarks/results-2026-09-04.json`（merged 占 grep/read 8.1%，hit@5 merged 0.917）；
   GRAPH_REPORT 由 rebuild 重新生成（graphify-out 派生产物，gitignore）。
6. **依赖清理 + 登记**：pyproject 无 codegraph 依赖（watchdog extras 保留供 serve_watcher）；
   check-custom.sh 全绿（exit 0，无 ✗）。
