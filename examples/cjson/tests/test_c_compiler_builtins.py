"""Toolchain-builtin seeding for C preprocessor liveness.

When a compiler is available, ``analyze_package`` may seed from
``compiler -E -dM`` on an empty TU so platform conditionals become decidable
with ``basis=builtin:…``. Without a compiler, platform macros stay ``unknown``
and ``eval_mode=no_compiler`` is recorded on the stamp.

Run: uv run python -m pytest examples/cjson/tests/test_c_compiler_builtins.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
from c_preprocessor import (  # type: ignore
    analyze_package,
    annotate_byog,
    branches_for_span,
    compare_liveness_to_compiler,
    fetch_compiler_builtins,
    find_c_compiler,
    region_liveness,
)


def _cc():
    return find_c_compiler()


def _region_counts(pa) -> tuple[int, int, int]:
    live = dead = unk = 0
    seen = set()
    for fa in pa.files.values():
        if id(fa) in seen:
            continue
        seen.add(id(fa))
        for reg in fa.regions:
            if reg.is_include_guard:
                continue
            lv, _ = region_liveness(pa, fa, reg)
            if lv == "live":
                live += 1
            elif lv == "dead":
                dead += 1
            else:
                unk += 1
    return live, dead, unk


@pytest.mark.skipif(_cc() is None, reason="no C compiler available")
def test_builtins_drop_cjson_unknown_rate_and_oracle_stays_clean():
    before = analyze_package(ROOT / "examples" / "cjson", use_compiler_builtins=False)
    after = analyze_package(ROOT / "examples" / "cjson", use_compiler_builtins=True)
    assert before.eval_mode == "no_compiler"
    assert after.eval_mode == "compiler_builtins"
    assert after.compiler_builtins
    assert "__GNUC__" in after.compiler_builtins or "__clang__" in after.compiler_builtins

    _, _, unk0 = _region_counts(before)
    _, _, unk1 = _region_counts(after)
    total = sum(_region_counts(before))
    assert total == sum(_region_counts(after))
    rate0 = unk0 / total
    rate1 = unk1 / total
    assert rate0 >= 0.5  # baseline floor without builtins
    assert rate1 < rate0
    # On this codebase the empty-TU table decides every platform branch; the
    # only residual the *oracle* still parks is system-header macro overrides.
    assert rate1 <= 0.05, (rate0, rate1, unk1)

    report = compare_liveness_to_compiler(
        ROOT / "examples" / "cjson", use_compiler_builtins=True
    )
    assert report["disagreements"] == 0, report["disagreement_details"]
    assert report["ok"] is True
    assert report["eval_mode"] == "compiler_builtins"
    # vacuous and unknown stay out of the agreement numerator
    assert report["agreements"] == report["regions_scored"]
    assert (
        report["regions_scored"]
        + report["regions_unknown"]
        + report["regions_vacuous"]
        == report["regions_total"]
    )


@pytest.mark.skipif(_cc() is None, reason="no C compiler available")
@pytest.mark.parametrize("pkg", ["inih", "jsmn"])
def test_builtins_oracle_clean_for_other_packages(pkg: str):
    report = compare_liveness_to_compiler(
        ROOT / "examples" / pkg, use_compiler_builtins=True
    )
    assert report["disagreements"] == 0, report["disagreement_details"]
    assert report["ok"] is True
    assert report["unknown_rate"] <= 0.05


def test_no_compiler_mode_keeps_platform_unknown():
    """Forced no_compiler: __GNUC__ stays unknown even if a compiler exists."""
    pa = analyze_package(
        ROOT / "examples" / "cjson", use_compiler_builtins=False
    )
    assert pa.eval_mode == "no_compiler"
    assert pa.compiler_builtins == {}
    found = False
    seen = set()
    for fa in pa.files.values():
        if id(fa) in seen:
            continue
        seen.add(id(fa))
        for reg in fa.regions:
            if reg.kind == "ifdef" and (reg.condition or "").strip() == "__GNUC__":
                lv, basis = region_liveness(pa, fa, reg)
                assert lv == "unknown", (lv, basis)
                assert "not in compile -D" in basis or "builtins" in basis
                found = True
    assert found


@pytest.mark.skipif(_cc() is None, reason="no C compiler available")
def test_stamp_records_eval_mode_and_builtin_basis():
    data = {
        "entities": [
            {
                "title": "cJSON:get_decimal_point",
                "type": "function",
                "source_file": str(ROOT / "examples/cjson/cJSON.c"),
                "span": "279:0-287:1",
            }
        ],
        "relationships": [],
        "call_observations": [],
    }
    # with builtins
    s = annotate_byog(
        data, ROOT / "examples" / "cjson", use_compiler_builtins=True
    )
    assert s["eval_mode"] == "compiler_builtins"
    ent = data["entities"][0]
    assert ent["preprocessor_eval_mode"] == "compiler_builtins"
    # without
    data2 = {
        "entities": [dict(data["entities"][0])],
        "relationships": [],
        "call_observations": [],
    }
    s2 = annotate_byog(
        data2, ROOT / "examples" / "cjson", use_compiler_builtins=False
    )
    assert s2["eval_mode"] == "no_compiler"
    assert data2["entities"][0]["preprocessor_eval_mode"] == "no_compiler"


@pytest.mark.skipif(_cc() is None, reason="no C compiler available")
def test_builtin_basis_provenance_on_gnuc():
    pa = analyze_package(ROOT / "examples" / "cjson", use_compiler_builtins=True)
    seen = set()
    for fa in pa.files.values():
        if id(fa) in seen:
            continue
        seen.add(id(fa))
        for reg in fa.regions:
            if reg.kind == "ifdef" and (reg.condition or "").strip() == "__GNUC__":
                lv, basis = region_liveness(pa, fa, reg)
                assert lv == "live"
                assert basis.startswith("builtin:__GNUC__=")
                return
    pytest.fail("no ifdef __GNUC__ region found")


def test_fetch_builtins_empty_without_compiler(monkeypatch):
    import c_preprocessor as cp

    monkeypatch.setattr(cp, "find_c_compiler", lambda: None)
    assert fetch_compiler_builtins() == {}


def test_include_provided_macros_are_not_called_undefined():
    """`#ifndef NAN` is dead: math.h defines NAN before cJSON's block runs.

    Empty-TU builtins cannot see include-provided macros, so calling them
    undefined labelled `isinf`, `isnan` and `NAN` in cJSON.c *live* — three
    confidently wrong labels. The compiler oracle then declined to score
    exactly those regions as a "model gap", so nothing caught it. The labeller
    now consults the real translation unit's macro table.
    """
    pa_pkg = analyze_package(ROOT / "examples" / "cjson", use_compiler_builtins=True)
    if pa_pkg.eval_mode != "compiler_builtins":
        pytest.skip("compiler builtins unavailable")
    for name in ("NAN", "isinf", "isnan"):
        assert name in pa_pkg.include_macros, name

    br = branches_for_span(
        pa_pkg, str(ROOT / "examples/cjson/cJSON.c"), "70:0-90:0"
    )
    guards = {
        b["condition"]: b
        for b in br
        if b["kind"] == "ifndef" and b["condition"] in {"isinf", "isnan", "NAN"}
    }
    assert set(guards) == {"isinf", "isnan", "NAN"}, br
    for name, b in guards.items():
        assert b["liveness"] == "dead", (name, b)


def test_include_macro_inference_stays_non_circular():
    """A package's own `#define` must never make itself look include-provided."""
    pa_pkg = analyze_package(ROOT / "examples" / "cjson", use_compiler_builtins=True)
    if pa_pkg.eval_mode != "compiler_builtins":
        pytest.skip("compiler builtins unavailable")
    # cJSON defines these itself; they must not be attributed to an include.
    for name in ("cJSON_Invalid", "cJSON_Number", "CJSON_NESTING_LIMIT"):
        assert name not in pa_pkg.include_macros, (
            name,
            pa_pkg.include_macros.get(name),
        )


def test_system_override_is_scored_not_parked():
    """The oracle must score these regions rather than call them unscoreable."""
    report = compare_liveness_to_compiler(ROOT / "examples" / "cjson")
    assert report["disagreements"] == 0, report["disagreement_details"]
    for rec in report["unknown_samples"]:
        assert rec.get("note") != "macro_state_system_override", rec
    # With include macros modelled, cJSON has no undecidable regions left.
    assert report["regions_unknown"] == 0, report["unknown_samples"]
