#!/usr/bin/env bash
set -euo pipefail
echo "=== 你的自定义改动（CUSTOM 标记）==="
git grep -l "CUSTOM:" || echo "(无标记改动)"
echo ""
echo "=== 新增文件 ==="
git diff --name-status upstream/v8...HEAD | grep "^A" | awk '{print $2}' || true
echo ""
echo "=== 修改的上游文件 ==="
git diff --name-status upstream/v8...HEAD | grep "^M" | awk '{print $2}' || true
