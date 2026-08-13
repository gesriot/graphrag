"""Read-only integrity audit for persisted configured Clang call evidence.

Pure synthetic/corruption tests always run. Live disposable-graph tests skip
when no C compiler is present. Nothing here writes a published byog_* root.

Run:
  uv run python -m pytest examples/cjson/tests/test_c_clang_call_graph_audit.py -q
"""
from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

from c_clang_call_audit import source_byte_offset  # type: ignore
from c_clang_call_graph_audit import (  # type: ignore
    AUDIT_MODE,
    audit_graph_root,
    audit_rows,
    audit_to_json,
    format_report,
    main as audit_main,
    read_only_fingerprint,
)
from c_clang_calls import (  # type: ignore
    EXTRACTOR,
    FACT_KIND,
    MODE,
    apply_clang_calls_from_report,
    build_disabled_provenance,
)
from c_preprocessor import find_c_compiler  # type: ignore

DIGEST = "abc123"
COMPILER_PATH = "/usr/bin/clang"
COMPILER_ID = "Apple clang version test"
COMPILER_VERSION = "17.0.0"

_CALL_FIELDS = (
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
)
_FAIL_CLOSED = (
    "clang_only_internal",
    "ambiguous",
    "macro_location_unsupported",
)


def _cc():
    return find_c_compiler()


def _codes(report) -> set:
    return {a.get("code") for a in report.get("anomalies") or []}


