"""Task 03: docstring field extraction (Python / JS / TS) — the six-piece
contract's ``docstring`` field.

Drives fixtures through extract -> build -> to_json and asserts the docstring
field contract (ticket 03-field-contract, sections B1-B3):

- verbatim (raw) docstring on function/method/class/module symbol nodes
- >1500 chars: semantic-boundary truncation in the [1350, 1500] window with a
  quantized `` …[+N chars truncated]`` marker (N = original length - cut point)
- <5 chars (after strip) stores null
- precise first-statement rule (Python): only the body's first child statement,
  an ``expression_statement`` containing exactly one plain ``string`` node — no
  concatenation, no f/b/r prefix, no assignment; leading module comments
  (shebang / coding header) are skipped as non-statements
- JS/TS: a JSDoc ``/** … */`` block associates with the function/method/class
  declaration it immediately precedes — export / const-arrow wrappers unwrapped,
  nested ``function`` declarations scoped under their enclosing named scope, and
  ``const a = 1, b = () => 2`` landing on the function-valued declarator
- the existing rationale node/edge behavior is untouched

The build layer has no field whitelist, so the field must land in graph.json
verbatim — asserted by the Seam-2 persist test below. Exact values are the
external contract; a fixture edit that changes them must update these tests
(same discipline as tests/test_extraction_contract.py).
"""

import json
import textwrap
from pathlib import Path

from graphify.build import build_from_json
from graphify.export import to_json
from graphify.extract import extract_js, extract_python

FIXTURES = Path(__file__).parent / "fixtures"
PY_FIXTURE = FIXTURES / "sample_docstrings.py"
TS_FIXTURE = FIXTURES / "sample_docstrings.ts"
JS_FIXTURE = FIXTURES / "sample_docstrings.js"


def _by_label(nodes, label):
    return [n for n in nodes if n["label"] == label]


def _docstrings(result):
    return {n["label"]: n.get("docstring") for n in result["nodes"]}


# ── Seam 1: Python fixture (module/class/method/function + edge cases) ───────


def test_python_module_docstring_on_file_node():
    # The fixture opens with a shebang + `# -*- coding` header; the module
    # docstring is still the first *statement* and must be captured (comments
    # are not statements, PEP 257).
    result = extract_python(PY_FIXTURE)
    file_node = _by_label(result["nodes"], PY_FIXTURE.name)[0]
    assert file_node["docstring"] == (
        "Module docstring: coordinates the nightly settlement reconciliation pipeline."
    )


def test_python_module_docstring_survives_leading_comments(tmp_path):
    """A shebang / coding / plain comment before the module docstring must not
    disqualify it (module-level comments are real children of the module node,
    unlike block bodies)."""
    for head in ("#!/usr/bin/env python3\n",
                 "# -*- coding: utf-8 -*-\n",
                 "# some plain comment\n",
                 "#!/usr/bin/env python3\n# -*- coding: utf-8 -*-\n",
                 ""):
        path = tmp_path / "sample.py"
        path.write_text(head + '"""Module docstring here."""\n\ndef f():\n    pass\n',
                        encoding="utf-8", newline="\n")
        docs = _docstrings(extract_python(path))
        assert docs[path.name] == "Module docstring here.", head


def test_python_class_and_function_docstrings_verbatim():
    docs = _docstrings(extract_python(PY_FIXTURE))
    assert docs["Widget"] == "A widget class that renders itself."
    assert docs[".__init__()"] == "Build the widget instance."
    assert docs[".label()"] == (
        "Decorated property docstring is attached to the method node."
    )
    assert docs["module_fn()"] == "A module-level function with a docstring."


def test_python_multiline_docstring_kept_verbatim():
    """Internal newlines and indentation are preserved (原文存储), not collapsed
    like the rationale label path (which normalizes whitespace to a single space)."""
    docs = _docstrings(extract_python(PY_FIXTURE))
    assert docs[".render()"] == (
        "Render the widget.\n\n        Multi-paragraph docstring, kept verbatim."
    )


def test_python_first_statement_rule_rejects_non_docstrings():
    """Contract B1: only the body's first child statement that is exactly one
    plain string node. Concatenation, f/b/r prefixes, assignment and sub-5-char
    strings all store null."""
    docs = _docstrings(extract_python(PY_FIXTURE))
    assert docs["FirstStatement"] is None            # first statement is a def
    assert docs[".concat()"] is None                 # concatenated_string
    assert docs[".formatted()"] is None              # f-prefix
    assert docs[".assigned()"] is None               # `x = ...` assignment
    assert docs[".short()"] is None                  # "ab" strips to < 5 chars
    assert docs[".local()"] is None                  # first statement is `def inner`


