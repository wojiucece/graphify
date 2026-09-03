"""Task 02: extraction-depth contract across the 15 LanguageConfigs.

Extends the Task 01 (Python-only) six-piece field contract to the generic
extractor's 15 language configs. Seam 1, fixture-driven (extraction dict
assertions), covers per language:

- an exact function/method signature assertion (params + return, name excluded)
- ``qualified_name`` as ``Class::method``
- ``source_location`` as ``L<line>:C<col>`` on symbol nodes
- ``end_line`` / ``end_byte`` integers on symbol nodes

Variable/constant/field signatures (JS/TS module consts, C++ data members, C#
auto-properties) follow the three contract rules: has ``=`` -> flattened RHS
head; type-only / bare -> no signature field; token-boundary truncation with an
explicit `` …`` marker, plus a <5-char noise filter.

Exact values are the external contract; a fixture edit that moves these lines
must update them (same discipline as tests/test_extraction_contract.py).
"""

from pathlib import Path

from graphify.extract import (
    extract_c, extract_cpp, extract_csharp, extract_groovy, extract_java,
    extract_js, extract_kotlin, extract_lua, extract_php, extract_python,
    extract_ruby, extract_scala, extract_swift,
)

FIXTURES = Path(__file__).parent / "fixtures"

# Language config -> (extractor, fixture) driving the per-language assertions.
_LANGS = {
    "python": (extract_python, "sample.py"),
    "js": (extract_js, "cjs_require.js"),
    "ts": (extract_js, "sample.ts"),
    "tsx": (extract_js, "sample.tsx"),
    "java": (extract_java, "sample.java"),
    "groovy": (extract_groovy, "sample.groovy"),
    "c": (extract_c, "sample.c"),
    "cpp": (extract_cpp, "sample.cpp"),
    "ruby": (extract_ruby, "sample.rb"),
    "csharp": (extract_csharp, "sample.cs"),
    "kotlin": (extract_kotlin, "sample.kt"),
    "scala": (extract_scala, "sample.scala"),
    "php": (extract_php, "sample.php"),
    "lua": (extract_lua, "sample.luau"),
    "swift": (extract_swift, "sample.swift"),
}

# One sampled symbol per language: (label, signature, qualified_name, L:C).
# `label` is the node label as the extractor emits it; signature is the exact
# contract value; qualified_name the scope-chain; loc the exact start position.
_SAMPLED = {
    "python": (".forward()", "(x)", "Transformer::forward", "L5:C5"),
    "js": ("runDispatch()", "()", "runDispatch", "L5:C1"),
    "ts": (".get()", "(path: string) -> Promise<Response>",
           "HttpClient::get", "L10:C5"),
    "tsx": ("fmtDate()", "(d: Date) -> string", "fmtDate", "L1:C1"),
    "java": (".addItem()", "(String item) -> void",
             "DataProcessor::addItem", "L15:C5"),
    "groovy": (".process()", "(String input) -> String",
               "SampleService::process", "L13:C5"),
    "c": ("validate()", "(const char *input) -> int", "validate", "L12:C1"),
    "cpp": (".get()", "(const std::string& path) -> std::string",
            "HttpClient::get", "L9:C5"),
    "ruby": (".fetch()", "(path, method)", "ApiClient::fetch", "L19:C3"),
    "csharp": (".Process()", "(List<string> items) -> List<string>",
               "IProcessor::Process", "L9:C9"),
    "kotlin": (".get()", "(path: String) -> String", "HttpClient::get", "L7:C5"),
    "scala": (".get()", "(path: String) -> String", "HttpClient::get", "L12:C3"),
    "php": (".get()", "(string $path) -> string", "ApiClient::get", "L19:C5"),
    "lua": ("main()", "()", "main", "L28:C1"),
    "swift": (".process()", "() -> [String]", "DataProcessor::process", "L28:C5"),
}

# A class-like node per language for the class-depth assertion (None where the
# fixture defines no class).
_CLASS_SAMPLED = {
    "python": "Transformer", "js": None, "ts": "HttpClient", "tsx": None,
    "java": "DataProcessor", "groovy": "SampleService", "c": None,
    "cpp": "HttpClient", "ruby": "ApiClient", "csharp": "DataProcessor",
    "kotlin": "HttpClient", "scala": "HttpClient", "php": "ApiClient",
    "lua": None, "swift": "DataProcessor",
}


def _extract(lang: str):
    fn, fixture = _LANGS[lang]
    return fn(FIXTURES / fixture)


def _node_by_label(result, label):
    for n in result["nodes"]:
        if n["label"] == label:
            return n
    raise AssertionError(f"missing node label {label!r}")


def _symbol_nodes(result, fixture_name):
    return [n for n in result["nodes"] if n["label"] != fixture_name]


# ── Per-language function/method signature + depth-field assertions ─────────


def test_every_language_carries_sampled_signature_and_depth_fields():
    """Each of the 15 configs: exact signature + qualified_name + L:C + end_*."""
    for lang, (label, sig, qn, loc) in _SAMPLED.items():
        node = _node_by_label(_extract(lang), label)
        assert node["signature"] == sig, lang
        assert node["qualified_name"] == qn, lang
        assert node["source_location"] == loc, lang
        assert isinstance(node["end_line"], int) and node["end_line"] >= 1, lang
        assert isinstance(node["end_byte"], int) and node["end_byte"] >= 0, lang


