# MCP stdio server - exposes graph query tools to Claude and other agents
from __future__ import annotations
import json
import math
import os
import re
import sys
import time  # CUSTOM: A1b _derive_freshness 需要（状态文件时效逃生）
from array import array
from collections import OrderedDict
from pathlib import Path
import threading
from typing import NamedTuple
import networkx as nx
from networkx.readwrite import json_graph
from graphify.security import sanitize_label, check_graph_file_size_cap
from graphify.build import edge_data, edge_datas
from graphify.paths import default_graph_json as _default_graph_json

try:
    import jieba as _jieba  # type: ignore[import-untyped]
except ImportError:
    _jieba = None


class ToolError(Exception):
    """Raised by a tool handler to signal an error result.

    A normal string return is sent as an ordinary (successful) text result. A
    ToolError is instead turned into a tool result with ``isError: true`` so a
    client that only checks ``isError`` can tell a genuine failure — e.g. the
    ``gh`` CLI missing or a PR that cannot be resolved — from success.
    """


def _load_graph(graph_path: str) -> nx.Graph:
    try:
        resolved = Path(graph_path).resolve()
        if resolved.suffix != ".json":
            raise ValueError(f"Graph path must be a .json file, got: {graph_path!r}")
        if not resolved.exists():
            raise FileNotFoundError(f"Graph file not found: {resolved}")
        check_graph_file_size_cap(resolved)
        safe = resolved
        data = json.loads(safe.read_text(encoding="utf-8"))
        if "links" not in data and "edges" in data:
            data = dict(data, links=data["edges"])
        # Stash the on-disk logical flag before the load-time override below:
        # `directed: True` exists only so renderers can recover stored arc
        # order (#2309); tools that care about logical direction (#2487) must
        # not mistake the override for graph truth.
        _logical_directed = bool(data.get("directed", False))
        data = {**data, "directed": True}
        try:
            from graphify.build import graph_has_legacy_ids as _legacy
            if _legacy(data.get("nodes", [])):
                print(
                    "[graphify] note: this graph uses the pre-#1504 node-ID scheme; "
                    "rebuild with `graphify extract --force` for path-qualified IDs.",
                    file=sys.stderr,
                )
        except Exception:
            pass
        try:
            G = json_graph.node_link_graph(data, edges="links")
        except TypeError:
            G = json_graph.node_link_graph(data)
        G.graph["_logical_directed"] = _logical_directed
        # Attach the work-memory overlay (derived sidecar next to graph.json) so
        # the query/MCP read surface can annotate NODE lines display-only. Empty
        # when no sidecar exists, leaving un-annotated output byte-identical.
        try:
            from graphify.reflect import load_learning_overlay as _llo
            G.graph["_learning_overlay"] = _llo(resolved)
        except Exception:
            G.graph["_learning_overlay"] = {}
        return G
    except json.JSONDecodeError as exc:
        print(f"error: graph.json is corrupted ({exc}). Re-run /graphify to rebuild.", file=sys.stderr)
        sys.exit(1)
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


def _communities_from_graph(G: nx.Graph) -> dict[int, list[str]]:
    """Reconstruct community dict from community property stored on nodes."""
    communities: dict[int, list[str]] = {}
    for node_id, data in G.nodes(data=True):
        cid = data.get("community")
        if cid is not None:
            communities.setdefault(int(cid), []).append(node_id)
    return communities


def _max_server_contexts() -> int:
    """Return the project-context LRU capacity (default 8, minimum 1).

    ``GRAPHIFY_MAX_CONTEXTS`` overrides the default. Invalid or blank values
    use 8; zero and negative values clamp to 1, since each request needs a
    graph context. The server's configured default graph is pinned separately
    and does not count against this limit.
    """
    raw = os.environ.get("GRAPHIFY_MAX_CONTEXTS", "").strip()
    if not raw:
        return 8
    try:
        return max(1, int(raw))
    except ValueError:
        return 8


class _GraphContextCache:
    """Thread-safe graph contexts: one pinned default plus an LRU of projects."""

    def __init__(self, max_contexts: int):
        self._max_contexts = max_contexts
        self._entries: OrderedDict[str, dict] = OrderedDict()
        self._pinned: dict[str, dict] = {}
        self._lock = threading.Lock()

    def _load_entry(self, resolved_path: str, key: tuple[int, int]) -> dict:
        """Build one entry for an already-resolved path and known file key.

        ``_load_graph`` is also used by the CLI, where invalid input terminates
        the process. A client-supplied ``project_path`` must instead become a
        tool error, so the shared MCP server can continue serving other graphs.
        """
        try:
            graph = _load_graph(resolved_path)
        except SystemExit as exc:
            raise RuntimeError(f"could not load graph.json at {resolved_path}") from exc
        # Warm the index before exposing the graph so its first query does not
        # pay the expensive build cost.
        _get_trigram_index(graph)
        communities = _communities_from_graph(graph)
        entry = {
            "key": key,
            "G": graph,
            "communities": communities,
        }
        return entry

    def load(self, resolved_path: str, *, pinned: bool = False) -> tuple[nx.Graph, dict[int, list[str]]]:
        """Return a fresh context, retaining project contexts by LRU order.

        ``resolved_path`` is resolved by the caller, making this method the
        sole owner of file statting and cache-key construction.

        ``pinned=True`` is reserved for the server's configured default graph;
        it remains warm without consuming a project-cache slot.
        """
        with self._lock:
            try:
                stat_result = Path(resolved_path).stat()
            except FileNotFoundError:
                raise FileNotFoundError(f"graph.json not found: {resolved_path}") from None
            key = (stat_result.st_mtime_ns, stat_result.st_size)
            entries = self._pinned if pinned else self._entries
            entry = entries.get(resolved_path)
            if entry is not None and entry["key"] == key:
                if not pinned:
                    self._entries.move_to_end(resolved_path)
                return entry["G"], entry["communities"]

            entry = self._load_entry(resolved_path, key)
            entries[resolved_path] = entry
            if not pinned:
                self._entries.move_to_end(resolved_path)
                while len(self._entries) > self._max_contexts:
                    self._entries.popitem(last=False)
            return entry["G"], entry["communities"]


def _strip_diacritics(text: str | None) -> str:
    import unicodedata
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _search_tokens(text: str) -> list[str]:
    """Split text into word tokens, stripping punctuation and diacritics.

    `_` is a separator, exactly like `-`. `\\w` counts underscore as a word
    character but not hyphen, so `graph_first_guard` stayed one token while the
    label `graph-first-guard.py` split into three — and the query matched
    nothing. Both the query and the node label pass through here, so splitting
    on `_` keeps the two sides consistent and snake_case lookups still resolve
    (their tokens simply match the same way). Found 2026-07-29: the graph could
    not find the underscore spelling of its own `local_id`.
    """
    return re.findall(r"[^\W_]+", _strip_diacritics(str(text)).lower())


def _has_chinese(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


def _segment_chinese(text: str) -> list[str]:
    """Segment Chinese text and keep the original term for exact matching."""
    if _jieba is not None:
        segments = [w for w in _jieba.cut(text) if len(w.strip()) > 0]
    else:
        segments = [text[i:i + 2] for i in range(len(text) - 1)] or [text]
    if len(text) > 1 and text not in segments:
        segments.append(text)
    return segments


def _is_searchable(term: str) -> bool:
    """True if term is Chinese, non-English, or an English word longer than 2 chars."""
    if all("a" <= ch <= "z" for ch in term):
        return len(term) > 2
    return True


# Question/filler words dropped from query terms so content words drive BFS
# seeding. Without this, "how does the frontier cache work" seeds on "how"/
# "the"/"work" (which prefix-match prose labels like "Working Principles" at 100x)
# instead of "frontier"/"cache", and lands in the wrong part of the graph. Applied
# to query terms only — node text is never filtered, so a symbol literally named
# `work` stays findable via explain/path. `work`/`works`/`working` are included
# because "how does X work" / "how X works" is the most common question phrasing.
#
# Non-English question words are just as damaging (#1900): in a mostly-English
# code corpus, German "wie"/"funktioniert" are rare, so they get HIGH IDF weight
# and out-seed the actual content noun by orders of magnitude. So this also
# carries a curated German set plus a trimmed French/Spanish/Portuguese/Italian
# set of question/filler words. Diacritics are kept intact (the query tokenizer
# does not NFKD-strip).
#
# Collision tradeoff: a few foreign stopwords are also English content words.
# We include high-German-value ones like "die"/"hat" (the all-stopword fallback
# in _query_terms and the unfiltered find_node path keep an English "die"/"hat"
# query workable), but deliberately OMIT "war"/"bald" (German was/soon) so
# English queries about "war" or "bald" are not clobbered. On the Romance side
# we likewise omit "comment" (FR how), "come" (IT how), "son"/"sin"/"con" (ES),
# and "pour"/"des" (FR) — all too common as English/code terms.
_QUERY_STOPWORDS = frozenset({
    # English
    "how", "what", "why", "when", "where", "which", "who", "whom", "whose",
    "does", "did", "is", "are", "was", "were", "be", "been", "being",
    "can", "could", "should", "would", "will", "shall", "may", "might", "must",
    "has", "have", "had", "the", "and", "but", "not", "for", "from", "with",
    "without", "into", "onto", "off", "that", "this", "these", "those", "there",
    "here", "its", "their", "them", "they", "about", "any", "all", "some",
    "work", "works", "working",
    # German (articles/conjunctions/question words/auxiliaries/prepositions)
    "der", "die", "das", "den", "dem", "ein", "eine", "und", "oder", "nicht",
    "wie", "wer", "wann", "wo", "warum", "wieso",
    "welche", "welcher", "welches",
    "ist", "sind", "wird", "wurde", "hat", "haben",
    "kann", "koennen", "können", "soll", "muss", "sich",
    "bei", "mit", "von", "fuer", "für", "ueber", "über", "nach", "aus",
    "gibt", "es",
    "funktioniert", "geaendert", "geändert", "aendert", "ändert",
    # French
    "pourquoi", "quand", "quel", "quelle", "quels", "quelles", "quoi",
    "qui", "que", "est", "sont", "fonctionne", "cette", "dans", "avec", "où",
    # Spanish
    "cómo", "como", "qué", "cuál", "cuáles", "cuándo", "dónde", "donde",
    "porque", "por", "para", "funciona", "está", "están", "hay",
    # Portuguese
    "qual", "quais", "quando", "onde", "são", "estão", "tem", "uma", "não",
    # Italian
    "perché", "cosa", "quale", "quali", "dove", "funziona", "sono", "che",
    "della",
})


def _query_terms(question: str) -> list[str]:
    """Split a query into searchable terms, segmenting Chinese text, then drop
    question/filler words (`_QUERY_STOPWORDS`, English plus common German/
    Romance-language fillers) so content words drive seeding. Falls back to the
    unfiltered terms if the query is all stopwords, so a question like "how does
    it work" or "wie funktioniert das" still seeds on something."""
    terms: list[str] = []
    for raw in question.split():
        if _has_chinese(raw):
            for seg in _segment_chinese(raw.lower().strip()):
                seg = seg.strip()
                if seg and _is_searchable(seg):
                    terms.append(seg)
        else:
            # Strip punctuation without touching Unicode characters (avoid NFKD mangling non-Latin scripts)
            for tok in re.findall(r"\w+", raw.lower()):
                if _is_searchable(tok):
                    terms.append(tok)
    content = [t for t in terms if t not in _QUERY_STOPWORDS]
    return content or terms


_EXACT_MATCH_BONUS = 1000.0
_PREFIX_MATCH_BONUS = 100.0
_SUBSTRING_MATCH_BONUS = 1.0
_SOURCE_MATCH_BONUS = 0.5


def _compute_idf(G: nx.Graph, terms: list[str]) -> dict[str, float]:
    """IDF weights for query terms, cached in G.graph['_idf_cache'].

    Common terms like 'error' or 'exception' that match hundreds of nodes get
    low weights; rare identifiers like 'FooBarService' get high weights.
    Cache is stored on the graph object itself so it auto-invalidates when
    a hot-reload replaces G with a new object.
    """
    cache: dict[str, float] = G.graph.setdefault("_idf_cache", {})
    N = G.number_of_nodes() or 1
    uncached = [t for t in terms if t not in cache]
    if uncached:
        df: dict[str, int] = {t: 0 for t in uncached}
        for _, data in G.nodes(data=True):
            norm_label = (
                data.get("norm_label") or _strip_diacritics(data.get("label") or "")
            ).lower()
            for t in uncached:
                if t in norm_label:
                    df[t] += 1
        for t in uncached:
            cache[t] = math.log(1 + N / (1 + df[t]))
    return {t: cache.get(t, math.log(1 + N)) for t in terms}


def _trigrams(text: str) -> set[str]:
    """Character trigrams of `text`; for <3-char text the whole string is the key."""
    if len(text) < 3:
        return {text} if text else set()
    return {text[i:i + 3] for i in range(len(text) - 2)}


def _node_search_text(data: dict, nid: str) -> str:
    """Concatenate every field _score_nodes / _find_node match a query against, so
    one trigram index over this text is a complete candidate generator for both.

    - `norm_label` and `source_file` feed _score_nodes' per-term substring tiers.
    - `label_tokens` (the space-joined token form) feeds _find_node's
      `term in label_tokens` branch, where a multi-word `term` can span a token
      boundary that punctuation hides in `norm_label` (e.g. query "foo bar" matches
      label "foo.bar" only via its tokenized form).
    - `source_tokens` feeds _find_node's exact source-file path lookup, where a
      query like "app/api/example/route.ts" tokenizes to "app api example route ts".
    - `nid` feeds the whole-query `joined == nid_lower` tier.
    - a trailing diacritic-folded `nid` feeds _find_node's `norm_query == nid_norm`
      tier. Every query path folds through `_strip_diacritics` (NFKD), so a raw-only
      id field leaves the needle and the posting under different normal forms and
      the node is dropped before any predicate runs (#2467). Hangul is the common
      case: NFKD decomposes a syllable into conjoining jamo, which have combining
      class 0 and therefore survive the combining-character filter. The field is
      appended only when the fold actually differs, so the text an all-ASCII graph
      indexes — and every field position the other readers rely on — is unchanged.

    NUL separators stop a trigram from spanning two fields (a query never contains
    NUL, so a cross-field trigram can never be a real match).
    """
    norm_label = data.get("norm_label") or _strip_diacritics(data.get("label") or "").lower()
    label_tokens = " ".join(_search_tokens(data.get("label") or ""))
    source = (data.get("source_file") or "").lower()
    source_tokens = " ".join(_search_tokens(data.get("source_file") or ""))
    nid_text = str(nid).lower()
    fields = (norm_label, label_tokens, nid_text, source, source_tokens)
    if not nid_text.isascii():
        nid_folded = _strip_diacritics(str(nid)).lower()
        if nid_folded != nid_text:
            fields += (nid_folded,)
    return "\x00".join(fields)


def _get_trigram_index(G: nx.Graph) -> dict:
    """Lazily build and cache a trigram -> node-position postings map on the graph.

    Cached on `G.graph` so it auto-invalidates when a hot-reload swaps in a
    fresh graph object, exactly like `_idf_cache`. `set_cache` memoizes per-trigram
    id-sets across queries within one graph generation.
    """
    idx = G.graph.get("_trigram_index")
    if idx is not None:
        return idx
    ids = list(G.nodes())
    postings: dict[str, array] = {}
    for i, nid in enumerate(ids):
        for g in _trigrams(_node_search_text(G.nodes[nid], nid)):
            bucket = postings.get(g)
            if bucket is None:
                bucket = array("i")
                postings[g] = bucket
            bucket.append(i)
    idx = {"ids": ids, "postings": postings, "set_cache": {}}
    G.graph["_trigram_index"] = idx
    return idx


def _trigram_candidates(G: nx.Graph, needles: list[str], *, guard_frac: float = 0.10) -> list[str] | None:
    """Node IDs whose text could contain any `needle` as a substring, via the
    trigram index — a *superset* the caller then re-scores with the exact predicates.

    Returns candidates in graph-iteration order (so order-sensitive callers like
    _find_node stay byte-identical to a full scan), or **None** when the index isn't
    worth it — a needle is too short to trigram, or its rarest trigram is still
    common enough that the candidate set would approach the whole graph. The caller
    falls back to the full scan, preserving the never-worse contract. The guard is
    cheap: postings-length lookups only, no set intersection.
    """
    idx = _get_trigram_index(G)
    ids, postings, set_cache = idx["ids"], idx["postings"], idx["set_cache"]
    n = len(ids)
    if n == 0:
        return []
    needles = [s for s in needles if s]
    thresh = int(n * guard_frac)
    for s in needles:
        tgs = _trigrams(s)
        if not tgs or any(len(g) < 3 for g in tgs):
            return None  # too short to trigram-filter
        present = [len(postings[g]) for g in tgs if g in postings]
        if not present:
            continue  # this needle matches nothing — contributes no candidates
        if min(present) > thresh:
            return None  # rarest trigram still too common -> not worth the index
    cand: set[int] = set()
    for s in needles:
        sets: list[set] | None = []
        for g in _trigrams(s):
            bucket = postings.get(g)
            if bucket is None:
                sets = None  # a trigram absent everywhere -> needle matches nothing
                break
            cached = set_cache.get(g)
            if cached is None:
                cached = set(bucket)
                set_cache[g] = cached
            sets.append(cached)
        if not sets:
            continue
        sets.sort(key=len)  # intersect smallest-first
        hit = set(sets[0])
        for other in sets[1:]:
            hit &= other
            if not hit:
                break
        cand |= hit
    return [ids[i] for i in sorted(cand)]


class _QueryScores(NamedTuple):
    """Per-query scoring result, returned by the private `_score_query` helper.

    `ranked` is the existing ordered `(score, node_id)` ranking produced by the
    combined query scorer (the value `_score_nodes` always returned). When the
    caller asks for it via `collect_per_term_seeds=True`, `best_seed_by_term`
    additionally carries the winning node id for each normalized search token —
    the seed `_pick_seeds` would have picked for that token via the now-retired
    per-token `_score_nodes([token])` rescoring pass — computed in the *same*
    per-node traversal so the query path makes exactly one graph scoring pass
    regardless of query length. Empty when `collect_per_term_seeds=False`.
    """
    ranked: list[tuple[float, str]]
    best_seed_by_term: dict[str, str]


def _score_nodes(G: nx.Graph, terms: list[str]) -> list[tuple[float, str]]:
    """Combined query scorer returning the existing ranked `(score, node_id)` list.

    Backwards-compatible thin wrapper around `_score_query` for path, explain,
    tests, and every other caller that only needs the combined ranking. The
    per-term seed metadata computed by `_score_query` (when requested) is
    discarded here so existing callers see no API or runtime-cost change.
    """
    return _score_query(G, terms, collect_per_term_seeds=False).ranked


def _score_query(
    G: nx.Graph, terms: list[str], *, collect_per_term_seeds: bool
) -> _QueryScores:
    """Single-pass combined scorer that optionally also records the best seed
    for each normalized query token.

    The combined ranking is byte-identical to what `_score_nodes` produced
    before the refactor; `_score_nodes` is now a thin wrapper that asks for
    `collect_per_term_seeds=False` and returns only `.ranked`.

    When `collect_per_term_seeds=True`, the per-token singleton winner is
    computed alongside the combined score in the *same* per-node visit (it
    reuses the same `norm_label` / `label_tokens` / `source` already evaluated
    for the combined tier), so `_query_graph_text` can feed `best_seed_by_term`
    straight into `_pick_seeds` and skip the T additional whole-graph rescoring
    passes the old per-token `_score_nodes([token])` loop ran.

    Singleton-winner semantics match the legacy per-token path exactly. The
    score itself mirrors `_score_nodes([token])` with `n_terms == 1` (so the
    coverage term is 1 and the per-token tier is unscaled) plus the broader
    joined-singlet tier (which also checks `label_tokens` and `nid_lower`).
    Tie-break order is (1) highest singleton score, (2) highest graph degree,
    (3) shortest displayed label, (4) lexicographically smallest node id —
    exactly what `max(tied, key=degree)` over a sort by `(-score, label_len,
    nid)` produced in the legacy `_pick_seeds` per-token loop. The combined
    trigram candidate set (needles `norm_terms + [joined]`) is a superset of
    each per-token `[t]` candidate set, so iterating combined candidates
    discovers every non-zero singleton-score node for every term.
    """
    scored: list[tuple[float, str]] = []
    # Dedupe tokens, order-preserving (as _pick_seeds already does): a repeated
    # query word must not double-count every tier, and with coverage scaling
    # below it would also inflate the matched-term ratio (#1602).
    norm_terms = list(dict.fromkeys(tok for t in terms for tok in _search_tokens(t)))
    n_terms = len(norm_terms)
    idf = _compute_idf(G, norm_terms)
    # Whole-query string for full-label matching (mirrors _find_node's `term`).
    joined = " ".join(norm_terms)
    # Weight the full-query bonus by the rarest constituent term so a specific
    # multi-word label still outweighs common-token noise; floor at 1.0.
    joined_w = max((idf.get(t, 1.0) for t in norm_terms), default=1.0)
    # Trigram prefilter: score only nodes whose text could match a term, falling
    # back to the whole graph when the index isn't selective. The result is
    # identical either way — the per-node scoring below is unchanged and a
    # non-candidate node always scores 0. (IDF above stays a whole-graph statistic.)
    candidate_ids = _trigram_candidates(G, norm_terms + ([joined] if joined else []))
    node_iter = (
        G.nodes(data=True) if candidate_ids is None
        else ((nid, G.nodes[nid]) for nid in candidate_ids)
    )
    # Per-token best tracking, only when the caller (the query path) wants the
    # seed metadata. The key tuple is the full multi-key tie-break
    # (`(-singleton_score, -degree, label_len, nid)`), so `min` over the
    # stored key mirrors the legacy `max(tied, key=degree)` over a
    # (-score, label_len, nid)-sorted term_scored list. `None` is comparable
    # as "smaller" than every tuple, so the first non-zero candidate seeds the
    # entry without a separate `if t not in best_by_term` branch.
    best_by_term: dict[str, tuple[tuple, str]] | None = (
        {} if collect_per_term_seeds else None
    )
    for nid, data in node_iter:
        norm_label = data.get("norm_label") or _strip_diacritics(data.get("label") or "").lower()
        bare_label = norm_label.rstrip("()")
        # Tokenized form of the label (punctuation stripped, same transform as the
        # query). norm_label may still carry punctuation like ':' or '-', which a
        # tokenized query can never equal; comparing token-joined forms on both
        # sides makes "uoce: dehumidifier driver" match query "uoce dehumidifier
        # driver".
        label_tokens = " ".join(_search_tokens(data.get("label") or ""))
        source = (data.get("source_file") or "").lower()
        # `nid_lower` is needed both by the full-query tier (`if joined`) and by
        # the per-token singleton tier (joined-singlet exact-match check). When
        # neither runs (`joined` empty AND not collecting seeds) skip the call;
        # this preserves the single-query-time perf where nid_lower was lazy.
        nid_lower = nid.lower() if (joined or collect_per_term_seeds) else ""
        score = 0.0
        # Full-query tier: a multi-word query that equals (or prefixes) the whole
        # label must dominate the per-token bag-of-words sums below, so `path`/
        # `query` resolve the same node `explain` does (via _find_node). Without
        # this, no single token equals a multi-word label, the per-token exact
        # tier never fires, and every node sharing the token set ties -> arbitrary
        # node-id sort -> wrong/disconnected endpoint -> false "No path found".
        if joined:
            if joined in (norm_label, bare_label, label_tokens, nid_lower):
                score += _EXACT_MATCH_BONUS * 10 * joined_w
            elif (
                norm_label.startswith(joined)
                or bare_label.startswith(joined)
                or label_tokens.startswith(joined)
            ):
                score += _PREFIX_MATCH_BONUS * 10 * joined_w
        # Term coverage (#1602): scale the per-term exact/prefix tiers by the
        # squared fraction of query terms the node's LABEL matches, so a lone
        # generic word that happens to equal a short label (query term "home"
        # vs. a home() leaf) cannot bury nodes that match several of the
        # query's terms. Squaring matters because the exact tier is 10x the
        # prefix tier: at linear coverage a 1-of-10-terms exact match still
        # outscores a 3-of-10 prefix+substring match. Single-term and
        # full-coverage queries are unchanged (coverage == 1), so identifier
        # lookups keep exact-match dominance. Source-file hits score but do
        # not count as coverage: a colliding leaf whose directory shares
        # tokens with the query (common near the intended target) must not
        # win back its exact tier via path fragments. The substring/source
        # bonuses and the full-query tier above stay unscaled.
        matched = 0
        tiered = 0.0
        for t in norm_terms:
            w = idf.get(t, 1.0)
            # Per-tier contributions for this token, kept separate so the
            # singleton tracking below can reuse them without re-evaluating
            # the same predicates. Three-tier precedence: exact > prefix >
            # substring (take the strongest tier per term so a single term
            # cannot double-count).
            tier_value = 0.0
            substr_value = 0.0
            source_value = 0.0
            if t == norm_label or t == bare_label:
                tier_value = _EXACT_MATCH_BONUS * w
                matched += 1
            elif norm_label.startswith(t) or bare_label.startswith(t):
                tier_value = _PREFIX_MATCH_BONUS * w
                matched += 1
            elif t in norm_label:
                substr_value = _SUBSTRING_MATCH_BONUS * w
                score += substr_value
                matched += 1
            if t in source:
                source_value = _SOURCE_MATCH_BONUS * w
                score += source_value
            tiered += tier_value
            if collect_per_term_seeds and best_by_term is not None:
                # Singleton score for [t] on this node, mirroring
                # `_score_nodes(G, [t])` exactly (n_terms == 1, no coverage
                # scaling). The joined-singlet tier is broader than the per-
                # token tier: it also checks `label_tokens` and `nid_lower`,
                # matching the legacy single-token `_score_nodes([t])` call
                # (where `joined == t`).
                if t in (norm_label, bare_label, label_tokens, nid_lower):
                    singleton = _EXACT_MATCH_BONUS * 10 * w
                elif (
                    norm_label.startswith(t)
                    or bare_label.startswith(t)
                    or label_tokens.startswith(t)
                ):
                    singleton = _PREFIX_MATCH_BONUS * 10 * w
                else:
                    singleton = 0.0
                singleton += tier_value + substr_value + source_value
                if singleton > 0:
                    # Tie-break key mirrors the legacy sort+max(degree):
                    # (-singleton, -degree, label_len, nid) — the minimum
                    # tuple wins, exactly matching max(tied, key=degree)
                    # over (label_len asc, nid asc)-sorted ties.
                    key = (-singleton, -G.degree(nid), len(data.get("label") or nid), nid)
                    cur = best_by_term.get(t)
                    if cur is None or key < cur[0]:
                        best_by_term[t] = (key, nid)
        if tiered:
            score += tiered * (matched / n_terms) ** 2
        if score > 0:
            scored.append((score, nid))
    # Sort by score desc; break ties toward the shorter label so a concise exact
    # match beats a longer superset that happens to share the same score.
    scored.sort(key=lambda s: (-s[0], len(G.nodes[s[1]].get("label") or s[1]), s[1]))
    best_seed_by_term: dict[str, str] = {}
    if collect_per_term_seeds and best_by_term:
        best_seed_by_term = {t: nid for t, (_key, nid) in best_by_term.items()}
    return _QueryScores(ranked=scored, best_seed_by_term=best_seed_by_term)


def _pick_scored_endpoint(G: nx.Graph, scored: list[tuple[float, str]], query: str) -> str:
    """Pick a path endpoint from a _score_nodes result, preferring full-token matches.

    The full-query tier in _score_nodes only fires when the query equals or
    prefixes a label, so a query that is a token *subset* of the intended label
    (query "Reject-everything judge" vs. label "Degenerate Reject-Everything
    Judge") gets no bonus, and a node prefix-matching one rare token (label
    "Rejection Summary") can out-score it on IDF alone. Committing to scored[0]
    then anchors the path on an unrelated — often disconnected — node and yields
    a false "No path found". Scan the score-ordered list and take the first
    candidate whose label contains EVERY query token; when the top candidate
    already full-matches, or no candidate does, this is exactly scored[0].

    `scored` must be non-empty (both callers return early on no match).
    """
    qtokens = set(_search_tokens(query))
    if not qtokens:
        return scored[0][1]
    for _score, nid in scored:
        if qtokens <= set(_search_tokens(G.nodes[nid].get("label") or nid)):
            return nid
    return scored[0][1]


def _pick_seeds(
    scored: list[tuple[float, str]],
    max_k: int = 3,
    gap_ratio: float = 0.2,
    *,
    G: "nx.Graph | None" = None,
    best_seed_by_term: dict[str, str] | None = None,
) -> list[str]:
    """Select BFS seed nodes, stopping when score drops too far below the top.

    Prevents high-frequency noise terms (error, exception) from stealing seed
    slots from a dominant identifier match. When FooBarService scores 1000 and
    error nodes score 1.0, only FooBarService is seeded — the score gap is 99.9%
    which is well above the 20% threshold that would allow additional seeds.

    That same gap_ratio cutoff has a failure mode on multi-term natural-language
    queries: if one term happens to hit an EXACT label match on a node that is
    otherwise unrelated to the query's intent (e.g. a common word that is also
    used as an unrelated identifier or field name elsewhere in the corpus), it
    can outscore every SUBSTRING match on the query's other, actually-relevant
    terms by ~1000x (see `_EXACT_MATCH_BONUS` vs. `_SUBSTRING_MATCH_BONUS`).
    The 20%-gap cutoff then silently discards all of those substring-tier
    seeds, so the BFS traversal only ever explores the neighborhood of the one
    unrelated exact match — see #1445.

    When `G` and `best_seed_by_term` are supplied, this guarantees at least one
    seed per distinct query term that has any match at all, so one term's
    incidental collision cannot starve out the others. The per-token winners
    in `best_seed_by_term` are precomputed by `_score_query` (during the same
    traversal that produced `scored`) so this function no longer rescores the
    graph per term — see #1445 and the `_score_query` docstring.

    Coverage scaling in _score_nodes (#1602) now dampens a lone collision's
    exact tier on multi-term queries, which brings label-matching relevant
    nodes back inside the gap window; this per-term guarantee remains
    load-bearing for relevant nodes matched only via substrings, whose flat
    scores a dampened collision can still exceed.
    """
    if not scored:
        return []

    # Deduplicate seeds by (normalized) label so a generic, homonymous symbol —
    # e.g. dozens of route handlers all labelled `GET`/`POST`, or a `handler`
    # repeated across a framework — contributes at most one seed instead of
    # consuming every slot and flooding the BFS with near-identical neighborhoods
    # (#1766). The key mirrors _score_nodes' normalization so `GET`/`Get`/`get`
    # collapse together. When G is absent we can't read labels, so fall back to
    # the (unique) node id, which is a no-op — preserving the old behavior.
    def _seed_label_key(nid: str) -> str:
        if G is None:
            return nid
        data = G.nodes[nid]
        return (data.get("norm_label")
                or _strip_diacritics(data.get("label") or "").lower()) or nid

    top_score = scored[0][0]
    seeds: list[str] = []
    seen_labels: set[str] = set()
    for score, nid in scored:
        if len(seeds) >= max_k:
            break
        if seeds and score < top_score * gap_ratio:
            break
        key = _seed_label_key(nid)
        if key in seen_labels:
            continue
        seen_labels.add(key)
        seeds.append(nid)

    if G is not None and best_seed_by_term:
        # Guarantee one seed per distinct query term that has any match at all,
        # so an incidental exact match on one term cannot starve matches on
        # other terms (#1445). Iterate tokens in a deterministic sorted order
        # so seeds added by this loop have a stable order independent of dict
        # iteration — preserving the legacy `_pick_seeds(terms=...)` behavior
        # which iterated `sorted({tok ...})`. Per-token winners arrive
        # precomputed in `best_seed_by_term` from `_score_query`'s single
        # traversal, so `_pick_seeds` no longer rescoring the graph per term.
        # The per-label dedup cap also gates these additions, so the guarantee
        # cannot reintroduce a second copy of an already-seeded generic label
        # (#1766).
        for term in sorted(best_seed_by_term):
            best_nid = best_seed_by_term[term]
            # Honor the same per-label cap so the per-term guarantee can't
            # reintroduce a second copy of an already-seeded generic label.
            key = _seed_label_key(best_nid)
            if best_nid not in seeds and key not in seen_labels:
                seen_labels.add(key)
                seeds.append(best_nid)
    return seeds


# Verb-shaped tokens that express the RELATION a query asks about ("who calls
# X", "what uses Y") rather than a symbol to look up. `_query_terms` keeps them
# on purpose (a corpus can legitimately define an identifier named `calls`, see
# #1597), but they must not be handed a guaranteed seed slot in `_pick_seeds`:
# an incidental prefix match (e.g. "calls" prefixing `.callStoreWithAmount()`)
# would otherwise seat an unrelated decoy as a BFS root (#2507). Demotion
# happens at the `_query_graph_text` call site, so `_score_query`'s ranking —
# where such a verb can still win a seat on merit via the gap window — is
# untouched. Deliberately verbs only; relation NOUNS (module, field, return)
# stay eligible for the guarantee.
_RELATIONAL_INTENT_TERMS: frozenset[str] = frozenset({
    "call", "calls", "called", "caller", "callers",
    "invoke", "invokes", "invoked",
    "use", "uses", "used", "using",
    "import", "imports", "imported",
    "export", "exports", "exported",
    "extend", "extends", "extended",
    "implement", "implements", "implemented",
    "depend", "depends",
    "reference", "references", "referenced",
})


_CONTEXT_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("call", ("call", "calls", "called", "caller", "callers", "invoke", "invokes", "invoked")),
    ("import", ("import", "imports", "imported", "module", "modules")),
    ("field", ("field", "fields", "member", "members", "property", "properties")),
    ("parameter_type", ("parameter", "parameters", "param", "params", "argument", "arguments")),
    ("return_type", ("return", "returns", "returned")),
    ("generic_arg", ("generic", "generics", "template", "templates")),
)


