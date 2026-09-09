"""serve-memory 票 03：特性级内存验收（验收 9）——单/多项目 RSS 上限 + 新鲜重挂零重建。

spec: docs/specs/serve-memory-spec.md §Testing 验收 9（:106）+ US1/US5（:27/:35）。

测量策略（自定，稳定性优先）：
- 子进程 server（RSS 归零隔离 pytest 自身驻留，进程面可测量）。
- RSS 测量 OS 分支（用户缺口 1）：win32=PowerShell Get-Process WorkingSet64（现状，
  含共享页上近似）；posix=resource.getrusage(RUSAGE_SELF).ru_maxrss（Linux=KB /
  macOS=bytes，进程峰值上近似）——消除验收 9 CI 空跑。
- 阈值留裕量：合成语料（数百文件/上千节点）实测 RSS ~80MB，250MB/400MB 门槛远高于
  真实工作集——本验收是粗粒度回归守卫（抓灾难级内存 bug：重载不逐出、查询泄漏翻倍），
  精确水位验证归部署后 RSS 探针观察（spec Further Notes 部署条目）。
- 新鲜重挂零重建：子进程内打点 ServeWatcher._run_pipeline/_flush_batch 调用计数——
  所有挂载图均新鲜（刚 rebuild），补齐批次必须真实发生（flush ≥ 1）且门控全部跳过
  （pipeline == 0）。

测量口径注记（用户 Minor 1，口径差异声明）：win32 WorkingSet64 = **当前值**；posix
ru_maxrss = **进程峰值**（含生成语料 + rebuild 峰值，一般 > 当前值）——两平台口径不同属
**声明的已知限制**（不统一：峰值口径更严格，阈值超限属安全侧告警；Linux CI 上 250/400MB
阈值有峰值 flake 风险，如偶发超限先查是否峰值口径所致）。
"""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parent.parent

# 250 文件合成语料 → ~1250 节点 / ~700KB graph / 实测 RSS ~80MB（单项目验收用）
_SINGLE_CORPUS = 250
# 多项目验收用每项目文件数（3 项目 × 150；LRU 逐出下驻留 ≤2 项目，实测 RSS ~82MB）
_MULTI_CORPUS = 150


def _run_probe(payload: str, env: dict, args: "list[str] | None" = None, timeout: int = 300):
    """运行子进程探针，返回 (stdout, stderr, returncode)。PYTHONPATH 注入 worktree 根。"""
    full_env = dict(os.environ)
    full_env["PYTHONPATH"] = str(_WORKTREE) + os.pathsep + os.environ.get("PYTHONPATH", "")
    full_env.update(env)
    cmd = [sys.executable, "-c", textwrap.dedent(payload)]
    if args:
        cmd.extend(args)
    proc = subprocess.run(
        cmd, cwd=str(_WORKTREE), env=full_env,
        capture_output=True, text=True, timeout=timeout,
    )
    return proc.stdout, proc.stderr, proc.returncode


# === 验收 9（单项目）：查询若干轮 + 一次写盘重载后 RSS ≤ 250MB =====================

