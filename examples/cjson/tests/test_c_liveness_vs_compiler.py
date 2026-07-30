"""Compare preprocessor liveness labels to a real C compiler (-E).

The oracle is ``clang -E`` / ``cc -E`` with each package's
``compile_commands.json`` flags — not a second copy of our evaluation rules.

* ``live`` regions with non-directive body lines must leave ≥1 surviving line.
* ``dead`` regions must leave none.
* ``unknown`` is not scored as agreement or error; its rate and macro families
  are reported (and asserted present when expected).

Skips cleanly when no compiler is on PATH (same pattern as ASan tests).

Run: uv run python -m pytest examples/cjson/tests/test_c_liveness_vs_compiler.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
from c_preprocessor import (  # type: ignore
    analyze_package,
    compare_liveness_to_compiler,
    find_c_compiler,
    region_liveness,
)


def _cc():
    return find_c_compiler()


@pytest.mark.skipif(_cc() is None, reason="no C compiler available")
@pytest.mark.parametrize("pkg", ["cjson", "inih", "jsmn"])
def test_liveness_labels_agree_with_compiler(pkg: str):
    # Default: toolchain builtins when available (matches default stamps).
    report = compare_liveness_to_compiler(ROOT / "examples" / pkg)
    assert report["regions_scored"] > 0, report
    assert report["disagreements"] == 0, report["disagreement_details"]
    assert report["ok"] is True
    # unknown / vacuous are honest residual, not folded into agreements
    assert 0.0 <= report["unknown_rate"] <= 1.0
    assert (
        report["regions_unknown"] + report["regions_scored"] + report["regions_vacuous"]
        == report["regions_total"]
    )
    assert report["agreements"] == report["regions_scored"]
    # Directive-only regions must not pad the agreement count: they are either
    # decided against the compiler's macro table or set aside as vacuous.
    assert report["regions_vacuous"] == report["empty_body_regions"]
    assert report["agreement_evidence"].get("empty_body") is None

    # Toolchain-independent mode: platform macros dominate the residual.
    bare = compare_liveness_to_compiler(
        ROOT / "examples" / pkg, use_compiler_builtins=False
    )
    assert bare["disagreements"] == 0, bare["disagreement_details"]
    if pkg == "cjson":
        assert bare["unknown_rate"] >= 0.4, bare
        fams = bare["unknown_macro_families"]
        assert any(k.startswith("platform:") for k in fams), fams
        # Builtins must strictly lower unknown rate without inventing agreements.
        assert report["unknown_rate"] < bare["unknown_rate"]


@pytest.mark.skipif(_cc() is None, reason="no C compiler available")
def test_deliberately_wrong_label_fails_oracle():
    """Flip one decidable live region to dead → comparison must disagree."""
    from c_preprocessor import compare_liveness_to_compiler as real_compare
    import c_preprocessor as cp

    # Find a scored live region with non-empty body under cjson.
    pkg = ROOT / "examples" / "cjson"
    pa = analyze_package(pkg)
    target = None
    target_fa = None
    for fa in pa.files.values():
        if Path(fa.path).name != "cJSON.c":
            continue
        for reg in fa.regions:
            if reg.is_include_guard:
                continue
            live, _ = region_liveness(pa, fa, reg)
            if live != "live":
                continue
            lines = fa.path.read_text(encoding="utf-8", errors="replace").splitlines()
            body = [
                ln
                for ln in range(reg.start_line + 1, reg.end_line + 1)
                if 1 <= ln <= len(lines)
                and not cp._DIR_RE.match(lines[ln - 1])
                and lines[ln - 1].strip()
            ]
            if body:
                target = reg
                target_fa = fa
                break
        if target is not None:
            break
    assert target is not None, "need a live region with body content to flip"
    flip_key = (
        Path(target_fa.path).name,
        target.start_line,
        target.end_line,
        target.kind,
    )

    original = cp.region_liveness

    def flipped(pa_, fa_, reg_):
        live, basis = original(pa_, fa_, reg_)
        key = (Path(fa_.path).name, reg_.start_line, reg_.end_line, reg_.kind)
        if key == flip_key and live == "live":
            return "dead", f"deliberately flipped from {live}"
        return live, basis

    cp.region_liveness = flipped  # type: ignore[assignment]
    try:
        report = real_compare(pkg)
    finally:
        cp.region_liveness = original  # type: ignore[assignment]

    assert report["disagreements"] >= 1, report
    assert report["ok"] is False
    # The flip should surface as a dead label with survivors
    assert any(
        d["label"] == "dead" and d["surviving_body_lines"]
        for d in report["disagreement_details"]
    ), report["disagreement_details"]


@pytest.mark.skipif(_cc() is None, reason="no C compiler available")
def test_unknown_not_scored_as_agreement():
    """unknown_rate is the residual; agreement_rate only covers scored regions."""
    report = compare_liveness_to_compiler(
        ROOT / "examples" / "cjson", use_compiler_builtins=False
    )
    # If we wrongly counted unknown as agreement, agreement would equal total.
    assert report["agreements"] + report["disagreements"] == report["regions_scored"]
    assert (
        report["regions_unknown"]
        == report["regions_total"] - report["regions_scored"] - report["regions_vacuous"]
    )
    # Platform residual is real without builtins.
    assert report["regions_unknown"] > 0
    assert report["unknown_macro_families"]


@pytest.mark.skipif(_cc() is None, reason="no C compiler available")
def test_empty_scored_population_is_undefined_not_perfect(tmp_path: Path):
    """No comparable regions must print as n/a, never 100% agreement.

    The registry oracle previously caught this exact reporting error.  A C
    package with no conditionals makes the liveness oracle's judged population
    empty and exercises the same boundary directly.
    """
    source = tmp_path / "empty.c"
    source.write_text("int answer(void) { return 42; }\n", encoding="utf-8")
    compiler = _cc()
    assert compiler is not None  # skip marker above establishes this invariant.
    (tmp_path / "compile_commands.json").write_text(
        json.dumps(
            [
                {
                    "directory": str(tmp_path),
                    "command": f"{compiler} -c empty.c",
                    "file": str(source),
                }
            ]
        ),
        encoding="utf-8",
    )

    report = compare_liveness_to_compiler(tmp_path, compiler=compiler)
    assert report["regions_scored"] == 0
    assert report["agreements"] == 0
    assert report["disagreements"] == 0
    assert report["agreement_rate_scored"] is None


@pytest.mark.skipif(_cc() is None, reason="no C compiler available")
@pytest.mark.parametrize("pkg", ["cjson", "inih", "jsmn"])
def test_directive_only_regions_are_judged_by_the_macro_table(pkg: str):
    """`#ifndef X` / `#define X v` is scoreable, and is actually scored.

    Line survival cannot see a `#define`, so treating these as passes inflated
    the agreement count (cJSON read 15/15 when 5 regions were really checked).
    `-E -dM` decides them. What stays vacuous is only what genuinely cannot be
    attributed: function-like macros, and names several sibling branches define
    to the same replacement (`INI_API`, the cJSON `CJSON_PUBLIC` arms).
    """
    report = compare_liveness_to_compiler(ROOT / "examples" / pkg)
    assert report["agreement_evidence"].get("macro_state", 0) > 0, report[
        "agreement_evidence"
    ]
    for rec in report["vacuous_samples"]:
        assert rec["note"] == "empty_body_unscoreable", rec


@pytest.mark.skipif(_cc() is None, reason="no C compiler available")
def test_flipped_macro_state_label_fails_oracle():
    """The macro-table path must fail a wrong label, not just the line path."""
    import c_preprocessor as cp

    pkg = ROOT / "examples" / "inih"
    original = cp.region_liveness

    def flipped(pa_, fa_, reg_):
        live, basis = original(pa_, fa_, reg_)
        if (
            Path(fa_.path).name == "ini.h"
            and reg_.kind == "ifndef"
            and "INI_ALLOW_MULTILINE" in (reg_.condition or "")
        ):
            return "dead", "deliberately flipped"
        return live, basis

    cp.region_liveness = flipped  # type: ignore[assignment]
    try:
        report = compare_liveness_to_compiler(pkg)
    finally:
        cp.region_liveness = original  # type: ignore[assignment]

    assert report["ok"] is False
    assert any(
        d.get("note") == "macro_state" and "INI_ALLOW_MULTILINE" in str(d.get("condition"))
        for d in report["disagreement_details"]
    ), report["disagreement_details"]