_CONTEXT_FILTER_ALIASES: dict[str, str] = {
    "param": "parameter_type",
    "params": "parameter_type",
    "parameter": "parameter_type",
    "parameters": "parameter_type",
    "argument": "parameter_type",
    "arguments": "parameter_type",
    "arg": "parameter_type",
    "args": "parameter_type",
    "return": "return_type",
    "returns": "return_type",
    "returned": "return_type",
    "generic": "generic_arg",
    "generics": "generic_arg",
    "template": "generic_arg",
    "templates": "generic_arg",
    "annotation": "attribute",
    "annotations": "attribute",
    "decorator": "attribute",
    "decorators": "attribute",
    "calls": "call",
    "called": "call",
    "invoke": "call",
    "invocation": "call",
    "fields": "field",
    "property": "field",
    "properties": "field",
    "member": "field",
    "members": "field",
    "imports": "import",
    "imported": "import",
    "module": "import",
    "modules": "import",
    "exports": "export",
    "exported": "export",
}


def _normalize_context_filters(filters: list[str] | None) -> list[str]:
    if not filters:
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for value in filters:
        key = _strip_diacritics(str(value)).strip().lower()
        if not key:
            continue
        key = _CONTEXT_FILTER_ALIASES.get(key, key)
        if key not in seen:
            seen.add(key)
            normalized.append(key)
    return normalized


def _infer_context_filters(question: str) -> list[str]:
    lowered = {
        _strip_diacritics(token).lower()
        for token in question.replace("?", " ").replace(",", " ").split()
    }
    inferred: list[str] = []
    for context, hints in _CONTEXT_HINTS:
        if any(hint in lowered for hint in hints):
            inferred.append(context)
    return inferred


def _resolve_context_filters(question: str, explicit_filters: list[str] | None = None) -> tuple[list[str], str | None]:
    normalized = _normalize_context_filters(explicit_filters)
    if normalized:
        return normalized, "explicit"
    inferred = _infer_context_filters(question)
    if inferred:
        return inferred, "heuristic"
    return [], None


def _filter_graph_by_context(G: nx.Graph, context_filters: list[str] | None) -> nx.Graph:
    filters = set(_normalize_context_filters(context_filters))
    if not filters:
        return G
    H = G.__class__()
    H.add_nodes_from(G.nodes(data=True))
    if isinstance(G, (nx.MultiGraph, nx.MultiDiGraph)):
        for u, v, key, data in G.edges(keys=True, data=True):
            if data.get("context") in filters:
                H.add_edge(u, v, key=key, **data)
    else:
        for u, v, data in G.edges(data=True):
            if data.get("context") in filters:
                H.add_edge(u, v, **data)
    return H


def _complete_induced_edges(G: nx.Graph, visited: set[str], edges_seen: list[tuple]) -> None:
    """Append edges between visited nodes that the traversal never recorded (#2323).

    Both traversals only record an edge that *discovers* an unvisited neighbour,
    so what they return is a traversal tree, not the induced subgraph over the
    nodes they return. `_bfs` marks every seed visited up front, so an edge
    between two seeds can never be recorded — the reported symptom, where both
    endpoints render and the edge between them does not. It drops ordinary
    cross-edges for the same reason. `_dfs` appends on push rather than on
    visit, so it already captured those; its one gap is an edge between two
    non-seed hubs, since neither endpoint is ever expanded.

    Scans only edges incident to `visited`, so cost tracks the subgraph rather
    than the whole graph, bounded by O(2E) overall. A visited hub is rescanned
    in full even though the traversal deliberately did not expand it — that is
    unavoidable, since a hub-to-hub edge is exactly the case `_dfs` misses.
    `G` here is the context-filtered `traversal_graph` (see
    `_query_graph_text`), so a filtered-out relation cannot reappear.

    Self-loops are skipped. A recursive function legitimately carries one, but
    neither traversal has ever recorded one (`n` is always already visited when
    its own self-loop is examined), and surfacing them is a separate output
    change from the missing edges reported here.

    Dedup keys on the ordered pair for directed graphs and the unordered pair
    otherwise: on a DiGraph `u->v` and `v->u` are genuinely distinct edges
    (mutual recursion, circular imports), and collapsing them would drop a real
    one. On a multigraph parallel edges collapse to one entry, matching the
    renderer, which already shows only the first (`_subgraph_to_text`).

    Traversal edges keep their discovery order; completions are appended after.
    """
    directed = G.is_directed()

    def _key(u: str, v: str):
        return (u, v) if directed else frozenset((u, v))

    seen = {_key(u, v) for u, v in edges_seen}
    # sorted() so the appended order can't shift run-to-run with CPython's
    # per-process string-hash seed, the same reason the renderer sorts (#1753).
    for u, v in G.edges(sorted(visited)):
        if u == v or v not in visited:
            continue
        key = _key(u, v)
        if key in seen:
            continue
        seen.add(key)
        edges_seen.append((u, v))


def _bfs(G: nx.Graph, start_nodes: list[str], depth: int) -> tuple[set[str], list[tuple]]:
    # Compute hub threshold: nodes above this degree are not expanded as transit.
    # p99 of degree distribution, floored to avoid over-blocking small graphs.
    # CUSTOM: floor 50->30（实测调紧 hub 阈值，_bfs 与 _dfs 同步改）
    # 三图谱实测（graphify_fork 11206n / D:/code全量 14707n / quant-research 1234n）：
    #   1. hop1 直接邻居零损失（21 query 跨领域全 0）--hub 只挡非 seed 的 transit 扩展，
    #      seed 自身邻居遍历不受限（L759 `n not in seed_set` 条件保护查询核心）。
    #   2. 中大子图省 15-44% nodes（token 大头有效减少）；小子图(base<30)0 触发无损。
    #   3. 高频核心概念(如 quant "因子"query)0% 省（不误伤高密度概念簇）。
    #   4. 三图谱 p99 degree 均<50(28/23/33)，原 max(50,p99) 恒为 50，p99 分支实为死代码，
    #      本改动即调这个实际生效的常数。floor:30 保守值（floor:20 对 watch/close/__init__
    #      等省幅 60-70% 偏激进）。
    degrees = [G.degree(n) for n in G.nodes()]
    if degrees:
        degrees_sorted = sorted(degrees)
        p99_idx = int(len(degrees_sorted) * 0.99)
        hub_threshold = max(30, degrees_sorted[p99_idx])  # CUSTOM: floor 50->30（实测调紧，hop1零损省15-44%）
    else:
        hub_threshold = 50
    seed_set = set(start_nodes)
    visited: set[str] = set(start_nodes)
    frontier = set(start_nodes)
    edges_seen: list[tuple] = []
    for _ in range(depth):
        next_frontier: set[str] = set()
        for n in frontier:
            # Don't expand through high-degree hubs (except seeds - a hub that
            # is the starting node should still be explored).
            if n not in seed_set and G.degree(n) >= hub_threshold:
                continue
            for neighbor in G.neighbors(n):
                if neighbor not in visited:
                    next_frontier.add(neighbor)
                    edges_seen.append((n, neighbor))
        visited.update(next_frontier)
        frontier = next_frontier
    _complete_induced_edges(G, visited, edges_seen)
    return visited, edges_seen


def _dfs(G: nx.Graph, start_nodes: list[str], depth: int) -> tuple[set[str], list[tuple]]:
    degrees = [G.degree(n) for n in G.nodes()]
    if degrees:
        degrees_sorted = sorted(degrees)
        p99_idx = int(len(degrees_sorted) * 0.99)
        hub_threshold = max(30, degrees_sorted[p99_idx])  # CUSTOM: floor 50->30（实测调紧，hop1零损省15-44%）
    else:
        hub_threshold = 50
    seed_set = set(start_nodes)
    visited: set[str] = set()
    edges_seen: list[tuple] = []
    stack = [(n, 0) for n in reversed(start_nodes)]
    while stack:
        node, d = stack.pop()
        if node in visited or d > depth:
            continue
        visited.add(node)
        if node not in seed_set and G.degree(node) >= hub_threshold:
            continue
        for neighbor in G.neighbors(node):
            if neighbor not in visited:
                stack.append((neighbor, d + 1))
                edges_seen.append((node, neighbor))
    _complete_induced_edges(G, visited, edges_seen)
    return visited, edges_seen


