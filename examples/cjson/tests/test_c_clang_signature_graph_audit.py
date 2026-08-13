"""Read-only integrity audit for persisted configured Clang function signatures.

Pure synthetic/corruption tests always run. Live disposable-graph tests skip
when no C compiler is present. Nothing here writes a published byog_* root.

Run:
  uv run python -m pytest examples/cjson/tests/test_c_clang_signature_graph_audit.py -q
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

from c_clang_signature_graph_audit import (  # type: ignore
    AUDIT_MODE,
    ClangSignatureGraphAuditError,
    audit_graph_root,
    audit_rows,
    audit_to_json,
    format_report,
    main as audit_main,
    read_only_fingerprint,
    resolve_snapshot,
)
from c_clang_signatures import (  # type: ignore
    EXTRACTOR,
    FACT_KIND,
    MODE,
    apply_clang_signatures_from_report,
    build_disabled_provenance,
)
from c_preprocessor import find_c_compiler  # type: ignore

DIGEST = "abc123"
COMPILER_PATH = "/usr/bin/clang"
COMPILER_ID = "Apple clang version test"
COMPILER_VERSION = "17.0.0"

_SIGNATURE_FIELDS = (
    "clang_signature_status",
    "clang_qual_type",
    "clang_storage_class",
    "clang_inline",
    "clang_variadic",
    "clang_mangled_name",
    "clang_location_origin",
    "clang_signature_fact_kind",
    "clang_signature_extractor",
    "clang_signature_confidence",
    "clang_signature_is_deterministic",
    "clang_signature_compiler_path",
    "clang_signature_compiler_id",
    "clang_signature_compile_commands_digest",
    "clang_signature_entry_indices",
    "clang_signature_observations_json",
    "clang_signature_description",
)

_FAIL_CLOSED = ("clang_only", "ambiguous", "macro_location_unsupported")
_OBSERVATION = ("tree_sitter_only", "out_of_compile_db_scope")


def _cc():
    return find_c_compiler()


def _codes(report) -> set:
    return {a.get("code") for a in report.get("anomalies") or []}


def _entity(*, title: str, etype: str, source_file: str, **extra) -> dict:
    e = {
        "id": f"ent:{etype}:{title}",
        "title": title,
        "type": etype,
        "description": f"{etype} {title}",
        "source_file": source_file,
        "span": "1:0-2:1",
        "extractor": "tree-sitter-c",
        "confidence": 1.0,
        "is_deterministic": True,
    }
    e.update(extra)
    return e


def _matched_row(
    *,
    title: str,
    source_path: str,
    name: str | None = None,
    qual: str = "int (void)",
    entry_indices: list | None = None,
    observations: list | None = None,
    **extra,
) -> dict:
    name = name or title.rsplit(":", 1)[-1]
    indices = entry_indices or [0]
    row = {
        "name": name,
        "source_path": source_path,
        "tree_sitter_title": title,
        "tree_sitter_line": 1,
        "tree_sitter_col": 0,
        "clang_line": 1,
        "clang_col": 0,
        "line_column_confirmed": True,
        "qualType": qual,
        "storageClass": "static",
        "inline": False,
        "variadic": False,
        "mangledName": f"_{name}",
        "location_origin": "direct",
        "entry_indices": indices,
        "compiler_path": COMPILER_PATH,
        "compiler_id": COMPILER_ID,
        "compile_commands_digest": DIGEST,
        "observations": observations
        or [
            {
                "entry_indices": list(indices),
                "compiler_path": COMPILER_PATH,
                "compiler_id": COMPILER_ID,
                "compile_commands_digest": DIGEST,
                "qualType": qual,
                "storageClass": "static",
            }
        ],
    }
    row.update(extra)
    return row


def _clean_report(matched: list, *, n_compile_entries: int = 1, **counts_extra) -> dict:
    counts = {
        "matched": len(matched),
        "tree_sitter_only": 0,
        "clang_only": 0,
        "ambiguous": 0,
        "macro_location_unsupported": 0,
        "out_of_compile_db_scope": 0,
        "clang_definitions_package_local": len(matched),
        "tree_sitter_definitions_total": len(matched),
        "tree_sitter_definitions_in_scope": len(matched),
    }
    counts.update(counts_extra)
    return {
        "mode": "clang_ast_json_audit",
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
        "n_compile_entries": n_compile_entries,
        "translation_units": [
            {
                "entry_index": i,
                "file": "a.c",
                "package_local": True,
                "compiler_path": COMPILER_PATH,
                "compiler_id": COMPILER_ID,
            }
            for i in range(n_compile_entries)
        ],
        "in_scope_source_paths": sorted({m["source_path"] for m in matched}),
        "counts": counts,
        "matched": matched,
        "tree_sitter_only": [],
        "clang_only": [],
        "ambiguous": [],
        "macro_location_unsupported": [],
        "out_of_compile_db_scope": [],
        "limitations": [],
        "confidence_boundary": "test",
    }


def _enabled_graph(tmp_path: Path, *, residual: bool = False):
    pkg = tmp_path / "pkg"
    pkg.mkdir(parents=True, exist_ok=True)
    src = pkg / "a.c"
    src.write_text("static int helper(void) { return 1; }\n", encoding="utf-8")
    entities = [
        _entity(title="a:helper", etype="function", source_file=str(src)),
        _entity(title="a:a.c", etype="file", source_file=str(src)),
    ]
    data = {"entities": entities, "relationships": []}
    extra = {}
    if residual:
        extra = {"tree_sitter_only": 2, "out_of_compile_db_scope": 3}
    report = _clean_report(
        [_matched_row(title="a:helper", source_path="a.c", name="helper")],
        **extra,
    )
    if residual:
        report["tree_sitter_only"] = [{"name": "g1"}, {"name": "g2"}]
        report["out_of_compile_db_scope"] = [
            {"name": "h1"},
            {"name": "h2"},
            {"name": "h3"},
        ]
    block = apply_clang_signatures_from_report(data, report, pkg)
    manifest = {
        "counts": {"entities": len(entities)},
        "clang_signatures": block,
    }
    return data["entities"], manifest


def _off_graph():
    entities = [
        _entity(title="a:f", etype="function", source_file="a.c"),
    ]
    manifest = {
        "counts": {"entities": 1},
        "clang_signatures": build_disabled_provenance(),
    }
    return entities, manifest


def _legacy_graph():
    entities = [
        _entity(title="a:f", etype="function", source_file="a.c"),
    ]
    return entities, {"counts": {"entities": 1}}


def _decorated(entities: list) -> dict:
    return next(e for e in entities if e.get("clang_signature_status") == "matched")


# ---------------------------------------------------------------------------
# Supported states
# ---------------------------------------------------------------------------


def test_legacy_absent_passes():
    entities, manifest = _legacy_graph()
    report = audit_rows(entities, manifest)
    assert report["ok"] is True
    assert report["status"] == "legacy_absent"
    assert report["state"] == "legacy_absent"
    assert report["classification"] == "legacy_absent"
    assert report["n_decorated_entities"] == 0
    assert report["n_violations"] == 0
    assert report["audit_mode"] == AUDIT_MODE
    bare = audit_rows(entities, None)
    assert bare["ok"] is True
    assert bare["status"] == "legacy_absent"


def test_explicit_off_passes():
    entities, manifest = _off_graph()
    report = audit_rows(entities, manifest)
    assert report["ok"] is True
    assert report["status"] == "off"
    assert report["n_decorated_entities"] == 0
    assert manifest["clang_signatures"] == {
        "mode": "off",
        "enabled": False,
        "n_facts": 0,
        "n_translation_units": 0,
    }

    extra = copy.deepcopy(manifest)
    extra["clang_signatures"]["unexpected"] = 0
    extra_report = audit_rows(entities, extra)
    assert extra_report["ok"] is False
    assert "manifest_contract_claim" in _codes(extra_report)


def test_enabled_overlay_passes(tmp_path: Path):
    entities, manifest = _enabled_graph(tmp_path, residual=True)
    report = audit_rows(entities, manifest)
    assert report["ok"] is True, report["anomalies"]
    assert report["status"] == "enabled"
    assert report["n_decorated_entities"] == 1
    assert report["counts"]["matched"] == 1
    assert report["counts"]["tree_sitter_only"] == 2
    assert report["counts"]["out_of_compile_db_scope"] == 3
    assert report["fact_kind"] == FACT_KIND
    assert report["extractor"] == EXTRACTOR
    assert report["overlay_mode"] == MODE
    target = _decorated(entities)
    assert all(field in target for field in _SIGNATURE_FIELDS)


# ---------------------------------------------------------------------------
# Fail-closed states
# ---------------------------------------------------------------------------


def test_malformed_manifest_value_types_fail():
    entities, _manifest = _legacy_graph()
    for value in (None, [], "off", 0):
        report = audit_rows(
            entities, {"counts": {"entities": 1}, "clang_signatures": value}
        )
        assert report["ok"] is False, value
        assert "invalid_enabled_block" in _codes(report), value


def test_partial_manifest_fails(tmp_path: Path):
    entities, manifest = _enabled_graph(tmp_path)
    for field in (
        "fact_kind",
        "extractor",
        "counts",
        "compilers",
        "compile_commands_digest",
        "n_compile_entries",
        "confidence_boundary",
    ):
        broken = copy.deepcopy(manifest)
        broken["clang_signatures"].pop(field)
        report = audit_rows(entities, broken)
        assert report["ok"] is False, field

    extra = copy.deepcopy(manifest)
    extra["clang_signatures"]["unexpected"] = "not producer-owned"
    extra_report = audit_rows(entities, extra)
    assert extra_report["ok"] is False
    assert "manifest_contract_claim" in _codes(extra_report)

    extra_count = copy.deepcopy(manifest)
    extra_count["clang_signatures"]["counts"]["unexpected"] = 0
    count_report = audit_rows(entities, extra_count)
    assert count_report["ok"] is False
    assert "manifest_count_mismatch" in _codes(count_report)


def test_evidence_without_manifest_fails(tmp_path: Path):
    entities, _manifest = _enabled_graph(tmp_path)
    report = audit_rows(entities, {"counts": {"entities": len(entities)}})
    assert report["ok"] is False
    assert "legacy_block_missing_with_fields" in _codes(report)
    none_manifest = audit_rows(entities, None)
    assert none_manifest["ok"] is False
    assert "legacy_block_missing_with_fields" in _codes(none_manifest)


def test_manifest_without_required_evidence_fails(tmp_path: Path):
    entities, manifest = _enabled_graph(tmp_path)
    clean, _ = _legacy_graph()
    report = audit_rows(clean, manifest)
    assert report["ok"] is False
    assert "manifest_count_mismatch" in _codes(report)


def test_off_with_fields_fails(tmp_path: Path):
    entities, _manifest = _enabled_graph(tmp_path)
    manifest = {
        "counts": {"entities": len(entities)},
        "clang_signatures": build_disabled_provenance(),
    }
    report = audit_rows(entities, manifest)
    assert report["ok"] is False
    assert "off_with_decorated_entities" in _codes(report)


def test_inconsistent_enablement_fails(tmp_path: Path):
    entities, manifest = _enabled_graph(tmp_path)
    for mode, enabled in ((MODE, False), ("off", True), ("weird", True), (MODE, None)):
        broken = copy.deepcopy(manifest)
        broken["clang_signatures"]["mode"] = mode
        broken["clang_signatures"]["enabled"] = enabled
        report = audit_rows(entities, broken)
        assert report["ok"] is False, (mode, enabled)
        assert "invalid_enabled_block" in _codes(report)


def test_every_missing_producer_field_fails(tmp_path: Path):
    entities, manifest = _enabled_graph(tmp_path)
    for field in _SIGNATURE_FIELDS:
        rows = copy.deepcopy(entities)
        _decorated(rows).pop(field)
        report = audit_rows(rows, manifest)
        assert report["ok"] is False, field
        assert "partial_signature_payload" in _codes(report), field


def test_nullable_keys_present_as_null_pass(tmp_path: Path):
    entities, manifest = _enabled_graph(tmp_path)
    rows = copy.deepcopy(entities)
    target = _decorated(rows)
    target["clang_storage_class"] = None
    target["clang_inline"] = None
    target["clang_variadic"] = None
    target["clang_mangled_name"] = None
    report = audit_rows(rows, manifest)
    assert report["ok"] is True, report["anomalies"]


def test_unknown_material_signature_field_fails(tmp_path: Path):
    entities, manifest = _enabled_graph(tmp_path)
    rows = copy.deepcopy(entities)
    _decorated(rows)["clang_signature_abi_proof"] = True
    report = audit_rows(rows, manifest)
    assert report["ok"] is False
    assert "unknown_signature_field" in _codes(report)


def test_wrong_entity_kind_fails(tmp_path: Path):
    entities, manifest = _enabled_graph(tmp_path)
    rows = copy.deepcopy(entities)
    payload = {
        key: value
        for key, value in _decorated(rows).items()
        if key in _SIGNATURE_FIELDS
    }
    file_ent = next(e for e in rows if e["type"] == "file")
    file_ent.update(payload)
    report = audit_rows(rows, manifest)
    assert report["ok"] is False
    assert "stale_signature_metadata" in _codes(report)


def test_duplicate_and_empty_entity_id_fail(tmp_path: Path):
    entities, manifest = _enabled_graph(tmp_path)
    rows = copy.deepcopy(entities)
    rows[1]["id"] = rows[0]["id"]
    report = audit_rows(rows, manifest)
    assert report["ok"] is False
    assert "duplicate_entity_id" in _codes(report)

    empty = copy.deepcopy(entities)
    empty[0]["id"] = ""
    report_empty = audit_rows(empty, manifest)
    assert report_empty["ok"] is False
    assert "empty_entity_id" in _codes(report_empty)


# ---------------------------------------------------------------------------
# Observations JSON
# ---------------------------------------------------------------------------


def test_malformed_and_noncanonical_observations_json_fail(tmp_path: Path):
    entities, manifest = _enabled_graph(tmp_path)
    for value in (
        "not json",
        "[]",
        json.dumps(
            json.loads(_decorated(entities)["clang_signature_observations_json"]),
            indent=2,
        ),
        json.dumps(
            json.loads(_decorated(entities)["clang_signature_observations_json"]),
            separators=(", ", ": "),
        ),
    ):
        rows = copy.deepcopy(entities)
        _decorated(rows)["clang_signature_observations_json"] = value
        report = audit_rows(rows, manifest)
        assert report["ok"] is False, value
        assert "observations_json" in _codes(report), (value, _codes(report))


def test_duplicate_json_keys_refused(tmp_path: Path):
    entities, manifest = _enabled_graph(tmp_path)
    raw = (
        '[{"compile_commands_digest":"abc123","compiler_id":"Apple clang '
        'version test","compiler_path":"/usr/bin/clang","entry_indices":[0],'
        '"qualType":"int (void)","qualType":"void (void)","storageClass":'
        '"static"}]'
    )
    rows = copy.deepcopy(entities)
    _decorated(rows)["clang_signature_observations_json"] = raw
    report = audit_rows(rows, manifest)
    assert report["ok"] is False
    assert "observations_json" in _codes(report)


def test_nan_and_infinity_refused(tmp_path: Path):
    entities, manifest = _enabled_graph(tmp_path)
    for token in ("NaN", "Infinity"):
        raw = (
            '[{"compile_commands_digest":"abc123","compiler_id":"Apple clang '
            'version test","compiler_path":"/usr/bin/clang","entry_indices":[0],'
            f'"qualType":{token},"storageClass":"static"}}]'
        )
        rows = copy.deepcopy(entities)
        _decorated(rows)["clang_signature_observations_json"] = raw
        report = audit_rows(rows, manifest)
        assert report["ok"] is False, token
        assert "observations_json" in _codes(report)


def test_observation_qualtype_mismatch_fails(tmp_path: Path):
    entities, manifest = _enabled_graph(tmp_path)
    rows = copy.deepcopy(entities)
    target = _decorated(rows)
    obs = json.loads(target["clang_signature_observations_json"])
    obs[0]["qualType"] = "void (void)"
    target["clang_signature_observations_json"] = json.dumps(
        obs, sort_keys=True, separators=(",", ":")
    )
    report = audit_rows(rows, manifest)
    assert report["ok"] is False
    assert "observation_qual_type" in _codes(report)


def test_digest_mismatch_fails(tmp_path: Path):
    entities, manifest = _enabled_graph(tmp_path)
    rows = copy.deepcopy(entities)
    _decorated(rows)["clang_signature_compile_commands_digest"] = "zzz"
    report = audit_rows(rows, manifest)
    assert report["ok"] is False
    assert "digest_mismatch" in _codes(report)

    broken = copy.deepcopy(manifest)
    broken["clang_signatures"]["compile_commands_digest"] = "other"
    report_manifest = audit_rows(entities, broken)
    assert report_manifest["ok"] is False
    assert "digest_mismatch" in _codes(report_manifest)


def test_unknown_compiler_identity_fails(tmp_path: Path):
    entities, manifest = _enabled_graph(tmp_path)
    rows = copy.deepcopy(entities)
    target = _decorated(rows)
    target["clang_signature_compiler_id"] = "other clang"
    obs = json.loads(target["clang_signature_observations_json"])
    obs[0]["compiler_id"] = "other clang"
    target["clang_signature_observations_json"] = json.dumps(
        obs, sort_keys=True, separators=(",", ":")
    )
    report = audit_rows(rows, manifest)
    assert report["ok"] is False
    assert "compiler_mismatch" in _codes(report)


def test_relative_compiler_path_fails(tmp_path: Path):
    entities, manifest = _enabled_graph(tmp_path)
    rows = copy.deepcopy(entities)
    _decorated(rows)["clang_signature_compiler_path"] = "clang"
    report = audit_rows(rows, manifest)
    assert report["ok"] is False
    assert "compiler_mismatch" in _codes(report)

    broken = copy.deepcopy(manifest)
    broken["clang_signatures"]["compilers"][0]["compiler_path"] = "clang"
    broken["clang_signatures"]["compiler_path"] = "clang"
    report_manifest = audit_rows(entities, broken)
    assert report_manifest["ok"] is False
    assert "manifest_identity_mismatch" in _codes(report_manifest)

    whitespace = copy.deepcopy(entities)
    target = _decorated(whitespace)
    target["clang_signature_compiler_path"] = f" {COMPILER_PATH}"
    whitespace_report = audit_rows(whitespace, manifest)
    assert whitespace_report["ok"] is False
    assert "compiler_mismatch" in _codes(whitespace_report)


def test_duplicate_compiler_identity_fails(tmp_path: Path):
    entities, manifest = _enabled_graph(tmp_path)
    broken = copy.deepcopy(manifest)
    only = copy.deepcopy(broken["clang_signatures"]["compilers"][0])
    broken["clang_signatures"]["compilers"] = [only, copy.deepcopy(only)]
    report = audit_rows(entities, broken)
    assert report["ok"] is False
    assert "manifest_identity_mismatch" in _codes(report)


def test_noncanonical_compiler_ordering_fails(tmp_path: Path):
    entities, manifest = _enabled_graph(tmp_path)
    broken = copy.deepcopy(manifest)
    second = {
        "compiler_path": "/usr/bin/other",
        "compiler_id": "other clang",
        "compiler_version": "16.0.0",
    }
    first = broken["clang_signatures"]["compilers"][0]
    # Unsorted relative to producer canonical key.
    broken["clang_signatures"]["compilers"] = [second, first]
    broken["clang_signatures"]["compiler_path"] = None
    broken["clang_signatures"]["compiler_id"] = None
    broken["clang_signatures"]["compiler_version"] = None
    report = audit_rows(entities, broken)
    assert report["ok"] is False
    assert "manifest_identity_mismatch" in _codes(report)


def test_ambiguous_singular_compiler_fields_fail(tmp_path: Path):
    entities, manifest = _enabled_graph(tmp_path)
    broken = copy.deepcopy(manifest)
    second = {
        "compiler_path": "/usr/bin/other",
        "compiler_id": "other clang",
        "compiler_version": "16.0.0",
    }
    first = broken["clang_signatures"]["compilers"][0]
    ordered = sorted(
        [first, second],
        key=lambda row: json.dumps(row, sort_keys=True, separators=(",", ":")),
    )
    broken["clang_signatures"]["compilers"] = ordered
    # Singular fields remain set while two identities exist.
    report = audit_rows(entities, broken)
    assert report["ok"] is False
    assert "manifest_identity_mismatch" in _codes(report)

    version = copy.deepcopy(manifest)
    version["clang_signatures"]["compiler_version"] = None
    version_report = audit_rows(entities, version)
    assert version_report["ok"] is False
    assert "manifest_identity_mismatch" in _codes(version_report)


def test_entry_index_variants_fail(tmp_path: Path):
    entities, manifest = _enabled_graph(tmp_path)
    for value in ([], [1], [0, 0], [1, 0], [-1], ["0"], [0.5]):
        rows = copy.deepcopy(entities)
        _decorated(rows)["clang_signature_entry_indices"] = value
        report = audit_rows(rows, manifest)
        assert report["ok"] is False, value
        assert "entry_index_census" in _codes(report), (value, _codes(report))


def test_observation_entry_index_union_mismatch_fails(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    src = pkg / "a.c"
    src.write_text("int f(void){return 0;}\n")
    data = {
        "entities": [_entity(title="a:f", etype="function", source_file=str(src))],
        "relationships": [],
    }
    obs = [
        {
            "entry_indices": [0],
            "compiler_path": COMPILER_PATH,
            "compiler_id": COMPILER_ID,
            "compile_commands_digest": DIGEST,
            "qualType": "int (void)",
        },
        {
            "entry_indices": [1],
            "compiler_path": COMPILER_PATH,
            "compiler_id": COMPILER_ID,
            "compile_commands_digest": DIGEST,
            "qualType": "int (void)",
        },
    ]
    report = _clean_report(
        [
            _matched_row(
                title="a:f",
                source_path="a.c",
                name="f",
                entry_indices=[0, 1],
                observations=obs,
            )
        ],
        n_compile_entries=2,
    )
    block = apply_clang_signatures_from_report(data, report, pkg)
    manifest = {"counts": {"entities": 1}, "clang_signatures": block}
    rows = copy.deepcopy(data["entities"])
    # Drop the second observation after a successful produce.
    remaining = [json.loads(rows[0]["clang_signature_observations_json"])[0]]
    rows[0]["clang_signature_observations_json"] = json.dumps(
        remaining, sort_keys=True, separators=(",", ":")
    )
    audit = audit_rows(rows, manifest)
    assert audit["ok"] is False
    assert "entry_index_census" in _codes(audit)


def test_count_mismatch_fails(tmp_path: Path):
    entities, manifest = _enabled_graph(tmp_path)
    for field, value in (
        ("n_facts", 2),
        ("n_facts_changed", 99),
        ("n_translation_units", 2),
        ("n_compile_entries", 0),
    ):
        broken = copy.deepcopy(manifest)
        broken["clang_signatures"][field] = value
        report = audit_rows(entities, broken)
        assert report["ok"] is False, field
        assert "manifest_count_mismatch" in _codes(report), field

    counts = copy.deepcopy(manifest)
    counts["clang_signatures"]["counts"]["matched"] = 9
    report_counts = audit_rows(entities, counts)
    assert report_counts["ok"] is False
    assert "manifest_count_mismatch" in _codes(report_counts)


def test_nonzero_loss_bearing_counts_fail(tmp_path: Path):
    entities, manifest = _enabled_graph(tmp_path)
    for bucket in _FAIL_CLOSED:
        broken = copy.deepcopy(manifest)
        broken["clang_signatures"]["counts"][bucket] = 1
        report = audit_rows(entities, broken)
        assert report["ok"] is False, bucket
        assert "residual_bucket_nonzero" in _codes(report), bucket


def test_observation_counts_are_non_negative(tmp_path: Path):
    entities, manifest = _enabled_graph(tmp_path, residual=True)
    report = audit_rows(entities, manifest)
    assert report["ok"] is True, report["anomalies"]
    for bucket in _OBSERVATION:
        negative = copy.deepcopy(manifest)
        negative["clang_signatures"]["counts"][bucket] = -1
        report_neg = audit_rows(entities, negative)
        assert report_neg["ok"] is False, bucket
        assert "manifest_count_mismatch" in _codes(report_neg), bucket


def test_exact_description_mismatch_fails(tmp_path: Path):
    entities, manifest = _enabled_graph(tmp_path)
    rows = copy.deepcopy(entities)
    _decorated(rows)["clang_signature_description"] = (
        "configured Clang function signature for a:helper: int (void)"
    )
    report = audit_rows(rows, manifest)
    assert report["ok"] is False
    assert "description_mismatch" in _codes(report)


def test_identity_field_mismatch_fails(tmp_path: Path):
    entities, manifest = _enabled_graph(tmp_path)
    for field, value in (
        ("clang_signature_status", "residual"),
        ("clang_signature_fact_kind", "other"),
        ("clang_signature_extractor", "tree-sitter-c"),
        ("clang_signature_confidence", 0.5),
        ("clang_signature_is_deterministic", False),
        ("clang_qual_type", ""),
        ("clang_location_origin", "  "),
        ("clang_inline", "yes"),
    ):
        rows = copy.deepcopy(entities)
        _decorated(rows)[field] = value
        report = audit_rows(rows, manifest)
        assert report["ok"] is False, (field, value)


# ---------------------------------------------------------------------------
# Determinism / CLI / read-only
# ---------------------------------------------------------------------------


def test_output_is_deterministic(tmp_path: Path):
    entities, manifest = _enabled_graph(tmp_path)
    first = audit_to_json(audit_rows(entities, manifest))
    second = audit_to_json(
        audit_rows(copy.deepcopy(entities), copy.deepcopy(manifest))
    )
    assert first == second
    assert json.loads(first)["ok"] is True


def test_audit_does_not_mutate_inputs(tmp_path: Path):
    entities, manifest = _enabled_graph(tmp_path)
    before_entities = json.dumps(entities, sort_keys=True, default=str)
    before_manifest = json.dumps(manifest, sort_keys=True, default=str)
    audit_rows(entities, manifest)
    assert json.dumps(entities, sort_keys=True, default=str) == before_entities
    assert json.dumps(manifest, sort_keys=True, default=str) == before_manifest


def _publish(tmp_path: Path, entities: list, manifest_block, *, name: str = "g") -> Path:
    graph = tmp_path / name
    snap = graph / "snapshots" / "s1"
    snap.mkdir(parents=True)
    pd.DataFrame(entities).to_parquet(snap / "entities.parquet")
    pd.DataFrame(
        [{"id": "r1", "source": "a", "target": "b", "type": "calls"}]
    ).to_parquet(snap / "relationships.parquet")
    pd.DataFrame([{"id": "t1", "title": "a"}]).to_parquet(snap / "text_units.parquet")
    manifest = {"counts": {"entities": len(entities)}}
    if manifest_block is not None:
        manifest["clang_signatures"] = manifest_block
    (snap / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    (graph / "current").write_text("s1", encoding="utf-8")
    return graph


def test_graph_root_audit_is_byte_for_byte_read_only(tmp_path: Path):
    entities, manifest = _enabled_graph(tmp_path)
    graph = _publish(tmp_path, entities, manifest["clang_signatures"])
    snap = graph / "snapshots" / "s1"
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
    assert report["status"] == "enabled"
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
    assert read_only_fingerprint(graph, snap) == report["read_only_verification"][
        "fingerprint"
    ]

    for field in (
        "clang_storage_class",
        "clang_inline",
        "clang_variadic",
        "clang_mangled_name",
    ):
        corrupt = tmp_path / f"missing-{field}"
        shutil.copytree(graph, corrupt)
        corrupt_snap = corrupt / "snapshots" / "s1"
        frame = pd.read_parquet(corrupt_snap / "entities.parquet")
        assert field in frame.columns
        frame.drop(columns=[field]).to_parquet(
            corrupt_snap / "entities.parquet"
        )
        corrupt_report = audit_graph_root(corrupt)
        assert corrupt_report["ok"] is False, field
        assert "partial_signature_payload" in _codes(corrupt_report), field


def test_cli_refuses_output_inside_audited_graph(tmp_path: Path, capsys):
    entities, manifest = _enabled_graph(tmp_path)
    graph = _publish(tmp_path, entities, manifest["clang_signatures"])
    before = {
        path.relative_to(graph).as_posix(): path.read_bytes()
        for path in sorted(graph.rglob("*"))
        if path.is_file()
    }
    forbidden = graph / "snapshots" / "s1" / "sig-audit.json"
    assert audit_main(["--graph", str(graph), "--output", str(forbidden)]) == 2
    assert "outside the audited graph root" in capsys.readouterr().err
    assert not forbidden.exists()
    after = {
        path.relative_to(graph).as_posix(): path.read_bytes()
        for path in sorted(graph.rglob("*"))
        if path.is_file()
    }
    assert after == before


def test_cli_refuses_symlink_that_resolves_inside_graph(tmp_path: Path, capsys):
    entities, manifest = _enabled_graph(tmp_path)
    graph = _publish(tmp_path, entities, manifest["clang_signatures"])
    alias = tmp_path / "alias"
    alias.symlink_to(graph)
    forbidden = alias / "via-symlink.json"
    assert audit_main(["--graph", str(graph), "--output", str(forbidden)]) == 2
    assert "outside the audited graph root" in capsys.readouterr().err
    assert not forbidden.exists()
    assert not (graph / "via-symlink.json").exists()


def test_cli_exit_codes(tmp_path: Path, capsys):
    entities, manifest = _enabled_graph(tmp_path)
    good = _publish(tmp_path, entities, manifest["clang_signatures"], name="good")
    out_path = tmp_path / "report.json"
    assert audit_main(["--graph", str(good), "--output", str(out_path)]) == 0
    written = json.loads(out_path.read_text(encoding="utf-8"))
    assert written["ok"] is True
    assert written["audit_mode"] == AUDIT_MODE
    capsys.readouterr()

    bad_entities = copy.deepcopy(entities)
    _decorated(bad_entities)["clang_signature_confidence"] = 0.5
    bad = _publish(tmp_path, bad_entities, manifest["clang_signatures"], name="bad")
    assert audit_main(["--graph", str(bad)]) == 1
    capsys.readouterr()
    assert audit_main(["--graph", str(tmp_path / "missing")]) == 2


def test_malformed_manifest_is_load_error(tmp_path: Path):
    entities, _manifest = _legacy_graph()
    graph = _publish(tmp_path, entities, None, name="nan")
    (graph / "snapshots" / "s1" / "manifest.json").write_text(
        '{"counts": {"entities": NaN}}', encoding="utf-8"
    )
    with pytest.raises(ClangSignatureGraphAuditError, match="malformed manifest"):
        resolve_snapshot(graph)
    assert audit_main(["--graph", str(graph)]) == 2


def test_format_report_is_human_readable(tmp_path: Path):
    entities, manifest = _enabled_graph(tmp_path)
    text = format_report(audit_rows(entities, manifest))
    assert "RESULT: PASS" in text
    assert "read-only" in text


# ---------------------------------------------------------------------------
# published_graph_health
# ---------------------------------------------------------------------------


def test_published_graph_health_states(tmp_path: Path):
    from published_graph_health import (  # type: ignore
        _signature_integrity,
        _type_integrity,
        _type_shape_integrity,
        _type_use_integrity,
    )

    entities, manifest = _enabled_graph(tmp_path)
    enabled = _signature_integrity({"entities": entities}, manifest, indexer="c")
    assert enabled is not None
    assert enabled["ok"] is True
    assert enabled["status"] == "enabled"
    assert enabled["n_decorated_entities"] == 1

    legacy_entities, legacy_manifest = _legacy_graph()
    legacy = _signature_integrity(
        {"entities": legacy_entities}, legacy_manifest, indexer="c"
    )
    assert legacy["ok"] is True and legacy["status"] == "legacy_absent"

    off_entities, off_manifest = _off_graph()
    off = _signature_integrity({"entities": off_entities}, off_manifest, indexer="c")
    assert off["ok"] is True and off["status"] == "off"

    orphan = _signature_integrity(
        {"entities": entities},
        {"counts": {"entities": len(entities)}},
        indexer="c",
    )
    assert orphan["ok"] is False

    assert (
        _signature_integrity({"entities": entities}, manifest, indexer="python")
        is None
    )

    # Existing C overlay helpers remain independent and still skip-or-pass.
    assert _type_use_integrity(
        {"entities": entities, "relationships": []},
        {"counts": {"entities": len(entities)}},
        indexer="c",
    )["ok"] is True
    assert _type_shape_integrity(
        {"entities": entities},
        {"counts": {"entities": len(entities)}},
        indexer="c",
    )["ok"] is True
    assert _type_integrity(
        {"entities": entities},
        {"counts": {"entities": len(entities)}},
        indexer="c",
    )["ok"] is True


# ---------------------------------------------------------------------------
# Live disposable graphs
# ---------------------------------------------------------------------------


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


def _index(package: Path, graph: Path, *, signatures: bool) -> None:
    from index_c import main as index_c_main  # type: ignore

    index_c_main(
        package=package,
        graph=graph,
        keep_snapshots=2,
        compiler_builtins=False,
        compiler_dependencies=False,
        compiler_includes=False,
        clang_signatures=signatures,
        clang_calls=False,
        clang_types=False,
        clang_type_uses=False,
        clang_type_shapes=False,
        allow_toolchain_drift=False,
    )


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_cjson_signature_graph_audit(tmp_path: Path, monkeypatch):
    pkg = ROOT / "examples" / "cjson"
    package_before = {p.name for p in pkg.iterdir()}
    graph = tmp_path / "byog_cjson_sigs"
    _index(pkg, graph, signatures=True)
    snap = (graph / "current").read_text(encoding="utf-8").strip()
    block = json.loads(
        (graph / "snapshots" / snap / "manifest.json").read_text(encoding="utf-8")
    )["clang_signatures"]
    columns = set(
        pd.read_parquet(graph / "snapshots" / snap / "entities.parquet").columns
    )
    assert {
        "clang_storage_class",
        "clang_inline",
        "clang_variadic",
        "clang_mangled_name",
    } <= columns
    _fail_if_clang_used(monkeypatch)
    report = audit_graph_root(graph)
    assert report["ok"] is True, report["anomalies"]
    assert report["status"] == "enabled"
    assert report["n_decorated_entities"] == block["n_facts"] == block["counts"]["matched"]
    assert report["n_decorated_entities"] > 0
    for bucket in _FAIL_CLOSED:
        assert report["counts"][bucket] == 0
    assert report["read_only_verification"]["verified"] is True
    assert {p.name for p in pkg.iterdir()} == package_before
    for pattern in ("*.o", "*.ast", "*.d", "*.i", "*.ii", "*.pch"):
        assert not list(pkg.rglob(pattern))


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_inih_signature_graph_audit(tmp_path: Path, monkeypatch):
    pkg = ROOT / "examples" / "inih"
    graph = tmp_path / "byog_inih_sigs"
    _index(pkg, graph, signatures=True)
    snap = (graph / "current").read_text(encoding="utf-8").strip()
    block = json.loads(
        (graph / "snapshots" / snap / "manifest.json").read_text(encoding="utf-8")
    )["clang_signatures"]
    columns = set(
        pd.read_parquet(graph / "snapshots" / snap / "entities.parquet").columns
    )
    assert {
        "clang_storage_class",
        "clang_inline",
        "clang_variadic",
        "clang_mangled_name",
    } <= columns
    _fail_if_clang_used(monkeypatch)
    report = audit_graph_root(graph)
    assert report["ok"] is True, report["anomalies"]
    assert report["status"] == "enabled"
    assert report["n_decorated_entities"] == block["n_facts"] == block["counts"]["matched"]
    assert report["n_decorated_entities"] > 0
    assert report["read_only_verification"]["verified"] is True
    for pattern in ("*.o", "*.ast", "*.d", "*.i", "*.ii", "*.pch"):
        assert not list(pkg.rglob(pattern))


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_default_off_graph_passes(tmp_path: Path, monkeypatch):
    pkg = ROOT / "examples" / "inih"
    graph = tmp_path / "byog_inih_off"
    _index(pkg, graph, signatures=False)
    _fail_if_clang_used(monkeypatch)
    report = audit_graph_root(graph)
    assert report["ok"] is True
    assert report["status"] == "off"
    assert report["n_decorated_entities"] == 0


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_corrupted_temporary_copy_fails(tmp_path: Path):
    pkg = ROOT / "examples" / "inih"
    graph = tmp_path / "byog_inih_sigs"
    _index(pkg, graph, signatures=True)
    snap_id = (graph / "current").read_text(encoding="utf-8").strip()

    parquet_copy = tmp_path / "copy_entities"
    shutil.copytree(graph, parquet_copy)
    copy_snap = parquet_copy / "snapshots" / snap_id
    ents = pd.read_parquet(copy_snap / "entities.parquet")
    mask = ents["clang_signature_fact_kind"].astype(str) == FACT_KIND
    assert int(mask.sum()) > 0
    ents.loc[ents.index[mask][0], "clang_signature_confidence"] = 0.1
    ents.to_parquet(copy_snap / "entities.parquet")
    report_entities = audit_graph_root(parquet_copy)
    assert report_entities["ok"] is False
    assert "signature_field_type" in _codes(report_entities)

    manifest_copy = tmp_path / "copy_manifest"
    shutil.copytree(graph, manifest_copy)
    manifest_snap = manifest_copy / "snapshots" / snap_id
    manifest = json.loads(
        (manifest_snap / "manifest.json").read_text(encoding="utf-8")
    )
    manifest.pop("clang_signatures")
    (manifest_snap / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    report_manifest = audit_graph_root(manifest_copy)
    assert report_manifest["ok"] is False
    assert "legacy_block_missing_with_fields" in _codes(report_manifest)
    assert audit_graph_root(graph)["ok"] is True


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_published_graph_health_on_disposable_graph(tmp_path: Path):
    from published_graph_health import PublishedGraphSpec, check_spec  # type: ignore

    graph = tmp_path / "byog_inih_sigs"
    _index(ROOT / "examples" / "inih", graph, signatures=True)
    spec = PublishedGraphSpec(
        ident="inih_tmp",
        source=Path("examples/inih"),
        graph=str(graph),
        indexer="c",
        mode="mutable",
    )
    result = check_spec(spec, root=ROOT, graph_root=graph)
    assert result["status"] == "pass", result
    sig = result["clang_signature_integrity"]
    assert sig["ok"] is True
    assert sig["status"] == "enabled"
    assert sig["n_decorated_entities"] > 0
    assert result["clang_type_integrity"]["ok"] is True
    assert result["clang_type_use_integrity"]["ok"] is True
    assert result["clang_type_shape_integrity"]["ok"] is True