def _base_call(*, source: str, target: str, source_file: str, span: str, rid: str) -> dict:
    return {
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


def _matched_row(*, caller: str, target: str, source_path: str, span: str, byte_offset: int, **extra) -> dict:
    line = int(span.split(":")[0])
    col0 = int(span.split(":")[1])
    row = {
        "caller_title": caller,
        "target_title": target,
        "source_path": source_path,
        "line": line,
        "col0": col0,
        "byte_offset": byte_offset,
        "tree_sitter_span": span,
        "clang_line": line,
        "clang_col1": col0 + 1,
        "clang_byte_offset": byte_offset,
        "match_basis": "exact_byte_offset",
        "clang_entry_indices": [0],
        "clang_resolve_reason": "function_decl",
        "ref_kind": "FunctionDecl",
        "ref_type": "int (void)",
        "compiler_path": COMPILER_PATH,
        "compiler_id": COMPILER_ID,
        "compile_commands_digest": DIGEST,
        "clang_observations": [
            {
                "classification": "internal_direct",
                "entry_index": 0,
                "compiler_path": COMPILER_PATH,
                "compiler_id": COMPILER_ID,
                "compile_commands_digest": DIGEST,
                "resolve_reason": "function_decl",
                "ref_kind": "FunctionDecl",
                "ref_type": "int (void)",
                "target_title": target,
            }
        ],
        "clang_compilers": [
            {
                "compiler_path": COMPILER_PATH,
                "compiler_id": COMPILER_ID,
                "compile_commands_digest": DIGEST,
            }
        ],
    }
    row.update(extra)
    return row


def _clean_report(matched: list, *, total_calls: int, **count_extra) -> dict:
    counts = {
        "matched_internal": len(matched),
        "clang_only_internal": 0,
        "tree_sitter_only_internal": 0,
        "external_direct": 0,
        "indirect": 0,
        "ambiguous": 0,
        "macro_location_unsupported": 0,
        "out_of_compile_db_scope": 0,
    }
    counts.update(count_extra)
    return {
        "mode": "clang_ast_json_call_audit",
        "package": "pkg",
        "compiler_path": COMPILER_PATH,
        "compiler_id": COMPILER_ID,
        "compiler_version": COMPILER_VERSION,
        "compilers": [
            {
                "compiler_path": COMPILER_PATH,
                "compiler_id": COMPILER_ID,
                "compiler_version": COMPILER_VERSION,
            }
        ],
        "compile_commands_digest": DIGEST,
        "n_compile_entries": 1,
        "translation_units": [
            {
                "entry_index": 0,
                "file": "a.c",
                "package_local": True,
                "compiler_path": COMPILER_PATH,
                "compiler_id": COMPILER_ID,
            }
        ],
        "counts": counts,
        "tree_sitter_accounting": {
            "total_calls": total_calls,
            "matched_internal": counts["matched_internal"],
            "covered_by_noninternal_clang_observation": 0,
            "tree_sitter_only_internal": counts["tree_sitter_only_internal"],
            "out_of_compile_db_scope": counts["out_of_compile_db_scope"],
        },
        "matched_internal": matched,
        "clang_only_internal": [],
        "tree_sitter_only_internal": [],
        "external_direct": [],
        "indirect": [],
        "ambiguous": [],
        "macro_location_unsupported": [],
        "out_of_compile_db_scope": [],
    }


def _enabled_graph(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir(parents=True, exist_ok=True)
    src_text = (
        "static int helper(void) { return 1; }\n"
        "int caller(void) { return helper(); }\n"
    )
    src = pkg / "a.c"
    src.write_text(src_text, encoding="utf-8")
    col0 = src_text.splitlines()[1].index("helper")
    span = f"2:{col0}"
    bo = int(source_byte_offset(src, 2, col0))
    rels = [
        _base_call(
            source="a:caller",
            target="a:helper",
            source_file=str(src),
            span=span,
            rid="rel:calls:1",
        ),
        {
            "id": "rel:contains:1",
            "source": "a:a.c",
            "target": "a:caller",
            "type": "contains",
            "description": "file contains caller",
            "source_file": str(src),
            "span": "2:0",
            "extractor": "tree-sitter-c",
            "confidence": 1.0,
            "is_deterministic": True,
        },
    ]
    data = {"entities": [], "relationships": rels}
    row = _matched_row(
        caller="a:caller",
        target="a:helper",
        source_path="a.c",
        span=span,
        byte_offset=bo,
    )
    block = apply_clang_calls_from_report(
        data, _clean_report([row], total_calls=1), pkg
    )
    manifest = {"clang_calls": block}
    return data["relationships"], manifest


def _off_graph():
    rels = [
        _base_call(
            source="a:caller",
            target="a:helper",
            source_file="a.c",
            span="2:20",
            rid="rel:calls:1",
        )
    ]
    return rels, {"clang_calls": build_disabled_provenance()}


def _legacy_graph():
    rels = [
        _base_call(
            source="a:caller",
            target="a:helper",
            source_file="a.c",
            span="2:20",
            rid="rel:calls:1",
        )
    ]
    return rels, {}


def _decorated(rels: list) -> dict:
    return next(r for r in rels if r.get("clang_call_status") == "matched")


def test_legacy_absent_passes():
    rels, manifest = _legacy_graph()
    report = audit_rows(rels, manifest)
    assert report["ok"] is True
    assert report["status"] == "legacy_absent"
    assert report["classification"] == "legacy_absent"
    assert report["n_decorated_relationships"] == 0
    assert audit_rows(rels, None)["status"] == "legacy_absent"


def test_exact_off_passes():
    rels, manifest = _off_graph()
    report = audit_rows(rels, manifest)
    assert report["ok"] is True
    assert report["status"] == "off"
    assert manifest["clang_calls"] == {
        "mode": "off",
        "enabled": False,
        "n_facts": 0,
        "n_translation_units": 0,
    }


def test_enabled_overlay_passes(tmp_path: Path):
    rels, manifest = _enabled_graph(tmp_path)
    report = audit_rows(rels, manifest)
    assert report["ok"] is True, report["anomalies"]
    assert report["status"] == "enabled"
    assert report["n_decorated_relationships"] == 1
    assert report["n_calls"] == 1
    assert report["counts"]["matched_internal"] == 1
    assert report["fact_kind"] == FACT_KIND
    assert report["extractor"] == EXTRACTOR
    assert report["overlay_mode"] == MODE
    assert report["audit_mode"] == AUDIT_MODE
    assert all(field in _decorated(rels) for field in _CALL_FIELDS)


def test_malformed_and_partial_and_extra_manifest(tmp_path: Path):
    rels, manifest = _enabled_graph(tmp_path)
    for value in (None, [], "off", 0):
        report = audit_rows(rels, {"clang_calls": value})
        assert report["ok"] is False, value
        assert "invalid_enabled_block" in _codes(report)

    missing = copy.deepcopy(manifest)
    missing["clang_calls"].pop("counts")
    report_missing = audit_rows(rels, missing)
    assert report_missing["ok"] is False
    assert "missing_manifest_key" in _codes(report_missing)

    extra = copy.deepcopy(manifest)
    extra["clang_calls"]["unexpected"] = True
    report_extra = audit_rows(rels, extra)
    assert report_extra["ok"] is False
    assert "extra_manifest_key" in _codes(report_extra)


def test_extra_count_and_accounting_keys_fail(tmp_path: Path):
    rels, manifest = _enabled_graph(tmp_path)
    counts = copy.deepcopy(manifest)
    counts["clang_calls"]["counts"]["bonus"] = 1
    report_counts = audit_rows(rels, counts)
    assert report_counts["ok"] is False
    assert "extra_count_key" in _codes(report_counts)

    acc = copy.deepcopy(manifest)
    acc["clang_calls"]["tree_sitter_accounting"]["bonus"] = 1
    report_acc = audit_rows(rels, acc)
    assert report_acc["ok"] is False
    assert "extra_accounting_key" in _codes(report_acc)


def test_evidence_without_manifest_fails(tmp_path: Path):
    rels, _manifest = _enabled_graph(tmp_path)
    report = audit_rows(rels, {})
    assert report["ok"] is False
    assert "legacy_block_missing_with_fields" in _codes(report)


def test_manifest_without_evidence_fails(tmp_path: Path):
    _rels, manifest = _enabled_graph(tmp_path)
    clean, _ = _legacy_graph()
    report = audit_rows(clean, manifest)
    assert report["ok"] is False
    assert "manifest_count_mismatch" in _codes(report)


def test_off_with_evidence_fails(tmp_path: Path):
    rels, _manifest = _enabled_graph(tmp_path)
    report = audit_rows(rels, {"clang_calls": build_disabled_provenance()})
    assert report["ok"] is False
    assert "off_with_decorated_relationships" in _codes(report)


def test_every_missing_relationship_field_fails(tmp_path: Path):
    rels, manifest = _enabled_graph(tmp_path)
    for field in _CALL_FIELDS:
        rows = copy.deepcopy(rels)
        _decorated(rows).pop(field)
        report = audit_rows(rows, manifest)
        assert report["ok"] is False, field
        assert "partial_call_payload" in _codes(report), field


def test_nullable_keys_present_as_null_pass(tmp_path: Path):
    rels, manifest = _enabled_graph(tmp_path)
    rows = copy.deepcopy(rels)
    target = _decorated(rows)
    target["clang_call_resolve_reason"] = None
    target["clang_call_ref_type"] = None
    obs = json.loads(target["clang_call_observations_json"])
    obs[0]["ref_type"] = None
    target["clang_call_observations_json"] = json.dumps(
        obs, sort_keys=True, separators=(",", ":")
    )
    report = audit_rows(rows, manifest)
    assert report["ok"] is True, report["anomalies"]


def test_dropped_nullable_parquet_column_fails_without_repair(tmp_path: Path):
    rels, manifest = _enabled_graph(tmp_path)
    graph = _publish(tmp_path, rels, manifest["clang_calls"])
    snap = graph / "snapshots" / "s1"
    df = pd.read_parquet(snap / "relationships.parquet")
    assert "clang_call_resolve_reason" in df.columns
    df = df.drop(columns=["clang_call_resolve_reason"])
    df.to_parquet(snap / "relationships.parquet")
    report = audit_graph_root(graph)
    assert report["ok"] is False
    assert "partial_call_payload" in _codes(report)


def test_unknown_call_field_fails(tmp_path: Path):
    rels, manifest = _enabled_graph(tmp_path)
    rows = copy.deepcopy(rels)
    _decorated(rows)["clang_call_abi_proof"] = True
    report = audit_rows(rows, manifest)
    assert report["ok"] is False
    assert "unknown_call_field" in _codes(report)


def test_non_calls_carrier_fails(tmp_path: Path):
    rels, manifest = _enabled_graph(tmp_path)
    rows = copy.deepcopy(rels)
    payload = {
        key: value
        for key, value in _decorated(rows).items()
        if key in _CALL_FIELDS
    }
    contains = next(r for r in rows if r["type"] == "contains")
    contains.update(payload)
    report = audit_rows(rows, manifest)
    assert report["ok"] is False
    assert "stale_call_metadata" in _codes(report)


def test_empty_and_duplicate_relationship_ids_fail(tmp_path: Path):
    rels, manifest = _enabled_graph(tmp_path)
    dup = copy.deepcopy(rels)
    dup[1]["id"] = dup[0]["id"]
    report_dup = audit_rows(dup, manifest)
    assert report_dup["ok"] is False
    assert "duplicate_relationship_id" in _codes(report_dup)

    empty = copy.deepcopy(rels)
    empty[0]["id"] = ""
    report_empty = audit_rows(empty, manifest)
    assert report_empty["ok"] is False
    assert "empty_relationship_id" in _codes(report_empty)


def test_invalid_match_basis_and_byte_offset_fail(tmp_path: Path):
    rels, manifest = _enabled_graph(tmp_path)
    rows = copy.deepcopy(rels)
    _decorated(rows)["clang_call_match_basis"] = "fuzzy"
    report = audit_rows(rows, manifest)
    assert report["ok"] is False
    assert "match_basis" in _codes(report)

    off = copy.deepcopy(rels)
    _decorated(off)["clang_call_byte_offset"] = -1
    report_off = audit_rows(off, manifest)
    assert report_off["ok"] is False
    assert "byte_offset" in _codes(report_off)


def test_entry_index_variants_fail(tmp_path: Path):
    rels, manifest = _enabled_graph(tmp_path)
    for value in ([], [1], [0, 0], [1, 0], [-1], ["0"], [0.0], [0.5]):
        rows = copy.deepcopy(rels)
        _decorated(rows)["clang_call_entry_indices"] = value
        report = audit_rows(rows, manifest)
        assert report["ok"] is False, value
        assert "entry_index_census" in _codes(report), (value, _codes(report))


def test_malformed_and_noncanonical_json_fail(tmp_path: Path):
    rels, manifest = _enabled_graph(tmp_path)
    for field, code in (
        ("clang_call_compilers_json", "compilers_json"),
        ("clang_call_observations_json", "observations_json"),
    ):
        for value in (
            "not json",
            "[]",
            json.dumps(json.loads(_decorated(rels)[field]), indent=2),
        ):
            rows = copy.deepcopy(rels)
            _decorated(rows)[field] = value
            report = audit_rows(rows, manifest)
            assert report["ok"] is False, (field, value)
            assert code in _codes(report), (field, value, _codes(report))


def test_duplicate_json_keys_and_nan_infinity_fail(tmp_path: Path):
    rels, manifest = _enabled_graph(tmp_path)
    dup = (
        '[{"compile_commands_digest":"abc123","compiler_id":"Apple clang '
        'version test","compiler_path":"/usr/bin/clang","compiler_path":'
        '"/usr/bin/other"}]'
    )
    rows = copy.deepcopy(rels)
    _decorated(rows)["clang_call_compilers_json"] = dup
    report = audit_rows(rows, manifest)
    assert report["ok"] is False
    assert "compilers_json" in _codes(report)

    for token in ("NaN", "Infinity"):
        raw = (
            '[{"classification":"internal_direct","compile_commands_digest":'
            '"abc123","compiler_id":"Apple clang version test",'
            '"compiler_path":"/usr/bin/clang","entry_index":0,'
            f'"ref_kind":"FunctionDecl","ref_type":{token},'
            '"resolve_reason":"function_decl","target_title":"a:helper"}]'
        )
        nan_rows = copy.deepcopy(rels)
        _decorated(nan_rows)["clang_call_observations_json"] = raw
        report_nan = audit_rows(nan_rows, manifest)
        assert report_nan["ok"] is False, token
        assert "observations_json" in _codes(report_nan)


def test_compiler_identity_failures(tmp_path: Path):
    rels, manifest = _enabled_graph(tmp_path)

    unknown = copy.deepcopy(rels)
    target = _decorated(unknown)
    target["clang_call_compiler_id"] = "other"
    comps = json.loads(target["clang_call_compilers_json"])
    comps[0]["compiler_id"] = "other"
    target["clang_call_compilers_json"] = json.dumps(
        comps, sort_keys=True, separators=(",", ":")
    )
    obs = json.loads(target["clang_call_observations_json"])
    obs[0]["compiler_id"] = "other"
    target["clang_call_observations_json"] = json.dumps(
        obs, sort_keys=True, separators=(",", ":")
    )
    report_unknown = audit_rows(unknown, manifest)
    assert report_unknown["ok"] is False
    assert "compiler_mismatch" in _codes(report_unknown)

    relative = copy.deepcopy(rels)
    _decorated(relative)["clang_call_compiler_path"] = "clang"
    report_rel = audit_rows(relative, manifest)
    assert report_rel["ok"] is False
    assert "compiler_mismatch" in _codes(report_rel)

    broken = copy.deepcopy(manifest)
    only = copy.deepcopy(broken["clang_calls"]["compilers"][0])
    broken["clang_calls"]["compilers"] = [only, copy.deepcopy(only)]
    report_dup = audit_rows(rels, broken)
    assert report_dup["ok"] is False
    assert "manifest_identity_mismatch" in _codes(report_dup)

    singular = copy.deepcopy(manifest)
    second = {
        "compiler_path": "/usr/bin/other",
        "compiler_id": "other clang",
        "compiler_version": "16.0.0",
    }
    first = singular["clang_calls"]["compilers"][0]
    singular["clang_calls"]["compilers"] = sorted(
        [first, second],
        key=lambda item: (item["compiler_path"], item["compiler_id"]),
    )
    report_singular = audit_rows(rels, singular)
    assert report_singular["ok"] is False
    assert "manifest_identity_mismatch" in _codes(report_singular)

    for mutation in ("missing", "extra", "invalid_version"):
        compiler_contract = copy.deepcopy(manifest)
        compiler = compiler_contract["clang_calls"]["compilers"][0]
        if mutation == "missing":
            compiler.pop("compiler_version")
        elif mutation == "extra":
            compiler["unexpected"] = True
        else:
            compiler["compiler_version"] = 17
            compiler_contract["clang_calls"]["compiler_version"] = 17
        contract_report = audit_rows(rels, compiler_contract)
        assert contract_report["ok"] is False, mutation
        assert "manifest_identity_mismatch" in _codes(contract_report), mutation


def test_observation_mismatches_fail(tmp_path: Path):
    rels, manifest = _enabled_graph(tmp_path)

    target_rows = copy.deepcopy(rels)
    target = _decorated(target_rows)
    obs = json.loads(target["clang_call_observations_json"])
    obs[0]["target_title"] = "someone-else"
    target["clang_call_observations_json"] = json.dumps(
        obs, sort_keys=True, separators=(",", ":")
    )
    report_target = audit_rows(target_rows, manifest)
    assert report_target["ok"] is False
    assert "observation_record" in _codes(report_target)

    reftype = copy.deepcopy(rels)
    target = _decorated(reftype)
    obs = json.loads(target["clang_call_observations_json"])
    obs[0]["ref_type"] = "void (void)"
    target["clang_call_observations_json"] = json.dumps(
        obs, sort_keys=True, separators=(",", ":")
    )
    report_ref = audit_rows(reftype, manifest)
    assert report_ref["ok"] is False
    assert "observation_record" in _codes(report_ref)

    digest_rows = copy.deepcopy(rels)
    target = _decorated(digest_rows)
    obs = json.loads(target["clang_call_observations_json"])
    obs[0]["compile_commands_digest"] = "zzz"
    target["clang_call_observations_json"] = json.dumps(
        obs, sort_keys=True, separators=(",", ":")
    )
    report_digest = audit_rows(digest_rows, manifest)
    assert report_digest["ok"] is False
    assert "digest_mismatch" in _codes(report_digest)

    float_index = copy.deepcopy(rels)
    target = _decorated(float_index)
    obs = json.loads(target["clang_call_observations_json"])
    obs[0]["entry_index"] = 0.0
    target["clang_call_observations_json"] = json.dumps(
        obs, sort_keys=True, separators=(",", ":")
    )
    report_float_index = audit_rows(float_index, manifest)
    assert report_float_index["ok"] is False
    assert "observation_record" in _codes(report_float_index)


def test_observation_entry_index_coverage_mismatch_fails(tmp_path: Path):
    rels, manifest = _enabled_graph(tmp_path)
    rows = copy.deepcopy(rels)
    target = _decorated(rows)
    target["clang_call_entry_indices"] = [0]
    obs = json.loads(target["clang_call_observations_json"])
    obs.append(copy.deepcopy(obs[0]))
    obs[1]["entry_index"] = 0
    target["clang_call_observations_json"] = json.dumps(
        sorted(obs, key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":"))),
        sort_keys=True,
        separators=(",", ":"),
    )
    report = audit_rows(rows, manifest)
    assert report["ok"] is False
    assert "observation_coverage" in _codes(report)