def _subgraph_to_text(G: nx.Graph, nodes: set[str], edges: list[tuple], token_budget: int = 2000, *, seeds: list[str] | None = None) -> str:
    """Render subgraph as text, cutting at token_budget (approx 3 chars/token).

    seeds: exact-match nodes rendered first before the degree-sorted expansion,
    so the queried symbol always appears at the top of the output.
    """
    char_budget = token_budget * 3
    lines = []
    # Work-memory overlay (derived sidecar) stashed on the graph at load time.
    # Empty when no sidecar exists, so un-annotated output stays byte-identical.
    overlay = getattr(G, "graph", {}).get("_learning_overlay", {}) or {}
    seed_set = set(seeds or [])
    seed_hits = [n for n in (seeds or []) if n in nodes]
    # Rank non-seed nodes by hop distance from the seeds so the node that answers
    # the query (a direct hit or its close neighbors) survives the budget cut
    # instead of being pushed past it by incidental high-degree hubs (#BUG2). BFS
    # discovery order was discarded upstream (_bfs returns a set), so recompute
    # layers here over BOTH edge directions. Deterministic: neighbor iteration is
    # insertion-ordered and the sort key ends in str(n) (no hash-order).
    def _adj(n):
        if G.is_directed():
            yield from G.successors(n)
            yield from G.predecessors(n)
        else:
            yield from G.neighbors(n)
    dist: dict[str, int] = {n: 0 for n in seed_hits}
    frontier, hop = seed_hits, 0
    while frontier:
        hop += 1
        nxt = []
        for n in frontier:
            for nb in _adj(n):
                if nb in nodes and nb not in dist:
                    dist[nb] = hop
                    nxt.append(nb)
        frontier = nxt
    ordered = seed_hits + sorted(
        nodes - seed_set,
        key=lambda n: (dist.get(n, 1 << 30), -G.degree(n), str(n)),
    )
    for nid in ordered:
        d = G.nodes[nid]
        # Every LLM-derived field passes through sanitize_label before being
        # concatenated into MCP tool output (F-010): an attacker who controls a
        # corpus document can otherwise inject ANSI escapes, fake graphify-out
        # log lines, or prompt-injection markup into the model's context via
        # source_file / source_location / community.
        # The learning= suffix is appended INSIDE the bracket and BEFORE the
        # budget check below, so it counts in char_budget accounting.
        entry = overlay.get(str(nid))
        learning_suffix = ""
        if entry:
            status = sanitize_label(str(entry.get("status", "")))
            if status:
                learning_suffix = f" learning={status}{':stale' if entry.get('stale') else ''}"
        line = (
            f"NODE {sanitize_label(d.get('label', nid))} "
            f"[src={sanitize_label(str(d.get('source_file', '')))} "
            f"loc={sanitize_label(str(d.get('source_location', '')))} "
            f"community={sanitize_label(str(d.get('community_name') or d.get('community', '')))}"
            f"{learning_suffix}]"
        )
        lines.append(line)
    for u, v in edges:
        if u in nodes and v in nodes:
            raw = G[u][v]
            d = next(iter(raw.values()), {}) if isinstance(G, (nx.MultiGraph, nx.MultiDiGraph)) else raw
            # (u, v) is BFS/DFS visit order, not necessarily the true edge
            # direction: on an undirected graph G.neighbors() walks callers
            # and callees alike, so a caller->callee edge renders backwards
            # whenever the callee is visited first. _src/_tgt (stashed on the
            # edge data by the `query` CLI loader) carry the real direction;
            # fall back to (u, v) for graphs/edges that don't set them.
            src = d.get("_src", u)
            tgt = d.get("_tgt", v)
            # Guard against a stray/dangling _src/_tgt (hand-edited or adversarial
            # graph.json): only trust them when they name exactly this edge's
            # endpoints, else fall back to (u, v). Without this, G.nodes[src]
            # would KeyError on an unknown id (#2080 review).
            if {src, tgt} != {u, v}:
                src, tgt = u, v
            context = d.get("context")
            context_suffix = f" context={sanitize_label(str(context))}" if context else ""
            # The relation SITE (call/import/reference line in the source's
            # file), not a def line — so "who calls X" cites a clickable call
            # location, not the caller's def (#BUG1).
            _loc = str(d.get("source_location") or "")
            at_suffix = (
                f" at={sanitize_label(str(d.get('source_file') or ''))}:{sanitize_label(_loc)}"
                if _loc else ""
            )
            line = (
                f"EDGE {sanitize_label(G.nodes[src].get('label', src))} "
                f"--{sanitize_label(str(d.get('relation', '')))} "
                f"[{sanitize_label(str(d.get('confidence', '')))}{context_suffix}]--> "
                f"{sanitize_label(G.nodes[tgt].get('label', tgt))}{at_suffix}"
            )
            lines.append(line)
    output = "\n".join(lines)
    if len(output) > char_budget:
        cut_at = output[:char_budget].rfind("\n")
        cut_at = cut_at if cut_at > 0 else char_budget
        # Never cut the seed nodes: they render first, so if the budget lands
        # inside the seed block, extend the cut to cover it. The symbol the
        # question named must always be in the answer (#BUG2). Seeds are bounded
        # (_pick_seeds max_k + one per term), so the overshoot is a few lines.
        if seed_hits:
            seed_block_end = sum(len(lines[i]) + 1 for i in range(len(seed_hits))) - 1
            cut_at = max(cut_at, min(seed_block_end, len(output)))
        total_nodes = sum(1 for l in lines if l.startswith("NODE "))
        shown_nodes = output[:cut_at].count("\nNODE ") + (1 if output.startswith("NODE ") else 0)
        cut_count = total_nodes - shown_nodes
        # Nodes render before edges, so a char-budget overflow whose cut lands
        # past the last NODE line drops only trailing edges — no whole node is
        # lost. Announcing "showing N of N nodes … among the 0 cut nodes" then
        # reads as a false truncation warning that teaches an agent to distrust a
        # complete answer and burn follow-up narrowing calls for nodes that were
        # never cut (#2601). When every node is shown the answer is complete, so
        # edges are never dropped either (returning output[:cut_at] here would
        # silently truncate them) — but that completeness guarantee is exactly
        # why a query can quietly cost 4-6x its requested budget once the last
        # node crosses the fit line (#2784): the check above only ever compared
        # the FULL output (nodes+edges) against char_budget, so this branch was
        # already known to be over budget, yet said nothing about it. Report the
        # real size instead of silence — still the complete, non-truncated
        # answer, just an honest one.
        if cut_count == 0:
            # Reached only inside `len(output) > char_budget`, so every node
            # fits but the full nodes+edges output does not: an honest
            # over-budget notice, never a truncation.
            total_edges = sum(1 for l in lines if l.startswith("EDGE "))
            est_tokens = len(output) // 3
            return (
                f"[i] Complete answer over budget: all {total_nodes} nodes and "
                f"{total_edges} edges shown (~{est_tokens} tokens vs the "
                f"requested ~{token_budget}-token budget). Edges are never "
                f"dropped once every node fits, so this is already the full "
                f"answer — raising --budget further will not shrink it. Narrow "
                f"with context_filter=['call'] or use get_node for a specific "
                f"symbol to reduce size instead.\n\n"
            ) + output
        # Prominent notice at the TOP so a truncated answer can never be mistaken
        # for a complete one — silence used to read as absence (#BUG2). The
        # notice + end marker sit OUTSIDE char_budget by design (two bounded
        # wrapper lines, like the existing end marker).
        output = (
            f"[!] TRUNCATED: showing {shown_nodes} of {total_nodes} nodes "
            f"(~{token_budget}-token budget). The answer may be among the "
            f"{cut_count} cut nodes — raise the token budget (CLI: --budget) or "
            f"narrow the query (e.g. context_filter=['call'], or get_node for a "
            f"specific symbol).\n\n"
            + output[:cut_at]
            + f"\n... (truncated — {cut_count} more nodes cut by ~{token_budget}-token budget."
            f" Narrow with context_filter=['call'] or use get_node for a specific symbol)"
        )
    return output


def _cut_lines_to_budget(lines: list[str], token_budget: int, narrow_hint: str) -> str:
    """Render pre-built lines under the same ~3-chars/token budget rule as
    _subgraph_to_text; over-budget output is cut at a line boundary with a count and a
    narrowing hint instead of flooding the caller's context window."""
    output = "\n".join(lines)
    char_budget = token_budget * 3
    if len(output) <= char_budget:
        return output
    cut_at = output[:char_budget].rfind("\n")
    cut_at = cut_at if cut_at > 0 else char_budget
    kept = output[:cut_at]
    shown = kept.count("\n") + 1
    cut_count = len(lines) - shown
    # Announce truncation at the TOP as well, matching _subgraph_to_text — a
    # bottom-only marker reads as silence/absence (the BUG-2 fix rationale). The
    # notice sits outside char_budget by design (one bounded wrapper line).
    return (
        f"[!] TRUNCATED: showing {shown} of {len(lines)} lines "
        f"(~{token_budget}-token budget). {narrow_hint}\n\n"
        + kept
        + f"\n... (truncated — {cut_count} more lines cut by ~{token_budget}-token budget. "
        + narrow_hint
        + ")"
    )


def _display_graph_path(graph_path: str) -> str:
    """Render a graph path for the query header.

    Relative to the CWD when it sits underneath it — `graphify-out/graph.json`,
    which is the ordinary case and stays short. Absolute otherwise, because a
    graph outside the directory you are standing in is precisely the situation
    the header exists to make visible (#2789). Always POSIX separators so the
    line reads the same on either platform. Falls back to the path as given if
    it cannot be resolved; this is a display helper and must never be the reason
    a query fails.
    """
    try:
        p = Path(graph_path).resolve()
        try:
            return p.relative_to(Path.cwd().resolve()).as_posix()
        except ValueError:
            return p.as_posix()
    except (OSError, RuntimeError, ValueError):
        return str(graph_path)


def _query_graph_text(
    G: nx.Graph,
    question: str,
    *,
    mode: str = "bfs",
    depth: int = 3,
    token_budget: int = 2000,
    context_filters: list[str] | None = None,
    graph_path: str | None = None,
) -> str:
    terms = _query_terms(question)
    # One graph scoring pass produces both the combined ranking (used to drive
    # the gap-based seed selection below) and the per-token singleton winners
    # (used by _pick_seeds' per-term guarantee). Previously this was T+1 passes
    # — one combined + one per query token — re-walking the whole graph each
    # time; on a 100k-node, three-term benchmark ~71% of scoring time was
    # spent in those redundant per-term passes.
    qs = _score_query(G, terms, collect_per_term_seeds=True)
    # Relational-intent verbs ("calls", "uses", ...) describe the relation the
    # question asks about, not a symbol to seed from; drop them from the
    # per-term seed GUARANTEE so an incidental verb match cannot seat a decoy
    # BFS root (#2507). They keep their place in `qs.ranked`, so a genuine
    # identifier named after a verb can still win a seat on merit via the gap
    # window — and when the query consists ONLY of intent words (bare "calls"),
    # the guarantee is left intact so such an identifier stays reachable.
    best_seed_by_term = qs.best_seed_by_term
    intent = {t for t in best_seed_by_term if t in _RELATIONAL_INTENT_TERMS}
    if intent and any(t not in _RELATIONAL_INTENT_TERMS for t in terms):
        best_seed_by_term = {
            t: nid for t, nid in best_seed_by_term.items() if t not in intent
        }
    start_nodes = _pick_seeds(qs.ranked, G=G, best_seed_by_term=best_seed_by_term)
    if not start_nodes:
        return "No matching nodes found."
    resolved_filters, filter_source = _resolve_context_filters(question, context_filters)
    traversal_graph = _filter_graph_by_context(G, resolved_filters)
    nodes, edges = _dfs(traversal_graph, start_nodes, depth) if mode == "dfs" else _bfs(traversal_graph, start_nodes, depth)
    header_parts = [
        f"Traversal: {mode.upper()} depth={depth}",
        f"Start: {[G.nodes[n].get('label', n) for n in start_nodes]}",
    ]
    # Name the graph this answer came from. `graphify-out/` resolves against the
    # CWD, so running a query from a parent project while thinking about a
    # vendored subproject silently answers from the wrong corpus — the output is
    # well-formed and confidently wrong, and nothing in it said which graph was
    # opened (#2789). Shown relative when the graph is under the CWD (the normal
    # case, and short), absolute when it is not — which is exactly the case worth
    # noticing. The node count travels with it because "355 nodes" vs "3178
    # nodes" is often the first thing that looks wrong.
    if graph_path:
        header_parts.insert(0, f"Graph: {_display_graph_path(graph_path)} "
                               f"({G.number_of_nodes()} nodes)")
    if resolved_filters:
        header_parts.append(f"Context: {', '.join(resolved_filters)} ({filter_source})")
    header_parts.append(f"{len(nodes)} nodes found")
    header = " | ".join(header_parts) + "\n\n"
    # Pass the seeds so the queried symbol renders first and survives truncation
    # (#BUG2): a branch merge had silently dropped this argument, leaving the
    # seed-first ordering as dead code.
    return header + _subgraph_to_text(traversal_graph, nodes, edges, token_budget, seeds=start_nodes)


def _find_node_tiers(
    G: nx.Graph, label: str
) -> tuple[list[str], list[str], list[str], list[str]]:
    """Return match tiers in precedence order: (source_exact, exact, prefix, substring).

    Split out of `_find_node` so callers that must not guess between equally-good
    matches can inspect the winning tier alone. `_find_node` flattens these, and
    its consumers take `[0]` — which resolves by graph-iteration order when one
    tier holds several nodes from different files. See `find_node_ambiguity`.
    """
    term = " ".join(_search_tokens(label))
    if not term:
        return []
    # Punctuation-preserving normalized query. `term` tokenizes on \w+ (so
    # "blockStream.ts" -> "blockstream ts", space where the '.' was), but a node's
    # stored `norm_label` keeps punctuation ("blockstream.ts"). Matching only via
    # `term`/`label_tokens` works when the node label tokenizes the same way, but is
    # fragile if `label` and `norm_label` diverge. `norm_query` matches `norm_label`
    # symmetrically so an exactly-typed punctuated label always resolves (#1704).
    # `nid_norm` below extends that symmetry to node ids, which keep their
    # punctuation too and are compared raw against the tokenized `term` (#2467).
    norm_query = _strip_diacritics(str(label)).lower().strip()
    source_exact: list[str] = []
    exact: list[str] = []
    prefix: list[str] = []
    substring: list[str] = []
    # Trigram prefilter (graph-iteration order preserved so exact/prefix/substring
    # ordering — and thus matches[0] — is byte-identical to the full scan).
    candidate_ids = _trigram_candidates(G, [term, norm_query])
    node_iter = (
        G.nodes(data=True) if candidate_ids is None
        else ((nid, G.nodes[nid]) for nid in candidate_ids)
    )
    for nid, d in node_iter:
        norm_label = d.get("norm_label") or _strip_diacritics(d.get("label") or "").lower()
        bare_label = norm_label.rstrip("()")
        label_tokens = " ".join(_search_tokens(d.get("label") or ""))
        source_tokens = " ".join(_search_tokens(d.get("source_file") or ""))
        nid_lower = nid.lower()
        # `_strip_diacritics` is the identity on ASCII, so the NFKD fold is only
        # paid for ids that actually carry non-ASCII text.
        nid_norm = nid_lower if nid.isascii() else _strip_diacritics(nid).lower()
        if term == source_tokens:
            source_exact.append(nid)
        elif (
            term == norm_label or term == bare_label or term == label_tokens or term == nid_lower
            or norm_query == norm_label or norm_query == bare_label or norm_query == nid_norm
        ):
            exact.append(nid)
        elif (
            norm_label.startswith(term)
            or bare_label.startswith(term)
            or label_tokens.startswith(term)
            or nid_lower.startswith(term)
            or norm_label.startswith(norm_query)
            or bare_label.startswith(norm_query)
        ):
            prefix.append(nid)
        elif term in norm_label or term in label_tokens or norm_query in norm_label:
            substring.append(nid)

    if source_exact:
        query_basename = _strip_diacritics(Path(label).name).lower()
        preferred = []
        for nid in source_exact:
            if str(G.nodes[nid].get("source_location", "")) != "L1":
                continue
            # File-node label is the bare basename OR a directory-qualified form
            # from the #2032 disambiguation pass (e.g. "process-order/index.ts").
            lbl = _strip_diacritics(str(G.nodes[nid].get("label") or "")).lower()
            if lbl == query_basename or lbl.endswith("/" + query_basename):
                preferred.append(nid)
        if len(preferred) == 1:
            source_exact = preferred + [nid for nid in source_exact if nid != preferred[0]]

    return source_exact, exact, prefix, substring


def _find_node(G: nx.Graph, label: str) -> list[str]:
    """Return node IDs whose label or ID matches the search term (diacritic-insensitive).

    Results are ordered by precedence: exact source-file path match first, then
    exact (label/ID) match, then prefix match, then substring match. Node-ID exact
    matches are grouped with label exact matches.
    """
    source_exact, exact, prefix, substring = _find_node_tiers(G, label)
    return source_exact + exact + prefix + substring


def find_node_ambiguity(G: nx.Graph, label: str) -> list[str]:
    """Return rival candidates when the winning match tier spans several source files.

    `_find_node` ranks matches but never reports that a tie was broken, so callers
    taking `[0]` present one arbitrary file as the answer. Two workspaces that each
    define `MetricsPort` put both nodes in the same `exact` tier, separated only by
    `G.nodes()` iteration order — reorder the graph and the same query answers with
    a different file, equally confidently.

    Returns one representative node id per distinct source file when the winning
    tier is split that way, else `[]`. Several matches *within one file* (a file
    node plus its members) are ordinary precedence, not ambiguity, and return `[]`.

    `_disambiguate_file_node_labels` (#2032) already relabels colliding *file*
    nodes; this covers the symbol case it does not reach.
    """
    for tier in _find_node_tiers(G, label):
        if not tier:
            continue
        by_source: dict[str, str] = {}
        for nid in tier:
            source = str(G.nodes[nid].get("source_file") or "")
            by_source.setdefault(source, nid)
        return list(by_source.values()) if len(by_source) > 1 else []
    return []


def _resolve_single_node(G: nx.Graph, label: str) -> tuple[str | None, str | None]:
    """Shared node resolution for the get_node / get_neighbors tools.

    Returns ``(node_id, None)`` when *label* resolves to a single winner via the
    tiered `_find_node` ranking, or ``(None, message)`` when there is no match or
    the winning tier spans several source files. Routing both tools through this
    keeps get_node from silently returning a `G.nodes()` iteration-order match for
    a hub name while get_neighbors reports the same lookup as ambiguous (#ADR-0001).
    """
    matches = _find_node(G, label)
    if not matches:
        return None, f"No node matching '{label}' found."
    rivals = find_node_ambiguity(G, label)
    if rivals:
        listing = "\n".join(
            f"  {G.nodes[r].get('source_file') or r}\n    id: {r}" for r in rivals
        )
        return None, (
            f"Ambiguous: '{label}' matches {len(rivals)} nodes in different files.\n"
            f"{listing}\n"
            "Retry with the repo-relative path or the full node id."
        )
    return matches[0], None


def _shortest_path_text(G: nx.Graph, arguments: dict) -> str:
    """Body of the `shortest_path` MCP tool (module-level so tests can call it
    without an mcp install).

    Directed by default (#2487): the returned path must follow stored
    caller→callee direction; pass ``undirected=True`` to ignore it.
    """
    src_scored = _score_nodes(G, [t.lower() for t in arguments["source"].split()])
    tgt_scored = _score_nodes(G, [t.lower() for t in arguments["target"].split()])
    if not src_scored:
        return f"No node matching source '{arguments['source']}' found."
    if not tgt_scored:
        return f"No node matching target '{arguments['target']}' found."
    src_nid = _pick_scored_endpoint(G, src_scored, arguments["source"])
    tgt_nid = _pick_scored_endpoint(G, tgt_scored, arguments["target"])
    # Ambiguity guard: when both queries resolve to the same node, the
    # shortest path is trivially zero hops, which is almost never what the
    # caller wanted (see bug #828).
    if src_nid == tgt_nid:
        return (
            f"'{arguments['source']}' and '{arguments['target']}' both resolved to "
            f"the same node '{src_nid}'. Use a more specific label or the exact node ID."
        )
    warnings: list[str] = []
    for name, scored, nid in (
        ("source", src_scored, src_nid),
        ("target", tgt_scored, tgt_nid),
    ):
        # Only meaningful when the raw score head is what got picked — a
        # full-token override was chosen on token coverage, not score.
        if len(scored) >= 2 and nid == scored[0][1]:
            top, runner = scored[0][0], scored[1][0]
            if top > 0 and (top - runner) / top < 0.10:
                warnings.append(
                    f"warning: {name} match was ambiguous "
                    f"(top score {top:g}, runner-up {runner:g})"
                )
    max_hops = int(arguments.get("max_hops", 8))
    undirected = bool(arguments.get("undirected", False))
    try:
        # Deterministic path (#2074): the hash-seeded undirected view picked an
        # arbitrary route among equal-length paths. Build a sorted, materialized
        # graph so the chosen path is canonical. Serve's shared G is left
        # untouched (its degree feeds query-seed tie-breaks).
        if undirected:
            _und = nx.Graph()
            _und.add_nodes_from(sorted(G.nodes))
            _und.add_edges_from(sorted((min(u, v), max(u, v)) for u, v in G.edges()))
            path_nodes = nx.shortest_path(_und, src_nid, tgt_nid)
        else:
            # Directed by default (#2487). True direction is NOT raw arc
            # order: legacy canonicalized files persist a flipped arc with
            # _src/_tgt markers (#2309), so build the digraph from _src/_tgt
            # (falling back to the loaded arc) rather than to_directed().
            _dg = nx.DiGraph()
            _dg.add_nodes_from(sorted(G.nodes))
            _dg.add_edges_from(sorted(
                (d.get("_src", u), d.get("_tgt", v)) for u, v, d in G.edges(data=True)
            ))
            path_nodes = nx.shortest_path(_dg, src_nid, tgt_nid)
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        src_label = G.nodes[src_nid].get("label", src_nid)
        tgt_label = G.nodes[tgt_nid].get("label", tgt_nid)
        if undirected:
            return f"No path found between '{src_label}' and '{tgt_label}'."
        return (
            f"No directed path found between '{src_label}' and '{tgt_label}'. "
            "Retry with undirected=true to search ignoring edge direction."
        )
    hops = len(path_nodes) - 1
    if hops > max_hops:
        return f"Path exceeds max_hops={max_hops} ({hops} hops found)."
    segments = []
    for i in range(len(path_nodes) - 1):
        u, v = path_nodes[i], path_nodes[i + 1]
        # Report the actual stored relation(s), never a fabricated `calls`;
        # fall back to an honest "related" when the edge has no relation (#2074).
        # Direction truth lives in the per-link _src/_tgt markers (#2309): a
        # legacy canonicalized file can persist a flipped arc, so classify each
        # hop by _src (falling back to the arc tail) instead of raw arc order.
        fwd, bwd = [], []
        for a, b in ((u, v), (v, u)):
            if G.has_edge(a, b):
                for d in edge_datas(G, a, b):
                    (fwd if d.get("_src", a) == u else bwd).append(d)
        datas = fwd or bwd
        forward = bool(fwd)
        rels = sorted({d.get("relation") for d in datas if d.get("relation")})
        rel = "/".join(rels) if rels else "related"
        confs = sorted({d.get("confidence") for d in datas if d.get("confidence")})
        conf_str = f" [{'/'.join(confs)}]" if confs else ""
        if i == 0:
            segments.append(G.nodes[u].get("label", u))
        if forward:
            segments.append(f"--{rel}{conf_str}--> {G.nodes[v].get('label', v)}")
        else:
            segments.append(f"<--{rel}{conf_str}-- {G.nodes[v].get('label', v)}")
    prefix = ("\n".join(warnings) + "\n") if warnings else ""
    return prefix + f"Shortest path ({hops} hops):\n  " + " ".join(segments)


