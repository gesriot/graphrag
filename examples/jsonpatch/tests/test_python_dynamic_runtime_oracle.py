"""Runtime oracle for Python dynamic-dispatch registry extraction.

Imports real registry objects in a subprocess and scores the AST extractor:
same keys, same target names, nothing invented, nothing silently missed.

Run: uv run python -m pytest examples/jsonpatch/tests/test_python_dynamic_runtime_oracle.py -q
"""
from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
from python_dynamic import (  # type: ignore
    compare_registries_to_runtime,
    format_runtime_oracle_report,
    read_runtime_registry,
)


def test_jsonpatch_operations_fully_agree():
    """Ground-truth Name-valued registry: extractor matches runtime 6/6."""
    report = compare_registries_to_runtime(ROOT / "examples" / "jsonpatch")
    assert report["status"] == "ok"
    assert report["skip_reason"] is None
    assert report["disagreements"] == 0
    assert report["missed"] == 0
    assert report["agreements"] == 6
    assert report["entries_runtime"] == 6
    assert report["entries_extracted"] == 6
    assert report["ok"] is True
    assert report["agreement_rate_scored"] == 1.0
    assert report["coverage_of_runtime"] == 1.0
    # Real import, not a second parse: values are classes from the module.
    rt = read_runtime_registry(
        ROOT / "examples" / "jsonpatch",
        "jsonpatch",
        "JsonPatch.operations",
    )
    assert rt["ok"] is True
    assert rt["entries"]["add"]["kind"] == "type"
    assert rt["entries"]["add"]["name"] == "AddOperation"


def test_injected_wrong_candidate_is_disagreement():
    """A deliberately wrong candidate must not be absorbed into agreement."""
    # Invented key.
    report = compare_registries_to_runtime(
        ROOT / "examples" / "jsonpatch",
        extra_extracted={
            "JsonPatch.operations": [("not-a-real-op", "FabricatedOperation")]
        },
    )
    assert report["disagreements"] >= 1
    assert report["invented"] >= 1
    assert report["ok"] is False
    kinds = {d["kind"] for d in report["disagreement_details"]}
    assert "invented" in kinds

    # Wrong target for a real key.
    report2 = compare_registries_to_runtime(
        ROOT / "examples" / "jsonpatch",
        extra_extracted={"JsonPatch.operations": [("add", "NotAddOperation")]},
    )
    assert report2["wrong_target"] >= 1
    assert report2["ok"] is False
    assert any(
        d.get("kind") == "wrong_target" and d.get("key") == "add"
        for d in report2["disagreement_details"]
    )


def test_lambda_tables_remain_missed_not_guessed():
    """Lambda-valued registries: keys are visible, values have no honest name.

    Naming "the one function a lambda body calls" would be a guess (bodies often
    call methods on the bound object, format helpers, or multiple names). The
    diagnostic leaves all 25 isodate entries as *missed*, not invented targets.
    """
    iso = compare_registries_to_runtime(ROOT / "examples" / "isodate")
    assert iso["status"] == "ok"
    assert iso["disagreements"] == 0
    assert iso["entries_extracted"] == 0
    assert iso["missed"] == 25  # 15 STRF_DT_MAP + 10 STRF_D_MAP
    assert iso["entries_runtime"] == 25
    assert iso["coverage_of_runtime"] == 0.0
    assert iso["entries_scored"] == 0
    assert iso["agreement_rate_scored"] is None  # nothing scored → n/a, not 1.0
    assert iso["by_value_shape"].get("Lambda", {}).get("missed") == 25

    # humanize's `_TRANSLATIONS` holds a NullTranslations *instance*, not a
    # callable, so it is a false-positive detection rather than a missed
    # dispatch target — see test_non_callable_tables_are_not_counted_as_missed.
    hum = compare_registries_to_runtime(ROOT / "examples" / "humanize")
    assert hum["missed"] == 0
    assert hum["false_positive_entries"] == 1
    assert hum["entries_extracted"] == 0
    assert hum["disagreements"] == 0


def test_unimportable_package_skips_with_reason(tmp_path: Path):
    """Import failure is a named skip, not an empty-registry agreement."""
    pkg = tmp_path / "broken_pkg"
    pkg.mkdir()
    (pkg / "broken_pkg.py").write_text(
        textwrap.dedent(
            """
            import definitely_not_a_real_module_for_oracle_test  # noqa: F401
            OPS = {"a": int, "b": str}
            def run(k):
                return OPS[k]
            """
        ),
        encoding="utf-8",
    )
    report = compare_registries_to_runtime(pkg)
    assert report["status"] == "skipped"
    assert report["skip_reason"]
    assert "definitely_not_a_real_module" in report["skip_reason"] or "ModuleNotFound" in (
        report["skip_reason"] + str(report.get("import_failures"))
    )
    # Must not look like "agreed on nothing".
    assert report["agreements"] == 0
    assert report["entries_scored"] == 0
    assert report.get("registries_import_failed", 0) >= 1


def test_oracle_reads_subprocess_not_static_reparse(tmp_path: Path):
    """Mutating the registry at import time must be what the oracle sees."""
    pkg = tmp_path / "live_reg"
    pkg.mkdir()
    (pkg / "live_reg.py").write_text(
        textwrap.dedent(
            """
            class A: pass
            class B: pass
            class C: pass
            OPS = {"a": A, "b": B}
            # Runtime-only mutation the AST dict-literal pass cannot see:
            OPS["c"] = C
            del OPS["b"]
            """
        ),
        encoding="utf-8",
    )
    report = compare_registries_to_runtime(pkg)
    # AST still sees a=A, b=B from the literal; runtime has a=A, c=C.
    assert report["status"] == "ok"
    assert report["entries_runtime"] == 2
    assert report["agreements"] == 1  # a
    assert report["invented"] == 1  # b gone at runtime
    assert report["missed"] == 1  # c added at runtime
    assert report["ok"] is False