def test_python_local_function_never_a_node_or_field():
    result = extract_python(PY_FIXTURE)
    labels = {n["label"] for n in result["nodes"]}
    assert "inner()" not in labels
    # No docstring field anywhere carries the local function's prose.
    assert not any("inner is never a node" in (n.get("docstring") or "")
                   for n in result["nodes"])


def test_python_rationale_nodes_and_labels_unchanged():
    """The docstring field is a pure addition: the rationale node/edge behavior
    (loose first-string parse, 20-char filter, collapsed labels) is untouched.
    The `Part one.""" """Part two.` label can only be produced by the loose
    ``_get_docstring`` path — proving it still runs and we did not swap it for
    the precise first-statement rule."""
    result = extract_python(PY_FIXTURE)
    rationale = [n for n in result["nodes"] if n.get("file_type") == "rationale"]
    labels = {n["label"] for n in rationale}
    assert "A widget class that renders itself." in labels
    assert "Render the widget. Multi-paragraph docstring, kept verbatim." in labels
    # Loose path still accepts the concatenated/f-prefixed strings as rationale
    # (this is pre-existing behavior, decoupled from the strict docstring field).
    assert 'Part one.""" """Part two.' in labels
    assert "f\"\"\"Formatted {name} docstring, f-prefix disqualifies." in labels
    # All rationale_for edges still target real nodes.
    node_ids = {n["id"] for n in result["nodes"]}
    for edge in result["edges"]:
        if edge.get("relation") == "rationale_for":
            assert edge["target"] in node_ids, edge


# ── Seam 1: TS fixture (JSDoc declaration-preceding association) ─────────────


def test_ts_jsdoc_attaches_to_following_function_and_class():
    docs = _docstrings(extract_js(TS_FIXTURE))
    assert docs["dispatchBatch()"] == "Dispatch a settlement batch for the night run."
    assert docs["WidgetRenderer"] == "Renders a widget into the host DOM."
    assert docs[".constructor()"] == "Build the renderer with a mount point."
    assert docs[".render()"] == "Paint the widget at the current size."
    assert docs[".clear()"] == "Clear the mount point for the next frame."
    assert docs["prepareFrame()"] == "Prepare the canvas for a new frame."


def test_ts_non_jsdoc_comments_not_attached():
    docs = _docstrings(extract_js(TS_FIXTURE))
    assert docs["plainBlock()"] is None   # `/* ... */` block, not `/** ... */`
    assert docs["lineComment()"] is None  # `// ...` line comment


def test_js_jsdoc_attaches_to_following_function_and_class():
    docs = _docstrings(extract_js(JS_FIXTURE))
    assert docs["computeBounds()"] == "Computes the widget's bounding box."
    assert docs["LegacyWidget"] == "A legacy widget class."
    assert docs[".reset()"] == "Reset its internal state."
    assert docs["makeWidget()"] == "Arrow factory for plain widgets."


def test_js_multiline_jsdoc_strips_continuation_stars(tmp_path):
    path = _write_ts(tmp_path, '''
        /**
         * First line of the doc.
         * Second line of the doc.
         */
        export function multiLine(): void {}
    ''')
    docs = _docstrings(extract_js(path))
    assert docs["multiLine()"] == "First line of the doc.\nSecond line of the doc."


def test_ts_jsdoc_before_decorated_export_class_attaches(tmp_path):
    path = _write_ts(tmp_path, '''
        /** A decorated service class. */
        @Injectable()
        export class Service {}
    ''')
    docs = _docstrings(extract_js(path))
    assert docs["Service"] == "A decorated service class."


def test_ts_jsdoc_on_nested_function_in_body_attaches(tmp_path):
    """JSDoc before a ``function`` nested inside another function's body attaches
    to the nested node the engine emits under the enclosing scope (#2653)."""
    path = _write_ts(tmp_path, '''
        export function outer(): void {
          /** Inner helper doc. */
          function helper(): number {
            /** Deepest doc. */
            function deep(): number { return 1; }
            return deep();
          }
          return helper();
        }
    ''')
    docs = _docstrings(extract_js(path))
    assert docs["helper()"] == "Inner helper doc."
    assert docs["deep()"] == "Deepest doc."