_SINGLE_PROBE = """
import os, sys
from pathlib import Path
wt = Path(sys.argv[1]); root = Path(sys.argv[2]); n_files = int(sys.argv[3])
sys.path.insert(0, str(wt)); sys.path.insert(0, str(wt / 'scripts'))
import rebuild_entry

def _gen(root, n):
    root.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        body = ''.join(f'def fn_{i}_{j}(x):\\n    return x + {j}\\n' for j in range(4))
        (root / f'mod_{i}.py').write_text(body, encoding='utf-8')

_gen(root, n_files)
out = root / 'graphify-out'
rebuild_entry.rebuild(root)

import graphify.serve as S
server = S._build_server(str(out / 'graph.json'))
for _ in range(5):
    server._graphify_select_graph(str(root))
# 一次写盘重载：touch graph.json（mtime_ns/size 缓存键失配 → evict-before-reload）
os.utime(out / 'graph.json', None)
for _ in range(3):
    server._graphify_select_graph(str(root))

import subprocess as sp
# RSS 测量 OS 分支（用户缺口 1：消除验收 9 CI 空跑）——win32=PowerShell WorkingSet64
# （现状），posix=resource.getrusage ru_maxrss（Linux=KB / macOS=bytes，峰值上近似）
if sys.platform == 'win32':
    r = sp.run(['powershell', '-NoProfile', '-Command',
                f'(Get-Process -Id {os.getpid()}).WorkingSet64'],
               capture_output=True, text=True).stdout.strip()
    rss_mb = round(int(float(r)) / 1024 / 1024, 1)
else:
    import resource as _res
    _r = _res.getrusage(_res.RUSAGE_SELF).ru_maxrss
    rss_mb = round(_r / 1024 / 1024, 1) if sys.platform == 'darwin' else round(_r / 1024, 1)
print('RSS_MB', rss_mb)
print('PROBE_DONE')
"""


def test_memory_single_project_rss_limit(tmp_path):
    """验收 9（单项目）：查询若干轮 + 一次写盘重载后 RSS ≤ 250MB。"""
    root = tmp_path / "proj"
    payload = _SINGLE_PROBE
    stdout, stderr, rc = _run_probe(payload, {}, [str(_WORKTREE), str(root), str(_SINGLE_CORPUS)])
    assert rc == 0, f"探针退出码 {rc}; stderr:\n{stderr[-2000:]}"
    assert "PROBE_DONE" in stdout, f"探针未完成; stdout:\n{stdout[-2000:]}"
    rss = None
    for line in stdout.splitlines():
        if line.startswith("RSS_MB"):
            rss = float(line.split()[1])
    assert rss is not None, f"探针未输出 RSS_MB; stdout:\n{stdout[-2000:]}"
    assert rss > 0, f"RSS 测量失败（-1）; stderr:\n{stderr[-2000:]}"
    assert rss <= 250, f"单项目查询+重载后 RSS {rss}MB > 250MB（水位未受控）"


# === 验收 9（多项目）：5 轮换 ≤ 400MB 且新鲜重挂零重建 =============================