def test_exact_description_mismatch_fails(tmp_path: Path):
    rels, manifest = _enabled_graph(tmp_path)
    rows = copy.deepcopy(rels)
    _decorated(rows)["clang_call_description"] = "configured Clang call"
    report = audit_rows(rows, manifest)
    assert report["ok"] is False
    assert "description_mismatch" in _codes(report)


def test_count_and_accounting_mismatch_fail(tmp_path: Path):
    rels, manifest = _enabled_graph(tmp_path)
    broken = copy.deepcopy(manifest)
    broken["clang_calls"]["n_facts"] = 9
    report = audit_rows(rels, broken)
    assert report["ok"] is False
    assert "manifest_count_mismatch" in _codes(report)

    acc = copy.deepcopy(manifest)
    acc["clang_calls"]["tree_sitter_accounting"]["total_calls"] = 99
    report_acc = audit_rows(rels, acc)
    assert report_acc["ok"] is False
    assert "accounting_mismatch" in _codes(report_acc)


def test_every_fail_closed_residual_fails(tmp_path: Path):
    rels, manifest = _enabled_graph(tmp_path)
    for bucket in _FAIL_CLOSED:
        broken = copy.deepcopy(manifest)
        broken["clang_calls"]["counts"][bucket] = 1
        report = audit_rows(rels, broken)
        assert report["ok"] is False, bucket
        assert "residual_bucket_nonzero" in _codes(report), bucket

    covered = copy.deepcopy(manifest)
    covered["clang_calls"]["tree_sitter_accounting"][
        "covered_by_noninternal_clang_observation"
    ] = 1
    covered["clang_calls"]["tree_sitter_accounting"]["matched_internal"] = 0
    report_covered = audit_rows(rels, covered)
    assert report_covered["ok"] is False
    assert "residual_bucket_nonzero" in _codes(report_covered)


