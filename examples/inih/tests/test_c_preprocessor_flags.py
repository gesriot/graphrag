"""Preprocessor-dependence provenance for the tree-sitter C frontend.

Locks the diagnostic increment (no clang): facts inside #if regions and calls
to function-like macros are labelled, without demoting is_deterministic or
changing audit pass-rate semantics.

Run: uv run python -m pytest examples/inih/tests/test_c_preprocessor_flags.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
from c_preprocessor import analyze_package, annotate_byog  # type: ignore
from extract_c import build_c_byog  # type: ignore


def test_inih_detects_handler_macro_and_conditional_calls():
    """inih is the known preprocessor-heavy target (PROVENANCE.md)."""
    data = build_c_byog(ROOT / "examples" / "inih")
    # HANDLER is a function-like macro; observations under that name must be labelled.
    handler_obs = [
        o for o in data["call_observations"] if o.get("display_target") == "HANDLER"
    ]
    assert handler_obs, "expected HANDLER call observations"
    assert all(o.get("preprocessor_dependent") for o in handler_obs)
    assert any(
        any(str(r).startswith("function_like_macro:HANDLER") for r in o.get("preprocessor_reasons") or [])
        for o in handler_obs
    )
    # At least one trusted call sits inside an INI_* conditional (default-config
    # path still present in the source; tree-sitter sees every branch).
    flagged_calls = [
        r
        for r in data["relationships"]
        if r.get("type") == "calls" and r.get("preprocessor_dependent")
    ]
    assert flagged_calls, "expected at least one preprocessor-dependent call edge"
    assert all(bool(r.get("is_deterministic")) for r in flagged_calls), (
        "diagnostic must not demote is_deterministic"
    )
    assert any(
        any("INI_ALLOW_MULTILINE" in str(x) for x in (r.get("preprocessor_reasons") or []))
        for r in flagged_calls
    ), "expected INI_ALLOW_MULTILINE region to flag a call (inih known case)"


def test_fragmented_body_flags_entity_without_creating_keyword_phantom(tmp_path):
    (tmp_path / "frag.c").write_text(
        "int helper(int x) { return x; }\n"
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
    assert "frag:if" not in titles
    real = next(e for e in data["entities"] if e["title"] == "frag:real")
    assert real.get("preprocessor_dependent") is True
    assert "entity_body_has_preprocessor" in (real.get("preprocessor_reasons") or [])
    # Call inside #if FEATURE remains deterministic but labelled.
    calls = [r for r in data["relationships"] if r["type"] == "calls"]
    assert any(r["source"] == "frag:real" and r["target"] == "frag:helper" for r in calls)
    flagged = next(r for r in calls if r["source"] == "frag:real")
    assert bool(flagged.get("is_deterministic"))
    assert flagged.get("preprocessor_dependent") is True


def test_function_like_macro_requires_no_space_before_paren():
    """#define NAME (x) is object-like; #define NAME(x) is function-like."""
    from c_preprocessor import analyze_source_text  # type: ignore

    text = "#define OBJ (1)\n#define FUN(x) (x)\n"
    fa = analyze_source_text(text, path=Path("t.h"))
    names_fn = {m.name for m in fa.macros if m.function_like}
    names_obj = {m.name for m in fa.macros if not m.function_like}
    assert "FUN" in names_fn
    assert "OBJ" in names_obj
    assert "OBJ" not in names_fn
