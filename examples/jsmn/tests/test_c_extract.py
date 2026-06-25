"""Regression for the Phase 6 C frontend bootstrap (tree-sitter-c extractor).

Locks the jsmn BYOG: known functions are extracted and the internal call graph
(jsmn_parse -> helpers) is captured as deterministic CALLS edges, while external
calls (printf/CHECK/...) stay as weak observations -- so the audit rails apply to
C graphs unchanged.

Run: uv run python -m pytest examples/jsmn/tests/test_c_extract.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
from extract_c import build_c_byog  # type: ignore


def test_jsmn_functions_and_call_graph():
    data = build_c_byog(ROOT / "examples" / "jsmn")
    titles = {e["title"] for e in data["entities"]}
    for fn in (
        "jsmn:jsmn_parse",
        "jsmn:jsmn_init",
        "jsmn:jsmn_alloc_token",
        "jsmn:jsmn_parse_string",
        "jsmn:jsmn_parse_primitive",
    ):
        assert fn in titles, f"missing function entity {fn}"
    # structs/enums extracted
    assert "jsmn:jsmn_parser" in titles
    assert "jsmn:jsmnerr" in titles

    calls = {
        (r["source"], r["target"])
        for r in data["relationships"]
        if r["type"] == "calls"
    }
    for callee in (
        "jsmn:jsmn_alloc_token",
        "jsmn:jsmn_parse_string",
        "jsmn:jsmn_parse_primitive",
    ):
        assert ("jsmn:jsmn_parse", callee) in calls, f"missing jsmn_parse -> {callee}"

    # external/undefined C calls must be weak observations, never core edges.
    targets = {r["target"] for r in data["relationships"] if r["type"] == "calls"}
    assert all(t.startswith("jsmn:") for t in targets), (
        "non-package call leaked into core edges"
    )
    assert any(o["reason"] == "external/undefined C call" for o in data["call_observations"])


def test_duplicate_c_function_names_prefer_same_file_and_demote_ambiguous(tmp_path):
    (tmp_path / "a.c").write_text(
        "static int helper(void) { return 1; }\n"
        "int a(void) { return helper(); }\n",
        encoding="utf-8",
    )
    (tmp_path / "b.c").write_text(
        "static int helper(void) { return 2; }\n"
        "int b(void) { return helper(); }\n",
        encoding="utf-8",
    )
    (tmp_path / "c.c").write_text(
        "int c(void) { return helper(); }\n",
        encoding="utf-8",
    )

    data = build_c_byog(tmp_path)
    calls = {
        (r["source"], r["target"])
        for r in data["relationships"]
        if r["type"] == "calls"
    }
    assert ("a:a", "a:helper") in calls
    assert ("b:b", "b:helper") in calls
    assert not any(source == "c:c" for source, _target in calls)
    assert any(
        o["source"] == "c:c"
        and o["display_target"] == "helper"
        and o["reason"] == "ambiguous C call"
        for o in data["call_observations"]
    )