def _filter_blank_stdin() -> None:
    """Filter blank lines from stdin before MCP reads it.

    Some MCP clients (Claude Desktop, etc.) send blank lines between JSON
    messages. The MCP stdio transport tries to parse every line as a
    JSONRPCMessage, so a bare newline triggers a Pydantic ValidationError.
    This installs an OS-level pipe that relays stdin while dropping blanks.
    """
    r_fd, w_fd = os.pipe()
    saved_fd = os.dup(sys.stdin.fileno())

    def _relay() -> None:
        try:
            with open(saved_fd, "rb") as src, open(w_fd, "wb") as dst:
                for line in src:
                    if line.strip():
                        dst.write(line)
                        dst.flush()
        except Exception:
            pass

    threading.Thread(target=_relay, daemon=True).start()
    os.dup2(r_fd, sys.stdin.fileno())
    os.close(r_fd)
    sys.stdin = open(0, "r", closefd=False)


def _community_header(cid: int, community_name) -> str:
    # Header for get_community: "Community N — Name", matching get_node / query
    # output which read the community_name attribute to_json writes onto nodes.
    # Skip the name when it is just the "Community N" placeholder (written for
    # unnamed communities) so the header never reads "Community 12 — Community 12";
    # also falls back to the bare id when there is no name. Name is sanitised
    # (F-010) like every other LLM-derived field.
    base = f"Community {cid}"
    if community_name:
        clean = sanitize_label(str(community_name))
        if clean and clean != base:
            return f"{base} — {clean}"
    return base


# === CUSTOM: A1b 检索诚实性信封（Phase 4）====================================
# verdict ∈ {ok, low_confidence, absent, degraded}，freshness ∈ {fresh, stale_index,
# rebuilding}；confidence 初期一律 declared（§8 纪律）。状态文件解析按 A1a 的
# schema 自包含实现——serve.py 不 import scripts/rebuild_entry（graphify 包不反向
# 依赖 scripts/）；complete/error 载荷不含 db_fingerprint（以 A1a 实现为准），
# 本层只依赖 phase/started/last_duration 三字段。
_REBUILD_STALE_FLOOR_S = 1800  # 自愈上限 30 分钟，不依赖人工干预

# 检索型工具清单（判据 = 响应内容反映图数据现状）；新工具（B1/B3/C 系）登记处——
# 清单外工具裸 str 直通，新增检索型工具若拒绝登记则出口不予合并。
# C 工具族空结果语义（M2，Task 12 前统一登记，防第三种发明；Task 13 补 C2 行——
# C2 归 C1 派：空 = 有效结论）：
#   C1 find_dead_code       空 = 有效结论（无死代码），found 恒 True + low_confidence
#   C2 get_untested_symbols 空 = 有效结论（全部符号已被测试覆盖），found 恒 True + low_confidence
#   C3 get_changed_symbols  空 = absent（无变更信息 ≠ ok）
#   C4 get_hotspots         空 = absent（通常环境缺轴：非 git/无 DB）
_SEARCH_TOOLS = frozenset({"query_graph", "get_node", "get_neighbors", "get_community",
                           "god_nodes", "shortest_path", "graph_stats",
                           "get_ranked_context",  # CUSTOM: B1 融合检索登记
                           "get_changed_symbols",  # CUSTOM: C3 git 轴登记（Task 10）
                           "get_hotspots",  # CUSTOM: C4 热区（churn×度数代理）登记（Task 11）
                           "find_dead_code",  # CUSTOM: C1 死代码（入口闭包+闸门）登记（Task 12）
                           "get_untested_symbols"})  # CUSTOM: C2 未覆盖符号（测试子图可达）登记（Task 13）


def _envelope(text: str, verdict: str, freshness: str, **extra) -> str:
    """A1b 尾部行信封：既有正文逐字节不变，追加 '\\n\\n_meta: {json 单行}'。"""
    meta = {"verdict": verdict, "freshness": freshness, "confidence": "declared", **extra}
    return f"{text}\n\n_meta: {json.dumps(meta, ensure_ascii=False, separators=(',', ':'))}"


def _derive_freshness(state_path):
    """从 A1a 状态文件推导 freshness；路径联动总条款见 call_tool 出口。

    - 无状态文件 = 未迁移项目，守卫回退 fresh（不得全标 stale）
    - rebuilding 超时效（max(2*last_duration, 1800)s）判 stale_index（Q3 时效逃生，
      kill -9 残留的自愈上限）
    - complete 态比对 FTS 缓存 vs graph.json（is_fresh 指纹对比：meta 表记录构建时
      graph.json 的 (mtime_ns, size)；缓存落后于事实层→stale_index，一致→fresh）
    """
    try:
        d = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "fresh"  # 无状态文件 = 未迁移项目，守卫回退
    if not isinstance(d, dict):
        # 终审 Imp-1：损坏状态文件（如 [] 等非 dict）——call_tool 对每个工具（含
        # list_prs）都求值 freshness，AttributeError 会把所有调用变 Error executing，
        # serve 永不自愈。非 dict 即损坏形态，守卫回退 fresh（与兄弟读取器
        # _tool_get_changed_symbols / rebuild_entry._read_prev_git_head 同防护）。
        return "fresh"
    if d.get("phase") == "rebuilding":
        limit = max(2 * float(d.get("last_duration", 0)), _REBUILD_STALE_FLOOR_S)
        if time.time() - float(d.get("started", 0)) > limit:
            print(f"[serve] 状态文件超时效（{limit:.0f}s），判 stale_index", file=sys.stderr)
            return "stale_index"
        return "rebuilding"
    # complete 态：FTS 缓存 vs 事实层（06 票换源——旧链路 WAL mtime 对比退役）。
    # 缓存落后于事实层（缓存缺失 / 指纹失配 = graph.json 更新于缓存）→ stale_index
    # 诚实标注；一致 → fresh。is_fresh 即指纹对比（meta 表记录构建时 graph.json 的
    # (mtime_ns, size)；A1 已落地 stat 先于 read，接线不依赖旧时序）。
    # graph.json 与状态文件同目录（自动兼容 GRAPHIFY_OUT 重定向），不硬编码 'graphify-out'。
    fts = _fts_cache()
    cache = state_path.parent / ".fts-index.db"
    graph = state_path.parent / "graph.json"
    try:
        if not graph.exists():
            return "stale_index"   # R5-3：事实层缺失即最陈旧形态，不崩出口
        if fts is None:
            return "stale_index"   # B3：fts_cache 不可用（非 editable 安装）——缓存状态不可知，最诚实 = 陈旧
        return "fresh" if fts.is_fresh(cache, graph) else "stale_index"
    except OSError:
        # freshness 是附加诚实层，无权让 call_tool 出口崩溃杀死全部响应；
        # 读失败即最保守形态，判 stale_index 诚实且安全。
        return "stale_index"


def _derive_verdict(tool: str, found: bool, scanned_nodes: int, degraded: bool = False):
    """verdict 三分推导：degraded 压倒一切 > found=ok > absent（携带扫描计数）。

    degraded/found/scanned_nodes 全由出口（call_tool）装配，工具只报事实；
    low_confidence 无推导路径（R3-3：工具侧诚实自评经 verdict_override 直通）。
    """
    if degraded:
        return "degraded", {}
    if found:
        return "ok", {}
    if scanned_nodes > 0:
        return "absent", {"scanned_nodes": scanned_nodes}
    return "absent", {"scanned_nodes": 0, "empty_graph": True}


def _apply_envelope(name: str, result, freshness: str, verdict_override: str | None = None) -> str:
    """N1 返回契约：检索型清单内 result=(text, found, scanned_nodes) 解包过信封；
    清单外裸 str 直通。模块级纯函数——不经 MCP server 即可测.
    R3-3：verdict_override 非 None 时直通（_derive_verdict 无 low_confidence 推导路径——
    它是工具侧的诚实自评，不是出口可推导的；B3 遍历档/C 系分析工具用）。
    B2：get_node 自报 override——result 四元组 (text, found, scanned, override) 取末元
    （显式 param 优先，契约不破）。verdict 优先级：degraded（freshness=rebuilding）压倒
    一切 > override > 推导（Task 2 minor 交接：override 原分支绕过 degraded，B2 首个真实
    消费者，两者可同现——顺带修正并锁定）。
    B3：5 元组 (text, found, scanned, override, extra_meta) 时 extra_meta 并入 _meta
    （blast-radius 的 _meta.closure_size）；3/4 元组路径逐字节不受影响（tool_meta={} 不并入）。
    """
    if name not in _SEARCH_TOOLS:
        return result
    tool_meta = {}
    if isinstance(result, tuple) and len(result) == 5:
        text, found, scanned, tool_override, tool_meta = result
        if verdict_override is None:
            verdict_override = tool_override   # 无显式 param 时取工具自报
    elif isinstance(result, tuple) and len(result) == 4:
        text, found, scanned, tool_override = result
        if verdict_override is None:
            verdict_override = tool_override   # 无显式 param 时取工具自报
    else:
        text, found, scanned = result
    degraded = freshness == "rebuilding"
    v, meta = _derive_verdict(tool=name, found=found, scanned_nodes=scanned, degraded=degraded)
    if v != "degraded" and verdict_override is not None:
        v, meta = verdict_override, {}
    if tool_meta:
        meta = {**meta, **tool_meta}
    return _envelope(text, verdict=v, freshness=freshness, **meta)


# === CUSTOM: 06 票 FTS 缓存消费侧（点查类工具换源）============================


def _fts_cache():
    """lazy fts_cache（scripts/ 无包结构——graphify 包不反向依赖 scripts/，rebuild_entry
    先例：消费侧调用时才挂 sys.path）。模块缓存于 sys.modules，后续调用近零开销。
    B3（06 二轮评审）：import 失败（非 editable 安装缺 scripts/）→ 返回 None——调用方
    诚实降级（freshness 判 stale_index、get_node 走无缓存路径），不逃逸 ModuleNotFoundError
    （_derive_freshness 每次工具调用执行，裸 import 会让所有工具变 Error executing）。"""
    import sys as _sys
    _scripts_dir = str(Path(__file__).resolve().parent.parent / "scripts")
    if _scripts_dir not in _sys.path:
        _sys.path.insert(0, _scripts_dir)
    try:
        import fts_cache
        return fts_cache
    except ImportError:
        return None


def _ensure_fts_retry(graph_path, fts_path, attempts=5, base_delay=0.1):
    """ensure_fts + os.replace 锁重试退避（Task 05 ⚠️③ checklist 接线决策）。

    连接纪律：serve 侧一律短连接（查询完即关，Windows 只读句柄窗口最小）；残余碰撞
    （并发查询线程持句柄时 os.replace WinError 5）用指数退避重试兜底（5 次，0.1→0.8s）。
    指纹命中/重建成功返回；重试耗尽向上抛（缓存构建失败是事实层派生失败，诚实暴露给
    get_node 的 except 降级路径）。fts_cache 不可用（B3，_fts_cache 返回 None）→ 直接
    False 不重建——调用方按无缓存降级。"""
    fts = _fts_cache()
    if fts is None:
        return False
    delay = base_delay
    for attempt in range(attempts):
        try:
            return fts.ensure_fts(graph_path, fts_path)
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)
            delay *= 2
    return False


# === CUSTOM: B2 get_node include_source 符号切片（Phase 4）====================
# 06 票换源：切片原语升级 end_byte 字节切（Task 01 原语）——source_location
# `L<line>:C<col>`（C = 1-based 字节列，与 end_byte 同字节坐标系）定位起点，end_byte
# （exclusive）定终点，raw 字节切后 decode——行尾/同行尾随代码不再误收。end_byte 缺失
# （legacy 图/文件节点）→ 行级切片回退（旧链路语义）。校验失败仍走行级 fuzzy 重定位。

_SOURCE_LOC_RE = re.compile(r"^L(\d+)(?::C(\d+))?$")


def _line_byte_starts(raw: bytes) -> list[int]:
    """1-based 每行起始字节偏移（按 \\n 切——与 tree-sitter end_byte 字节坐标系一致）。
    尾随空行剔除（splitlines 同语义：'a\\n' 只有 1 行）。"""
    starts = [0]
    for i, b in enumerate(raw):
        if b == 0x0A:
            starts.append(i + 1)
    if raw.endswith(b"\n") and len(starts) > 1:
        starts.pop()
    return starts


def _slice_raw_lines(raw: bytes, starts: list[int], lo: int, hi: int, n: int) -> str:
    """行区间 [lo, hi]（1-based 含）的字节切——行尾换行剔除（splitlines 展示无差异）。"""
    exp_start = starts[lo - 1]
    if hi < n:
        exp_end = starts[hi] - 1              # 排除行 hi 的 \n（starts[hi] = 行 hi+1 起点）
    else:
        exp_end = len(raw)
        if exp_end and raw[exp_end - 1] == 0x0A:
            exp_end -= 1                       # 尾行无换行/尾随换行统一剔除
    return raw[exp_start:exp_end].decode("utf-8", errors="replace")


def _slice_source(project_root, file_path, source_location, end_line, end_byte,
                  name, signature=None, pad=0):
    """B2 符号切片：字节精确——source_location `L<line>:C<col>` 起点 + end_byte 终点
    （exclusive）。end_byte 缺失（legacy 图/文件节点）→ 行级切片回退（旧链路语义）。
    校验（name 或 signature 出现于切片头部 3 行内）失败 → 全文逐行 fuzzy 定位
    `def/class <name>` 行并重定区间 ±40 行；仍失败 → (原切片, False, 0, 0)。
    pad>0：最终区间前后各扩 pad 行——body+context 的 ±3 上下文窗内聚于此（单次读文件
    完成切片+扩窗，消除原 _last_slice_meta 模块级全局 + TOCTOU 双读）。重定位窗尾非
    文件尾（±40 硬窗可能截断长函数）→ 附 `(relocated window, may be truncated)` 提示行。
    返回 (切片文本, slice_verified, slice_start, slice_end)。
    """
    src = Path(project_root) / file_path
    try:
        raw = src.read_bytes()
    except OSError:
        return "", False, 0, 0
    m = _SOURCE_LOC_RE.match(source_location or "")
    if not m:
        return "", False, 0, 0
    start_line, start_col = int(m.group(1)), (int(m.group(2)) if m.group(2) else None)
    starts = _line_byte_starts(raw)
    n = len(starts)
    if n == 0 or start_line > n:
        return "", False, 0, 0

    def _expand(lo: int, hi: int) -> tuple[int, int]:
        return max(1, lo - pad), min(n, hi + pad)

    body_text = ""
    # ── 字节精确主路径（新链路 end_byte 原语）──
    if end_byte is not None:
        start_byte = starts[start_line - 1] + (start_col - 1 if start_col else 0)
        if 0 <= start_byte < end_byte <= len(raw):
            body_text = raw[start_byte:end_byte].decode("utf-8", errors="replace")
            head = "\n".join(body_text.splitlines()[:3])
            if (name and name in head) or (signature and signature in head):
                lo, hi = _expand(start_line, end_line or start_line)
                text = _slice_raw_lines(raw, starts, lo, hi, n) if pad else body_text
                return text, True, lo, hi
    # ── 行级回退（legacy 无 end_byte / 字节切校验失败但有行区间）──
    if end_byte is None and end_line is not None:
        lines = raw.decode("utf-8", errors="replace").splitlines()
        nl = len(lines)
        body_text = "\n".join(lines[start_line - 1:min(end_line, nl)])
        head = "\n".join(lines[start_line - 1:min(start_line + 2, nl)])
        if body_text and ((name and name in head) or (signature and signature in head)):
            lo, hi = _expand(start_line, end_line)
            return "\n".join(lines[lo - 1:hi]), True, lo, hi
    # 行漂移 / 无区间：fuzzy 逐行定位 def/class + name（±40 硬窗）
    lines = raw.decode("utf-8", errors="replace").splitlines()
    nl = len(lines)
    for i, ln in enumerate(lines, 1):
        stripped = ln.lstrip()
        if name and name in ln and (stripped.startswith("def ") or stripped.startswith("class ")):
            lo, hi = _expand(i - 40, i + 40)
            relocated = "\n".join(lines[lo - 1:hi])
            if relocated:
                # L1：±40 硬窗对长函数（>80 行）可能静默截断——窗尾非文件尾时附提示
                if hi < nl:
                    relocated += "\n(relocated window, may be truncated)"
                return relocated, True, lo, hi
    return body_text, False, 0, 0


def _verdict_for_source_request(has_source: bool, explicit_body: bool):
    """B2 Q9 分档：语义节点（无源文件/DB 行）显式请求 body/body+context → 'absent'
    （get_node 装配 scanned=1，扫描语义表）；缺省/none/signature 档名片正常返回 → None
    （Code: 段自然省略）。"""
    if not has_source and explicit_body:
        return "absent"
    return None


def _card_short_name(d: dict, nid: str) -> str:
    """B2 名片短名（函数/方法/类名）：qualified_name 优先（原生形态 `Class::method`/裸名，
    无括号）；回退 label 末段并剥 `()` 调用后缀——原生函数/方法 label 带 `()`/`.method()`
    形态（旧链路 label 是 dotted qualified_name 无括号），`def <short>` 重构与切片校验
    都需要裸名。"""
    raw = str(d.get("qualified_name") or d.get("name") or d.get("label") or nid)
    return raw.rsplit(".", 1)[-1].rsplit("::", 1)[-1].removesuffix("()")


def _signature_line(sig: str, short: str) -> str:
    """B2 fix：真实 DB signature 列无 def/class/async 前缀（fork 实测 7378/7378 函数/方法
    签名均无前缀，`__init__` → `(self)`；新链路契约同名，名字不进签名）——缺前缀且为
    括号形态（Python 函数签名）时前置 `def <short_name>` 重构出 def 行；已有前缀原样
    （不重复拼接）；非括号形态（非 Python 语言签名，如 `void (String item)`）不套 Python def。"""
    sig = sig.strip()
    if not sig:
        return ""
    if re.match(r"^(def|class|async def)\b", sig):
        return sig
    if sig.startswith("("):
        return f"def {short}{sig}"
    return sig


def _top_neighbor_ids(G, nid, top=10):
    """B2 M1: 1-hop 邻接去重 + 按邻居度数排序取 top-k（与 B3 blast-radius top-k 精神
    一致——god node 度数 100+ 时邻接只取最重要 top-k）。返回 (top_ids, 去重后总数)。"""
    if G.is_directed():
        nb = list(G.successors(nid)) + list(G.predecessors(nid))
    else:
        nb = list(G.neighbors(nid))
    nbs = list(dict.fromkeys(nb))  # 无向图同一邻居集不再双扫，去重仍留作防御
    ranked = sorted(nbs, key=lambda nb: (-G.degree(nb), nb))
    return ranked[:top], len(nbs)


def _neighbor_signatures(fts_path, ids):
    """B2 M1: FTS 缓存 nodes 表 signature 列（只读短连接）——body+context 邻接段签名
    摘要用。id 是图节点原生形态，直接命中（__cg 消歧后缀回退已删，wayfinder E）。
    返回 {id: signature}。缓存缺失/损坏 → {}（诚实降级，不崩出口）。"""
    if not ids:
        return {}
    import sqlite3 as _sqlite3
    sigs = {}
    try:
        conn = _sqlite3.connect(f"file:{fts_path.as_posix()}?mode=ro", uri=True)
        try:
            ph = ",".join("?" * len(ids))
            for nid, sig in conn.execute(
                    f"SELECT id, signature FROM nodes WHERE id IN ({ph})", tuple(ids)):
                sigs[nid] = sig or ""
        finally:
            conn.close()
    except _sqlite3.Error:
        return {}
    return sigs


def _neighbor_summary_lines(G, nid, fts_path, top=10):
    """B2 M1: 1-hop 邻接签名摘要——每邻居 label + Signature:（FTS 缓存 nodes 表 signature
    列），度数排序取 top-k，超限标 (+N more)。替代 _tool_get_neighbors 全量邻接文本
    （god node 时是 token 放大器）。label 过 sanitize_label（图 label 可为 LLM 字段，
    F-010）；Signature 是代码文本非 LLM 字段（L2 语义锁定：不做 F-010 sanitize）。"""
    lines = []
    top_ids, total = _top_neighbor_ids(G, nid, top=top)
    sigs = _neighbor_signatures(fts_path, top_ids)
    for nb in top_ids:
        label = str(G.nodes[nb].get("label", nb))
        sig = sigs.get(nb)
        if sig:
            nb_short = _card_short_name(G.nodes[nb], nb)
            lines.append(f"  {sanitize_label(label)}  Signature: {_signature_line(sig, nb_short).replace('\n', '\n  ')}")
        else:
            lines.append(f"  {sanitize_label(label)}")
    extra = total - len(top_ids)
    if extra > 0:
        lines.append(f"  (+{extra} more)")
    return lines


