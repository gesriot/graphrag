"""Regression for the inih C frontend bootstrap (Phase 6, second C target).

Locks the inih BYOG and the preprocessor-fragmentation fix:
- the real functions are extracted and the intra-package call graph
  (ini_parse -> ini_parse_file -> ini_parse_stream, plus the string path and the
  helper calls) is captured as deterministic CALLS edges;
- callbacks are invoked through the `handler`/`reader` function-pointer
  parameters and libc calls are undefined, so both stay weak observations;
- no phantom control-keyword "function" leaks in. tree-sitter-c does not
  evaluate the preprocessor, so inih's `#if`-fragmented `else if (cond) {..}`
  blocks were misparsed as `function_definition`s named `if`; `_func_name` now
  rejects C reserved words.

Run: uv run python -m pytest examples/inih/tests/test_inih_extract.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
from extract_c import build_c_byog  # type: ignore


def test_inih_functions_and_call_graph():
    data = build_c_byog(ROOT / "examples" / "inih")
    titles = {e["title"] for e in data["entities"]}
    for fn in (
        "ini:ini_parse",
        "ini:ini_parse_file",
        "ini:ini_parse_stream",
        "ini:ini_parse_string",
        "ini:ini_parse_string_length",
        "ini:ini_reader_string",
        "ini:ini_rstrip",
        "ini:ini_lskip",
        "ini:ini_find_chars_or_comment",
        "ini:ini_strncpy0",
    ):
        assert fn in titles, f"missing function entity {fn}"
    # anonymous-struct typedef captured
    assert "ini:ini_parse_string_ctx" in titles

    calls = {
        (r["source"], r["target"])
        for r in data["relationships"]
        if r["type"] == "calls"
    }
    for edge in (
        ("ini:ini_parse", "ini:ini_parse_file"),
        ("ini:ini_parse_file", "ini:ini_parse_stream"),
        ("ini:ini_parse_string", "ini:ini_parse_string_length"),
        ("ini:ini_parse_string_length", "ini:ini_parse_stream"),
        ("ini:ini_parse_stream", "ini:ini_rstrip"),
        ("ini:ini_parse_stream", "ini:ini_lskip"),
        ("ini:ini_parse_stream", "ini:ini_find_chars_or_comment"),
        ("ini:ini_parse_stream", "ini:ini_strncpy0"),
    ):
        assert edge in calls, f"missing call edge {edge}"

    # Library purity: every call originating in the inih library (ini.c/ini.h)
    # resolves to a library function -- external/libc calls and the
    # handler/reader callbacks must never become core deterministic edges. (The
    # co-located golden runner is also package code; scope this to the library.)
    lib_targets = {
        r["target"]
        for r in data["relationships"]
        if r["type"] == "calls" and r["source"].startswith("ini:")
    }
    assert all(t.startswith("ini:") for t in lib_targets), (
        "non-library call leaked into the inih core edges"
    )
    # The handler callback is invoked via the HANDLER macro and the line reader
    # via the `reader` function pointer. tree-sitter-c does not expand macros, so
    # the handler call is observed under the macro name. Both stay observations.
    callback_targets = {o["display_target"] for o in data["call_observations"]}
    assert "reader" in callback_targets, "reader callback should be an observation"
    assert "HANDLER" in callback_targets, "handler (macro) callback should be an observation"


def test_no_phantom_keyword_functions_from_preprocessor_fragmentation():
    """tree-sitter-c misparses #if-split bodies; keyword names must be rejected."""
    data = build_c_byog(ROOT / "examples" / "inih")
    titles = {e["title"] for e in data["entities"]}
    for kw in ("ini:if", "ini:else", "ini:for", "ini:while", "ini:switch", "ini:int"):
        assert kw not in titles, f"phantom keyword function leaked: {kw}"
    # and no call edge may originate from a phantom keyword caller
    sources = {r["source"] for r in data["relationships"] if r["type"] == "calls"}
    assert not any(
        s.split(":", 1)[-1] in {"if", "else", "for", "while", "int"} for s in sources
    )


def test_func_name_rejects_reserved_words(tmp_path):
    """A function body split by #if can look like `else if (cond) {..}`."""
    (tmp_path / "frag.c").write_text(
        "int real(int x) {\n"
        "#if FEATURE\n"
        "    if (x > 0) { return helper(x); }\n"
        "#endif\n"
        "    return 0;\n"
        "}\n",
        encoding="utf-8",
    )
    data = build_c_byog(tmp_path)
    titles = {e["title"] for e in data["entities"]}
    assert "frag:real" in titles
    assert "frag:if" not in titles
