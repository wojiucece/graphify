"""Task 01 tracer bullet: Python extraction-field contract (five of six).

Drives a Python fixture through the whole chain — extract -> build -> to_json
persist — and asserts the five non-docstring contract fields:

- ``signature`` on function/method nodes (params + return annotation, name
  excluded; constructor kept as its own class-attached node)
- ``qualified_name`` as ``Class::method`` (module-level symbols stay bare,
  nested classes chain, local functions are not extracted at all)
- ``source_location`` as ``L<line>:C<col>`` on symbol nodes (file node stays
  ``L1``, edges stay line-only)
- ``end_line`` / ``end_byte`` as top-level integers on symbol nodes

The build layer has no field whitelist, so every new top-level field must land
in graph.json verbatim — asserted by the Seam-2 persist test below.
"""

import json
from pathlib import Path

from graphify.build import build_from_json
from graphify.export import to_json
from graphify.extract import extract_python

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE = FIXTURES / "sample_native_fields.py"

# Documented source layout of the fixture (LF line endings). Exact values are
# the external contract; a fixture edit that moves these lines must update them.
FILE_NODE_ID_SUFFIX = "sample_native_fields_py"


def _extract():
    return extract_python(FIXTURE)


def _by_label(nodes, label):
    return [n for n in nodes if n["label"] == label]


def _symbol_nodes(nodes):
    """All nodes except the file node (the file node is the L1 exemption)."""
    return [n for n in nodes if n["label"] != FIXTURE.name]


# ── Seam 1: extraction dict (fixture-driven) ──────────────────────────────


def test_module_function_signature_excludes_name():
    n = _by_label(_extract()["nodes"], "module_fn()")[0]
    assert n["signature"] == '(a: int, b: str = "x") -> bool'
    assert "module_fn" not in n["signature"]  # the name never enters the signature


def test_init_is_own_node_with_signature_and_class_association():
    result = _extract()
    init = _by_label(result["nodes"], ".__init__()")
    assert len(init) == 1
    assert init[0]["signature"] == "(name: str, age: int = 0)"  # self dropped
    # The constructor is its own node associated with its class (method edge).
    assert any(e["relation"] == "method" and e["target"] == init[0]["id"]
               for e in result["edges"])


def test_method_signature_is_call_shape_without_self():
    n = _by_label(_extract()["nodes"], ".fetch()")[0]
    assert n["signature"] == "(limit: int = 10) -> list"


def test_decorated_method_keeps_signature_and_class_link():
    result = _extract()
    n = _by_label(result["nodes"], ".display_name()")[0]
    assert n["signature"] == "() -> str"
    assert n["qualified_name"] == "UserService::display_name"


def test_qualified_name_shapes():
    qn = {n["label"]: n.get("qualified_name") for n in _extract()["nodes"]}
    assert qn["module_fn()"] == "module_fn"                 # module-level bare
    assert qn["UserService"] == "UserService"
    assert qn[".__init__()"] == "UserService::__init__"
    assert qn[".fetch()"] == "UserService::fetch"
    assert qn["Nested"] == "UserService::Nested"            # nested class chains
    assert qn[".inner()"] == "UserService::Nested::inner"
    assert qn["Empty"] == "Empty"


def test_local_function_is_not_extracted():
    # `helper` is defined inside `fetch`; local functions never become nodes.
    labels = [n["label"] for n in _extract()["nodes"]]
    assert "helper()" not in labels


def test_source_location_symbol_vs_file_vs_edge():
    result = _extract()
    file_node = _by_label(result["nodes"], FIXTURE.name)[0]
    assert file_node["source_location"] == "L1"  # file node exemption
    for n in _symbol_nodes(result["nodes"]):
        loc = n["source_location"]
        assert loc.startswith("L") and ":C" in loc  # symbols: L<line>:C<col>
    for e in result["edges"]:
        assert ":" not in e["source_location"]  # edges stay line-only


def test_symbol_nodes_carry_end_line_and_end_byte_integers():
    for n in _symbol_nodes(_extract()["nodes"]):
        assert isinstance(n.get("end_line"), int), n["label"]
        assert isinstance(n.get("end_byte"), int), n["label"]
        assert n["end_line"] >= 1
        assert n["end_byte"] >= 0


def test_source_location_column_is_byte_offset_not_character():
    """C column is the 1-based BYTE offset of the symbol start (same byte
    coordinate system as end_byte), NOT the editor's character column."""
    result = _extract()
    n = _by_label(result["nodes"], ".after_cjk()")[0]
    lines = FIXTURE.read_text(encoding="utf-8").splitlines()
    line_idx = next(i for i, l in enumerate(lines) if "def after_cjk" in l)
    line = lines[line_idx]
    # `def` sits after the multibyte 中文属性 = "值"; on the same line, so its
    # byte offset (27) differs from the character offset (17).
    byte_col = len(line.encode("utf-8").split(b"def after_cjk")[0]) + 1
    char_col = len(line.split("def after_cjk")[0]) + 1
    assert byte_col != char_col  # fixture must actually exercise the distinction
    assert n["source_location"] == f"L{line_idx + 1}:C{byte_col}"
    assert f":C{char_col}" not in n["source_location"]
    assert n["qualified_name"] == "Cjk::after_cjk"


def test_file_node_keeps_no_contract_fields():
    f = _by_label(_extract()["nodes"], FIXTURE.name)[0]
    for field in ("signature", "qualified_name", "end_line", "end_byte"):
        assert field not in f, f"file node must not carry {field}"


# ── Seam 2: extraction -> build -> persist (graph.json node fields) ───────


def test_contract_fields_survive_build_and_to_json(tmp_path):
    result = _extract()
    G = build_from_json(result)
    fetch = [nid for nid in G.nodes if nid.endswith("userservice_fetch")][0]
    attrs = G.nodes[fetch]
    assert attrs["signature"] == "(limit: int = 10) -> list"
    assert attrs["qualified_name"] == "UserService::fetch"
    assert attrs["source_location"] == "L9:C5"
    assert attrs["end_line"] == 12
    assert attrs["end_byte"] == 275

    out = tmp_path / "graph.json"
    assert to_json(G, {}, str(out)) is True
    data = json.loads(out.read_text(encoding="utf-8"))
    persisted = [n for n in data["nodes"] if n["id"] == fetch][0]
    for field in ("signature", "qualified_name", "source_location",
                  "end_line", "end_byte"):
        assert persisted[field] == attrs[field], field


def test_extraction_is_repeatable():
    # Same input -> identical extraction dict (nodes/edges byte-for-byte).
    first, second = _extract(), _extract()
    assert first["nodes"] == second["nodes"]
    assert first["edges"] == second["edges"]