def _get_node_tool(G, active_graph_path, arguments: dict) -> tuple[str, bool, int, str | None]:
    """B2 get_node 正文装配（模块级——test 不经 mcp 可直接调用，_shortest_path_text 先例；
    serve 闭包 _tool_get_node 只做薄转发）。06 票换源：元数据点查走 .fts-index.db nodes
    表（05 票三表之一）——id 是图节点原生 kind+hash 形态，直接命中（__cg 消歧后缀与回退
    点查已删，wayfinder E）；源码切片升级 end_byte 字节切（Task 01 原语）。连接纪律：
    短连接 + ensure_fts os.replace 锁重试退避（Task 05 ⚠️③ checklist 接线决策）。
    返回 (text, found, scanned, verdict_override)（B2 四元组，末元工具侧诚实自评 R3-3）。"""
    import sqlite3 as _sqlite3
    label = arguments["label"].lower()
    include_source = arguments.get("include_source", "signature")
    nid, err = _resolve_single_node(G, label)
    if err:
        # CUSTOM: N1 found=err is None（机械约定，Ambiguous 亦计 absent 文本保留）；
        # scanned=1（扫描语义表：get_node 只针对单节点解析）。
        return err, False, 1, None
    d = G.nodes[nid]
    card = [_format_node_card(G, nid, d)]  # none 档锚点：与扩展前逐字节一致
    # 路径联动总条款：root/缓存路径从当前 active graph（graphify-out/graph.json）推导
    # （与 _derive_freshness / _tool_get_ranked_context 同构）。FTS 缓存由同一
    # graph.json 投影（05 _build），节点 id 逐字同源——WHERE id=? 直接命中，无需
    # __cg 基 id 回退。缓存缺失/指纹失配先 ensure_fts 惰性重建（锁重试退避）。
    row = None
    fts_path = Path(active_graph_path).parent / ".fts-index.db"
    fts = _fts_cache()
    if fts is not None:
        # B3（06 二轮评审）：fts_cache 不可用（非 editable 安装缺 scripts/）→ 无缓存降级
        # （row=None → has_source=False → body 档 absent）——不逃逸 AttributeError。
        try:
            _ensure_fts_retry(active_graph_path, fts_path)
            conn = fts.open_readonly(fts_path)
            try:
                row = conn.execute(
                    "SELECT source_file, source_location, end_line, end_byte, "
                    "signature, docstring FROM nodes WHERE id = ?", (nid,)).fetchone()
            finally:
                conn.close()
        except (_sqlite3.Error, OSError):
            row = None  # 缓存缺失/损坏/锁重试耗尽：语义节点等价（has_source=False，诚实降级）
    # 可切片判定：source_file + 可解析起点（L<line>）——概念/语义节点 source_location
    # 为 null，无切片面（旧链路 DB 无概念节点 → has_source=False → body 档 absent 等价）。
    has_source = (row is not None and bool(row[0])
                  and bool(_SOURCE_LOC_RE.match(row[1] or "")))
    explicit_body = include_source in ("body", "body+context")
    override = _verdict_for_source_request(has_source=has_source, explicit_body=explicit_body)
    if include_source == "none":
        # none 档回归锚点：名片无 Signature:/Doc:/Code: 行，与扩展前逐字节一致
        return "\n".join(card), True, 1, override
    # 名片增强：Signature:/Doc: 行（FTS 缓存一次查询带出）。short = 函数/方法/类名
    # （_card_short_name 剥原生 label 的 `()` 后缀），body 与 signature 两档共用。
    short = _card_short_name(d, nid)
    if row is not None:
        sig = (row[4] or "").strip()
        doc = (row[5] or "").strip()
        if sig:
            # 新链路 signature 列无 def 前缀（提取契约：名字不进签名）——重构出 def 行。
            # B2 L2：Signature 是代码文本非 LLM 字段——不做 F-010 sanitize
            # （sanitize_label 剥 \n/\t 等控制空白，变形多行签名；新链路亦存在多行
            # 签名形态，见 test_sanitize_label_strips_*）。
            card.append(f"  Signature: {_signature_line(sig, short)}")
        if doc:
            # Doc: 头行：docstring 预览行（splitlines 已剥换行），保留 sanitize 防御
            card.append(f"  Doc: {sanitize_label(doc.splitlines()[0])}")
    if explicit_body and has_source:
        project_root = Path(active_graph_path).parent.parent
        # B2 M2：pad=3（body+context ±3 上下文窗）内聚于 _slice_source——单次读文件
        # 完成字节切片+扩窗，返回 (text, ok, slice_start, slice_end)；删 _last_slice_meta
        # 全局（FastMCP sync handler 线程池并发 get_node 交错互覆）+ 消 TOCTOU 双读。
        text, slice_ok, _s, _e = _slice_source(
            project_root, row[0], row[1], row[2], row[3], short,
            signature=_signature_line((row[4] or "").strip(), short) or None,
            pad=3 if include_source == "body+context" else 0)
        if slice_ok:
            card.append("Code:")
            card.extend(f"  {ln}" for ln in text.splitlines())
            if include_source == "body+context":
                # B2 M1：1-hop 邻接签名摘要（label + Signature: 双列，度数 top-10 +
                # (+N more) 截断标注）——god node（度数 100+）不拼全量邻接文本
                # （原 _tool_get_neighbors 全量输出是 token 放大器）。
                card.append("Context (1-hop neighbors):")
                card.extend(_neighbor_summary_lines(G, nid, fts_path))
        else:
            # 切片不可得（字节区间失效/legacy 无 end_byte）：名片仍返回，Code: 段省略
            # + 标注 + low_confidence
            card.append("Code: (slice unavailable — source body could not be located precisely)")
            override = override or "low_confidence"
    return "\n".join(card), True, 1, override  # N1 found=节点已解析, scanned=1（扫描语义表）


# === CUSTOM: B3 get_neighbors direction/depth/fanout/blast-radius（Phase 4 Task 9）===


def _symbol_short_name(d: dict, nid: str) -> str:
    """B3 R3-2 合并图短名：label 首 token 末段（消歧后缀 ' (N)' 剥离）。
    serve 自包含副本——graphify 包不反向依赖 scripts/（rebuild_entry._parse_refresh
    "scripts 无包结构，两文件各持自包含副本，不互相导入" 先例）；与 scripts/adapter.py
    _symbol_short_name 同源，防漂移锁定靠 test_dispatch_trace 一致性测试。"""
    return str(d.get("label") or nid).split(" ")[0].rsplit(".", 1)[-1]


def _digraph_view(graph_path) -> nx.DiGraph:
    """B3 R3-1 有向视图：从原始 graph.json 的 links source/target 重建方向（DiGraph）。
    生产 graph.json 实测 directed:false——_load_graph 产 nx.Graph，node_link_graph 无向化
    即丢方向（links 的 source/target 被折叠进无向邻接）；B3 不吃无向 G，反向闭包在无向
    图上混入下游调用方（系统性过报）。
    I2（用户 L3 裁决）：单入口单一载荷缓存（scripts/ranked.py _GRAPH_CACHE lazy 双视图）
    ——DiGraph 与 B1 结构派生（degree/collision_bases/nodes）同 entry 驻留，不二次
    json.loads；独立 _DIGRAPH_CACHE 已删除。serve 接 scripts 走既有 lazy sys.path+import
    先例（_tool_get_ranked_context 同款，graphify 包不反向依赖 scripts/ 只在调用时挂
    sys.path——rebuild_entry 先例，非破例）。
    节点/边属性（kind/label/source_file/relation/confidence）原样带上（E2/N4 纪律）。
    I4（Task 12 实测）：serve 侧 active_graph_path 恒为 str（_default_graph_path 与
    _select_graph 均 str(Path(...))）——ranked.get_digraph/_cache_load 走 graph_path.stat()
    要求 Path，str 直传会 AttributeError。入口统一 Path() 归一化（B3 与 C1 两个调用点
    同受益；Task 9 仅以 Path 测试，str 生产路径为未覆盖的集成盲区，Task 12 闭包实测撞出）。"""
    import sys as _sys
    _scripts_dir = str(Path(__file__).resolve().parent.parent / "scripts")
    if _scripts_dir not in _sys.path:
        _sys.path.insert(0, _scripts_dir)
    from ranked import get_digraph
    return get_digraph(Path(graph_path))


def _reverse_closure(DG: nx.DiGraph, node, depth, top_k) -> tuple[list[str], int]:
    """B3 blast-radius 载体：有向图上反向 BFS（等价 nx.bfs_layers(DG.reverse(copy=False),
    node) 取前 depth 层）得祖先，按度数排序取 top-k；返回 (ids, 全量闭包尺寸)。
    只吃 DiGraph——无向图上"反向"退化为普通 BFS，调用方与被调用方混入 = 系统性过报，
    类型注解 + 运行时 isinstance 断言强制防误用。"""
    if not isinstance(DG, nx.DiGraph):
        raise TypeError("_reverse_closure requires nx.DiGraph (call _digraph_view first)")
    # nx.bfs_layers 首层是源节点自身（距离 0），blast-radius 要距离 1..depth 的祖先，
    # 故切 [1:depth+1]（与"depth=1 即直接邻居"语义一致）。
    layers = list(nx.bfs_layers(DG.reverse(copy=False), node))[1:depth + 1]
    ids = [n for layer in layers for n in layer]
    ranked = sorted(ids, key=lambda n: (-DG.degree(n), n))
    return ranked[:top_k], len(ids)


def _fanout_targets(DG: nx.DiGraph, target_id) -> tuple[list[str], str]:
    """B3 fan-out 重展开：多态调用目标（dispatch 退役后由边置信度 INFERRED/AMBIGUOUS
    信号触发）→ 全部分类 override 候选。三边联走：反向 contains 且 predecessor
    kind=='class' 得所属类 → extends/implements 反向 BFS 得子类集 → 各子类 contains
    出边下短名与 target 短名相等的节点 = 全部可能目标。任一步无果 → (原单目标,
    'unavailable: no owning class')。同名匹配只走 label（id 是 hash 形态无语义，
    R3-2 _symbol_short_name）。"""
    # M5（review）：与 _reverse_closure 一致拒无向图——类型注解 + 运行时 isinstance 断言。
    if not isinstance(DG, nx.DiGraph):
        raise TypeError("_fanout_targets requires nx.DiGraph (call _digraph_view first)")
    if target_id not in DG:
        return [target_id], "unavailable: no owning class"
    tgt_short = _symbol_short_name(DG.nodes[target_id], target_id)
    owning = [p for p in DG.predecessors(target_id)
              if DG.nodes[p].get("kind") == "class"
              and DG.edges[p, target_id].get("relation") == "contains"]
    if not owning:
        return [target_id], "unavailable: no owning class"
    subclasses: list[str] = []
    frontier = list(owning)
    seen = set(owning)
    while frontier:
        nxt = []
        for cls in frontier:
            for child in DG.predecessors(cls):
                if child in seen:
                    continue
                if DG.edges[child, cls].get("relation") not in ("extends", "implements"):
                    continue
                seen.add(child)
                subclasses.append(child)
                nxt.append(child)
        frontier = nxt
    targets = [target_id]
    for cls in subclasses:
        for succ in DG.successors(cls):
            if DG.edges[cls, succ].get("relation") != "contains":
                continue
            if _symbol_short_name(DG.nodes[succ], succ) == tgt_short:
                targets.append(succ)
    return targets, ""


def _neighbors_lines(G, nid, rel_filter=""):
    """B3 缺省路径正文装配：direction/depth 未给定时的既有 G 语义——与扩展前
    _tool_get_neighbors 的 successors/predecessors 循环逐字节一致（无向图上双向输出
    本就同集合；方向只在 B3 有向视图路径用）。模块级提取以便缺省回归锚点测试。
    Task 08：边属性 resolved_by（04 打点）存在时附到行尾（缺省 fixture 无该属性，
    字节锚点回归不变）——边置信度与 resolved_by 从响应读出。"""
    lines = [f"Neighbors of {sanitize_label(G.nodes[nid].get('label', nid))}:"]

    def _edge_at(d: dict) -> str:
        # Edge location = the relation SITE (call/import line) in the source
        # node's file, not a def line (#BUG1).
        loc = str(d.get("source_location") or "")
        return (
            f" at={sanitize_label(str(d.get('source_file') or ''))}:{sanitize_label(loc)}"
            if loc else ""
        )

    def _edge_rb(d: dict) -> str:
        rb = d.get("resolved_by")
        return f" [resolved_by={sanitize_label(str(rb))}]" if rb else ""

    # 生产 G 是 _load_graph 强制 directed:True 的 DiGraph（有向边逐一存储）——
    # 保持 successors/predecessors 双循环逐字节不变；无向 Graph（缺省锚点测试的
    # 逻辑无向形态）退化为 neighbors 双扫，双向输出本就同集合（_top_neighbor_ids 同款）。
    if G.is_directed():
        succ_iter, pred_iter = G.successors(nid), G.predecessors(nid)
    else:
        nb = list(G.neighbors(nid))
        succ_iter, pred_iter = nb, nb
    for nb in succ_iter:
        d = edge_data(G, nid, nb)
        rel = d.get("relation", "")
        if rel_filter and rel_filter not in rel.lower():
            continue
        lines.append(
            f"  --> {sanitize_label(G.nodes[nb].get('label', nb))} "
            f"[{sanitize_label(str(rel))}] [{sanitize_label(str(d.get('confidence', '')))}]"
            f"{_edge_rb(d)}{_edge_at(d)}"
        )
    for nb in pred_iter:
        d = edge_data(G, nb, nid)
        rel = d.get("relation", "")
        if rel_filter and rel_filter not in rel.lower():
            continue
        lines.append(
            f"  <-- {sanitize_label(G.nodes[nb].get('label', nb))} "
            f"[{sanitize_label(str(rel))}] [{sanitize_label(str(d.get('confidence', '')))}]"
            f"{_edge_rb(d)}{_edge_at(d)}"
        )
    return lines


def _toward_edge(DG, closure_set, nid, u, incoming):
    """B3 新路径辅助：闭包节点 u 指向 nid 的那条边——ancestor（incoming）扫 successors、
    descendant 扫 predecessors，取 (closure∪{nid}) 内排序首个；无则 (None, None)。
    排序保证确定性输出（BFS 保证 toward 节点必在闭包内，排序仅影响多后继时的取舍）。"""
    it = DG.successors(u) if incoming else DG.predecessors(u)
    for v in sorted(it):
        if v == nid or v in closure_set:
            return v, DG.edges[(u, v) if incoming else (v, u)]
    return None, None


def _blast_radius_lines(DG, nid, direction, depth, top_k, rel_filter,
                        fanout_limited=False):
    """B3 新路径正文装配（direction 或 depth>1 给定）：_digraph_view 产物上反向/正向闭包
    + 逐节点 toward 边 + 多态标注（仅 depth>1 且 rel=='calls'，R5-5(b)：depth=1 普通
    邻接不标——hub 节点 50-200 邻边逐一展开是 P95 杀手，且 depth=1 场景 agent 本要逐条
    看边、标注价值最低）+ fanout 重展开 + 截断标志。Task 08：dispatch 概念退役——
    标注改读边属性 confidence（INFERRED/AMBIGUOUS 保留信号）+ resolved_by（04 提取期
    打点），不再 JOIN codegraph DB。edge_kinds 已由调用方过滤进 DG（本函数不重收）；
    fanout_limited 由调用方按 edge_kinds 是否含 fanout 遍历关系判定（M1b）。
    返回 (lines, closure_total)。
    """
    if nid not in DG:
        return [f"Neighbors of {sanitize_label(str(nid))} "
                f"(direction={direction}, depth={depth}): (node missing from current directed view)"], 0
    lines = [f"Neighbors of {sanitize_label(str(DG.nodes[nid].get('label') or nid))} "
             f"(direction={direction}, depth={depth}):"]
    FULL = 10 ** 9  # 取全量闭包 id（top-k 截断后 toward 边可能在截断区外，闭包集须全）
    if direction == "in":
        full_ids, total = _reverse_closure(DG, nid, depth, top_k=FULL)
        show = full_ids[:top_k]
        in_set, out_set = set(full_ids), set()
    elif direction == "out":
        full_ids, total = _reverse_closure(DG.reverse(copy=False), nid, depth, top_k=FULL)
        show = full_ids[:top_k]
        in_set, out_set = set(), set(full_ids)
    else:
        in_full, _in_total = _reverse_closure(DG, nid, depth, top_k=FULL)
        out_full, _out_total = _reverse_closure(DG.reverse(copy=False), nid, depth, top_k=FULL)
        merged = list(dict.fromkeys(in_full + out_full))
        total = len(merged)
        show = sorted(merged, key=lambda n: (-DG.degree(n), n))[:top_k]
        in_set, out_set = set(in_full), set(out_full)
    closure_full = in_set | out_set
    for u in show:
        # L1（review 注释登记）：both 闭包下节点双在 in/out 集时只按祖先侧展示
        # （incoming=True + <-> 箭头）——toward 边归属非最优（双向节点可能两侧各
        # 有一条 toward 边，只取祖先侧），已知近似不修（文本摘要工具，主信息
        # "节点在闭包内 + 双向"已正确）。
        if u in in_set and u in out_set:
            arrow, incoming = "<->", True
        elif u in out_set:
            arrow, incoming = "-->", False
        else:
            arrow, incoming = "<--", True
        v, ed = _toward_edge(DG, closure_full, nid, u, incoming)
        if v is None:
            continue
        rel = str(ed.get("relation", ""))
        if rel_filter and rel_filter not in rel.lower():
            continue
        loc = str(ed.get("source_location") or "")
        at = (f" at={sanitize_label(str(ed.get('source_file') or ''))}:{sanitize_label(loc)}"
              if loc else "")
        line = (f"  {arrow} {sanitize_label(str(DG.nodes[u].get('label') or u))} "
                f"[{sanitize_label(rel)}] [{sanitize_label(str(ed.get('confidence', '')))}]{at}")
        # I1（controller 裁决）：多态/fanout 仅对 calls 类边触发——宁多标语义对
        # contains/imports 无意义（无"分发到子类 override"语义），非 calls 边不展开。
        # R5-5(b) 沿用：depth>1 才标（depth=1 普通邻接不标，与工具描述"depth>1 返回
        # blast radius + fanout"一致）。
        if depth > 1 and rel == "calls":
            # C1（review）：out 方向边缘是 (调用方 v → 被调方 u)，fanout 展开的是被
            # 调用方法（分发目标）的类层级——callee 必须随 incoming 分支定序（in/both
            # 入侧真序恰与 (u, v) 一致；out/both 出侧失真需换 (v, u)）。
            _, callee = (u, v) if incoming else (v, u)
            # Task 08：dispatch 概念退役——不再 JOIN codegraph DB（_edge_dispatch_info
            # 已删），标注改读边属性：resolved_by（04 打点，数据读出）+ confidence
            # （INFERRED/AMBIGUOUS 保留信号，多态 fanout 判断）。
            rb = ed.get("resolved_by")
            if rb:
                line += f" [resolved_by={sanitize_label(str(rb))}]"
            if ed.get("confidence") in ("INFERRED", "AMBIGUOUS"):
                # EXTRACTED 确定性调用不标注、不展开；INFERRED/AMBIGUOUS = 分发候选。
                fan_targets, note = _fanout_targets(DG, callee)
                if note:
                    # M1b：edge_kinds 滤掉 fanout 遍历关系（contains/extends）时展开
                    # 受限——文案须区分"被过滤"与"无所属类"（宁多标方向都诚实）。
                    if fanout_limited:
                        line += " fanout=(limited by edge_kinds filter: contains/extends removed)"
                    else:
                        line += f" fanout=({note})"
                elif len(fan_targets) > 1:
                    # fanout 覆写用完整 label（短名歧义：Base.handle 与 Child.handle
                    # 短名同为 handle，列表须可区分）。
                    line += " fanout=(" + ", ".join(
                        sanitize_label(str(DG.nodes[t].get("label") or t))
                        for t in fan_targets[1:]) + ")"
        lines.append(line)
    if total > len(show):
        # M1a：both 分支语义对称性透明化——闭包是 in/out 两方向并集，标注去向
        by = "by degree across both directions" if direction == "both" else "by degree"
        lines.append(f"  (blast radius: {total} nodes; showing top {len(show)} {by})")
    return lines, total


def _format_node_card(G, nid, d) -> str:
    """B2 get_node 名片正文（none 档回归锚点——与扩展前逐字节一致，勿加字段）。
    名片增强（Signature:/Doc:）由 get_node 在非 none 档追加于本卡尾部。"""
    return "\n".join([
        f"Node: {sanitize_label(d.get('label', nid))}",
        f"  ID: {sanitize_label(nid)}",
        f"  Source: {sanitize_label(str(d.get('source_file', '')))} {sanitize_label(str(d.get('source_location', '')))}",
        *([f"  Defined in: {sanitize_label(str(d.get('definition_file', '')))} "
           f"{sanitize_label(str(d.get('definition_location', '')))}"]
          if d.get("definition_file") else []),
        f"  Type: {sanitize_label(str(d.get('file_type', '')))}",
        f"  Community: {sanitize_label(str(d.get('community_name') or d.get('community', '')))}",
        f"  Degree: {G.degree(nid)}",
    ])
# === CUSTOM: B2 end ===========================================================

# === CUSTOM: A1b end ==========================================================


