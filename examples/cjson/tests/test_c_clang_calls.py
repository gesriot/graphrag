"""Optional Clang configured direct-call evidence overlay.

Pure application tests always run. Live Clang tests skip when no compiler.

Run: uv run python -m pytest examples/cjson/tests/test_c_clang_calls.py -q
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
from c_clang_call_audit import (  # type: ignore
    parse_tree_sitter_call_span,
    source_byte_offset,
)
from c_clang_calls import (  # type: ignore
    FACT_KIND,
    MODE,
    ClangCallOverlayError,
    append_clang_calls,
    apply_clang_calls_from_report,
    build_disabled_provenance,
)
from c_preprocessor import find_c_compiler  # type: ignore
from extract_c import build_c_byog  # type: ignore
from index_c import main as index_c_main  # type: ignore


def _cc():
    return find_c_compiler()


def _base_call(
    *,
    source: str,
    target: str,
    source_file: str,
    span: str,
    rid: str = "rel:calls:0",
    **extra,
) -> dict:
    rel = {
        "id": rid,
        "source": source,
        "target": target,
        "type": "calls",
        "description": f"{source} calls {target}",
        "source_file": source_file,
        "span": span,
        "extractor": "tree-sitter-c",
        "confidence": 0.9,
        "is_deterministic": True,
        "weight": 1.0,
    }
    rel.update(extra)
    return rel


def _matched_row(
    *,
    caller_title: str,
    target_title: str,
    source_path: str,
    tree_sitter_span: str,
    byte_offset: int,
    match_basis: str = "exact_byte_offset",
    entry_indices: list | None = None,
    observations: list | None = None,
    compilers: list | None = None,
    **extra,
) -> dict:
    line, col0 = parse_tree_sitter_call_span(tree_sitter_span)
    indices = entry_indices if entry_indices is not None else [0]
    comps = compilers or [
        {
            "compiler_path": "/usr/bin/clang",
            "compiler_id": "Apple clang version test",
            "compile_commands_digest": "abc123",
        }
    ]
    obs = observations or [
        {
            "classification": "internal_direct",
            "entry_index": indices[0],
            "compiler_path": "/usr/bin/clang",
            "compiler_id": "Apple clang version test",
            "compile_commands_digest": "abc123",
            "resolve_reason": "function_decl",
            "ref_kind": "FunctionDecl",
            "ref_type": "int (void)",
            "target_title": target_title,
        }
    ]
    row = {
        "caller_title": caller_title,
        "target_title": target_title,
        "source_path": source_path,
        "line": line,
        "col0": col0,
        "byte_offset": byte_offset,
        "tree_sitter_span": tree_sitter_span,
        "clang_line": line,
        "clang_col1": (col0 + 1) if col0 is not None else None,
        "clang_byte_offset": byte_offset,
        "match_basis": match_basis,
        "clang_entry_indices": indices,
        "clang_resolve_reason": "function_decl",
        "ref_kind": "FunctionDecl",
        "ref_type": "int (void)",
        "compiler_path": "/usr/bin/clang",
        "compiler_id": "Apple clang version test",
        "compile_commands_digest": "abc123",
        "clang_observations": obs,
        "clang_compilers": comps,
    }
    row.update(extra)
    return row


def _clean_report(
    matched: list,
    *,
    package: str = "pkg",
    total_calls: int | None = None,
    tree_sitter_only: list | None = None,
    out_of_scope: list | None = None,
    external_direct: list | None = None,
    indirect: list | None = None,
    clang_only: list | None = None,
    ambiguous: list | None = None,
    macro_loc: list | None = None,
    covered: int = 0,
    **count_overrides,
) -> dict:
    def classified(rows: list | None, classification: str) -> list:
        return [
            {**row, "classification": row.get("classification", classification)}
            for row in (rows or [])
        ]

    ts_only = classified(tree_sitter_only, "tree_sitter_only_internal")
    oos = classified(out_of_scope, "out_of_compile_db_scope")
    external = classified(external_direct, "external_direct")
    ind = classified(indirect, "indirect")
    c_only = classified(clang_only, "internal_direct")
    amb = classified(ambiguous, "ambiguous")
    macro = classified(macro_loc, "macro_location_unsupported")
    if total_calls is None:
        total_calls = len(matched) + covered + len(ts_only) + len(oos)
    counts = {
        "matched_internal": len(matched),
        "clang_only_internal": len(c_only),
        "tree_sitter_only_internal": len(ts_only),
        "external_direct": len(external),
        "indirect": len(ind),
        "ambiguous": len(amb),
        "macro_location_unsupported": len(macro),
        "out_of_compile_db_scope": len(oos),
    }
    counts.update(count_overrides)
    return {
        "mode": "clang_ast_json_call_audit",
        "package": package,
        "compiler_path": "/usr/bin/clang",
        "compiler_id": "Apple clang version test",
        "compiler_version": "17.0.0",
        "compilers": [
            {
                "compiler_path": "/usr/bin/clang",
                "compiler_id": "Apple clang version test",
                "compiler_version": "17.0.0",
            }
        ],
        "compile_commands_digest": "abc123",
        "n_compile_entries": 1,
        "translation_units": [
            {
                "entry_index": 0,
                "file": "a.c",
                "package_local": True,
                "compiler_path": "/usr/bin/clang",
                "compiler_id": "Apple clang version test",
            }
        ],
        "counts": counts,
        "tree_sitter_accounting": {
            "total_calls": total_calls,
            "matched_internal": counts["matched_internal"],
            "covered_by_noninternal_clang_observation": covered,
            "tree_sitter_only_internal": counts["tree_sitter_only_internal"],
            "out_of_compile_db_scope": counts["out_of_compile_db_scope"],
        },
        "matched_internal": matched,
        "clang_only_internal": c_only,
        "tree_sitter_only_internal": ts_only,
        "external_direct": external,
        "indirect": ind,
        "ambiguous": amb,
        "macro_location_unsupported": macro,
        "out_of_compile_db_scope": oos,
        "limitations": [],
        "confidence_boundary": "test",
    }


def _pkg_with_call(tmp_path: Path) -> tuple[Path, dict, dict, int]:
    """Minimal package: one function calling another at a known span."""
    pkg = tmp_path / "pkg"
    pkg.mkdir(parents=True, exist_ok=True)
    # line 1: int helper...  line 2: int caller... with call on that line
    src_text = (
        "static int helper(void) { return 1; }\n"
        "int caller(void) { return helper(); }\n"
    )
    src = pkg / "a.c"
    src.write_text(src_text, encoding="utf-8")
    # Call site is "helper" on line 2; find column of 'h' in helper()
    line2 = src_text.splitlines()[1]
    col0 = line2.index("helper")
    span = f"2:{col0}"
    bo = source_byte_offset(src, 2, col0)
    assert bo is not None
    call = _base_call(
        source="a:caller",
        target="a:helper",
        source_file=str(src),
        span=span,
        rid="rel:calls:1",
    )
    data = {
        "entities": [],
        "relationships": [call],
        "text_units": [],
        "call_observations": [],
    }
    row = _matched_row(
        caller_title="a:caller",
        target_title="a:helper",
        source_path="a.c",
        tree_sitter_span=span,
        byte_offset=int(bo),
    )
    return pkg, data, row, int(bo)


# ---------------------------------------------------------------------------
# Pure application
# ---------------------------------------------------------------------------


def test_exact_matched_relationship_receives_metadata(tmp_path: Path):
    pkg, data, row, bo = _pkg_with_call(tmp_path)
    report = _clean_report([row], total_calls=1)
    prov = apply_clang_calls_from_report(data, report, pkg)
    assert prov["enabled"] is True
    assert prov["mode"] == MODE
    assert prov["n_facts"] == 1
    assert prov["fact_kind"] == FACT_KIND
    assert prov["extractor"] == "clang-ast-json"
    rel = data["relationships"][0]
    assert rel["clang_call_status"] == "matched"
    assert rel["clang_call_fact_kind"] == FACT_KIND
    assert rel["clang_call_extractor"] == "clang-ast-json"
    assert rel["clang_call_confidence"] == 1.0
    assert rel["clang_call_is_deterministic"] is True
    assert rel["clang_call_match_basis"] == "exact_byte_offset"
    assert rel["clang_call_byte_offset"] == bo
    assert rel["clang_call_entry_indices"] == [0]
    assert rel["clang_call_compile_commands_digest"] == "abc123"
    assert "configured Clang direct-call confirmation" in rel["clang_call_description"]
    assert "compile_commands.json" in rel["clang_call_description"]
    # Base fields unchanged
    assert rel["extractor"] == "tree-sitter-c"
    assert rel["confidence"] == 0.9
    assert rel["is_deterministic"] is True


def test_apply_materializes_absent_nullable_keys(tmp_path: Path):
    """Absent nullable payload keys must be written even when the value is None."""
    pkg, data, row, _bo = _pkg_with_call(tmp_path)
    row["clang_resolve_reason"] = None
    row["ref_type"] = None
    row["clang_observations"][0]["resolve_reason"] = None
    row["clang_observations"][0]["ref_type"] = None
    rel = data["relationships"][0]
    assert "clang_call_resolve_reason" not in rel
    assert "clang_call_ref_type" not in rel
    apply_clang_calls_from_report(data, _clean_report([row], total_calls=1), pkg)
    assert "clang_call_resolve_reason" in rel
    assert rel["clang_call_resolve_reason"] is None
    assert "clang_call_ref_type" in rel
    assert rel["clang_call_ref_type"] is None
    for field in (
        "clang_call_status",
        "clang_call_fact_kind",
        "clang_call_extractor",
        "clang_call_confidence",
        "clang_call_is_deterministic",
        "clang_call_match_basis",
        "clang_call_byte_offset",
        "clang_call_entry_indices",
        "clang_call_compile_commands_digest",
        "clang_call_compiler_path",
        "clang_call_compiler_id",
        "clang_call_compilers_json",
        "clang_call_resolve_reason",
        "clang_call_ref_kind",
        "clang_call_ref_type",
        "clang_call_observations_json",
        "clang_call_description",
    ):
        assert field in rel


def test_graph_shape_and_base_fields_unchanged(tmp_path: Path):
    pkg, data, row, _bo = _pkg_with_call(tmp_path)
    other = _base_call(
        source="a:caller",
        target="a:helper",
        source_file=str(pkg / "a.c"),
        span="2:99",  # different span; not matched
        rid="rel:calls:orphan",
    )
    # Force a second relationship with unreachable span so it is residual-like
    # (won't be selected). Use a synthetic source without needing real offset.
    data["relationships"].append(other)
    # Accounting: total_calls must equal base calls count
    report = _clean_report(
        [row],
        total_calls=2,
        tree_sitter_only=[
            {
                "caller_title": "a:caller",
                "target_title": "a:helper",
                "source_path": "a.c",
                "tree_sitter_span": "2:99",
            }
        ],
    )
    before_ids = [(r["id"], r["source"], r["target"], r["type"]) for r in data["relationships"]]
    before_base = {
        r["id"]: {
            k: r.get(k)
            for k in (
                "extractor",
                "confidence",
                "is_deterministic",
                "source",
                "target",
                "type",
                "span",
                "source_file",
            )
        }
        for r in data["relationships"]
    }
    n_obs = len(data["call_observations"])
    apply_clang_calls_from_report(data, report, pkg)
    after_ids = [(r["id"], r["source"], r["target"], r["type"]) for r in data["relationships"]]
    assert before_ids == after_ids
    assert len(data["relationships"]) == 2
    assert len(data["call_observations"]) == n_obs
    for r in data["relationships"]:
        for k, v in before_base[r["id"]].items():
            assert r.get(k) == v
    # Unselected relationship has no invented metadata
    assert data["relationships"][1].get("clang_call_status") is None


def test_default_build_c_byog_unchanged():
    d1 = build_c_byog(ROOT / "examples" / "inih")
    d2 = build_c_byog(ROOT / "examples" / "inih")
    for r in d1["relationships"]:
        assert not any(str(k).startswith("clang_call_") for k in r)
    assert d1 == d2


def test_byte_offset_mismatch_fails_atomically(tmp_path: Path):
    pkg, data, row, bo = _pkg_with_call(tmp_path)
    bad = dict(row)
    bad["byte_offset"] = bo + 1
    bad["clang_byte_offset"] = bo + 1
    report = _clean_report([bad], total_calls=1)
    before = json.dumps(data, sort_keys=True)
    with pytest.raises(ClangCallOverlayError, match="no exact calls relationship"):
        apply_clang_calls_from_report(data, report, pkg)
    assert json.dumps(data, sort_keys=True) == before


def test_missing_relationship_match_fails_atomically(tmp_path: Path):
    pkg, data, row, bo = _pkg_with_call(tmp_path)
    data["relationships"] = []  # no edges
    report = _clean_report([row], total_calls=0)
    # total_calls=0 but matched has 1 → accounting mismatch first, or
    # rebuild so accounting is consistent and miss is the failure mode
    report = _clean_report([row], total_calls=1)
    # data has 0 calls → total_calls disagree
    before = json.dumps(data, sort_keys=True)
    with pytest.raises(ClangCallOverlayError, match="disagrees with base calls"):
        apply_clang_calls_from_report(data, report, pkg)
    assert json.dumps(data, sort_keys=True) == before

    # Genuine missing match: one unrelated call edge
    other_src = pkg / "b.c"
    other_src.write_text("int z(void){return 0;}\n", encoding="utf-8")
    data2 = {
        "entities": [],
        "relationships": [
            _base_call(
                source="b:z",
                target="b:z",
                source_file=str(other_src),
                span="1:0",
                rid="rel:other",
            )
        ],
        "text_units": [],
        "call_observations": [],
    }
    # Need valid byte offset for the other call so indexing works
    bo2 = source_byte_offset(other_src, 1, 0)
    # report still points at a.c caller which is not present
    report2 = _clean_report([row], total_calls=1)
    before2 = json.dumps(data2, sort_keys=True)
    with pytest.raises(ClangCallOverlayError, match="no exact calls relationship"):
        apply_clang_calls_from_report(data2, report2, pkg)
    assert json.dumps(data2, sort_keys=True) == before2
    assert bo2 is not None  # silence unused


def test_duplicate_relationship_match_fails_atomically(tmp_path: Path):
    pkg, data, row, bo = _pkg_with_call(tmp_path)
    dup = copy.deepcopy(data["relationships"][0])
    dup["id"] = "rel:calls:dup"
    data["relationships"].append(dup)
    # Two identical attachment keys; one matched row + residual for accounting.
    report = _clean_report(
        [row],
        total_calls=2,
        tree_sitter_only=[{"caller_title": "a:caller", "note": "dup_residual"}],
    )
    before = json.dumps(data, sort_keys=True)
    with pytest.raises(ClangCallOverlayError, match="multiple calls relationships"):
        apply_clang_calls_from_report(data, report, pkg)
    assert json.dumps(data, sort_keys=True) == before

    # Two matched rows with the same key against one unique relationship, plus a
    # second base edge (different span) so total_calls accounting stays honest.
    pkg2, data2, row2, _ = _pkg_with_call(tmp_path / "dupclaim")
    filler_src = pkg2 / "a.c"
    other_span = "2:0"
    other_bo = source_byte_offset(filler_src, 2, 0)
    data2["relationships"].append(
        _base_call(
            source="a:caller",
            target="a:helper",
            source_file=str(filler_src),
            span=other_span,
            rid="rel:filler",
        )
    )
    assert other_bo is not None
    report_claim = _clean_report([row2, dict(row2)], total_calls=2)
    before3 = json.dumps(data2, sort_keys=True)
    with pytest.raises(
        ClangCallOverlayError,
        match="two matched rows claim the same relationship",
    ):
        apply_clang_calls_from_report(data2, report_claim, pkg2)
    assert json.dumps(data2, sort_keys=True) == before3


def test_column_only_matching_impossible(tmp_path: Path):
    """Same caller/target/column-ish data without exact span+offset fails."""
    pkg, data, row, bo = _pkg_with_call(tmp_path)
    # Mutate span string while keeping a bogus offset that won't match
    wrong = dict(row)
    # Keep caller/target and invent a different span with same column digit
    wrong["tree_sitter_span"] = "99:" + row["tree_sitter_span"].split(":")[1]
    wrong["line"] = 99
    wrong["byte_offset"] = bo  # offset still points at real site, but span differs
    wrong["clang_byte_offset"] = bo
    report = _clean_report([wrong], total_calls=1)
    before = json.dumps(data, sort_keys=True)
    with pytest.raises(ClangCallOverlayError, match="no exact calls relationship"):
        apply_clang_calls_from_report(data, report, pkg)
    assert json.dumps(data, sort_keys=True) == before

    # Caller/target alone insufficient: wrong path
    wrong_path = dict(row)
    wrong_path["source_path"] = "other.c"
    report2 = _clean_report([wrong_path], total_calls=1)
    with pytest.raises(ClangCallOverlayError, match="no exact calls relationship"):
        apply_clang_calls_from_report(data, report2, pkg)
    assert json.dumps(data, sort_keys=True) == before

    forged_fallback = dict(row)
    forged_fallback["match_basis"] = "exact_line_col_fallback"
    # A fallback is only valid when Clang omitted its byte offset.
    report3 = _clean_report([forged_fallback], total_calls=1)
    with pytest.raises(ClangCallOverlayError, match="must not carry"):
        apply_clang_calls_from_report(data, report3, pkg)
    assert json.dumps(data, sort_keys=True) == before

    bad_fallback = dict(forged_fallback)
    bad_fallback["clang_byte_offset"] = None
    bad_fallback["clang_line"] = int(bad_fallback["line"]) + 1
    report4 = _clean_report([bad_fallback], total_calls=1)
    with pytest.raises(ClangCallOverlayError, match="unconfirmed"):
        apply_clang_calls_from_report(data, report4, pkg)
    assert json.dumps(data, sort_keys=True) == before

    unconfirmed_exact = dict(row)
    unconfirmed_exact["clang_byte_offset"] = None
    report5 = _clean_report([unconfirmed_exact], total_calls=1)
    with pytest.raises(ClangCallOverlayError, match="offsets disagree"):
        apply_clang_calls_from_report(data, report5, pkg)
    assert json.dumps(data, sort_keys=True) == before


def test_incompatible_and_stale_metadata_fails(tmp_path: Path):
    pkg, data, row, _bo = _pkg_with_call(tmp_path)
    data["relationships"][0]["clang_call_confidence"] = 0.5
    report = _clean_report([row], total_calls=1)
    before = json.dumps(data, sort_keys=True)
    with pytest.raises(ClangCallOverlayError, match="conflicting pre-existing"):
        apply_clang_calls_from_report(data, report, pkg)
    assert json.dumps(data, sort_keys=True) == before

    # Stale metadata on unselected relationship
    pkg2, data2, row2, _ = _pkg_with_call(tmp_path / "stale")
    data2["relationships"][0]["clang_call_future_field"] = "stale"
    empty = _clean_report([], total_calls=1, tree_sitter_only=[{"x": 1}])
    before2 = json.dumps(data2, sort_keys=True)
    with pytest.raises(ClangCallOverlayError, match="stale clang_call_"):
        apply_clang_calls_from_report(data2, empty, pkg2)
    assert json.dumps(data2, sort_keys=True) == before2

    pkg3, data3, row3, _ = _pkg_with_call(tmp_path / "unknown-selected")
    data3["relationships"][0]["clang_call_future_field"] = "unknown"
    report3 = _clean_report([row3], total_calls=1)
    before3 = json.dumps(data3, sort_keys=True)
    with pytest.raises(ClangCallOverlayError, match="unknown pre-existing"):
        apply_clang_calls_from_report(data3, report3, pkg3)
    assert json.dumps(data3, sort_keys=True) == before3

    pkg4, data4, row4, _ = _pkg_with_call(tmp_path / "stale-non-call")
    data4["relationships"].append(
        {
            "id": "rel:contains",
            "source": "a:file",
            "target": "a:caller",
            "type": "contains",
            "clang_call_status": "matched",
        }
    )
    report4 = _clean_report([row4], total_calls=1)
    before4 = json.dumps(data4, sort_keys=True)
    with pytest.raises(ClangCallOverlayError, match="type=contains"):
        apply_clang_calls_from_report(data4, report4, pkg4)
    assert json.dumps(data4, sort_keys=True) == before4


def test_idempotent_identical_application(tmp_path: Path):
    pkg, data, row, _bo = _pkg_with_call(tmp_path)
    report = _clean_report([row], total_calls=1)
    p1 = apply_clang_calls_from_report(data, report, pkg)
    snap = json.dumps(data["relationships"][0], sort_keys=True)
    p2 = apply_clang_calls_from_report(data, report, pkg)
    assert p1["n_facts"] == 1
    assert p2["n_facts"] == 1
    assert p2["n_facts_changed"] == 0
    assert json.dumps(data["relationships"][0], sort_keys=True) == snap


def test_multiple_compiler_observations_preserved_canonically(tmp_path: Path):
    pkg, data, row, _bo = _pkg_with_call(tmp_path)
    obs = [
        {
            "classification": "internal_direct",
            "entry_index": 1,
            "compiler_path": "/usr/bin/clang",
            "compiler_id": "Apple clang version test",
            "compile_commands_digest": "abc123",
            "ref_kind": "FunctionDecl",
            "ref_type": "int (void)",
            "target_title": "a:helper",
            "tag": "b",
        },
        {
            "classification": "internal_direct",
            "entry_index": 0,
            "compiler_path": "/opt/homebrew/opt/llvm/bin/clang",
            "compiler_id": "Homebrew clang version test",
            "compile_commands_digest": "abc123",
            "ref_kind": "FunctionDecl",
            "ref_type": "int (void)",
            "target_title": "a:helper",
            "tag": "a",
        },
    ]
    comps = [
        {
            "compiler_path": "/usr/bin/clang",
            "compiler_id": "Apple clang version test",
            "compile_commands_digest": "abc123",
        },
        {
            "compiler_path": "/opt/homebrew/opt/llvm/bin/clang",
            "compiler_id": "Homebrew clang version test",
            "compile_commands_digest": "abc123",
        },
    ]
    row = dict(row)
    row["clang_observations"] = obs
    row["clang_compilers"] = comps
    row["clang_entry_indices"] = [0, 1]
    report = _clean_report([row], total_calls=1)
    report["compilers"] = [
        {
            "compiler_path": "/usr/bin/clang",
            "compiler_id": "Apple clang version test",
            "compiler_version": "17.0.0",
        },
        {
            "compiler_path": "/opt/homebrew/opt/llvm/bin/clang",
            "compiler_id": "Homebrew clang version test",
            "compiler_version": "18.0.0",
        },
    ]
    report["n_compile_entries"] = 2
    report["translation_units"] = [
        {
            "entry_index": 0,
            "file": "a.c",
            "package_local": True,
            "compiler_path": "/opt/homebrew/opt/llvm/bin/clang",
            "compiler_id": "Homebrew clang version test",
        },
        {
            "entry_index": 1,
            "file": "a.c",
            "package_local": True,
            "compiler_path": "/usr/bin/clang",
            "compiler_id": "Apple clang version test",
        },
    ]
    report["compiler_path"] = None
    report["compiler_id"] = None
    report["compiler_version"] = None
    row["compiler_path"] = None
    row["compiler_id"] = None
    report["matched_internal"] = [row]
    apply_clang_calls_from_report(data, report, pkg)
    raw = data["relationships"][0]["clang_call_observations_json"]
    parsed = json.loads(raw)
    assert len(parsed) == 2
    assert raw == json.dumps(
        parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    # Order-independent: reverse observations yields same canonical string
    data2 = copy.deepcopy(data)
    # strip clang fields
    for k in list(data2["relationships"][0].keys()):
        if str(k).startswith("clang_call_"):
            del data2["relationships"][0][k]
    row_rev = dict(row)
    row_rev["clang_observations"] = list(reversed(obs))
    row_rev["clang_compilers"] = list(reversed(comps))
    report2 = copy.deepcopy(report)
    report2["matched_internal"] = [row_rev]
    apply_clang_calls_from_report(data2, report2, pkg)
    assert data2["relationships"][0]["clang_call_observations_json"] == raw
    assert (
        data2["relationships"][0]["clang_call_compilers_json"]
        == data["relationships"][0]["clang_call_compilers_json"]
    )


@pytest.mark.parametrize(
    "bucket",
    [
        "clang_only_internal",
        "ambiguous",
        "macro_location_unsupported",
    ],
)
def test_fail_closed_residuals_abort(tmp_path: Path, bucket: str):
    pkg, data, row, _bo = _pkg_with_call(tmp_path)
    report = _clean_report([row], total_calls=1)
    report["counts"][bucket] = 1
    classification = (
        "internal_direct" if bucket == "clang_only_internal" else bucket
    )
    report[bucket] = [
        {
            "caller_title": "x",
            "target_title": "y",
            "classification": classification,
        }
    ]
    before = json.dumps(data, sort_keys=True)
    with pytest.raises(ClangCallOverlayError, match="unclean call-audit residuals"):
        apply_clang_calls_from_report(data, report, pkg)
    assert json.dumps(data, sort_keys=True) == before


def test_covered_by_noninternal_aborts(tmp_path: Path):
    pkg, data, row, bo = _pkg_with_call(tmp_path / "cov")
    filler = pkg / "a.c"
    other_span = "1:0"
    other_bo = source_byte_offset(filler, 1, 0)
    data["relationships"].append(
        _base_call(
            source="a:helper",
            target="a:helper",
            source_file=str(filler),
            span=other_span,
            rid="rel:filler",
        )
    )
    report = _clean_report([row], total_calls=2, covered=1)
    assert other_bo is not None
    before = json.dumps(data, sort_keys=True)
    with pytest.raises(
        ClangCallOverlayError,
        match="covered_by_noninternal_clang_observation",
    ):
        apply_clang_calls_from_report(data, report, pkg)
    assert json.dumps(data, sort_keys=True) == before


def test_allowed_residuals_do_not_abort_and_get_no_metadata(tmp_path: Path):
    pkg, data, row, _bo = _pkg_with_call(tmp_path)
    # Add filler edges for accounting
    filler = pkg / "a.c"
    for i, (line, col) in enumerate([(1, 0), (1, 1), (1, 2)]):
        span = f"{line}:{col}"
        data["relationships"].append(
            _base_call(
                source="a:helper",
                target="a:helper",
                source_file=str(filler),
                span=span,
                rid=f"rel:fill:{i}",
            )
        )
    # 1 matched + 1 ts_only + 1 oos + 0 covered = need total 3? we have 4 rels
    # Use 4 total: 1 matched, 1 ts_only, 1 oos, and 1 more oos → total 4
    report = _clean_report(
        [row],
        total_calls=4,
        tree_sitter_only=[{"caller_title": "a:helper", "note": "ts_only"}],
        out_of_scope=[
            {"caller_title": "a:helper", "note": "oos1"},
            {"caller_title": "a:helper", "note": "oos2"},
        ],
        external_direct=[{"caller_title": "a:caller", "target_title": "printf"}],
        indirect=[{"caller_title": "a:caller", "note": "fp"}],
    )
    prov = apply_clang_calls_from_report(data, report, pkg)
    assert prov["n_facts"] == 1
    assert prov["counts"]["tree_sitter_only_internal"] == 1
    assert prov["counts"]["out_of_compile_db_scope"] == 2
    assert prov["counts"]["external_direct"] == 1
    assert prov["counts"]["indirect"] == 1
    matched = [
        r for r in data["relationships"] if r.get("clang_call_status") == "matched"
    ]
    assert len(matched) == 1
    for r in data["relationships"]:
        if r.get("clang_call_status") != "matched":
            assert not any(
                str(k).startswith("clang_call_") and r.get(k) is not None
                for k in r
            )


def test_report_accounting_inconsistencies_abort(tmp_path: Path):
    pkg, data, row, _bo = _pkg_with_call(tmp_path)
    report = _clean_report([row], total_calls=1)
    report["counts"]["matched_internal"] = 0  # disagree with list len
    before = json.dumps(data, sort_keys=True)
    with pytest.raises(ClangCallOverlayError, match="count/list mismatch"):
        apply_clang_calls_from_report(data, report, pkg)
    assert json.dumps(data, sort_keys=True) == before

    report2 = _clean_report([row], total_calls=1)
    # Keep total_calls == base calls (1) but break the component sum.
    report2["tree_sitter_accounting"]["covered_by_noninternal_clang_observation"] = 1
    with pytest.raises(ClangCallOverlayError, match="do not sum to total_calls"):
        apply_clang_calls_from_report(data, report2, pkg)
    assert json.dumps(data, sort_keys=True) == before

    report3 = _clean_report([row], total_calls=1)
    report3["tree_sitter_accounting"]["total_calls"] = 1
    report3["tree_sitter_accounting"]["matched_internal"] = 1
    # base has 1 call; force disagree by... already 1. Change base:
    data4 = copy.deepcopy(data)
    data4["relationships"].append(
        _base_call(
            source="x",
            target="y",
            source_file=str(pkg / "a.c"),
            span="1:0",
            rid="extra",
        )
    )
    with pytest.raises(ClangCallOverlayError, match="disagrees with base calls"):
        apply_clang_calls_from_report(data4, report3, pkg)

    report4 = _clean_report([row], total_calls=1)
    report4["mode"] = "wrong_mode"
    with pytest.raises(ClangCallOverlayError, match="unexpected call audit mode"):
        apply_clang_calls_from_report(data, report4, pkg)
    assert json.dumps(data, sort_keys=True) == before

    report5 = _clean_report([copy.deepcopy(row)], total_calls=1)
    report5["matched_internal"][0]["clang_observations"][0][
        "compile_commands_digest"
    ] = "forged"
    with pytest.raises(ClangCallOverlayError, match="digest disagrees"):
        apply_clang_calls_from_report(data, report5, pkg)
    assert json.dumps(data, sort_keys=True) == before

    report6 = _clean_report([copy.deepcopy(row)], total_calls=1)
    report6["matched_internal"][0]["clang_compilers"][0][
        "compiler_id"
    ] = "unrecorded compiler"
    with pytest.raises(ClangCallOverlayError, match="absent from report.compilers"):
        apply_clang_calls_from_report(data, report6, pkg)
    assert json.dumps(data, sort_keys=True) == before

    duplicate_entries = copy.deepcopy(row)
    duplicate_entries["clang_entry_indices"] = [0, 0]
    report7 = _clean_report([duplicate_entries], total_calls=1)
    with pytest.raises(ClangCallOverlayError, match="duplicate entry indices"):
        apply_clang_calls_from_report(data, report7, pkg)
    assert json.dumps(data, sort_keys=True) == before

    report8 = _clean_report([copy.deepcopy(row)], total_calls=1)
    report8["translation_units"] = []
    with pytest.raises(ClangCallOverlayError, match="length disagrees"):
        apply_clang_calls_from_report(data, report8, pkg)
    assert json.dumps(data, sort_keys=True) == before

    report9 = _clean_report(
        [], total_calls=1, tree_sitter_only=[{"classification": "unknown"}]
    )
    with pytest.raises(ClangCallOverlayError, match="unexpected classification"):
        apply_clang_calls_from_report(data, report9, pkg)
    assert json.dumps(data, sort_keys=True) == before


def test_bad_timeout_and_missing_compile_db_fail(tmp_path: Path):
    pkg, data, _row, _ = _pkg_with_call(tmp_path)
    with pytest.raises(ClangCallOverlayError, match="timeout must be a positive"):
        append_clang_calls(data, pkg, timeout=0)

    # No compile_commands.json → audit fails honestly
    before = json.dumps(data, sort_keys=True)
    with pytest.raises(ClangCallOverlayError):
        append_clang_calls(data, pkg, timeout=5)
    assert json.dumps(data, sort_keys=True) == before


def test_disabled_provenance_shape():
    assert build_disabled_provenance() == {
        "mode": "off",
        "enabled": False,
        "n_facts": 0,
        "n_translation_units": 0,
    }


# ---------------------------------------------------------------------------
# CLI / parquet
# ---------------------------------------------------------------------------


def test_cli_default_off_manifest(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.c").write_text("int f(void){return 0;}\n", encoding="utf-8")
    graph = tmp_path / "g"
    baseline = build_c_byog(pkg)
    index_c_main(
        package=pkg,
        graph=graph,
        keep_snapshots=2,
        compiler_builtins=False,
        compiler_dependencies=False,
        compiler_includes=False,
        clang_signatures=False,
        clang_calls=False,
        clang_types=False,
        clang_type_uses=False,
        clang_type_shapes=False,
        allow_toolchain_drift=False,
    )
    snapshot = (graph / "current").read_text(encoding="utf-8").strip()
    manifest = json.loads(
        (graph / "snapshots" / snapshot / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["clang_calls"] == {
        "mode": "off",
        "enabled": False,
        "n_facts": 0,
        "n_translation_units": 0,
    }
    assert manifest["counts"]["entities"] == len(baseline["entities"])
    assert manifest["counts"]["relationships"] == len(baseline["relationships"])
    import pandas as pd

    rels = pd.read_parquet(
        graph / "snapshots" / snapshot / "relationships.parquet"
    )
    assert not any(str(column).startswith("clang_call_") for column in rels.columns)


# ---------------------------------------------------------------------------
# Live packages
# ---------------------------------------------------------------------------


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_inih_calls_overlay():
    pkg = ROOT / "examples" / "inih"
    data = build_c_byog(pkg)
    n_ent = len(data["entities"])
    n_rel = len(data["relationships"])
    n_obs = len(data.get("call_observations") or [])
    call_ids = [
        (r["id"], r["source"], r["target"], r["type"], r.get("confidence"), r.get("extractor"))
        for r in data["relationships"]
        if r.get("type") == "calls"
    ]
    before_files = {
        p.relative_to(pkg).as_posix() for p in pkg.rglob("*") if p.is_file()
    }
    prov = append_clang_calls(data, pkg)
    after_files = {
        p.relative_to(pkg).as_posix() for p in pkg.rglob("*") if p.is_file()
    }
    assert before_files == after_files
    assert not any(pkg.rglob("*.o"))
    assert not any(pkg.rglob("*.ast"))
    assert not any(pkg.rglob("*.d"))
    assert not any(pkg.rglob("*.i"))
    assert prov["n_facts"] == 16
    assert prov["counts"]["matched_internal"] == 16
    assert prov["counts"]["tree_sitter_only_internal"] == 1
    assert prov["counts"]["out_of_compile_db_scope"] == 21
    assert prov["counts"]["clang_only_internal"] == 0
    assert prov["counts"]["ambiguous"] == 0
    assert len(data["entities"]) == n_ent
    assert len(data["relationships"]) == n_rel
    assert len(data.get("call_observations") or []) == n_obs
    after_ids = [
        (r["id"], r["source"], r["target"], r["type"], r.get("confidence"), r.get("extractor"))
        for r in data["relationships"]
        if r.get("type") == "calls"
    ]
    assert call_ids == after_ids
    matched = [
        r for r in data["relationships"] if r.get("clang_call_status") == "matched"
    ]
    assert len(matched) == 16
    for r in matched:
        assert r["clang_call_confidence"] == 1.0
        assert r["confidence"] == 0.9
        assert r["extractor"] == "tree-sitter-c"
        assert r["clang_call_match_basis"] == "exact_byte_offset"
    # Residuals receive no invented metadata
    unmatched_calls = [
        r
        for r in data["relationships"]
        if r.get("type") == "calls" and r.get("clang_call_status") != "matched"
    ]
    assert len(unmatched_calls) == 38 - 16
    for r in unmatched_calls:
        assert r.get("clang_call_status") is None
    # Idempotent
    prov2 = append_clang_calls(data, pkg)
    assert prov2["n_facts"] == 16
    assert prov2["n_facts_changed"] == 0


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_cjson_calls_overlay():
    pkg = ROOT / "examples" / "cjson"
    data = build_c_byog(pkg)
    n_ent = len(data["entities"])
    n_rel = len(data["relationships"])
    n_calls = sum(1 for r in data["relationships"] if r.get("type") == "calls")
    ids_before = [
        (r["id"], r["source"], r["target"], r["type"]) for r in data["relationships"]
    ]
    before_files = {
        p.relative_to(pkg).as_posix() for p in pkg.rglob("*") if p.is_file()
    }
    prov = append_clang_calls(data, pkg)
    after_files = {
        p.relative_to(pkg).as_posix() for p in pkg.rglob("*") if p.is_file()
    }
    assert before_files == after_files
    assert not any(pkg.rglob("*.o"))
    assert not any(pkg.rglob("*.ast"))
    assert not any(pkg.rglob("*.d"))
    assert not any(pkg.rglob("*.i"))
    assert prov["n_facts"] == 188
    assert prov["counts"]["matched_internal"] == 188
    assert prov["counts"]["tree_sitter_only_internal"] == 0
    assert prov["counts"]["out_of_compile_db_scope"] == 307
    assert prov["tree_sitter_accounting"]["total_calls"] == 495
    assert len(data["entities"]) == n_ent
    assert len(data["relationships"]) == n_rel
    ids_after = [
        (r["id"], r["source"], r["target"], r["type"]) for r in data["relationships"]
    ]
    assert ids_before == ids_after
    matched = [
        r for r in data["relationships"] if r.get("clang_call_status") == "matched"
    ]
    assert len(matched) == 188
    unmatched = [
        r
        for r in data["relationships"]
        if r.get("type") == "calls" and r.get("clang_call_status") != "matched"
    ]
    assert len(unmatched) == n_calls - 188
    for r in unmatched:
        assert r.get("clang_call_status") is None
    for r in matched:
        assert r["clang_call_fact_kind"] == FACT_KIND
        assert r["extractor"] == "tree-sitter-c"
        assert r["confidence"] == 0.9


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_parquet_roundtrip_call_fields(tmp_path: Path):
    pkg = ROOT / "examples" / "inih"
    graph = tmp_path / "g-calls"
    baseline = build_c_byog(pkg)
    index_c_main(
        package=pkg,
        graph=graph,
        keep_snapshots=2,
        compiler_builtins=False,
        compiler_dependencies=False,
        compiler_includes=False,
        clang_signatures=True,
        clang_calls=True,
        clang_types=False,
        clang_type_uses=False,
        clang_type_shapes=False,
        allow_toolchain_drift=False,
    )
    snapshot = (graph / "current").read_text(encoding="utf-8").strip()
    snap = graph / "snapshots" / snapshot
    import pandas as pd

    rels = pd.read_parquet(snap / "relationships.parquet")
    ents = pd.read_parquet(snap / "entities.parquet")
    assert len(rels) == len(baseline["relationships"])
    assert len(ents) == len(baseline["entities"])
    assert (ents["clang_signature_status"].dropna() == "matched").all()
    calls = rels[rels["type"].astype(str) == "calls"]
    signed = calls[calls["clang_call_status"].astype(str) == "matched"]
    assert len(signed) == 16
    assert (signed["clang_call_fact_kind"] == FACT_KIND).all()
    assert (signed["confidence"] == 0.9).all()
    assert (signed["extractor"] == "tree-sitter-c").all()
    manifest = json.loads((snap / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["clang_calls"]["enabled"] is True
    assert manifest["clang_calls"]["n_facts"] == 16
    assert manifest["clang_calls"]["mode"] == MODE
    assert manifest["clang_signatures"]["enabled"] is True
    assert not any(pkg.rglob("*.o"))
    assert not any(pkg.rglob("*.ast"))


def test_non_clang_compiler_fails(tmp_path: Path):
    pkg = tmp_path / "non-clang"
    pkg.mkdir()
    (pkg / "a.c").write_text("int f(void) { return 0; }\n", encoding="utf-8")
    fake_gcc = tmp_path / "fake-gcc"
    fake_gcc.write_text(
        "#!/bin/sh\necho 'gcc (test) 99.0'\n",
        encoding="utf-8",
    )
    fake_gcc.chmod(0o755)
    (pkg / "compile_commands.json").write_text(
        json.dumps(
            [
                {
                    "directory": str(pkg),
                    "arguments": [
                        str(fake_gcc),
                        "-c",
                        "a.c",
                        "-o",
                        "a.o",
                    ],
                    "file": "a.c",
                }
            ]
        ),
        encoding="utf-8",
    )
    data = build_c_byog(pkg)
    before = json.dumps(data, sort_keys=True)
    with pytest.raises(ClangCallOverlayError, match="not a verified Clang"):
        append_clang_calls(data, pkg)
    assert json.dumps(data, sort_keys=True) == before
    assert not (pkg / "a.o").exists()
