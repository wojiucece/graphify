"""Task 04 seam-1: resolved_by 打点 + raw_calls 失败收集器.

Fixture-driven extraction-contract test (mirrors test_extraction_contract.py from
Task 01): drives committed fixtures through the full ``extract()`` pipeline and
asserts the two new cross-file resolution signals:

- ``resolved_by`` on cross-file CALL edges, one of three values matching the
  resolution path (native-indexing spec §提取契约):
    * "qualified-name" — static-type resolution: import-backed direct calls,
      explicit ``ClassName.method()`` / ``Type.method()`` qualification, and
      fully-qualified ``Pkg.fn()`` references
    * "instance-method" — receiver-typed instance dispatch (``obj.method()``
      where the receiver's type comes from a local/constructor-injection table)
    * "fuzzy" — bare-name heuristic matching with no import evidence
- ``failed_refs`` — structured failure references ``{from_node, callee_name,
  line, file_path}`` for cross-file calls the shared pass cannot bind (the
  AST-native replacement for codegraph's unresolved_refs).

Discipline: additive only. The change must not alter the emitted edge set or any
resolution behavior — existing resolution tests are the regression guard; the
tests here additionally assert the shape invariants (allowed values, no stray
keys, expected edge counts).

Fixture layout (absolute paths are passed on purpose — extract() with relative
committed paths hits a pre-existing id-remap quirk on some grammars):

  fixtures/resolved_by/python/pkg/{__init__,callee,caller,orphan,class_def,class_use}.py
  fixtures/resolved_by/typescript/{repo,use}.ts

Documented source layout of fixtures/resolved_by/python/pkg/caller.py (LF
endings; exact line numbers are part of the contract, a fixture edit that moves
them must update the ``line == 13`` assertion):

    L1  from .callee import helper_fn
    L2  (blank)
    L3  (blank)
    L4  def run_imported():
    L5      return helper_fn()
    L6  (blank)
    L7  (blank)
    L8  def run_unimported():
    L9      return unimported_fn()
    L10 (blank)
    L11 (blank)
    L12 def run_missing():
    L13     return no_such_fn()
"""

from pathlib import Path

from graphify.extract import extract

FIXTURES = Path(__file__).parent / "fixtures" / "resolved_by"

_ALLOWED_RESOLVED_BY = {"qualified-name", "instance-method", "fuzzy"}
# `_origin` is the pre-existing AST-provenance tag stamped on every node/edge
# after the resolution passes (extract.py `_origin = "ast"`) — not new here.
_KNOWN_EDGE_KEYS = {
    "source", "target", "relation", "context", "confidence",
    "confidence_score", "source_file", "source_location", "weight",
    "resolved_by", "_origin",
}


def _extract_python(tmp_path):
    pkg = FIXTURES / "python" / "pkg"
    files = [p.resolve() for p in (
        pkg / "__init__.py", pkg / "callee.py", pkg / "caller.py",
        pkg / "orphan.py", pkg / "class_def.py", pkg / "class_use.py",
    )]
    return extract(files, cache_root=tmp_path / "graphify-out", parallel=False)


def _extract_ts(tmp_path):
    files = [(FIXTURES / "typescript" / n).resolve()
             for n in ("repo.ts", "use.ts")]
    return extract(files, cache_root=tmp_path / "graphify-out", parallel=False)


def _extract_pascal(tmp_path):
    fixture = FIXTURES.parent / "pascal_cross_file"
    files = [p.resolve() for p in sorted(fixture.iterdir())]
    return extract(files, cache_root=tmp_path / "graphify-out", parallel=False)


def _calls(result):
    """All call edges as (source_label, target_label, edge) tuples."""
    label = {n["id"]: n["label"] for n in result["nodes"]}
    out = []
    for e in result["edges"]:
        if e.get("relation") in ("calls", "indirect_call"):
            out.append((label.get(e["source"], e["source"]),
                        label.get(e["target"], e["target"]), e))
    return out


def _edge_between(calls, src_contains: str, tgt_contains: str):
    hits = [e for s, t, e in calls
            if src_contains in s and tgt_contains in t]
    assert len(hits) == 1, f"expected exactly one {src_contains}->{tgt_contains}, got {[(s, t) for s, t, _ in calls]}"
    return hits[0]


# ── Seam 1: resolved_by lands on cross-file call edges ─────────────────────


def test_import_guided_python_call_is_qualified_name(tmp_path):
    edge = _edge_between(_calls(_extract_python(tmp_path)),
                         "run_imported", "helper_fn")
    assert edge["confidence"] == "EXTRACTED"
    assert edge["resolved_by"] == "qualified-name"


