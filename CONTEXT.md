# CONTEXT — 领域术语表

## 事实层（Fact Layer）

graph.json 与 extraction 原生产物。仓库符号的唯一持久真相来源；任何派生物（FTS 缓存、报告、wiki）均可由它确定性重建。不引入第二个持久事实源。

## FTS 缓存（FTS Cache）

落盘的 sqlite FTS5 索引文件。从事实层派生，架构地位是可重建缓存而非事实源：删除无损，指纹失效自动重建。

## 提取深度（Extraction Depth）

提取产物携带的符号细节水平：signature（签名）、docstring（文档注释摘要）、列定位（行列而非仅行号）。深度不足是 FTS 检索面的前置缺口，与存储形态无关。

## 守护进程（Daemon）

保持索引新鲜的常驻机制。本项目定性为 serve 进程内置的 watcher（非独立进程）：文件监听 → 增量提取 → 更新事实层 → 重投影 FTS 缓存，单进程内串行。

## 符号寻址（Symbol Addressing)

以唯一 ID + 源码定位精确指向符号的能力。graphify 形态：kind+内容 hash 的 ID（如 `class:3610...`）+ source_file + source_location。