# === CUSTOM: A2 响应自动脱敏（Phase 4）========================================
# CUSTOM: A2 响应脱敏（Phase 4）。L1 厂商前缀类（默认启用）——前缀+长度+字符集三重校验，
# 误报率近零。L2 泛型启发式默认关（逐条校准后启用，见方案 §3-A2）。漏报可接受，误报不可接受。
_REDACT_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    ("openai", re.compile(r"sk-(?!ant-|proj-|svcacct-|None-)[A-Za-z0-9]{32,}")),
    ("openai-proj", re.compile(r"sk-proj-[A-Za-z0-9_-]{40,}")),
    ("openai-svc", re.compile(r"sk-svcacct-[A-Za-z0-9_-]{20,}")),
    ("openai-none", re.compile(r"sk-None-[A-Za-z0-9_-]{20,}")),
    ("anthropic", re.compile(r"sk-ant-(api|admin)\d+-[A-Za-z0-9_-]{80,}")),
    ("gemini", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("huggingface", re.compile(r"hf_[A-Za-z0-9]{34,40}")),
    ("groq", re.compile(r"gsk_[A-Za-z0-9]{48,}")),
    ("openrouter", re.compile(r"sk-or-(v1-)?[A-Za-z0-9_-]{20,}")),
    ("xai", re.compile(r"xai-[A-Za-z0-9]{80}")),
    ("perplexity", re.compile(r"pplx-[A-Za-z0-9]{20,}")),
    ("tavily", re.compile(r"tvly-[A-Za-z0-9]{20,}")),
    ("together", re.compile(r"tgp_v1_[A-Za-z0-9]{20,}")),
    ("replicate", re.compile(r"r8_[A-Za-z0-9]{20,}")),
    ("langsmith", re.compile(r"lsv2_(pt|sk)_[A-Za-z0-9]{20,}")),
    ("slack", re.compile(r"xox[bpars]-[A-Za-z0-9-]{10,}")),
    ("github", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}")),
    ("aws", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("pem", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]


def _redact(text: str) -> str:
    for kind, pat in _REDACT_PATTERNS:
        text = pat.sub(f"[REDACTED:{kind}]", text)
    return text
# === CUSTOM: A2 end ===========================================================


# === CUSTOM: C3 get_changed_symbols 正文装配（Phase 4 Task 10）=================


def _format_changed_symbols(r: dict, from_head: str | None = None) -> str:
    """C3 正文装配：文件集（与 git diff --stat 文件级吻合）+ 变更符号标签（命中文件内
    节点）。basis 明示锚定方式——basis=graph_diff 时正文声明无变更信息（verdict 走 N1
    推导 absent，"没有变更信息"≠ok，C 信封纪律不走 override）。"""
    files = r.get("files", [])
    symbols = r.get("symbols", [])
    basis = r.get("basis", "graph_diff")
    git_avail = r.get("git_available", False)
    anchor = f" since {from_head[:8]}" if from_head else ""
    lines = [f"Changed symbols{anchor} (basis={basis}, git_available={git_avail}):"]
    if not files:
        # Imp-1：按 basis 分支——basis=git_head 空文件集 = 基线锚定成功且无变更（rebuild
        # 刚完成后最常见调用），谎报 "baseline unavailable" 是假信息；只有 basis=graph_diff
        # 才是真的未锚定。empty_graph 是 N1 scanned=0 的机械副产物，正文改了信号就对了。
        if basis == "git_head":
            lines.append("  No changed files since the recorded baseline.")
        else:
            lines.append("  (no change information — git baseline unavailable)")
        return "\n".join(lines)
    lines.append(f"  {len(files)} file(s) changed, {len(symbols)} symbol(s) affected")
    for f in files:
        lines.append(f"  {f}")
    if symbols:
        shown = symbols[:50]
        lines.append("  Symbols:")
        lines.extend(f"    + {sanitize_label(s['label'])} ({sanitize_label(s['file'])})"
                     for s in shown)
        if len(symbols) > 50:
            lines.append(f"    ... and {len(symbols) - 50} more")
    return "\n".join(lines)


# === CUSTOM: C4 get_hotspots 正文装配（Phase 4 Task 11）========================


def _format_hotspots(r: dict) -> str:
    """C4 正文装配：top-N 热区（file + churn/degree/score 三轴）。**declared 纪律**：
    头行声明 score = churn × 度数是代理值、明示非圈复杂度（graph 无每文件复杂度属性，
    方案 §5-C4 实测条款）；degree 单位 = graph.json 合并图边计数（与 god_nodes/B1 同
    单位，06 票换源后诚实标注）。空结果按缺失轴分支说真话（C3 Imp-1 同款纪律）：
    非 git（churn 轴缺）/ 无提交史（churn 空）/ graph.json 缺失（度数轴缺）/ churn
    文件无图边（交叉积为 0）。"""
    items = r.get("hotspots", [])
    git_avail = r.get("git_available", False)
    deg_avail = r.get("degree_available", False)
    scanned = r.get("scanned", 0)
    if not items:
        if not git_avail:
            return "Code hotspots: git unavailable — no churn axis, no hotspot information."
        if scanned == 0:
            return ("Code hotspots: no commit history (empty repo, or git log failed) "
                    "— no churn signal.")
        if not deg_avail:
            return ("Code hotspots: graph.json unavailable — degree axis empty, "
                    "churn alone cannot rank hotspots.")
        return ("Code hotspots: no hotspots — churn files carry no graph edges "
                "(score = churn × degree = 0).")
    lines = ["Code hotspots (declared proxies: score = churn × degree, not cyclomatic "
             "complexity; degree = merged-graph edge count per file from graph.json, "
             "same unit as god_nodes — the graph carries no per-file complexity attribute):"]
    for i, h in enumerate(items, 1):
        lines.append(f"  {i}. {sanitize_label(h['file'])}  churn={h['churn']}  "
                     f"degree={h['degree']}  score={h['score']}")
    return "\n".join(lines)


def _parse_top_n(arguments: dict) -> int:
    """C4 top_n 解析（Minor-2）：MCP schema enum [5,10,20,50] 之外的防御层——客户端
    不守 enum / handler 直调时非整数回退缺省 10，不抛 ValueError 崩出口。"""
    try:
        return int(arguments.get("top_n", 10))
    except (TypeError, ValueError):
        return 10


# === CUSTOM: C1 find_dead_code 正文装配（Phase 4 Task 12）=====================


def _format_dead_code(DG: nx.DiGraph, r: dict, show: int = 50) -> str:
    """C1 正文装配：不可达符号清单（advisory——静态可达性看不见动态分发，声明不声称
    确定性）。gate_failed（不可达率 >50%）时降级为孤儿符号提示措辞——样本过大/图覆盖
    demo/test 内容或边稀疏，正文显式声明非确定性判定（legitimate degraded delivery，
    不是失败）。label/kind/source_file 取自 _digraph_view 节点属性（R3-2 短名事实源）。"""
    mode = r["entry_mode"]
    unreach = r["unreachable"]
    rate = r["unreachable_rate"]
    scanned = int(r["scanned"])
    if scanned == 0:
        # 退化形态：图中无代码符号节点（节点缺 kind 属性——非 codegraph-merged 图，
        # 如 graphify 原生 AST 图）。诚实声明，不谎报 "0 of 0 unreachable"。
        return ("Dead-code scan: no code symbol nodes in this graph (nodes lack kind "
                "attributes — not a codegraph-merged graph). Advisory only.")
    if r.get("gate_failed"):
        head = (f"Dead-code scan degraded to orphan-symbol hints (advisory): unreachable "
                f"rate {rate:.1%} ({len(unreach)}/{scanned}) exceeds the 50% gate — the "
                f"graph likely covers demo/test content or has sparse call edges, so this "
                f"is NOT a deterministic dead-code verdict.")
    else:
        head = (f"Dead-code scan (advisory — static reachability cannot see dynamic "
                f"dispatch): {len(unreach)} of {scanned} symbols unreachable "
                f"({rate:.1%}), entry_mode={mode}.")
    lines = [head]
    for nid in unreach[:show]:
        d = DG.nodes[nid]
        lines.append(f"  + {sanitize_label(str(d.get('label') or nid))} "
                     f"[{sanitize_label(str(d.get('kind') or ''))}] "
                     f"({sanitize_label(str(d.get('source_file') or ''))})")
    if len(unreach) > show:
        lines.append(f"  ... and {len(unreach) - show} more")
    return "\n".join(lines)


# === CUSTOM: C2 get_untested_symbols 正文装配（Phase 4 Task 13）================


def _format_untested(DG: nx.DiGraph, r: dict, show: int = 50) -> str:
    """C2 正文装配：未覆盖符号清单（advisory——图边是唯一覆盖证据，声明不声称确定性）。
    gate_failed（未覆盖率 >30%，C2 误报闸门代理）时降级为"疑似未覆盖"措辞——覆盖率
    证据稀疏，正文显式声明非确定性判定（legitimate degraded delivery，不是失败）。
    label/kind/source_file 取自 _digraph_view 节点属性（R3-2 短名事实源）。"""
    untested = r["untested"]
    rate = r["untested_rate"]
    scanned = int(r["scanned"])
    n_tests = len(r["test_files"])
    if scanned == 0:
        # 退化形态：图中无代码符号节点（节点缺 kind 属性——非 codegraph-merged 图，
        # 如 graphify 原生 AST 图）。诚实声明，不谎报 "0 of 0 untested"。
        return ("Untested-symbol scan: no code symbol nodes in this graph (nodes lack kind "
                "attributes — not a codegraph-merged graph). Advisory only.")
    if r.get("gate_failed"):
        head = (f"Untested-symbol scan degraded to suspected-untested hints (advisory): "
                f"{len(untested)} of {scanned} symbols ({rate:.1%}) not reached from "
                f"{n_tests} test files exceeds the 30% gate — coverage evidence is sparse, "
                f"so this is a SUSPECTED list, NOT a confirmed coverage verdict.")
    else:
        head = (f"Untested-symbol scan (advisory — graph edges are the only coverage "
                f"evidence): {len(untested)} of {scanned} symbols ({rate:.1%}) not reached "
                f"from {n_tests} test files.")
    lines = [head]
    for nid in untested[:show]:
        d = DG.nodes[nid]
        lines.append(f"  + {sanitize_label(str(d.get('label') or nid))} "
                     f"[{sanitize_label(str(d.get('kind') or ''))}] "
                     f"({sanitize_label(str(d.get('source_file') or ''))})")
    if len(untested) > show:
        lines.append(f"  ... and {len(untested) - show} more")
    return "\n".join(lines)


# CUSTOM: §8 schema 预算——15 个工具的唯一事实源（name/description/inputSchema 纯 dict）。
# list_tools()（在 _build_server 闭包内）从此构建 types.Tool；_all_tool_schemas() 返回
# 与 list_tools 发布形态逐字节一致的纯 schema dict（含 _meta 后缀 + project_path 注入），
# 测试不启 MCP server 也能量预算（tests/test_schema_budget.py）。加工具/改 schema 只动此处。
_TOOL_SPECS: list[dict] = [
    {
        "name": "query_graph",
        "description": "Search the knowledge graph using BFS or DFS. Returns relevant nodes and edges as text context.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "Natural language question or keyword search"},
                "mode": {"type": "string", "enum": ["bfs", "dfs"], "default": "bfs",
                         "description": "bfs=broad context, dfs=trace a specific path"},
                "depth": {"type": "integer", "default": 3, "description": "Traversal depth (1-6)"},
                "token_budget": {"type": "integer", "default": 2000, "description": "Max output tokens"},
                "context_filter": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional explicit edge-context filter, e.g. ['call', 'field']",
                },
            },
            "required": ["question"],
        },
    },
    {
        # CUSTOM: B2 include_source 四档（受限取值参数 → JSON schema enum，
        # 无效值走参数校验失败 isError）。缺省 signature（DB 名片增强），
        # none 档输出与扩展前逐字节一致（回归锚点）。
        "name": "get_node",
        "description": (
            "Get full details for a specific node by label or ID. include_source "
            "controls source slicing: signature (default; def line + signature + "
            "docstring head), body (source slice), body+context (±3 lines + "
            "1-hop neighbor signatures), or none (card only)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "label": {"type": "string", "description": "Node label or ID to look up"},
                "include_source": {
                    "type": "string",
                    "enum": ["none", "signature", "body", "body+context"],
                    "default": "signature",
                    "description": (
                        "Source slicing mode (default signature): none=card only; "
                        "signature=def line + signature + docstring head; body=source "
                        "slice; body+context=±3 lines + 1-hop neighbor signatures"
                    ),
                },
            },
            "required": ["label"],
        },
    },
    {
        "name": "get_neighbors",
        "description": (
            "Get neighbors of a node with edge details. depth=1 lists direct "
            "neighbors (undirected); depth>1 returns the blast radius on the "
            "directed view (callers for direction=in, callees for direction=out) "
            "with polymorphic fanout annotations and low-confidence verdict. "
            "Dispatch semantics are read natively from edge confidence/resolved_by "
            "(extraction-time inference, replacing external-resolver metadata); "
            "INFERRED/AMBIGUOUS call edges expand to subclass overrides. "
            "edge_kinds restricts traversal to the given relation kinds."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "label": {"type": "string"},
                "relation_filter": {"type": "string", "description": "Optional: filter by relation type"},
                "direction": {"type": "string", "enum": ["out", "in", "both"],
                              "description": "Edge direction on the directed view (default: undirected legacy)"},
                "depth": {"type": "integer", "minimum": 1, "maximum": 3, "default": 1,
                          "description": "BFS depth; 1=direct neighbors, >1=blast radius"},
                "edge_kinds": {"type": "array", "items": {"type": "string"},
                               "description": "Optional: restrict traversal to these relation kinds"},
                "token_budget": {"type": "integer", "default": 2000, "description": "Max output tokens"},
            },
            "required": ["label"],
        },
    },
    {
        "name": "get_community",
        "description": "Get all nodes in a community by community ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "community_id": {"type": "integer", "description": "Community ID (0-indexed by size)"},
                "token_budget": {"type": "integer", "default": 2000, "description": "Max output tokens"},
            },
            "required": ["community_id"],
        },
    },
    {
        "name": "god_nodes",
        "description": "Return the most connected nodes - the core abstractions of the knowledge graph.",
        "inputSchema": {"type": "object", "properties": {"top_n": {"type": "integer", "default": 10}}},
    },
    {
        "name": "graph_stats",
        "description": "Return summary statistics: node count, edge count, communities, confidence breakdown.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "shortest_path",
        "description": (
            "Find the shortest path between two concepts in the knowledge graph. "
            "Follows stored edge direction by default; set undirected=true to ignore it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "Source concept label or keyword"},
                "target": {"type": "string", "description": "Target concept label or keyword"},
                "max_hops": {"type": "integer", "default": 8, "description": "Maximum hops to consider"},
                "undirected": {"type": "boolean", "default": False,
                               "description": "Ignore stored edge direction when searching"},
            },
            "required": ["source", "target"],
        },
    },
    {
        # CUSTOM: B1 融合检索（scripts/ranked.py，四通道）。token_budget 用
        # enum（受限取值参数 → JSON schema enum，无效值走参数校验失败 isError）。
        "name": "get_ranked_context",
        "description": (
            "Rank code symbols by mixed retrieval: FTS5 BM25 x exact-name "
            "pinning x graph centrality under a token budget. Best first step "
            "to locate relevant symbols for a code query. Then fetch the actual "
            "implementation with get_node(label=..., include_source='body') "
            "(two-step: search here, fetch there)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "Code query: identifier tokens (English) route to FTS/pinning, CJK tokens to graph labels"},
                "token_budget": {"type": "integer", "default": 2000,
                                 "enum": [500, 1000, 2000, 4000, 8000],
                                 "description": "Max output tokens (tier)"},
            },
            "required": ["query"],
        },
    },
    {
        # CUSTOM: C3 git 轴（scripts/git_symbols.py，Task 10）。检索型工具
        # （_SEARCH_TOOLS）描述自动追加 _meta 信封契约（list_tools 循环）。
        "name": "get_changed_symbols",
        "description": (
            "Get knowledge-graph symbols changed since the last rebuild "
            "(anchored to the git HEAD recorded in the rebuild state file). "
            "Lists the changed files (matching git diff --stat) and the "
            "graph symbols inside them. Falls back to an unanchored basis "
            "when git or the git baseline is unavailable. "
            "Untracked new files are not included (git diff semantics)."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        # CUSTOM: C4 热区（scripts/git_symbols.py，Task 11）。declared 代理：
        # churn（git log 频次）× 度数（edges 双端点 GROUP BY——I1 fan-in 纳入，
        # 与 god_nodes 合并图度数 in+out 方向对齐）都是代理值，描述明示非圈
        # 复杂度（方案 §5-C4 实测条款）。
        "name": "get_hotspots",
        "description": (
            "Rank files by development activity x graph connectivity (hotspot "
            "analysis): score = churn (number of commits touching the file, full "
            "history) x degree (graph edges grouped by both endpoints, matching "
            "the god-nodes in+out degree). "
            "Both are declared proxies — the graph carries no per-file complexity "
            "attribute, so this is NOT cyclomatic complexity. Returns top-N files "
            "with both a churn and a connectivity signal."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "top_n": {"type": "integer", "enum": [5, 10, 20, 50],
                          "default": 10,
                          "description": "Number of hotspot files to return"},
            },
        },
    },
    {
        # CUSTOM: C1 死代码（scripts/structure_queries.py，Task 12）。检索型工具
        # （_SEARCH_TOOLS）描述自动追加 _meta 信封契约（list_tools 循环）。
        "name": "find_dead_code",
        "description": (
            "Scan the knowledge graph for potentially dead code: symbols "
            "unreachable from the project's entry points (main script / CLI "
            "for application projects, __init__ re-exports for libraries; "
            "auto-detected). Reports the unreachable symbol list with the "
            "unreachable rate. Advisory only — static reachability cannot see "
            "dynamic dispatch (reflection/import hooks), so this is a hint list, "
            "not a deterministic verdict. Degrades to orphan-symbol hints when "
            "the unreachable rate exceeds 50%."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        # CUSTOM: C2 未覆盖符号（scripts/structure_queries.py，Task 13）。检索型
        # 工具（_SEARCH_TOOLS）描述自动追加 _meta 信封契约（list_tools 循环）。
        # Python 单约定 test_*.py 判定测试文件；go/ts 约定留配置项（patterns 参数）。
        "name": "get_untested_symbols",
        "description": (
            "Find symbols in the knowledge graph not reached from any test file "
            "(Python test_*.py convention; non-Python test conventions (go/ts) "
            "are not supported yet). A symbol counts as covered when a test file "
            "reaches it through the graph, including import edges. Reports the "
            "untested symbol list with the coverage rate. Advisory only — graph "
            "edges are the only coverage evidence, so this is a hint list, not a "
            "test-coverage tool. Degrades to suspected-untested hints when the "
            "untested rate exceeds 30%."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_prs",
        "description": (
            "List open GitHub PRs with CI status, review state, and graph impact "
            "(which communities each PR touches, blast radius). Use this before starting "
            "work to check if a PR already covers the area you're about to change."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "base": {"type": "string", "description": "Base branch to filter PRs by (auto-detected if omitted)"},
                "repo": {"type": "string", "description": "GitHub repo (owner/repo). Defaults to current repo."},
            },
        },
    },
    {
        "name": "get_pr_impact",
        "description": (
            "Get detailed graph impact for a specific PR: which files it changes, "
            "which knowledge-graph communities are affected, and how many nodes are touched. "
            "Use this to assess merge risk or check for overlap with your current work."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pr_number": {"type": "integer", "description": "PR number to analyse"},
                "repo": {"type": "string", "description": "GitHub repo (owner/repo). Defaults to current repo."},
            },
            "required": ["pr_number"],
        },
    },
    {
        "name": "triage_prs",
        "description": (
            "Return all actionable open PRs (correct base, not stale) with full graph impact data "
            "so you can reason about review priority, merge order, and conflict risk. "
            "Call this when the user asks 'what PRs should I review?' or 'what's ready to merge?'"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "base": {"type": "string", "description": "Base branch to filter PRs by (auto-detected if omitted)"},
                "repo": {"type": "string", "description": "GitHub repo (owner/repo). Defaults to current repo."},
            },
        },
    },
]

# CUSTOM: §8 检索型工具的 _meta 信封契约后缀 + 全工具的 project_path 注入值——
# 抽成常量让 list_tools() 与 _all_tool_schemas() 逐字节同源（预算测的就是实际发布形态）。
_META_SUFFIX = (" Responses carry a trailing `_meta:` JSON line "
                "(verdict/freshness; body unchanged).")
_PROJECT_PATH_PROP = {
    "type": "string",
    "description": (
        "Absolute path to a project directory containing "
        "graphify-out/graph.json. Optional — defaults to the graph "
        "this server was started with."
    ),
}


def _all_tool_schemas() -> list[dict]:
    """§8 schema 预算：与 list_tools() 发布形态一致的纯 schema dict 列表.

    CUSTOM：唯一事实源 _TOOL_SPECS（模块级）——list_tools() 在其上构建 types.Tool，
    本函数在其上应用同款变换（_meta 后缀 + project_path 注入）返回纯 dict，测试
    （tests/test_schema_budget.py）不启 MCP server 即可对全部工具做 token 预算断言。
    注入值用 deepcopy：绝不污染 _TOOL_SPECS 共享源（list_tools 的 types.Tool 是
    pydantic 模型，本身隔离；此处对纯 dict 显式隔离）。
    """
    import copy as _copy
    schemas = []
    for _spec in _TOOL_SPECS:
        _s = {
            "name": _spec["name"],
            "description": _spec["description"],
            "inputSchema": _copy.deepcopy(_spec["inputSchema"]),
        }
        if _s["name"] in _SEARCH_TOOLS:
            _s["description"] += _META_SUFFIX
        _s["inputSchema"].setdefault("properties", {})["project_path"] = _copy.deepcopy(_PROJECT_PATH_PROP)
        schemas.append(_s)
    return schemas