def _publish(tmp_path: Path, relationships: list, block, *, name: str = "g") -> Path:
    graph = tmp_path / name
    snap = graph / "snapshots" / "s1"
    snap.mkdir(parents=True)
    pd.DataFrame([{"id": "e1", "title": "a", "type": "function"}]).to_parquet(
        snap / "entities.parquet"
    )
    pd.DataFrame(relationships).to_parquet(snap / "relationships.parquet")
    pd.DataFrame([{"id": "t1", "title": "a"}]).to_parquet(snap / "text_units.parquet")
    manifest = {}
    if block is not None:
        manifest["clang_calls"] = block
    (snap / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    (graph / "current").write_text("s1", encoding="utf-8")
    return graph


def test_output_is_deterministic(tmp_path: Path):
    rels, manifest = _enabled_graph(tmp_path)
    first = audit_to_json(audit_rows(rels, manifest))
    second = audit_to_json(audit_rows(copy.deepcopy(rels), copy.deepcopy(manifest)))
    assert first == second
    assert json.loads(first)["ok"] is True


def test_graph_root_is_read_only(tmp_path: Path):
    rels, manifest = _enabled_graph(tmp_path)
    graph = _publish(tmp_path, rels, manifest["clang_calls"])
    before = {
        path.relative_to(graph).as_posix(): path.read_bytes()
        for path in sorted(graph.rglob("*"))
        if path.is_file()
    }
    report = audit_graph_root(graph)
    after = {
        path.relative_to(graph).as_posix(): path.read_bytes()
        for path in sorted(graph.rglob("*"))
        if path.is_file()
    }
    assert report["ok"] is True, report["anomalies"]
    assert after == before
    assert report["read_only_verification"]["verified"] is True
    assert set(report["read_only_verification"]["inputs"]) == {
        "manifest.json",
        "entities.parquet",
        "relationships.parquet",
        "text_units.parquet",
        "current",
        "snapshots_dir",
    }
    assert (
        read_only_fingerprint(graph, graph / "snapshots" / "s1")
        == report["read_only_verification"]["fingerprint"]
    )


def test_cli_refuses_output_inside_graph_and_symlink(tmp_path: Path, capsys):
    rels, manifest = _enabled_graph(tmp_path)
    graph = _publish(tmp_path, rels, manifest["clang_calls"])
    forbidden = graph / "snapshots" / "s1" / "call-audit.json"
    assert audit_main(["--graph", str(graph), "--output", str(forbidden)]) == 2
    assert "outside the audited graph root" in capsys.readouterr().err
    assert not forbidden.exists()

    alias = tmp_path / "alias"
    alias.symlink_to(graph)
    via_link = alias / "via-symlink.json"
    assert audit_main(["--graph", str(graph), "--output", str(via_link)]) == 2
    assert "outside the audited graph root" in capsys.readouterr().err
    assert not via_link.exists()
    assert not (graph / "via-symlink.json").exists()


def test_cli_exit_codes(tmp_path: Path, capsys):
    rels, manifest = _enabled_graph(tmp_path)
    good = _publish(tmp_path, rels, manifest["clang_calls"], name="good")
    out_path = tmp_path / "report.json"
    assert audit_main(["--graph", str(good), "--output", str(out_path)]) == 0
    written = json.loads(out_path.read_text(encoding="utf-8"))
    assert written["ok"] is True
    capsys.readouterr()

    bad_rels = copy.deepcopy(rels)
    _decorated(bad_rels)["clang_call_confidence"] = 0.5
    bad = _publish(tmp_path, bad_rels, manifest["clang_calls"], name="bad")
    assert audit_main(["--graph", str(bad)]) == 1
    capsys.readouterr()
    assert audit_main(["--graph", str(tmp_path / "missing")]) == 2


def test_format_report_is_human_readable(tmp_path: Path):
    rels, manifest = _enabled_graph(tmp_path)
    text = format_report(audit_rows(rels, manifest))
    assert "RESULT: PASS" in text
    assert "read-only" in text


def test_published_graph_health_states(tmp_path: Path):
    from published_graph_health import (  # type: ignore
        _call_integrity,
        _signature_integrity,
        _type_integrity,
        _type_shape_integrity,
        _type_use_integrity,
    )

    rels, manifest = _enabled_graph(tmp_path)
    enabled = _call_integrity({"relationships": rels}, manifest, indexer="c")
    assert enabled is not None
    assert enabled["ok"] is True
    assert enabled["status"] == "enabled"
    assert enabled["n_decorated_relationships"] == 1

    legacy_rels, legacy_manifest = _legacy_graph()
    legacy = _call_integrity(
        {"relationships": legacy_rels}, legacy_manifest, indexer="c"
    )
    assert legacy["ok"] is True and legacy["status"] == "legacy_absent"

    off_rels, off_manifest = _off_graph()
    off = _call_integrity({"relationships": off_rels}, off_manifest, indexer="c")
    assert off["ok"] is True and off["status"] == "off"

    orphan = _call_integrity({"relationships": rels}, {}, indexer="c")
    assert orphan["ok"] is False
    assert _call_integrity({"relationships": rels}, manifest, indexer="python") is None

    empty = {"relationships": rels, "entities": []}
    assert _type_use_integrity(empty, {}, indexer="c")["ok"] is True
    assert _type_shape_integrity({"entities": []}, {}, indexer="c")["ok"] is True
    assert _type_integrity({"entities": []}, {}, indexer="c")["ok"] is True
    assert _signature_integrity({"entities": []}, {}, indexer="c")["ok"] is True


def _fail_if_clang_used(monkeypatch):
    import c_clang_ast_capture as cap_mod  # type: ignore
    import c_compiler_common as common_mod  # type: ignore

    def boom(*_args, **_kwargs):
        raise AssertionError("read-only audit must not invoke Clang or capture")

    monkeypatch.setattr(cap_mod, "capture_clang_ast_package", boom)
    monkeypatch.setattr(cap_mod, "run_ast_dump_for_entry", boom)
    monkeypatch.setattr(cap_mod, "load_compile_entries", boom)
    monkeypatch.setattr(common_mod, "load_compile_entries", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "check_output", boom)


def _index(package: Path, graph: Path, *, calls: bool) -> None:
    from index_c import main as index_c_main  # type: ignore

    index_c_main(
        package=package,
        graph=graph,
        keep_snapshots=2,
        compiler_builtins=False,
        compiler_dependencies=False,
        compiler_includes=False,
        clang_signatures=False,
        clang_calls=calls,
        clang_types=False,
        clang_type_uses=False,
        clang_type_shapes=False,
        allow_toolchain_drift=False,
    )


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_cjson_call_graph_audit(tmp_path: Path, monkeypatch):
    pkg = ROOT / "examples" / "cjson"
    package_before = {p.name for p in pkg.iterdir()}
    graph = tmp_path / "byog_cjson_calls"
    _index(pkg, graph, calls=True)
    snap = (graph / "current").read_text(encoding="utf-8").strip()
    block = json.loads(
        (graph / "snapshots" / snap / "manifest.json").read_text(encoding="utf-8")
    )["clang_calls"]
    _fail_if_clang_used(monkeypatch)
    report = audit_graph_root(graph)
    assert report["ok"] is True, report["anomalies"]
    assert report["status"] == "enabled"
    assert (
        report["n_decorated_relationships"]
        == block["n_facts"]
        == block["counts"]["matched_internal"]
    )
    assert report["n_decorated_relationships"] > 0
    assert report["n_calls"] == block["tree_sitter_accounting"]["total_calls"]
    for bucket in _FAIL_CLOSED:
        assert report["counts"][bucket] == 0
    assert report["read_only_verification"]["verified"] is True
    assert {p.name for p in pkg.iterdir()} == package_before
    for pattern in ("*.o", "*.ast", "*.d", "*.i", "*.ii", "*.pch"):
        assert not list(pkg.rglob(pattern))


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_inih_call_graph_audit(tmp_path: Path, monkeypatch):
    pkg = ROOT / "examples" / "inih"
    graph = tmp_path / "byog_inih_calls"
    _index(pkg, graph, calls=True)
    snap = (graph / "current").read_text(encoding="utf-8").strip()
    block = json.loads(
        (graph / "snapshots" / snap / "manifest.json").read_text(encoding="utf-8")
    )["clang_calls"]
    _fail_if_clang_used(monkeypatch)
    report = audit_graph_root(graph)
    assert report["ok"] is True, report["anomalies"]
    assert report["status"] == "enabled"
    assert (
        report["n_decorated_relationships"]
        == block["n_facts"]
        == block["counts"]["matched_internal"]
    )
    assert report["n_decorated_relationships"] > 0
    assert report["n_calls"] == block["tree_sitter_accounting"]["total_calls"]
    assert report["read_only_verification"]["verified"] is True
    for pattern in ("*.o", "*.ast", "*.d", "*.i", "*.ii", "*.pch"):
        assert not list(pkg.rglob(pattern))


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_default_off_and_health(tmp_path: Path, monkeypatch):
    from published_graph_health import PublishedGraphSpec, check_spec  # type: ignore

    pkg = ROOT / "examples" / "inih"
    off_graph = tmp_path / "byog_inih_off"
    _index(pkg, off_graph, calls=False)
    _fail_if_clang_used(monkeypatch)
    off_report = audit_graph_root(off_graph)
    assert off_report["ok"] is True
    assert off_report["status"] == "off"

    enabled_graph = tmp_path / "byog_inih_calls"
    # Recreate without the monkeypatch? check_spec uses extractor comparison
    # and the overlay validator, not Clang. Index first, then check.
    monkeypatch.undo()
    _index(pkg, enabled_graph, calls=True)
    spec = PublishedGraphSpec(
        ident="inih_tmp",
        source=Path("examples/inih"),
        graph=str(enabled_graph),
        indexer="c",
        mode="mutable",
    )
    result = check_spec(spec, root=ROOT, graph_root=enabled_graph)
    assert result["status"] == "pass", result
    call = result["clang_call_integrity"]
    assert call["ok"] is True
    assert call["status"] == "enabled"
    assert result["clang_signature_integrity"]["ok"] is True
    assert result["clang_type_integrity"]["ok"] is True
    assert result["clang_type_use_integrity"]["ok"] is True
    assert result["clang_type_shape_integrity"]["ok"] is True