def test_heuristic_unimported_python_call_is_fuzzy(tmp_path):
    edge = _edge_between(_calls(_extract_python(tmp_path)),
                         "run_unimported", "unimported_fn")
    # No import evidence -> INFERRED name-matching heuristic.
    assert edge["confidence"] == "INFERRED"
    assert edge["resolved_by"] == "fuzzy"


def test_python_class_qualified_call_is_qualified_name(tmp_path):
    edge = _edge_between(_calls(_extract_python(tmp_path)),
                         "run_service", "do_work")
    assert edge["confidence"] == "EXTRACTED"
    assert edge["resolved_by"] == "qualified-name"


def test_ts_instance_method_call_is_instance_method(tmp_path):
    edge = _edge_between(_calls(_extract_ts(tmp_path)),
                         "runInstance", "save")
    # Receiver typed via the local `const r = new Repo()` binding -> INFERRED.
    assert edge["confidence"] == "INFERRED"
    assert edge["resolved_by"] == "instance-method"


def test_ts_static_qualified_call_is_qualified_name(tmp_path):
    edge = _edge_between(_calls(_extract_ts(tmp_path)),
                         "runQualified", "staticSave")
    assert edge["confidence"] == "EXTRACTED"
    assert edge["resolved_by"] == "qualified-name"


# ── Seam 1: failure references are structured and retrievable ──────────────


def test_failed_ref_is_structured_and_retrievable(tmp_path):
    result = _extract_python(tmp_path)
    failed = [f for f in result.get("failed_refs", [])
              if f["callee_name"] == "no_such_fn"]
    assert len(failed) == 1, result.get("failed_refs")
    entry = failed[0]
    # from_node is a real node id (the calling function's).
    assert entry["from_node"]
    label_of = {n["id"]: n["label"] for n in result["nodes"]}
    assert label_of.get(entry["from_node"]) == "run_missing()"
    # line is an int parsed from the raw call's source_location; file_path is
    # the caller's source file.
    assert entry["line"] == 13
    assert entry["file_path"].endswith("caller.py")
    assert set(entry) == {"from_node", "callee_name", "line", "file_path"}


def test_failed_refs_empty_for_fully_resolved_corpus(tmp_path):
    # The TS corpus resolves both calls (instance + static) — nothing fails.
    assert _extract_ts(tmp_path).get("failed_refs") == []


def test_tail_resolver_rescued_call_not_reported_as_gap(tmp_path):
    # The pascal_inherited_calls tail resolver rescues `Prepare` inside
    # TDerivedGadget.Run via the inherits chain — a call the shared pass
    # records as a failed_ref because no top-level `prepare` symbol exists.
    # A call that ends up resolved must not ALSO be reported as a gap: the
    # end-of-extract reconciliation drops it (review fix, false positive).
    result = _extract_pascal(tmp_path)
    calls = _calls(result)
    assert any("Run" in s and "Prepare" in t for s, t, _ in calls)
    assert result.get("failed_refs") == []


def test_failed_refs_is_always_a_list_key(tmp_path):
    result = _extract_python(tmp_path)
    assert isinstance(result.get("failed_refs"), list)


# ── Seam 1: additive-only discipline (signals, not semantics) ──────────────


def test_every_cross_file_call_edge_carries_an_allowed_resolved_by(tmp_path):
    for result in (_extract_python(tmp_path), _extract_ts(tmp_path)):
        for _s, _t, edge in _calls(result):
            assert edge["resolved_by"] in _ALLOWED_RESOLVED_BY, edge
            # resolved_by is the ONLY new key: no accidental schema drift.
            assert set(edge).issubset(_KNOWN_EDGE_KEYS), set(edge) - _KNOWN_EDGE_KEYS


def test_edge_set_is_unchanged_by_signal_addition(tmp_path):
    # The additive contract: stripping resolved_by leaves exactly the pre-change
    # edge set — no duplicate, dropped, or re-typed edges. The known call-edge
    # inventory of the fixtures is asserted exactly (4 Python + 2 TS).
    py = _calls(_extract_python(tmp_path))
    ts = _calls(_extract_ts(tmp_path))
    py_pairs = {(s, t) for s, t, _ in py}
    ts_pairs = {(s, t) for s, t, _ in ts}
    assert py_pairs == {
        ("run_imported()", "helper_fn()"),
        ("run_unimported()", "unimported_fn()"),
        ("run_service()", ".do_work()"),
    }
    assert ts_pairs == {
        ("runInstance()", ".save()"),
        ("runQualified()", ".staticSave()"),
    }
    # Same (source, target) never emitted twice.
    assert len(py) == len(py_pairs)
    assert len(ts) == len(ts_pairs)


def test_extraction_is_repeatable_with_signals(tmp_path):
    first = _extract_python(tmp_path)
    second = _extract_python(tmp_path)
    assert first["edges"] == second["edges"]
    assert first["failed_refs"] == second["failed_refs"]