def _build_server(graph_path: str):
    """Build the configured low-level MCP Server (shared by every transport).

    All graph query tools and resources are registered here over a single
    ``mcp.server.Server`` instance; the caller picks the transport (stdio or
    Streamable HTTP) and runs it. Hot-reload of graph.json works the same way
    regardless of transport, since reloads happen inside the tool handlers.
    """
    try:
        from mcp.server import Server
        from mcp import types
    except ImportError as e:
        raise ImportError('mcp not installed. Run: pip install "graphifyy[mcp]"') from e
    try:
        from mcp.types import AnyUrl
    except ImportError:
        # mcp >= 2.0 dropped the AnyUrl re-export; it was always pydantic's
        # AnyUrl (pydantic is an mcp dependency, so this import cannot miss).
        from pydantic import AnyUrl

    from graphify import paths as _paths

    # Graph contexts comprise one pinned configured default plus a bounded LRU
    # of project_path graphs. This preserves the configured graph's warm index
    # while preventing a shared server from retaining every project it serves.
    _default_graph_path = str(Path(graph_path).resolve())
    _ctx_cache = _GraphContextCache(_max_server_contexts())

    def _load_ctx(path: str):
        """Return the current default or project graph context as a tool error.

        Unlike ``_load_graph``, this never lets a missing or corrupt client
        graph terminate the MCP process; it raises so other projects remain
        available on the same server.
        """
        resolved_path = str(Path(path).resolve())
        return _ctx_cache.load(resolved_path, pinned=resolved_path == _default_graph_path)

    def _resolve_graph_path(project_path) -> str:
        """Map an optional project_path to a concrete graph.json path. ``None``
        keeps the server's default graph (backward-compatible); a project_path
        resolves to ``<project_path>/<GRAPHIFY_OUT>/graph.json``, honouring the
        GRAPHIFY_OUT override so worktree/shared-output setups keep working."""
        if not project_path:
            return _default_graph_path
        return str(Path(project_path) / _paths.GRAPHIFY_OUT / "graph.json")

    # Active per-request context, rebound by _select_graph() and read by the tool
    # handlers below. No lock needed on the hot path: _select_graph and the
    # handler run in one synchronous stretch of each call_tool coroutine (no
    # await between them), so a concurrent call never observes a half-applied
    # swap.
    active_graph_path = _default_graph_path
    try:
        G, communities = _load_ctx(_default_graph_path)
    except (FileNotFoundError, RuntimeError):
        # No default graph at startup → run as a pure multi-project server. Tools
        # then require project_path; a call without one gets a clear error rather
        # than the process refusing to start (which is what _load_graph would do).
        G, communities = None, {}

    def _select_graph(project_path) -> None:
        nonlocal G, communities, active_graph_path
        path = _resolve_graph_path(project_path)
        G, communities = _load_ctx(path)
        active_graph_path = str(Path(path).resolve())

    # NOTE: no decorators here — the handlers below are plain coroutines,
    # bound to the Server at the END of this function in a version-aware way:
    # mcp 1.x exposes the @server.list_tools()/... decorator API, mcp 2.x
    # replaced it with on_list_tools=/... constructor callbacks.
    async def list_tools() -> list[types.Tool]:
        # CUSTOM: §8 schema 预算——从模块级唯一事实源 _TOOL_SPECS（纯 dict）构建
        # types.Tool（pydantic model，隔离共享源）；_meta 后缀与 project_path 注入
        # 与 _all_tool_schemas() 逐字节同源（预算测的就是实际发布形态）。
        _tools = [types.Tool(**spec) for spec in _TOOL_SPECS]
        # CUSTOM: A1b 清单标注——检索型工具（_SEARCH_TOOLS）的描述声明 _meta 信封契约，
        # 调用方在 tool discovery 时即知响应尾部有 verdict/freshness 行（正文不变）。
        for _t in _tools:
            if _t.name in _SEARCH_TOOLS:
                _t.description += _META_SUFFIX
        # Multi-project support: every tool accepts an optional project_path.
        # Injected here (rather than repeated in 11 literal schemas) so the set
        # stays in lockstep as tools are added. Omitting it keeps the historical
        # single-graph behaviour, so this is purely additive for existing callers.
        for _t in _tools:
            # The constructor accepts the camelCase alias in both majors, but
            # attribute access is inputSchema on mcp 1.x and input_schema on 2.x.
            _schema = getattr(_t, "inputSchema", None)
            if _schema is None:
                _schema = _t.input_schema
            _schema.setdefault("properties", {})["project_path"] = _PROJECT_PATH_PROP
        return _tools

    def _tool_query_graph(arguments: dict) -> tuple[str, bool, int]:  # CUSTOM: N1 三元组
        import time as _time
        from graphify import querylog
        question = arguments["question"]
        mode = arguments.get("mode", "bfs")
        depth = min(int(arguments.get("depth", 3)), 6)
        budget = int(arguments.get("token_budget", 2000))
        context_filter = arguments.get("context_filter")
        _t0 = _time.perf_counter()
        result = _query_graph_text(
            G,
            question,
            mode=mode,
            depth=depth,
            token_budget=budget,
            context_filters=context_filter,
            graph_path=str(active_graph_path),
        )
        querylog.log_query(
            kind="mcp_query",
            question=question,
            corpus=str(active_graph_path),
            # CUSTOM: A2 与 serve 返回共用 _redact——querylog 记脱敏后文本，密钥零进
            # 磁盘（GRAPHIFY_QUERY_LOG_RESPONSES=1 时 response 字段落盘同源脱敏）。
            result=_redact(result),
            mode=mode,
            depth=depth,
            token_budget=budget,
            duration_ms=(_time.perf_counter() - _t0) * 1000,
        )
        # CUSTOM: N1 (text, found, scanned)——found=查到结果（"No matching nodes
        # found." 是 _query_graph_text 的空态哨兵），scanned=全图节点数（扫描语义表）。
        return result, not result.startswith("No matching nodes found"), G.number_of_nodes()

    def _tool_get_node(arguments: dict) -> tuple[str, bool, int, str | None]:  # CUSTOM: B2 四元组
        # B2 include_source 四档：none/signature(缺省)/body/body+context。返回
        # (text, found, scanned, verdict_override)——末元是工具侧诚实自评（R3-3）。
        # 06 票换源实现在模块级 _get_node_tool（元数据点查走 FTS 缓存、字节精确切片、
        # __cg 回退删除）——此处只做闭包状态转发（_shortest_path_text 先例）。
        return _get_node_tool(G, active_graph_path, arguments)

    def _tool_get_neighbors(arguments: dict) -> tuple:  # CUSTOM: N1 三元组
        label = arguments["label"].lower()
        rel_filter = arguments.get("relation_filter", "").lower()
        nid, err = _resolve_single_node(G, label)
        if err:
            # CUSTOM: N1 未解析出起点 -> 邻接边数无定义；标签解析扫过全图，
            # scanned=全图节点数（空图时为 0，empty_graph 旗标恰真；非空图不误报）。
            return err, False, G.number_of_nodes()
        direction = arguments.get("direction")
        raw_depth = arguments.get("depth", 1)
        depth = raw_depth
        budget = int(arguments.get("token_budget", 2000))
        # 缺省路径回归锁定：无 direction 且 depth<=1 时保持既有 G 上 successors/
        # predecessors 循环逐字节不变（无向图上双向输出本就同集合；方向只在 B3
        # 有向视图路径用）。direction/depth>1 新路径只在 _digraph_view 产物上走。
        if direction is None and depth == 1:
            lines = _neighbors_lines(G, nid, rel_filter)
            # CUSTOM: N1 found=起点在图（已解析），scanned=邻接边数（扫描语义表；
            # 检查过的邻接边总数，不随 relation_filter 过滤结果减少）。
            return _cut_lines_to_budget(
                lines, budget, "Narrow with relation_filter or use get_node for a specific symbol"
            ), True, G.degree(nid)
        # --- B3 新路径（direction 或 depth>1 给定）---
        if not isinstance(depth, int) or isinstance(depth, bool) or not 1 <= depth <= 3:
            return (f"Invalid depth {raw_depth!r}; expected integer 1..3.",
                    False, G.number_of_nodes())
        if direction not in ("out", "in", "both"):
            return (f"Invalid direction {direction!r}; expected 'out', 'in' or 'both'.",
                    False, G.number_of_nodes())
        edge_kinds = arguments.get("edge_kinds")
        DG = _digraph_view(active_graph_path)
        # M4（review）：nid 不在有向视图（图 reload 竞态）→ found=False，不得谎报
        # True+closure_size=0。scanned=0（未计算闭包），extra_meta 空（不宣称 closure_size）。
        if nid not in DG:
            return (f"Neighbors of {sanitize_label(G.nodes[nid].get('label', nid))}: "
                    f"(node missing from current directed view)", False, 0, None, {})
        fanout_limited = False
        if edge_kinds:
            kinds = {k.lower() for k in edge_kinds}
            DGF = nx.DiGraph()
            DGF.add_nodes_from(DG.nodes(data=True))
            for u, v, d in DG.edges(data=True):
                if str(d.get("relation", "")).lower() in kinds:
                    DGF.add_edge(u, v, **d)
            DG = DGF
            # M1b：edge_kinds 滤掉 fanout 遍历所需关系（contains/extends/implements）时，
            # fanout 展开受限——标注须区分"被过滤"与"无所属类"。
            fanout_limited = ("contains" not in kinds or
                              ("extends" not in kinds and "implements" not in kinds))
        top_k = 50  # R5-5：top-50 截断（闭包可能远大于此，有界输出）
        lines, total = _blast_radius_lines(DG, nid, direction, depth, top_k, rel_filter,
                                           fanout_limited=fanout_limited)
        # R3-3 verdict：depth=1（无多态展开，R5-5(b)）→ 走推导（ok）；
        # depth>1（blast-radius）→ low_confidence 经 verdict_override 直通 + advisory
        # 措辞（Q14——输出建立在边语义不完整的图上，advisory 不可省）。
        # N1：found=起点在图，scanned=闭包全量尺寸，extra_meta 带 _meta.closure_size。
        override = "low_confidence" if depth > 1 else None
        text = _cut_lines_to_budget(lines, budget,
                                    "Narrow with relation_filter or reduce depth")
        return text, True, total, override, {"closure_size": total}

    def _tool_get_community(arguments: dict) -> tuple[str, bool, int]:  # CUSTOM: N1 三元组
        cid = int(arguments["community_id"])
        nodes = communities.get(cid, [])
        if not nodes:
            # CUSTOM: N1 found=结果非空（社区成员表），scanned=全图节点数（扫描语义表）。
            return f"Community {cid} not found.", False, G.number_of_nodes()
        header = _community_header(cid, G.nodes[nodes[0]].get("community_name"))
        lines = [f"{header} ({len(nodes)} nodes):"]
        for n in nodes:
            d = G.nodes[n]
            # Sanitise label and source_file (F-010).
            lines.append(
                f"  {sanitize_label(d.get('label', n))} "
                f"[{sanitize_label(str(d.get('source_file', '')))}]"
            )
        budget = int(arguments.get("token_budget", 2000))
        # CUSTOM: N1 found=社区非空，scanned=全图节点数（扫描语义表）。
        return _cut_lines_to_budget(
            lines, budget, "Raise token_budget or use get_node for specific members"
        ), True, G.number_of_nodes()

    def _tool_god_nodes(arguments: dict) -> tuple[str, bool, int]:  # CUSTOM: N1 三元组
        from graphify.analyze import god_nodes as _god_nodes
        nodes = _god_nodes(G, top_n=int(arguments.get("top_n", 10)))
        lines = ["God nodes (most connected):"]
        lines += [f"  {i}. {n['label']} - {n['degree']} edges" for i, n in enumerate(nodes, 1)]
        # CUSTOM: N1 found=结果非空（god 节点列表），scanned=全图节点数（扫描语义表）。
        return "\n".join(lines), bool(nodes), G.number_of_nodes()

    def _tool_graph_stats(_: dict) -> tuple[str, bool, int]:  # CUSTOM: N1 三元组
        confs = [d.get("confidence", "EXTRACTED") for _, _, d in G.edges(data=True)]
        total = len(confs) or 1
        return (
            f"Nodes: {G.number_of_nodes()}\n"
            f"Edges: {G.number_of_edges()}\n"
            f"Communities: {len(communities)}\n"
            f"EXTRACTED: {round(confs.count('EXTRACTED')/total*100)}%\n"
            f"INFERRED: {round(confs.count('INFERRED')/total*100)}%\n"
            f"AMBIGUOUS: {round(confs.count('AMBIGUOUS')/total*100)}%\n"
        # CUSTOM: N1 found=图非空（stats 反映图现状，空图即 absent+empty_graph），
        # scanned=全图节点数（扫描语义表）。
        ), G.number_of_nodes() > 0, G.number_of_nodes()

    def _tool_shortest_path(arguments: dict) -> tuple[str, bool, int]:  # CUSTOM: N1 三元组
        # _shortest_path_text 保持 -> str（tests/test_serve.py 直接 import 测它，签名不可动）；
        # found 从其既有文本哨兵推导：No* 前缀 = 端点缺失/无路径，同点退化 = 无有效路径
        # （"Path exceeds max_hops" 路径存在，计 found=True）。
        text = _shortest_path_text(G, arguments)
        found = not (text.startswith("No ") or "both resolved to the same node" in text)
        return text, found, G.number_of_nodes()

    def _tool_get_ranked_context(arguments: dict) -> tuple[str, bool, int]:  # CUSTOM: N1 三元组
        # scripts/ 无包结构：lazy sys.path + import（graphify 包不反向依赖 scripts/，
        # 仅在本工具被调用时才把 repo_root/scripts 挂到 sys.path——rebuild_entry 先例）。
        import sys as _sys
        _scripts_dir = str(Path(__file__).resolve().parent.parent / "scripts")
        if _scripts_dir not in _sys.path:
            _sys.path.insert(0, _scripts_dir)
        from ranked import ranked_context, format_ranked
        query = arguments["query"]
        budget = int(arguments.get("token_budget", 2000))
        # 路径联动总条款：root 从当前 active graph（graphify-out/graph.json）推导，
        # 多项目随请求图走（与 _derive_freshness 的 state_path.parent.parent 同构）。
        root = Path(active_graph_path).parent.parent
        r = ranked_context(root, query, token_budget=budget)
        # CUSTOM: N1 found=结果非空, scanned=候选池大小（query_shape.scanned）。
        return format_ranked(r), bool(r["results"]), int(r["query_shape"]["scanned"])

    def _tool_get_changed_symbols(arguments: dict) -> tuple[str, bool, int]:  # CUSTOM: C3 三元组
        # C3 git 轴（scripts/git_symbols.py）。路径联动总条款：root 从当前 active graph
        # 推导（与 _derive_freshness / B1/B2/B3 同构）。G3：from_head 从状态文件 git_head
        # 读出传入——字段缺失 -> from_head=None -> 与孤儿 hash 同走 graph_diff 回退。
        import sys as _sys
        _scripts_dir = str(Path(__file__).resolve().parent.parent / "scripts")
        if _scripts_dir not in _sys.path:
            _sys.path.insert(0, _scripts_dir)
        from git_symbols import changed_symbols
        root = Path(active_graph_path).parent.parent
        from_head = None
        state_path = Path(active_graph_path).parent / ".rebuild-state.json"
        try:
            d = json.loads(state_path.read_text(encoding="utf-8"))
            gh = d.get("git_head")
            from_head = gh if isinstance(gh, str) and gh else None
        except (OSError, ValueError, AttributeError):
            from_head = None   # 状态文件缺失/损坏/非 dict -> 未锚定
        r = changed_symbols(root, from_head=from_head)
        # N1：found=symbol_ids 非空（变更符号集），scanned=文件集大小（扫描语义表）。
        # C 信封纪律：不走 verdict_override——basis=git_head 有符号 -> ok；无变更信息
        # （非 git / 基线未锚定）-> absent（"没有变更信息"≠ok，最诚实形态）。
        return _format_changed_symbols(r, from_head), bool(r["symbol_ids"]), len(r["files"])

    def _tool_get_hotspots(arguments: dict) -> tuple[str, bool, int]:  # CUSTOM: C4 三元组
        # C4 热区（scripts/git_symbols.py）。路径联动总条款：root 从当前 active graph
        # 推导（与 _derive_freshness / B1/B2/B3/C3 同构）。
        import sys as _sys
        _scripts_dir = str(Path(__file__).resolve().parent.parent / "scripts")
        if _scripts_dir not in _sys.path:
            _sys.path.insert(0, _scripts_dir)
        from git_symbols import hotspots
        root = Path(active_graph_path).parent.parent
        r = hotspots(root, top_n=_parse_top_n(arguments))
        # N1：found=hotspots 非空，scanned=参与排序的文件数（churn>0 文件数）。
        # C 信封纪律：不走 verdict_override——有热区 -> ok；无信号（非 git/无提交史/
        # DB 缺失/churn 文件无图边）-> absent（"没有热区信息"≠ok，最诚实形态）。
        return _format_hotspots(r), bool(r["hotspots"]), int(r["scanned"])

    def _tool_find_dead_code(arguments: dict) -> tuple[str, bool, int, str]:  # CUSTOM: C1 四元组
        # C1 死代码（scripts/structure_queries.py，Task 12）。R4-1：有向视图（DiGraph）——
        # 生产 _load_graph 产 nx.Graph，无向图上传入会让 nx.descendants 退化为连通分量
        # 遍历（unreachable 系统性趋空、闸门永不触发）；find_dead_code 内部 TypeError
        # 防御（同 Task 9 手法），serve 侧挂载经 _digraph_view 重建方向。路径联动总条款：
        # project_root 从当前 active graph 推导（与 B1/B2/B3/C3/C4 同构）。
        import sys as _sys
        _scripts_dir = str(Path(__file__).resolve().parent.parent / "scripts")
        if _scripts_dir not in _sys.path:
            _sys.path.insert(0, _scripts_dir)
        from structure_queries import find_dead_code
        DG = _digraph_view(active_graph_path)
        r = find_dead_code(DG, project_root=Path(active_graph_path).parent.parent)
        # N1：found 恒 True（分析报告恒有效——空结果≠absent，"没有 dead code"是有效
        # 回答），scanned=符号总数（r["scanned"]）。C 信封纪律：verdict_override=
        # "low_confidence"（动态分发是静态图天生盲区，不声称确定性）。
        return _format_dead_code(DG, r), True, int(r["scanned"]), "low_confidence"

    def _tool_untested_symbols(arguments: dict) -> tuple[str, bool, int, str]:  # CUSTOM: C2 四元组
        # C2 未覆盖符号（scripts/structure_queries.py，Task 13）。R4-1：有向视图
        # （DiGraph）——生产 _load_graph 产 nx.Graph，无向图上传入会让测试子图可达退化为
        # 连通分量遍历（test_x.py 的可达集把整个连通分量误判"已覆盖"）；untested_symbols
        # 内部 TypeError 防御，serve 侧挂载经 _digraph_view 重建方向（同 C1）。
        import sys as _sys
        _scripts_dir = str(Path(__file__).resolve().parent.parent / "scripts")
        if _scripts_dir not in _sys.path:
            _sys.path.insert(0, _scripts_dir)
        from structure_queries import untested_symbols
        DG = _digraph_view(active_graph_path)
        r = untested_symbols(DG)
        # N1：found 恒 True（分析报告恒有效——空结果≠absent，"全部符号已被测试覆盖"是
        # 有效回答，C2 归 C1 派 M2 语义），scanned=符号总数（r["scanned"]）。
        # C 信封纪律：verdict_override="low_confidence"（图边是唯一覆盖证据，
        # conftest 自动发现 fixture 等静态盲区，不声称确定性）。
        return _format_untested(DG, r), True, int(r["scanned"]), "low_confidence"

    def _tool_list_prs(arguments: dict) -> str:
        from graphify.prs import fetch_prs, fetch_worktrees, format_prs_text, _detect_default_branch
        repo = arguments.get("repo") or None
        base = arguments.get("base") or _detect_default_branch(repo)
        try:
            prs = fetch_prs(repo=repo, base=base)
        except RuntimeError as e:
            raise ToolError(f"Error: {e}") from e
        worktrees = fetch_worktrees()
        for pr in prs:
            pr.worktree_path = worktrees.get(pr.branch)
        return format_prs_text(prs, base)

    def _tool_get_pr_impact(arguments: dict) -> str:
        from graphify.prs import fetch_pr_files, compute_pr_impact, _gh, _parse_ci
        number = int(arguments["pr_number"])
        repo = arguments.get("repo") or None
        # Use gh pr view directly — works for any base branch, not just the default
        view_args = ["pr", "view", str(number), "--json",
                     "title,headRefName,baseRefName,author,isDraft,reviewDecision,statusCheckRollup,updatedAt"]
        if repo:
            view_args += ["--repo", repo]
        pr_data = _gh(*view_args)
        if pr_data is None:
            raise ToolError(f"PR #{number} not found or gh not authenticated.")
        files = fetch_pr_files(number, repo)
        if not files:
            return f"PR #{number}: no changed files found (may require gh auth)."
        comms, nodes = compute_pr_impact(files, G)
        ci = _parse_ci(pr_data.get("statusCheckRollup") or [])
        lines = [
            f"PR #{number}: {pr_data['title']}",
            f"CI: {ci}  Review: {pr_data.get('reviewDecision') or 'none'}",
            f"Base: {pr_data['baseRefName']}  Author: {(pr_data.get('author') or {}).get('login', '?')}",
            f"\nGraph impact: {nodes} nodes across {len(comms)} communities",
            f"Communities touched: {comms}",
            f"Files changed ({len(files)}):",
        ]
        lines += [f"  {f}" for f in files[:20]]
        if len(files) > 20:
            lines.append(f"  … and {len(files) - 20} more")
        return "\n".join(lines)

    def _tool_triage_prs(arguments: dict) -> str:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from graphify.prs import fetch_prs, fetch_worktrees, fetch_pr_files, compute_pr_impact, _STATUS_ORDER, _detect_default_branch
        repo = arguments.get("repo") or None
        base = arguments.get("base") or _detect_default_branch(repo)
        try:
            prs = fetch_prs(repo=repo, base=base)
        except RuntimeError as e:
            raise ToolError(f"Error: {e}") from e
        worktrees = fetch_worktrees()
        for pr in prs:
            pr.worktree_path = worktrees.get(pr.branch)
        actionable = [p for p in prs if p.base_branch == base and p.status not in ("WRONG-BASE", "STALE")]
        if not actionable:
            return f"No actionable PRs targeting {base}."
        # Fetch diffs concurrently then compute graph impact using in-memory G
        workers = min(8, len(actionable))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_to_pr = {pool.submit(fetch_pr_files, pr.number, repo): pr for pr in actionable}
            for fut in as_completed(future_to_pr):
                pr = future_to_pr[fut]
                try:
                    files = fut.result()
                except Exception:
                    files = []
                if files:
                    pr.files_changed = files
                    pr.communities_touched, pr.nodes_affected = compute_pr_impact(files, G)
        header = (
            f"Actionable PRs targeting {base}: {len(actionable)}\n"
            "Rank these by review priority. Higher blast_radius = more graph communities affected = higher merge risk.\n"
        )
        lines = [header]
        for p in sorted(actionable, key=lambda x: (_STATUS_ORDER.index(x.status) if x.status in _STATUS_ORDER else 99)):
            impact = f"  blast_radius={p.blast_radius}" if p.blast_radius else ""
            wt = f"  worktree={p.worktree_path}" if p.worktree_path else ""
            lines.append(
                f"PR #{p.number} [{p.status}] CI={p.ci_status} review={p.review_decision or 'none'} "
                f"age={p.days_old}d author={p.author}{impact}{wt}\n  title: {p.title}"
            )
        return "\n\n".join(lines)

    _handlers = {
        "query_graph": _tool_query_graph,
        "get_node": _tool_get_node,
        "get_neighbors": _tool_get_neighbors,
        "get_community": _tool_get_community,
        "god_nodes": _tool_god_nodes,
        "graph_stats": _tool_graph_stats,
        "shortest_path": _tool_shortest_path,
        "get_ranked_context": _tool_get_ranked_context,  # CUSTOM: B1
        "get_changed_symbols": _tool_get_changed_symbols,  # CUSTOM: C3
        "get_hotspots": _tool_get_hotspots,  # CUSTOM: C4
        "find_dead_code": _tool_find_dead_code,  # CUSTOM: C1
        "get_untested_symbols": _tool_untested_symbols,  # CUSTOM: C2
        "list_prs": _tool_list_prs,
        "get_pr_impact": _tool_get_pr_impact,
        "triage_prs": _tool_triage_prs,
    }

    def _load_community_labels() -> dict[int, str]:
        labels_path = Path(active_graph_path).parent / ".graphify_labels.json"
        if labels_path.exists():
            try:
                return {int(k): v for k, v in json.loads(labels_path.read_text(encoding="utf-8")).items()}
            except Exception:
                pass
        return {cid: f"Community {cid}" for cid in communities}

    async def list_resources() -> list[types.Resource]:
        # Plain-string URIs on purpose: mcp 1.x types the field as AnyUrl and
        # coerces strings, mcp 2.x types it as str and REJECTS AnyUrl objects.
        return [
            types.Resource(uri="graphify://report", name="Graph Report", description="Full GRAPH_REPORT.md", mimeType="text/markdown"),
            types.Resource(uri="graphify://stats", name="Graph Stats", description="Node/edge/community counts and confidence breakdown", mimeType="text/plain"),
            types.Resource(uri="graphify://god-nodes", name="God Nodes", description="Top 10 most-connected nodes", mimeType="text/plain"),
            types.Resource(uri="graphify://surprises", name="Surprising Connections", description="Cross-community surprising connections", mimeType="text/plain"),
            types.Resource(uri="graphify://audit", name="Confidence Audit", description="EXTRACTED/INFERRED/AMBIGUOUS edge breakdown", mimeType="text/plain"),
            types.Resource(uri="graphify://questions", name="Suggested Questions", description="Suggested questions for this codebase", mimeType="text/plain"),
        ]

    async def read_resource(uri: AnyUrl) -> str:
        _select_graph(None)  # resources read the server's default graph
        uri_str = str(uri)
        if uri_str == "graphify://report":
            report_path = Path(active_graph_path).parent / "GRAPH_REPORT.md"
            if report_path.exists():
                return report_path.read_text(encoding="utf-8")
            return "GRAPH_REPORT.md not found. Run graphify extract first."
        if uri_str == "graphify://stats":
            # CUSTOM: N1 三元组解包——资源保持裸文本（资源不经 call_tool 信封出口）。
            return _tool_graph_stats({})[0]
        if uri_str == "graphify://god-nodes":
            return _tool_god_nodes({"top_n": 10})[0]
        if uri_str == "graphify://surprises":
            try:
                from graphify.analyze import surprising_connections
                surprises = surprising_connections(G, communities, top_n=10)
                if not surprises:
                    return "No surprising connections found."
                lines = ["Surprising cross-community connections:"]
                for s in surprises:
                    lines.append(f"  {s.get('source', '')} <-> {s.get('target', '')} [{s.get('relation', '')}]")
                return "\n".join(lines)
            except Exception as exc:
                return f"Could not compute surprising connections: {exc}"
        if uri_str == "graphify://audit":
            confs = [d.get("confidence", "EXTRACTED") for _, _, d in G.edges(data=True)]
            total = len(confs) or 1
            return (
                f"Total edges: {total}\n"
                f"EXTRACTED: {confs.count('EXTRACTED')} ({round(confs.count('EXTRACTED')/total*100)}%)\n"
                f"INFERRED: {confs.count('INFERRED')} ({round(confs.count('INFERRED')/total*100)}%)\n"
                f"AMBIGUOUS: {confs.count('AMBIGUOUS')} ({round(confs.count('AMBIGUOUS')/total*100)}%)\n"
            )
        if uri_str == "graphify://questions":
            try:
                from graphify.analyze import suggest_questions
                community_labels = _load_community_labels()
                questions = suggest_questions(G, communities, community_labels, top_n=10)
                if not questions:
                    return "No suggested questions available."
                lines = ["Suggested questions:"]
                for q in questions:
                    if isinstance(q, dict):
                        lines.append(f"  - {q.get('question', '')}")
                    else:
                        lines.append(f"  - {q}")
                return "\n".join(lines)
            except Exception as exc:
                return f"Could not generate questions: {exc}"
        raise ValueError(f"Unknown resource: {uri_str}")

    async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
        arguments = dict(arguments or {})
        project_path = arguments.pop("project_path", None)
        handler = _handlers.get(name)
        if not handler:
            return [types.TextContent(type="text", text=f"Unknown tool: {name}")]
        try:
            _select_graph(project_path)  # bind G/communities to the target graph
            # CUSTOM: A1b+N1 出口（G1 调用链）——检索型清单内工具闭包返回
            # (text, found, scanned_nodes) 三元组，出口装配 verdict/freshness 信封；
            # 清单外工具裸 str 直通。isError 真错误路径（ToolError/except Exception）
            # 不附 _meta.verdict——维持 upstream 既有错误通道。
            # 路径联动总条款：状态文件取 active_graph_path 所在输出目录（必须在
            # _select_graph 之后取，多项目场景才随请求图走）。
            result = handler(arguments)
            state_path = Path(active_graph_path).parent / ".rebuild-state.json"
            # CUSTOM: A2 出口脱敏顺序——工具文本 → _envelope → _redact → 返回。
            # 信封（_meta 行）本身无密钥，正文先包信封再统一脱敏一次即可；querylog
            # 落盘经同一 _redact（见 _tool_query_graph），密钥零进磁盘。
            return [types.TextContent(type="text",
                                      text=_redact(_apply_envelope(name, result, _derive_freshness(state_path))))]
        except ToolError:
            # A handler-signalled error: propagate so the result is marked
            # isError:true (the mcp 1.x decorator wraps a raised exception into
            # an error result; the 2.x path catches it in _on_call_tool).
            raise
        except Exception as exc:
            # 终审升格 2：错误路径同样过 _redact——Q7 出口统一承诺缺口（异常消息可能
            # 带密钥/敏感值，裸拼会泄漏）。except ToolError: raise 路径不动（upstream
            # 错误通道既定纪律）。
            return [types.TextContent(type="text", text=_redact(f"Error executing {name}: {exc}"))]

    if hasattr(Server, "list_tools"):
        # mcp 1.x: decorator-based registration. The SDK wraps the raw returns
        # (list[Tool] -> ListToolsResult, str -> resource contents) itself.
        server = Server("graphify")
        server.list_tools()(list_tools)
        server.call_tool()(call_tool)
        server.list_resources()(list_resources)
        server.read_resource()(read_resource)
    else:
        # mcp 2.x: handlers ride the Server constructor as on_* callbacks with
        # the (ctx, params) -> Result contract, so wrap the same impls and
        # build the result models the 1.x decorators used to build for us.
        async def _on_list_tools(ctx, params) -> types.ListToolsResult:
            return types.ListToolsResult(tools=await list_tools())

        async def _on_call_tool(ctx, params) -> types.CallToolResult:
            try:
                content = await call_tool(params.name, dict(params.arguments or {}))
            except ToolError as exc:
                return types.CallToolResult(
                    content=[types.TextContent(type="text", text=str(exc))],
                    isError=True,
                )
            return types.CallToolResult(content=content)

        async def _on_list_resources(ctx, params) -> types.ListResourcesResult:
            return types.ListResourcesResult(resources=await list_resources())

        async def _on_read_resource(ctx, params) -> types.ReadResourceResult:
            text = await read_resource(params.uri)
            mime = "text/markdown" if str(params.uri).startswith("graphify://report") else "text/plain"
            return types.ReadResourceResult(
                contents=[types.TextResourceContents(uri=params.uri, mimeType=mime, text=text)]
            )

        try:
            from importlib.metadata import version as _pkg_version
            _version = _pkg_version("graphifyy")
        except Exception:
            _version = "0"
        server = Server(
            "graphify",
            version=_version,
            on_list_tools=_on_list_tools,
            on_call_tool=_on_call_tool,
            on_list_resources=_on_list_resources,
            on_read_resource=_on_read_resource,
        )

    # === CUSTOM: /query and /health HTTP endpoints for prompt-hook begin ===
    async def _handle_query(request):
        """轻量级查询端点，供 prompt-hook 快速调用。
        定义在 _build_server 闭包内以访问 _ctx_cache、active_graph_path、_select_graph。
        v3 修订（审核优化 #4）：若 GRAPHIFY_API_KEY 已设置，/query 同样校验。
        """
        import os  # 闭包内局部导入（serve.py 顶部未导入 os）
        from starlette.responses import JSONResponse

        # v3 新增：API Key 校验（若 GRAPHIFY_API_KEY 已设置）
        api_key = os.environ.get("GRAPHIFY_API_KEY", "").strip()
        if api_key:
            auth = request.headers.get("Authorization", "")
            provided = auth.replace("Bearer ", "").strip() if auth.startswith("Bearer ") else auth
            if provided != api_key:
                return JSONResponse({"error": "unauthorized"}, status_code=401)

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json"}, status_code=400)

        prompt = body.get("prompt", "")
        if not prompt:
            return JSONResponse({"error": "no prompt"}, status_code=400)

        project_path = body.get("graph_path") or body.get("project_path")
        try:
            depth = int(body.get("depth", os.environ.get("GRAPHIFY_PROMPT_HOOK_DEPTH", "2")))
            budget = int(body.get("token_budget", os.environ.get("GRAPHIFY_PROMPT_HOOK_BUDGET", "3000")))
        except (ValueError, TypeError):
            depth = 2
            budget = 3000

        # 复用闭包中的 _select_graph（副作用设置 active_graph_path）
        try:
            if project_path:
                _select_graph(project_path)
            ctx = _ctx_cache.get(active_graph_path) if active_graph_path else None
            if not ctx:
                return JSONResponse({"error": "no graph loaded"}, status_code=404)
            G = ctx["G"]
        except Exception as e:
            return JSONResponse({"error": f"graph load failed: {e}"}, status_code=500)

        try:
            # v3 修订（审核优化 #5）：改用关键字参数
            result = _query_graph_text(
                G, question=prompt, mode="bfs", depth=depth,
                token_budget=budget, context_filters=None,
            )
            return JSONResponse({"result": result})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    async def _handle_health(request):
        from starlette.responses import JSONResponse
        return JSONResponse({
            "status": "ok",
            "graph_loaded": bool(active_graph_path and active_graph_path in _ctx_cache)
        })

    # 挂载到 server 对象，供 _build_http_app 取出构建 Route（审核 Bug 3 传递链路）
    # 注意：必须在上方 server 注册逻辑（1.x 装饰器或 2.x on_* 回调）创建 server 之后
    server._graphify_query_handler = _handle_query
    server._graphify_health_handler = _handle_health
    # === CUSTOM: /query and /health HTTP endpoints for prompt-hook end ===

    return server