def test_cli_vs_runtime_json():
    import subprocess

    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "python_dynamic.py"),
            "-p",
            str(ROOT / "examples" / "jsonpatch"),
            "--vs-runtime",
            "--json",
        ],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["agreements"] == 6
    assert data["disagreements"] == 0


def test_non_callable_tables_are_not_counted_as_missed_dispatch():
    """The AST criterion is syntactic; runtime settles what is a registry.

    `charset:UNICODE_RANGES_COMBINED` is str -> range, `humanize:_TRANSLATIONS`
    holds a NullTranslations instance, `semantic_version:SpecItem.KIND_ALIASES`
    is str -> str. Counting those as missed dispatch targets put 350 of a
    reported 375 misses into tables that are not registries at all.
    """
    for pkg, entries in (("charset_normalizer", 347), ("humanize", 1), ("semantic_version", 2)):
        report = compare_registries_to_runtime(ROOT / "examples" / pkg)
        assert report["false_positive_tables"] >= 1, (pkg, report)
        assert report["false_positive_entries"] == entries, (pkg, report)
        assert report["missed"] == 0, (pkg, report["missed_samples"])
        for row in report["registries"]:
            if row.get("status") == "not_a_callable_registry":
                assert row["runtime_value_kinds"], row
                assert "type" not in row["runtime_value_kinds"], row


def test_decorator_registry_is_detected_and_oracle_confirms():
    """``@BaseSpec.register_syntax`` fills SYNTAXES; extractor + oracle agree 2/2.

    The table starts as ``{}`` so a dict-literal pass never names members. The
    decorator path sees the class decorator, the register classmethod's
    ``cls.SYNTAXES[key] = subclass`` write, and the class-body ``SYNTAX`` key.
    """
    report = compare_registries_to_runtime(ROOT / "examples" / "semantic_version")
    assert report["disagreements"] == 0, report["disagreement_details"]
    assert report["agreements"] == 2, report
    assert report["missed"] == 0
    assert report["coverage_of_runtime"] == 1.0
    rows = {r["registry"]: r for r in report["registries"]}
    assert "BaseSpec.SYNTAXES" in rows, rows
    assert rows["BaseSpec.SYNTAXES"]["status"] == "compared"
    assert rows["BaseSpec.SYNTAXES"]["agreements"] == 2
    assert rows["BaseSpec.SYNTAXES"]["value_shapes"].get("Decorator") == 2
    # No longer invisible — independent discovery must not still list it.
    found = {u["registry"] for u in report["undetected_registries"]}
    assert "BaseSpec.SYNTAXES" not in found, report["undetected_registries"]

    jp = compare_registries_to_runtime(ROOT / "examples" / "jsonpatch")
    assert jp["undetected_registries"] == [], jp["undetected_registries"]


def test_wrong_decorator_inference_is_disagreement():
    """A planted wrong decorator candidate must fail the oracle, not vanish."""
    report = compare_registries_to_runtime(
        ROOT / "examples" / "semantic_version",
        extra_extracted={
            "BaseSpec.SYNTAXES": [("simple", "NotSimpleSpec"), ("forged", "FakeSpec")]
        },
    )
    assert report["ok"] is False
    assert report["wrong_target"] >= 1
    assert report["invented"] >= 1
    kinds = {d["kind"] for d in report["disagreement_details"]}
    assert "wrong_target" in kinds
    assert "invented" in kinds


def test_decorator_candidates_emitted_without_changing_edges():
    """Candidates for BaseSpec.parse name SimpleSpec/NpmSpec; no new call edges."""
    from mini_game_to_byog import build_byog_for_package  # type: ignore
    from python_dynamic import annotate_byog  # type: ignore

    data = build_byog_for_package(package_dir=ROOT / "examples" / "semantic_version")
    n_calls = sum(1 for r in data["relationships"] if r.get("type") == "calls")
    dets = [
        bool(r.get("is_deterministic"))
        for r in data["relationships"]
        if r.get("type") == "calls"
    ]
    annotate_byog(data, ROOT / "examples" / "semantic_version")
    assert sum(1 for r in data["relationships"] if r.get("type") == "calls") == n_calls
    assert [
        bool(r.get("is_deterministic"))
        for r in data["relationships"]
        if r.get("type") == "calls"
    ] == dets
    cands = [
        o
        for o in data["call_observations"]
        if str(o.get("reason", "")).startswith("registry_candidate:SYNTAXES")
    ]
    targets = {str(o.get("display_target")) for o in cands}
    assert any(t.endswith("SimpleSpec") for t in targets), targets
    assert any(t.endswith("NpmSpec") for t in targets), targets
    parse = next(
        e for e in data["entities"] if str(e.get("title", "")).endswith("BaseSpec.parse")
    )
    assert parse.get("dynamic_dependent") is True
    assert any("call_through_registry:SYNTAXES" in str(r) for r in (parse.get("dynamic_reasons") or []))


def test_rates_over_an_empty_population_are_undefined_not_perfect():
    """No comparable entries means "not measured", never 100%."""
    report = compare_registries_to_runtime(ROOT / "examples" / "sqlparse")
    assert report["entries_scored"] == 0
    assert report["agreement_rate_scored"] is None, report
    assert report["coverage_of_runtime"] is None, report
    text = format_runtime_oracle_report(report)
    assert "n/a" in text
    assert "100.0%" not in text
