"""§8 效率基准脚本的纯函数测试（不依赖 DB，可离线跑）.

覆盖 review 补丁的切片/root 解析逻辑（用户 M1/L1 + SDD Minor-4/Imp-1）：
--tasks 显式全量生效（不再封顶 12）；root 解析优先级 --root > env > 本仓根。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "benchmarks"))
import efficiency_benchmark as eb  # noqa: E402


def _fake_golden(n: int) -> list[dict]:
    return [{"q": f"q{i}", "expect": []} for i in range(n)]


def test_select_tasks_explicit_count_not_capped():
    """用户 M1 / SDD Minor-4：--tasks 15 必须返回 15 条（原写法在 n>=12 时封顶 12）. """
    golden = _fake_golden(20)
    assert len(eb._select_tasks(golden, 15)) == 15
    assert len(eb._select_tasks(golden, 12)) == 12
    assert len(eb._select_tasks(golden, 20)) == 20
    assert len(eb._select_tasks(golden, 3)) == 3


def test_resolve_root_explicit_wins(monkeypatch):
    """--root 显式参数最高优先，压过 env 与本仓根."""
    monkeypatch.setenv("GRAPHIFY_GOLDEN_ROOT", r"C:/env-root")
    root, src = eb._resolve_root(r"C:/explicit-root")
    assert root == Path(r"C:/explicit-root").resolve()
    assert src.startswith("explicit")


def test_resolve_root_env_takes_effect(monkeypatch):
    """用户 L1：GRAPHIFY_GOLDEN_ROOT env 生效（golden expect id 依赖 golden 根 DB）."""
    monkeypatch.setenv("GRAPHIFY_GOLDEN_ROOT", r"C:/env-root")
    root, src = eb._resolve_root(None)
    assert root == Path(r"C:/env-root").resolve()
    assert "env" in src


def test_resolve_root_defaults_to_repo_root(monkeypatch):
    """SDD Imp-1：无 env 无 --root → 脚本所在仓根（自指可复现，替代硬编码）. """
    monkeypatch.delenv("GRAPHIFY_GOLDEN_ROOT", raising=False)
    root, src = eb._resolve_root(None)
    assert root == eb._BENCH_ROOT
    assert "repo" in src