def test_ts_jsdoc_on_function_inside_arrow_const_body_attaches(tmp_path):
    """`const Panel = () => { function handleClick(){} }`: the arrow const owns
    the nested function's scope (engine scans the arrow body under the const's
    nid)."""
    path = _write_ts(tmp_path, '''
        const Panel = () => {
          /** Click handler doc. */
          function handleClick(): void {}
        };
    ''')
    docs = _docstrings(extract_js(path))
    assert docs["handleClick()"] == "Click handler doc."


def test_ts_jsdoc_on_function_inside_method_body_attaches(tmp_path):
    path = _write_ts(tmp_path, '''
        class Foo {
          method(): void {
            /** Method-local helper doc. */
            function insideMethod(): void {}
          }
        }
    ''')
    docs = _docstrings(extract_js(path))
    assert docs["insideMethod()"] == "Method-local helper doc."


def test_ts_jsdoc_on_multi_declarator_const_lands_on_function_value(tmp_path):
    """`const a = 1, b = () => 2`: a JSDoc before the declaration lands on the
    function-valued declarator (b), not a dead end on the scalar `a`."""
    path = _write_ts(tmp_path, '''
        /** Combiner doc. */
        const a = 1, b = () => 2;
    ''')
    docs = _docstrings(extract_js(path))
    assert docs["b()"] == "Combiner doc."
    # No `a` node exists (scalar consts are not function nodes).
    assert "a" not in docs or docs["a"] is None


def test_ts_jsdoc_on_body_level_arrow_does_not_shadow_module_symbol(tmp_path):
    """A JSDoc before a body-level ``const x = () => {}`` must not attach to a
    same-named module-level arrow's node (the engine only emits module-level
    declarations, and the id is name-based)."""
    path = _write_ts(tmp_path, '''
        /** Module-level arrow docstring. */
        export const inner = () => 1;

        export function outer(): void {
          /** This documents the LOCAL inner, not the module one. */
          const inner = () => 2;
        }
    ''')
    docs = _docstrings(extract_js(path))
    assert docs["inner()"] == "Module-level arrow docstring."
    assert not any("LOCAL inner" in (n.get("docstring") or "")
                   for n in extract_js(path)["nodes"])


def test_ts_jsdoc_followed_by_non_declaration_not_attached(tmp_path):
    """A JSDoc block whose next sibling is not a function/class (or a
    function-valued const) must not mint a docstring anywhere."""
    path = _write_ts(tmp_path, '''
        /** Standalone prose with a scalar const following. */
        export const LIMIT = 10;
    ''')
    result = extract_js(path)
    assert not any("Standalone prose" in (n.get("docstring") or "")
                   for n in result["nodes"])


# ── Truncation (>1500 chars) + quantized marker (contract B2) ────────────────


def _write_py(tmp_path: Path, code: str) -> Path:
    p = tmp_path / "sample_long_docstring.py"
    # ``newline=""`` keeps LF (fixture discipline) so docstring content is
    # byte-stable regardless of the host OS line ending.
    p.write_text(textwrap.dedent(code), encoding="utf-8", newline="\n")
    return p


def _write_ts(tmp_path: Path, code: str) -> Path:
    p = tmp_path / "sample_long_docstring.ts"
    p.write_text(textwrap.dedent(code), encoding="utf-8", newline="\n")
    return p


def test_python_paragraph_boundary_truncation_with_marker(tmp_path):
    """Over-limit docstring cut at a paragraph boundary (\\n\\n) inside the
    [1350, 1500] window, with the quantized `` …[+N chars truncated]`` marker."""
    content = "a" * 1398 + "\n\n" + "b" * 300  # boundary at 1398, inside window
    path = _write_py(tmp_path, f'''
        def long_doc():
            """{content}"""
            pass
    ''')
    docs = _docstrings(extract_python(path))
    expected = "a" * 1398 + "\n\n" + " …[+300 chars truncated]"
    assert docs["long_doc()"] == expected


def test_python_line_boundary_truncation(tmp_path):
    content = "a" * 1399 + "\n" + "b" * 200  # single \n at 1399, inside window
    path = _write_py(tmp_path, f'''
        def long_doc():
            """{content}"""
            pass
    ''')
    docs = _docstrings(extract_python(path))
    expected = "a" * 1399 + "\n" + " …[+200 chars truncated]"
    assert docs["long_doc()"] == expected


