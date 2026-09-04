"""watch.py 路由：代码事件 -> rebuild_entry，不再 build_from_json（退役断言）."""


def test_code_events_route_to_rebuild_not_build(monkeypatch, tmp_path):
    """喂 .py 事件批次 -> _route_flush_batch 调 _trigger_rebuild，不调 build_from_json.
    关键（D2）：直接 patch W.build_from_json（对函数对象设 __call__ 实例属性不拦截 f() 调用，
    走 C 级 tp_call，'build 不被调' 断言恒真，路由写错也测不出）。"""
    import graphify.watch as W
    from pathlib import Path
    called = {}
    monkeypatch.setattr(W, "_trigger_rebuild", lambda root, refresh, skip_sync: called.setdefault("rebuild", True))
    # 直接 patch W.build_from_json（替换整个引用），而非 __call__
    monkeypatch.setattr(W, "build_from_json", lambda *a, **k: called.setdefault("build", True))
    # 调用路由函数（Step 4 暴露为 _route_flush_batch）
    W._route_flush_batch(tmp_path, [Path("src/a.py")])
    assert called.get("rebuild") and not called.get("build")


def test_semantic_files_route_with_refresh(monkeypatch, tmp_path):
    """纯 .md 批次 -> _route_flush_batch 路由 _trigger_rebuild(skip_sync=True, refresh=[该文件])."""
    import graphify.watch as W
    from pathlib import Path
    captured = {}
    monkeypatch.setattr(W, "_trigger_rebuild", lambda root, refresh, skip_sync: captured.update(refresh=refresh, skip=skip_sync))
    W._route_flush_batch(tmp_path, [Path("docs/x.md")])
    assert captured.get("skip") is True and captured.get("refresh") == [Path("docs/x.md")]


def test_hook_scripts_reference_rebuild_entry():
    """静态防漂移：两个 .sh 含 'rebuild_entry.py'."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    for sh in ["scripts/sessionend-graphify-update.sh", "scripts/precompact-graphify-update.sh"]:
        assert "rebuild_entry.py" in (root / sh).read_text(encoding="utf-8"), f"{sh} 未改指 rebuild_entry"


def test_no_codegraph_gate_remains():
    """Task 11 收尾锁：codegraph 运行时已退役——watch.py 不得再直查 codegraph.db 路径，
    两个 hook 不得再以 .codegraph 目录判别路由（旧 Phase 3 拓扑切换门控已删，统一路由
    rebuild_entry）。静态防漂移：若未来误恢复 codegraph 门控，本测试立即红。
    注：注释中的历史提及（"codegraph 已退役"叙述）不触发——只锁可执行的运行时依赖形态。"""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    watch_src = (root / "graphify" / "watch.py").read_text(encoding="utf-8")
    assert "codegraph.db" not in watch_src, "watch.py 仍直查 codegraph.db（运行时已退役）"
    for sh in ["scripts/sessionend-graphify-update.sh", "scripts/precompact-graphify-update.sh"]:
        text = (root / sh).read_text(encoding="utf-8")
        assert ".codegraph" not in text, f"{sh} 仍以 .codegraph 目录判别路由（应统一 rebuild_entry）"
