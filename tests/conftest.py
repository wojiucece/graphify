from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture(scope="session")
def _can_symlink() -> bool:
    """Whether this machine can create symlinks at all (#2642).

    Probed rather than inferred from ``sys.platform``: Windows *can* create
    symlinks from an elevated shell or with Developer Mode enabled, and those
    runs should still get the coverage. A plain non-elevated Windows shell
    raises ``OSError: [WinError 1314] A required privilege is not held by the
    client``, which pytest reports as a FAILURE — 15 of them, drowning out real
    defects — when what it means is "unsupported here".

    One file symlink is enough to probe: Windows gates file and directory
    symlinks behind the same ``SeCreateSymbolicLinkPrivilege`` check.
    """
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "probe-src"
        src.write_text("x", encoding="utf-8")
        try:
            (Path(d) / "probe-link").symlink_to(src)
        except (OSError, NotImplementedError):
            return False
        return True


@pytest.fixture
def requires_symlinks(_can_symlink) -> None:
    """Skip a test that must create symlinks when the platform won't allow it.

    Take this as a parameter rather than wrapping each ``symlink_to()`` call in
    try/except: the guard then sits in the signature where it is visible, and
    an OSError from the code UNDER test is still a real failure instead of
    being swallowed into a skip.
    """
    if not _can_symlink:
        pytest.skip(
            "symlink creation unavailable on this machine "
            "(Windows requires an elevated shell or Developer Mode)"
        )


@pytest.fixture(autouse=True)
def _sandbox_home(tmp_path_factory, monkeypatch):
    """Every test gets a throwaway HOME so installers/uninstallers can never
    touch the developer's real ~/.claude, ~/.gemini, ~/.codebuddy, ~/.copilot,
    ~/.config, ~/.agents (issue #2168).

    Allocated via tmp_path_factory (not inside tmp_path) so tests that assert
    the exact contents of their own tmp_path are unaffected."""
    home = tmp_path_factory.mktemp("sandbox-home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))              # Windows ntpath.expanduser
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)     # escape hatch that bypasses Path.home
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home

_ANALYZE_WARNING_FILTERS = (
    "ignore:Tensorflow not installed; ParametricUMAP will be unavailable:ImportWarning:umap",
    "ignore:Please import `random` from the `scipy\\.sparse` namespace.*:"
    "DeprecationWarning:hyppo\\.independence\\.hhg",
    "ignore:The keyword argument 'nopython=False' was supplied.*:Warning:numba\\.core\\.decorators",
)


def pytest_collection_modifyitems(items: list[Any]) -> None:
    for item in items:
        if item.path.name != "test_analyze.py":
            continue
        for warning_filter in _ANALYZE_WARNING_FILTERS:
            item.add_marker(pytest.mark.filterwarnings(warning_filter))


import sys
from pathlib import Path
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

# === CUSTOM: B1 金标门（M3）====================================================
# 金标集（tests/fixtures/ranked_golden.json）是只读质量闸门，依赖真实事实层
# graph.json（新链路，含 failed_refs 供 gap 型查询）——事实层缺失时整体跳过
# （'skipped (golden)' 进测试摘要，不静默消失）。默认根保留本机 D:/code/graphify_fork
# 可跑；GRAPHIFY_GOLDEN_ROOT 环境变量覆盖（CI/其他环境指向已重建事实层的 fork）。
# `-m 'not golden'` 可整组排除金标闸门。
_GOLDEN_DEFAULT_ROOT = r"D:/code/graphify_fork"


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "golden: B1 只读金标集质量闸门（通过率≥95%/no-degrade），依赖真实事实层 graph.json；"
        "事实层缺失时以 'skipped (golden)' 跳过，`-m 'not golden'` 可整组排除"
        "（金标根默认 D:/code/graphify_fork，GRAPHIFY_GOLDEN_ROOT 覆盖）",
    )


@pytest.fixture(autouse=True)
def _golden_gate(request) -> None:
    """金标门：带 golden marker 的测试在真实新链路事实层缺失时显式跳过。

    原来在测试体内 `if not exists: pytest.skip` 硬编码 D:/code/graphify_fork——
    质量闸门在其他环境静默消失且无 marker 可筛选。现在：marker 注册（pytest_configure
    上面）+ env 覆盖 + 统一 gate，'skipped (golden)' 在测试摘要可见。
    07 票：事实层还须是"新链路"形态（graph.json 顶层含 failed_refs——Task 04 失败
    收集器持久化接线）——旧链路（codegraph 适配器产出的 function:md5 id 图）没有
    failed_refs，gap 型查询与 path-id expect 全部失配，跳过而非失败（'skipped (golden)'
    提示换根）。
    """
    if request.node.get_closest_marker("golden"):
        root = Path(os.environ.get("GRAPHIFY_GOLDEN_ROOT", _GOLDEN_DEFAULT_ROOT))
        graph_json = root / "graphify-out" / "graph.json"
        if not graph_json.exists():
            pytest.skip(
                f"skipped (golden): GRAPHIFY_GOLDEN_ROOT={root} 无 graphify-out/graph.json"
            )
        try:
            import json as _json
            _data = _json.loads(graph_json.read_text(encoding="utf-8"))
        except Exception:
            pytest.skip(
                f"skipped (golden): GRAPHIFY_GOLDEN_ROOT={root} 的 graph.json 不可解析"
            )
        if "failed_refs" not in _data:
            pytest.skip(
                f"skipped (golden): GRAPHIFY_GOLDEN_ROOT={root} 是旧链路数据 "
                f"（graph.json 无 failed_refs），金标需新链路 graph.json——"
                f"设 GRAPHIFY_GOLDEN_ROOT 指向新链路重建的项目"
            )