def test_python_sentence_boundary_truncation_cjk_and_ascii(tmp_path):
    for punct, n in (("。", 1400), (".", 1400), ("!", 1400), ("?", 1400)):
        content = "a" * n + punct + " " + "b" * 300
        path = _write_py(tmp_path, f'''
            def long_doc():
                """{content}"""
                pass
        ''')
        docs = _docstrings(extract_python(path))
        # cut after the terminator (before the separating space)
        expected = "a" * n + punct + " …[+{} chars truncated]".format(
            len(content) - (n + 1)
        )
        assert docs["long_doc()"] == expected, punct


def test_python_hard_cut_quantifies_marker(tmp_path):
    """No boundary in the window -> hard cut at 1500, marker quantifies N."""
    content = "a" * 2000
    path = _write_py(tmp_path, f'''
        def long_doc():
            """{content}"""
            pass
    ''')
    docs = _docstrings(extract_python(path))
    assert docs["long_doc()"] == "a" * 1500 + " …[+500 chars truncated]"


def test_python_latest_boundary_in_window_wins(tmp_path):
    """Scan backward from the cap: the latest boundary in the window is chosen,
    so a paragraph break at 1440 beats a line break at 1390."""
    content = "a" * 1389 + "\n" + "b" * 50 + "\n\n" + "c" * 200
    path = _write_py(tmp_path, f'''
        def long_doc():
            """{content}"""
            pass
    ''')
    docs = _docstrings(extract_python(path))
    expected = "a" * 1389 + "\n" + "b" * 50 + "\n\n" + " …[+200 chars truncated]"
    assert docs["long_doc()"] == expected


def test_python_unicode_truncation_never_splits_utf8_char(tmp_path):
    content = "中" * 2000
    path = _write_py(tmp_path, f'''
        def long_doc():
            """{content}"""
            pass
    ''')
    docs = _docstrings(extract_python(path))
    assert docs["long_doc()"] == "中" * 1500 + " …[+500 chars truncated]"


def test_ts_long_jsdoc_truncated_with_marker(tmp_path):
    content = "x" * 1398 + "\n\n" + "y" * 300
    path = _write_ts(tmp_path, f'''
        /**
         * {content}
         */
        export function longDoc(): void {{}}
    ''')
    docs = _docstrings(extract_js(path))
    assert docs["longDoc()"] == "x" * 1398 + "\n\n" + " …[+300 chars truncated]"


def test_short_docstring_both_languages_stores_null(tmp_path):
    path = _write_py(tmp_path, '''
        def foo():
            """ab"""
            pass
    ''')
    assert _docstrings(extract_python(path))["foo()"] is None
    path = _write_ts(tmp_path, '''
        /** ab */
        export function foo(): void {}
    ''')
    assert _docstrings(extract_js(path))["foo()"] is None


# ── Seam 2: extraction -> build -> persist (graph.json node fields) ──────────


def test_docstring_survives_build_and_to_json(tmp_path):
    result = extract_python(PY_FIXTURE)
    G = build_from_json(result)
    render = [nid for nid, a in G.nodes.items() if a.get("label") == ".render()"][0]
    assert G.nodes[render]["docstring"] == (
        "Render the widget.\n\n        Multi-paragraph docstring, kept verbatim."
    )

    out = tmp_path / "graph.json"
    assert to_json(G, {}, str(out)) is True
    data = json.loads(out.read_text(encoding="utf-8"))
    persisted = [n for n in data["nodes"] if n["id"] == render][0]
    assert persisted["docstring"] == G.nodes[render]["docstring"]
    # The module docstring lands on the file node in the persisted graph too.
    file_node = [n for n in data["nodes"] if n["label"] == PY_FIXTURE.name][0]
    assert file_node["docstring"] == (
        "Module docstring: coordinates the nightly settlement reconciliation pipeline."
    )


def test_docstring_extraction_is_repeatable():
    for fn, fixture in ((extract_python, PY_FIXTURE),
                        (extract_js, TS_FIXTURE),
                        (extract_js, JS_FIXTURE)):
        first, second = fn(fixture), fn(fixture)
        assert first["nodes"] == second["nodes"]
        assert first["edges"] == second["edges"]