_MULTI_PROBE = """
import os, sys
from pathlib import Path
wt = Path(sys.argv[1]); base = Path(sys.argv[2]); n_files = int(sys.argv[3])
sys.path.insert(0, str(wt)); sys.path.insert(0, str(wt / 'scripts'))
import rebuild_entry
import graphify.serve as S
import graphify.serve_watcher as W

def _gen(root, n):
    root.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        body = ''.join(f'def fn_{i}_{j}(x):\\n    return x + {j}\\n' for j in range(4))
        (root / f'mod_{i}.py').write_text(body, encoding='utf-8')

default = base / 'default'
default.mkdir(parents=True, exist_ok=True)
(default / 'd.py').write_text('def d1():\\n    return 1\\n', encoding='utf-8')
rebuild_entry.rebuild(default)
projs = []
for name in ('a', 'b', 'c'):
    p = base / name
    _gen(p, n_files)
    rebuild_entry.rebuild(p)   # 全部新鲜（source_count + started 写入状态文件）
    projs.append(p)

# 打点 _flush_batch + _run_pipeline：所有挂载图均新鲜 → 补齐批次必须真实发生（flush ≥ 1）
# 且被门控全部跳过（pipeline == 0）——只数 pipeline 的话，watcher 线程未及 flush 时
# pipeline==0 平凡成立（reviewer M4：零重建断言偏弱，补 flush 计数证明批次真实处理过）。
import time as _t
COUNTER = {'flush': 0, 'pipeline': 0}
_orig_flush = W.ServeWatcher._flush_batch
_orig_pipe = W.ServeWatcher._run_pipeline
def _count_flush(self, changed, deleted, **kw):
    COUNTER['flush'] += 1
    return _orig_flush(self, changed, deleted, **kw)
def _count_pipe(self, changed, deleted, semantic_refresh):
    COUNTER['pipeline'] += 1
    return _orig_pipe(self, changed, deleted, semantic_refresh)
W.ServeWatcher._flush_batch = _count_flush
W.ServeWatcher._run_pipeline = _count_pipe

# max_contexts=2：第 3 项目查询逐出 LRU → 重挂 = 新挂载周期（新鲜仍零重建）
os.environ['GRAPHIFY_MAX_CONTEXTS'] = '2'
server = S._build_server(str(default / 'graphify-out' / 'graph.json'), watch=True)
rounds = 5
for _ in range(rounds):
    for p in projs:
        server._graphify_select_graph(str(p))
# 重挂验证：轮换后再次查询 a = 重挂 + 补齐入队 → 门控跳过（零重建）
server._graphify_select_graph(str(projs[0]))
# 等补齐批次真实发生（flush ≥ 1）：避免 watcher 线程未及 flush 时 pipeline==0 平凡成立
_deadline = _t.time() + 30
while COUNTER['flush'] < 1 and _t.time() < _deadline:
    _t.sleep(0.2)

import subprocess as sp
# RSS 测量 OS 分支（用户缺口 1：消除验收 9 CI 空跑）——win32=PowerShell WorkingSet64
# （现状），posix=resource.getrusage ru_maxrss（Linux=KB / macOS=bytes，峰值上近似）
if sys.platform == 'win32':
    r = sp.run(['powershell', '-NoProfile', '-Command',
                f'(Get-Process -Id {os.getpid()}).WorkingSet64'],
               capture_output=True, text=True).stdout.strip()
    rss_mb = round(int(float(r)) / 1024 / 1024, 1)
else:
    import resource as _res
    _r = _res.getrusage(_res.RUSAGE_SELF).ru_maxrss
    rss_mb = round(_r / 1024 / 1024, 1) if sys.platform == 'darwin' else round(_r / 1024, 1)
print('RSS_MB', rss_mb)
print('PIPELINE_COUNT', COUNTER['pipeline'])
print('FLUSH_COUNT', COUNTER['flush'])
print('PROBE_DONE')
"""


def test_memory_multi_project_rotation(tmp_path):
    """验收 9（多项目）：5 轮换后 RSS ≤ 400MB 且新鲜重挂零重建（flush ≥ 1 + pipeline == 0）。"""
    root = tmp_path / "multi"
    payload = _MULTI_PROBE
    env = {"GRAPHIFY_MAX_CONTEXTS": "2"}
    stdout, stderr, rc = _run_probe(payload, env, [str(_WORKTREE), str(root), str(_MULTI_CORPUS)])
    assert rc == 0, f"探针退出码 {rc}; stderr:\n{stderr[-2000:]}"
    assert "PROBE_DONE" in stdout, f"探针未完成; stdout:\n{stdout[-2000:]}"
    rss = None
    pipelines = None
    flushes = None
    for line in stdout.splitlines():
        if line.startswith("RSS_MB"):
            rss = float(line.split()[1])
        if line.startswith("PIPELINE_COUNT"):
            pipelines = int(line.split()[1])
        if line.startswith("FLUSH_COUNT"):
            flushes = int(line.split()[1])
    assert rss is not None and pipelines is not None and flushes is not None, \
        f"探针输出缺失; stdout:\n{stdout[-2000:]}"
    assert rss > 0, f"RSS 测量失败（-1）; stderr:\n{stderr[-2000:]}"
    assert rss <= 400, f"多项目 5 轮换后 RSS {rss}MB > 400MB（水位未受控）"
    assert flushes >= 1, f"补齐批次未真实发生（flush={flushes}，pipeline==0 平凡成立）"
    assert pipelines == 0, f"新鲜重挂触发了 {pipelines} 次重建（门控未生效，零重建违背）"