def serve(graph_path: str | None = None) -> None:
    """Start the MCP server over stdio (the default, per-developer transport)."""
    graph_path = graph_path or _default_graph_json()
    try:
        from mcp.server.stdio import stdio_server
    except ImportError as e:
        raise ImportError('mcp not installed. Run: pip install "graphifyy[mcp]"') from e
    import asyncio

    server = _build_server(graph_path)

    async def main() -> None:
        async with stdio_server() as streams:
            await server.run(streams[0], streams[1], server.create_initialization_options())

    _filter_blank_stdin()
    asyncio.run(main())


class _MCPASGIApp:
    """Raw-ASGI wrapper around the Streamable HTTP session manager.

    Passed to a Starlette ``Route`` as a class instance (not a function) so
    Starlette treats it as an ASGI app: it serves the exact mount path for all
    methods (GET/POST/DELETE) with no request/response wrapping and no
    trailing-slash redirect — mirroring how FastMCP mounts the same manager.
    """

    def __init__(self, manager) -> None:
        self._manager = manager

    async def __call__(self, scope, receive, send) -> None:
        await self._manager.handle_request(scope, receive, send)


class _ApiKeyMiddleware:
    """Pure-ASGI API-key gate for the HTTP transport.

    Implemented as raw ASGI (not Starlette's BaseHTTPMiddleware) on purpose:
    BaseHTTPMiddleware buffers responses and breaks the Streamable HTTP SSE
    stream. This short-circuits with 401 before the request ever reaches the
    session manager, leaving the streaming path untouched for authorized calls.
    """

    def __init__(self, app, api_key: str) -> None:
        self.app = app
        self._expected = api_key.encode("utf-8")

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        import hmac
        headers = dict(scope.get("headers") or [])
        provided = headers.get(b"x-api-key")
        if provided is None:
            # RFC 6750: the auth scheme token is case-insensitive.
            scheme, _, token = headers.get(b"authorization", b"").partition(b" ")
            if scheme.lower() == b"bearer" and token:
                provided = token.strip()
        # Constant-time compare; reject when no key was supplied at all.
        if provided is None or not hmac.compare_digest(provided, self._expected):
            body = b'{"error": "unauthorized"}'
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


def _build_http_app(
    graph_path: str,
    *,
    host: str = "127.0.0.1",
    port: int = 8080,
    api_key: str | None = None,
    path: str = "/mcp",
    json_response: bool = False,
    stateless: bool = False,
    session_timeout: float | None = 3600.0,
):
    """Build the Starlette ASGI app for the Streamable HTTP transport.

    Split out from :func:`serve_http` (which blocks on uvicorn) so the wiring
    can be exercised with an in-process ASGI test client.

    ``session_timeout`` reaps stateful sessions idle for that many seconds so a
    long-running shared server does not leak memory when IDE clients disconnect
    without sending a DELETE. ``None`` (or <= 0) disables reaping; it is forced
    to ``None`` in stateless mode, which has no sessions to reap.
    """
    try:
        import contextlib

        from starlette.applications import Starlette
        from starlette.middleware import Middleware
        from starlette.routing import Route

        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
        from mcp.server.transport_security import TransportSecuritySettings
    except ImportError as e:
        raise ImportError(
            'HTTP transport needs the mcp extra (mcp + starlette + uvicorn). '
            'Run: pip install "graphifyy[mcp]"'
        ) from e

    # A blank key (e.g. --api-key "" or an empty GRAPHIFY_API_KEY) must not be
    # mistaken for "auth on" — normalize it to None so the gate is unambiguous.
    api_key = (api_key or "").strip() or None

    server = _build_server(graph_path)

    # DNS-rebinding protection. When the operator binds a wildcard address they
    # are intentionally exposing the server, so accept any Host header; for a
    # loopback/specific bind, restrict Host to that address (with and without
    # the port) plus the localhost aliases.
    if host in ("0.0.0.0", "::", ""):
        security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    else:
        allowed = {host, "localhost", "127.0.0.1"}
        allowed |= {f"{h}:{port}" for h in list(allowed)}
        security = TransportSecuritySettings(allowed_hosts=sorted(allowed))

    # The SDK rejects a non-positive timeout and forbids one in stateless mode.
    idle_timeout = None if (stateless or not session_timeout or session_timeout <= 0) else session_timeout

    manager = StreamableHTTPSessionManager(
        app=server,
        json_response=json_response,
        stateless=stateless,
        security_settings=security,
        session_idle_timeout=idle_timeout,
    )

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        # The session manager owns an anyio task group that must wrap the whole
        # server lifetime, so enter it here rather than per-request.
        async with manager.run():
            yield

    middleware = []
    if api_key:
        middleware.append(Middleware(_ApiKeyMiddleware, api_key=api_key))

    # === CUSTOM: inject /query and /health routes begin ===
    from starlette.routing import Route as _Route
    _extra_routes = []
    if hasattr(server, "_graphify_query_handler"):
        _extra_routes.append(_Route("/query", server._graphify_query_handler, methods=["POST"]))
    if hasattr(server, "_graphify_health_handler"):
        _extra_routes.append(_Route("/health", server._graphify_health_handler, methods=["GET"]))
    # === CUSTOM: inject /query and /health routes end ===

    return Starlette(
        routes=[Route(path, endpoint=_MCPASGIApp(manager))] + _extra_routes,  # === CUSTOM: 追加 extra_routes ===
        middleware=middleware,
        lifespan=lifespan,
    )


def serve_http(
    graph_path: str | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8080,
    api_key: str | None = None,
    path: str = "/mcp",
    json_response: bool = False,
    stateless: bool = False,
    session_timeout: float | None = 3600.0,
) -> None:
    """Start the MCP server over Streamable HTTP (MCP spec 2025-03-26).

    Serves the same tools/resources as the stdio transport, so a single shared
    process can host the graph for a whole team. Clients point their IDE MCP
    config at ``http://<host>:<port><path>`` (default ``/mcp``).

    ``api_key`` (or the ``GRAPHIFY_API_KEY`` env var) enables a simple header
    check (``Authorization: Bearer <key>`` or ``X-API-Key: <key>``). OAuth is a
    deliberate follow-up. Binding ``0.0.0.0`` exposes the server beyond
    localhost — set an api_key when you do.
    """
    graph_path = graph_path or _default_graph_json()
    try:
        import uvicorn
    except ImportError as e:
        raise ImportError(
            'HTTP transport needs the mcp extra (mcp + starlette + uvicorn). '
            'Run: pip install "graphifyy[mcp]"'
        ) from e

    api_key = (api_key or "").strip() or None

    app = _build_http_app(
        graph_path,
        host=host,
        port=port,
        api_key=api_key,
        path=path,
        json_response=json_response,
        stateless=stateless,
        session_timeout=session_timeout,
    )

    auth_note = "api-key required" if api_key else "no auth (set --api-key to require one)"
    print(
        f"graphify MCP server (streamable-http) on http://{host}:{port}{path} - {auth_note}",
        file=sys.stderr,
    )
    if host in ("0.0.0.0", "::", "") and not api_key:
        print(
            f"WARNING: binding {host or '0.0.0.0'} with no api-key exposes the graph "
            "unauthenticated on the network. Set --api-key (or GRAPHIFY_API_KEY).",
            file=sys.stderr,
        )
    uvicorn.run(app, host=host, port=port)


def _main(argv: list[str] | None = None) -> None:
    import argparse
    import os

    parser = argparse.ArgumentParser(
        prog="python -m graphify.serve",
        description="Serve a graphify knowledge graph over MCP (stdio or Streamable HTTP).",
    )
    parser.add_argument(
        "graph_path",
        nargs="?",
        default=None,
        help="Path to graph.json (default: graphify-out/graph.json)",
    )
    parser.add_argument(
        "--graph",
        dest="graph_flag",
        default=None,
        metavar="PATH",
        help="Path to graph.json — alias for the positional argument",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="Transport to serve on (default: stdio)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="HTTP bind port (default: 8080)")
    parser.add_argument(
        "--api-key",
        default=os.environ.get("GRAPHIFY_API_KEY"),
        help="Require this key on the HTTP transport (env: GRAPHIFY_API_KEY)",
    )
    parser.add_argument("--path", default="/mcp", help="HTTP mount path (default: /mcp)")
    parser.add_argument(
        "--json-response",
        action="store_true",
        help="Return plain JSON responses instead of SSE streams",
    )
    parser.add_argument(
        "--stateless",
        action="store_true",
        help="Run without per-session state (for load-balanced / CI deployments)",
    )
    parser.add_argument(
        "--session-timeout",
        type=float,
        default=3600.0,
        help="Reap stateful sessions idle this many seconds (default: 3600; 0 disables)",
    )
    args = parser.parse_args(argv)
    graph_path = args.graph_flag or args.graph_path or _default_graph_json()

    if args.transport == "http":
        serve_http(
            graph_path,
            host=args.host,
            port=args.port,
            api_key=args.api_key,
            path=args.path,
            json_response=args.json_response,
            stateless=args.stateless,
            session_timeout=args.session_timeout,
        )
    else:
        serve(graph_path)


if __name__ == "__main__":
    _main()