def test_symbol_nodes_use_byte_column_and_file_node_stays_l1():
    """Every depth-treated symbol node carries ``L<line>:C<col>`` (the C column
    is the 1-based BYTE offset, same coordinate system as end_byte); the file
    node keeps the ``L1`` exemption and edges stay line-only."""
    for lang in _LANGS:
        result = _extract(lang)
        fixture_name = _LANGS[lang][1]
        file_node = _node_by_label(result, fixture_name)
        assert file_node["source_location"] == "L1", lang  # file node exemption
        for n in _symbol_nodes(result, fixture_name):
            if "end_line" not in n:
                continue  # non-depth nodes (fields, stubs) keep line-only form
            loc = n["source_location"]
            assert loc.startswith("L") and ":C" in loc, (lang, n["label"])
        for e in result["edges"]:
            assert ":" not in e["source_location"], (lang, e["relation"])


def test_class_nodes_carry_depth_fields_but_no_signature():
    """Class nodes get qualified_name / L:C / end_* but no `signature` (import
    and class get no signature per the contract)."""
    for lang, class_label in _CLASS_SAMPLED.items():
        if class_label is None:
            continue
        node = _node_by_label(_extract(lang), class_label)
        assert "signature" not in node, (lang, class_label)
        assert node["qualified_name"] == class_label, (lang, class_label)
        assert ":C" in node["source_location"], (lang, class_label)
        assert isinstance(node["end_line"], int), (lang, class_label)
        assert isinstance(node["end_byte"], int), (lang, class_label)


def test_constructor_signatures():
    """Constructors are their own class-attached nodes with call-shape
    signatures (Java + Swift sample fixtures both define explicit ones)."""
    java = _extract("java")
    assert _node_by_label(java, ".DataProcessor()")["signature"] == "()"
    assert (_node_by_label(java, ".DataProcessor()")["qualified_name"]
            == "DataProcessor::DataProcessor")
    swift = _extract("swift")
    assert _node_by_label(swift, ".init()")["signature"] == "()"
    assert _node_by_label(swift, ".init()")["qualified_name"] == "DataProcessor::init"


def test_signature_name_never_enters_the_signature():
    """The function/method name is excluded from every signature."""
    for lang, (label, sig, _, _) in _SAMPLED.items():
        name = label.strip("().").lstrip(".")
        if name and name in sig:
            raise AssertionError(
                f"{lang}: function name {name!r} leaked into signature {sig!r}")


# ── Variable/constant/field signature: the three rules ──────────────────────


def _write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_js_const_rhs_head_rule(tmp_path):
    p = _write(tmp_path, "consts.js",
               "const named = { x: 1, y: 2 };\n"
               "const cfg = makeConfig('a', 1);\n")
    result = extract_js(p)
    assert _node_by_label(result, "named")["signature"] == "{ x: 1, y: 2 }"
    assert _node_by_label(result, "cfg")["signature"] == "makeConfig('a', 1)"


def test_js_const_short_rhs_has_no_signature(tmp_path):
    """Rule 2/3: a sub-5-char RHS head is filtered (no signature field)."""
    p = _write(tmp_path, "short.js",
               "const obj = {};\n"          # RHS `{}` = 2 chars -> filtered
               "export const n = 7;\n")     # exported scalar, RHS `7` -> filtered
    result = extract_js(p)
    assert "signature" not in _node_by_label(result, "obj")
    assert "signature" not in _node_by_label(result, "n")


def test_js_const_truncation_at_token_boundary_with_marker(tmp_path):
    """Rule 1+3: a >100-char RHS is cut at a token boundary and marked ` …`."""
    word = "w" * 60  # one 60-char unbroken token
    rhs = "[ " + " ".join([word] * 5) + " ]"  # ~309 chars flattened
    p = _write(tmp_path, "long.js", f"const LONG = {rhs};\n")
    sig = _node_by_label(extract_js(p), "LONG")["signature"]
    # Cut at the last space <= 100, so the head ends with the full first token
    # (never mid-token) and carries the explicit incomplete marker.
    assert sig == "[ " + word + " …"
    assert sig.endswith(" …")


def test_cpp_field_rhs_head_and_bare(tmp_path):
    p = _write(tmp_path, "fields.cpp",
               "class C {\n"
               "    std::string name = \"foo\";\n"
               "    static const int MAX = 100;\n"   # RHS < 5 chars -> filtered
               "    int plain;\n"                    # type-only -> no signature
               "};\n")
    result = extract_cpp(p)
    assert _node_by_label(result, "name")["signature"] == '"foo"'
    assert "signature" not in _node_by_label(result, "MAX")
    assert "signature" not in _node_by_label(result, "plain")


def test_csharp_property_rhs_head_and_type_only(tmp_path):
    p = _write(tmp_path, "props.cs",
               "public class C {\n"
               "    public Processor Owner { get; set; } = new Processor();\n"
               "    public string Name { get; set; }\n"   # type-only -> no sig
               "}\n")
    result = extract_csharp(p)
    assert _node_by_label(result, "Owner")["signature"] == "new Processor()"
    assert "signature" not in _node_by_label(result, "Name")


def test_extraction_is_repeatable_all_languages():
    """Same input -> identical extraction dict for every language config."""
    for lang in _LANGS:
        first, second = _extract(lang), _extract(lang)
        assert first["nodes"] == second["nodes"], lang
        assert first["edges"] == second["edges"], lang
