"""Read-only integrity audit for persisted configured Clang type-shape fields.

Pure synthetic/corruption tests always run. Live disposable-graph tests skip
when no C compiler is present. Nothing here writes a published byog_* root.

Run:
  uv run python -m pytest examples/cjson/tests/test_c_clang_type_shape_graph_audit.py -q
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

from c_clang_type_shape_graph_audit import (  # type: ignore
    AUDIT_MODE,
    ClangTypeShapeGraphAuditError,
    audit_graph_root,
    audit_rows,
    audit_to_json,
    format_report,
    main as audit_main,
    read_only_fingerprint,
    resolve_snapshot,
)
from c_clang_type_shapes import (  # type: ignore
    EXTRACTOR,
    FACT_KIND,
    HARD_EQUALITY,
    MODE,
    apply_clang_type_shapes_from_reports,
    build_disabled_provenance,
)
from c_preprocessor import find_c_compiler  # type: ignore

DIGEST = "abc123"
COMPILER_PATH = "/usr/bin/clang"
COMPILER_ID = "Apple clang version test"
COMPILER_VERSION = "17.0.0"

_FAIL_CLOSED_BUCKETS = (
    "tree_sitter_only_members",
    "clang_only_members",
    "member_order_mismatch",
    "duplicate_or_ambiguous_members",
    "macro_location_unsupported",
    "owner_unmatched",
)


def _cc():
    return find_c_compiler()


def _codes(report) -> set:
    return {a.get("code") for a in report.get("anomalies") or []}


# ---------------------------------------------------------------------------
# Producer-built fixtures (decoration comes from the real overlay, not by hand)
# ---------------------------------------------------------------------------


def _entity(*, title: str, etype: str, span: str, source_file: str, **extra) -> dict:
    e = {
        "id": f"ent:{etype}:{title}",
        "title": title,
        "type": etype,
        "description": f"{etype} {title}",
        "source_file": source_file,
        "span": span,
        "extractor": "tree-sitter-c",
        "confidence": 1.0,
        "is_deterministic": True,
        "symbol_name": title.rsplit(":", 1)[-1],
        "text_unit_ids": [f"tu:{title}"],
    }
    e.update(extra)
    return e


def _clang_member(name: str, order: int, *, form: str = "field") -> dict:
    return {
        "name": name,
        "order": order,
        "form": form,
        "is_bitfield": False,
        "bit_width": None,
        "qualType": "int",
        "desugaredQualType": None,
        "line": 2 + order,
        "col0": 2,
        "location_origin": "direct",
        "residual": None,
        "clang_kind": "FieldDecl" if form == "field" else "EnumConstantDecl",
    }


def _shape_row(*, title: str, source_path: str, member_names: list[str], span: str,
               entity_kind: str = "struct", line: int = 1, col0: int = 0) -> dict:
    return {
        "classification": "matched_shape",
        "entity_kind": entity_kind,
        "name": title.rsplit(":", 1)[-1],
        "source_path": source_path,
        "tree_sitter_title": title,
        "matched_site_span": span,
        "matched_site_line": line,
        "matched_site_col0": col0,
        "clang_line": line,
        "clang_col0": col0,
        "location_origin": "direct",
        "entry_indices": [0],
        "compiler_path": COMPILER_PATH,
        "compiler_id": COMPILER_ID,
        "compilers": [
            {
                "compiler_path": COMPILER_PATH,
                "compiler_id": COMPILER_ID,
                "compile_commands_digest": DIGEST,
            }
        ],
        "compile_commands_digest": DIGEST,
        "tree_sitter_members": [
            {
                "name": n,
                "order": i,
                "form": "field",
                "is_bitfield": False,
                "bit_width": None,
                "line": 2 + i,
                "col0": 2,
                "span": f"{2 + i}:2-{2 + i}:10",
                "residual": None,
            }
            for i, n in enumerate(member_names)
        ],
        "clang_members": [
            _clang_member(n, i, form="field" if entity_kind == "struct" else "enumerator")
            for i, n in enumerate(member_names)
        ],
        "tree_sitter_member_names": list(member_names),
        "clang_member_names": list(member_names),
        "detail": {},
        "confidence_boundary": "test",
    }


def _owner_row(*, title: str, source_path: str, span: str,
               entity_kind: str = "struct", line: int = 1, col0: int = 0) -> dict:
    return {
        "entity_kind": entity_kind,
        "name": title.rsplit(":", 1)[-1],
        "source_path": source_path,
        "tree_sitter_title": title,
        "graph_canonical_span": span,
        "graph_canonical_line": line,
        "graph_canonical_col0": col0,
        "graph_canonical_is_matched_site": True,
        "matched_site_span": span,
        "matched_site_line": line,
        "matched_site_col0": col0,
        "matched_site_is_canonical": True,
        "line_column_confirmed": True,
    }


def _provenance() -> dict:
    return {
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
    }


def _shape_report(matched: list, **counts_extra) -> dict:
    counts = {
        "matched_shape": len(matched),
        "unsupported_member_form": 0,
        "outside_package_declarations": 0,
        "type_declaration_matched_struct_enum": len(matched),
        "type_declaration_matched_total": len(matched),
        "shape_owners_classified": len(matched),
    }
    for bucket in _FAIL_CLOSED_BUCKETS:
        counts[bucket] = 0
    counts.update(counts_extra)
    report = {"mode": "clang_ast_json_type_shape_audit"}
    report.update(_provenance())
    report.update(
        {
            "type_declaration_audit_mode": "clang_ast_json_type_declaration_audit",
            "counts": counts,
            "matched_shape": matched,
            "unsupported_member_form": [],
            "outside_package_declarations": [],
            "limitations": [],
            "confidence_boundary": "test",
        }
    )
    for bucket in _FAIL_CLOSED_BUCKETS:
        report[bucket] = []
    return report


def _type_report(owners: list) -> dict:
    report = {"mode": "clang_ast_json_type_declaration_audit"}
    report.update(_provenance())
    report["matched"] = owners
    return report


def _cjson_style(tmp_path: Path):
    """Three decorated struct owners plus undecorated neighbours."""
    pkg = tmp_path / "pkg"
    pkg.mkdir(parents=True, exist_ok=True)
    src = pkg / "a.c"
    src.write_text("struct S {\n  int x;\n  int y;\n};\n", encoding="utf-8")
    specs = [
        ("a:struct:cJSON", "1:0-4:1", ["next", "prev", "child"]),
        ("a:struct:cJSON_Hooks", "6:0-9:1", ["malloc_fn", "free_fn"]),
        ("a:struct:internal_hooks", "11:0-15:1", ["allocate", "deallocate"]),
    ]
    entities = [
        _entity(title=title, etype="struct", span=span, source_file=str(src))
        for title, span, _ in specs
    ]
    entities.append(
        _entity(title="a:f", etype="function", span="20:0-21:1", source_file=str(src))
    )
    data = {"entities": entities, "relationships": []}
    shape = _shape_report(
        [
            _shape_row(
                title=title,
                source_path="a.c",
                member_names=members,
                span=span,
                line=int(span.split(":")[0]),
            )
            for title, span, members in specs
        ]
    )
    types = _type_report(
        [
            _owner_row(
                title=title,
                source_path="a.c",
                span=span,
                line=int(span.split(":")[0]),
            )
            for title, span, _ in specs
        ]
    )
    block = apply_clang_type_shapes_from_reports(data, shape, types, pkg)
    manifest = {
        "counts": {"entities": len(entities)},
        "clang_type_shapes": block,
    }
    return data["entities"], manifest


def _off_graph():
    entities = [
        _entity(title="a:struct:S", etype="struct", span="1:0-4:1", source_file="a.c")
    ]
    manifest = {
        "counts": {"entities": len(entities)},
        "clang_type_shapes": build_disabled_provenance(),
    }
    return entities, manifest


def _legacy_graph():
    entities = [
        _entity(title="a:struct:S", etype="struct", span="1:0-4:1", source_file="a.c")
    ]
    return entities, {"counts": {"entities": len(entities)}}


def _decorated(entities: list) -> dict:
    return next(e for e in entities if e.get("clang_shape_fact_kind") == FACT_KIND)


# ---------------------------------------------------------------------------
# Supported states
# ---------------------------------------------------------------------------


def test_legacy_absent_passes():
    entities, manifest = _legacy_graph()
    report = audit_rows(entities, manifest)
    assert report["ok"] is True
    assert report["status"] == "legacy_absent"
    assert report["mode"] == "legacy_absent"
    assert report["state"] == "legacy_absent"
    assert report["classification"] == "legacy_absent"
    assert report["n_decorated_entities"] == 0
    assert report["n_violations"] == 0
    assert report["violations"] == []
    assert report["audit_mode"] == AUDIT_MODE
    # A graph with no manifest object at all is still legacy.
    bare = audit_rows(entities, None)
    assert bare["ok"] is True
    assert bare["status"] == "legacy_absent"


def test_explicit_off_passes():
    entities, manifest = _off_graph()
    report = audit_rows(entities, manifest)
    assert report["ok"] is True
    assert report["status"] == "off"
    assert report["n_decorated_entities"] == 0
    assert manifest["clang_type_shapes"] == {
        "mode": "off",
        "enabled": False,
        "n_facts": 0,
        "n_translation_units": 0,
    }


def test_enabled_cjson_style_passes(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    report = audit_rows(entities, manifest)
    assert report["ok"] is True, report["anomalies"]
    assert report["status"] == "enabled"
    assert report["n_decorated_entities"] == 3
    assert report["n_members_validated"] == 7
    assert report["counts"]["matched_shape"] == 3
    assert report["provenance"]["compile_commands_digest"] == DIGEST
    assert report["provenance"]["n_compile_entries"] == 1
    assert report["hard_equality"] == HARD_EQUALITY
    assert report["fact_kind"] == FACT_KIND
    assert report["extractor"] == EXTRACTOR
    assert report["overlay_mode"] == MODE


def test_enabled_inih_style_zero_decorated_passes(tmp_path: Path):
    """Enabled overlay that matched no struct/enum owner is still valid."""
    pkg = tmp_path / "pkg"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "a.c").write_text("typedef int T;\n", encoding="utf-8")
    entities = [
        _entity(title="a:T", etype="typedef", span="1:0-1:13", source_file="a.c")
    ]
    data = {"entities": entities, "relationships": []}
    block = apply_clang_type_shapes_from_reports(
        data, _shape_report([]), _type_report([]), pkg
    )
    manifest = {"counts": {"entities": 1}, "clang_type_shapes": block}
    report = audit_rows(entities, manifest)
    assert report["ok"] is True, report["anomalies"]
    assert report["status"] == "enabled"
    assert report["n_decorated_entities"] == 0
    assert report["n_members_validated"] == 0
    assert not any(
        str(k).startswith("clang_shape_") for k in entities[0]
    )


# ---------------------------------------------------------------------------
# Fail-closed states
# ---------------------------------------------------------------------------


def test_missing_manifest_with_fields_fails(tmp_path: Path):
    entities, _manifest = _cjson_style(tmp_path)
    report = audit_rows(entities, {"counts": {"entities": len(entities)}})
    assert report["ok"] is False
    assert report["status"] == "invalid"
    assert "legacy_block_missing_with_fields" in _codes(report)
    # Absent manifest entirely is equally refused.
    none_manifest = audit_rows(entities, None)
    assert none_manifest["ok"] is False
    assert "legacy_block_missing_with_fields" in _codes(none_manifest)
    assert none_manifest["violations"] == none_manifest["anomalies"]


def test_off_manifest_with_fields_fails(tmp_path: Path):
    entities, _manifest = _cjson_style(tmp_path)
    manifest = {
        "counts": {"entities": len(entities)},
        "clang_type_shapes": build_disabled_provenance(),
    }
    report = audit_rows(entities, manifest)
    assert report["ok"] is False
    assert report["status"] == "invalid"
    assert "off_with_decorated_entities" in _codes(report)


def test_enabled_manifest_without_required_fields_fails(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    for field in (
        "fact_kind",
        "extractor",
        "counts",
        "compilers",
        "compile_commands_digest",
        "hard_equality",
        "evidence_only",
        "limitations",
        "confidence_boundary",
        "n_compile_entries",
    ):
        broken = copy.deepcopy(manifest)
        broken["clang_type_shapes"].pop(field)
        report = audit_rows(entities, broken)
        assert report["ok"] is False, field
        assert report["status"] == "invalid"


def test_inconsistent_enablement_fails(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    for mode, enabled in ((MODE, False), ("off", True), ("weird", True), (MODE, None)):
        broken = copy.deepcopy(manifest)
        broken["clang_type_shapes"]["mode"] = mode
        broken["clang_type_shapes"]["enabled"] = enabled
        report = audit_rows(entities, broken)
        assert report["ok"] is False, (mode, enabled)
        assert "invalid_enabled_block" in _codes(report)

    for value in ("enabled", None, []):
        not_object = copy.deepcopy(manifest)
        not_object["clang_type_shapes"] = value
        report = audit_rows(entities, not_object)
        assert report["ok"] is False, value
        assert "invalid_enabled_block" in _codes(report), value


def test_partial_entity_payload_fails(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    for field in (
        "clang_shape_member_names",
        "clang_shape_member_evidence",
        "clang_shape_member_count",
        "clang_shape_entry_indices",
        "clang_shape_compilers",
        "clang_shape_description",
        "clang_shape_graph_canonical_span",
    ):
        rows = copy.deepcopy(entities)
        _decorated(rows).pop(field)
        report = audit_rows(rows, manifest)
        assert report["ok"] is False, field
        assert "partial_shape_payload" in _codes(report), field


def test_null_payload_field_is_partial(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    rows = copy.deepcopy(entities)
    _decorated(rows)["clang_shape_member_names"] = None
    report = audit_rows(rows, manifest)
    assert report["ok"] is False
    assert "partial_shape_payload" in _codes(report)


def test_unknown_shape_field_fails(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    rows = copy.deepcopy(entities)
    _decorated(rows)["clang_shape_abi_layout_proof"] = True
    report = audit_rows(rows, manifest)
    assert report["ok"] is False
    assert "unknown_shape_field" in _codes(report)


def test_non_struct_enum_decorated_entity_fails(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    rows = copy.deepcopy(entities)
    payload = {
        key: value
        for key, value in _decorated(rows).items()
        if str(key).startswith("clang_shape_")
    }
    function = next(e for e in rows if e["type"] == "function")
    function.update(payload)
    report = audit_rows(rows, manifest)
    assert report["ok"] is False
    assert "stale_shape_metadata" in _codes(report)


def test_duplicate_entity_id_fails(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
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


def test_canonical_span_mismatch_fails(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    rows = copy.deepcopy(entities)
    _decorated(rows)["span"] = "99:0-99:1"
    report = audit_rows(rows, manifest)
    assert report["ok"] is False
    assert "canonical_span_mismatch" in _codes(report)


def test_identity_field_mismatch_fails(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    for field, value in (
        ("clang_shape_fact_kind", "something_else"),
        ("clang_shape_extractor", "tree-sitter-c"),
        ("clang_shape_entity_kind", "enum"),
    ):
        rows = copy.deepcopy(entities)
        _decorated(rows)[field] = value
        report = audit_rows(rows, manifest)
        assert report["ok"] is False, field
        assert "identity_mismatch" in _codes(report), field


def test_validation_marker_and_scalar_types(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    for field, value in (
        ("clang_shape_members_validated", False),
        ("clang_shape_members_validated", "true"),
        ("clang_shape_is_deterministic", "yes"),
        ("clang_shape_confidence", 0.5),
        ("clang_shape_matched_site_line", 0),
        ("clang_shape_matched_site_col0", -1),
        ("clang_shape_matched_site_span", ""),
        ("clang_shape_location_origin", "  "),
        ("clang_shape_matched_site_is_canonical", "yes"),
        ("clang_shape_member_count", -1),
        ("clang_shape_member_count", 1.5),
    ):
        rows = copy.deepcopy(entities)
        _decorated(rows)[field] = value
        report = audit_rows(rows, manifest)
        assert report["ok"] is False, (field, value)
        assert "shape_field_type" in _codes(report), (field, value)


def test_integral_float_scalars_are_accepted(tmp_path: Path):
    """Parquet widens int columns with nulls to float64; that is not corruption."""
    entities, manifest = _cjson_style(tmp_path)
    rows = copy.deepcopy(entities)
    target = _decorated(rows)
    target["clang_shape_member_count"] = float(target["clang_shape_member_count"])
    target["clang_shape_matched_site_line"] = float(
        target["clang_shape_matched_site_line"]
    )
    target["clang_shape_matched_site_col0"] = float(
        target["clang_shape_matched_site_col0"]
    )
    report = audit_rows(rows, manifest)
    assert report["ok"] is True, report["anomalies"]


# ---------------------------------------------------------------------------
# Member JSON evidence
# ---------------------------------------------------------------------------


def test_invalid_member_names_json_fails(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    for value, code in (
        ("not json", "member_names_json"),
        ('["a", "a"]', "member_names_json"),  # non-canonical spacing
        ('{"a":1}', "member_census"),
        ('["a",""]', "member_census"),
        ('["a","a"]', "member_census"),  # duplicates
        (123, "member_names_json"),
    ):
        rows = copy.deepcopy(entities)
        _decorated(rows)["clang_shape_member_names"] = value
        report = audit_rows(rows, manifest)
        assert report["ok"] is False, value
        assert code in _codes(report), (value, _codes(report))


def test_non_canonical_member_json_fails(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    rows = copy.deepcopy(entities)
    target = _decorated(rows)
    decoded = json.loads(target["clang_shape_member_names"])
    target["clang_shape_member_names"] = json.dumps(decoded, indent=2)
    report = audit_rows(rows, manifest)
    assert report["ok"] is False
    assert "member_names_json" in _codes(report)

    rows2 = copy.deepcopy(entities)
    target2 = _decorated(rows2)
    evidence = json.loads(target2["clang_shape_member_evidence"])
    # Same content, non-canonical key order.
    target2["clang_shape_member_evidence"] = json.dumps(
        evidence, sort_keys=False, separators=(", ", ": ")
    )
    report2 = audit_rows(rows2, manifest)
    assert report2["ok"] is False
    assert "member_evidence_json" in _codes(report2)


def test_nan_and_infinity_are_refused(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    for raw in (
        '[{"bit_width":NaN,"col0":2,"desugaredQualType":null,"enum_value":null,'
        '"form":"field","is_bitfield":false,"line":2,"name":"next","order":0,'
        '"qualType":"int"}]',
        '[{"bit_width":Infinity,"col0":2,"desugaredQualType":null,'
        '"enum_value":null,"form":"field","is_bitfield":false,"line":2,'
        '"name":"next","order":0,"qualType":"int"}]',
    ):
        rows = copy.deepcopy(entities)
        _decorated(rows)["clang_shape_member_evidence"] = raw
        report = audit_rows(rows, manifest)
        assert report["ok"] is False
        assert "member_evidence_json" in _codes(report)

    names = copy.deepcopy(entities)
    _decorated(names)["clang_shape_member_names"] = '["a",NaN]'
    report_names = audit_rows(names, manifest)
    assert report_names["ok"] is False
    assert "member_names_json" in _codes(report_names)

    # A float NaN scalar is a missing value, not a validated payload.
    partial = copy.deepcopy(entities)
    _decorated(partial)["clang_shape_member_names"] = float("nan")
    report_partial = audit_rows(partial, manifest)
    assert report_partial["ok"] is False
    assert "partial_shape_payload" in _codes(report_partial)


def test_member_count_name_evidence_mismatch_fails(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    rows = copy.deepcopy(entities)
    _decorated(rows)["clang_shape_member_count"] = 99
    report = audit_rows(rows, manifest)
    assert report["ok"] is False
    assert "member_census" in _codes(report)

    dropped = copy.deepcopy(entities)
    target = _decorated(dropped)
    evidence = json.loads(target["clang_shape_member_evidence"])
    target["clang_shape_member_evidence"] = json.dumps(
        evidence[:-1], sort_keys=True, separators=(",", ":")
    )
    report_dropped = audit_rows(dropped, manifest)
    assert report_dropped["ok"] is False
    assert "member_census" in _codes(report_dropped)


def test_evidence_order_and_name_mismatch_fails(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    rows = copy.deepcopy(entities)
    target = _decorated(rows)
    evidence = json.loads(target["clang_shape_member_evidence"])
    evidence[0]["order"] = 5
    target["clang_shape_member_evidence"] = json.dumps(
        evidence, sort_keys=True, separators=(",", ":")
    )
    report = audit_rows(rows, manifest)
    assert report["ok"] is False
    assert "member_evidence" in _codes(report)

    renamed = copy.deepcopy(entities)
    target2 = _decorated(renamed)
    evidence2 = json.loads(target2["clang_shape_member_evidence"])
    evidence2[0]["name"] = "not_the_published_name"
    target2["clang_shape_member_evidence"] = json.dumps(
        evidence2, sort_keys=True, separators=(",", ":")
    )
    report2 = audit_rows(renamed, manifest)
    assert report2["ok"] is False
    assert "member_evidence" in _codes(report2)


def test_residual_member_form_and_bad_scalar_forms_fail(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    mutations = (
        ("form", "anonymous"),
        ("form", "unnamed_bitfield"),
        ("form", "unsupported"),
        ("is_bitfield", "no"),
        ("bit_width", "3"),
        ("bit_width", -1),
        ("enum_value", "1"),
        ("line", 0),
        ("col0", -2),
        ("qualType", 7),
    )
    for field, value in mutations:
        rows = copy.deepcopy(entities)
        target = _decorated(rows)
        evidence = json.loads(target["clang_shape_member_evidence"])
        evidence[0][field] = value
        target["clang_shape_member_evidence"] = json.dumps(
            evidence, sort_keys=True, separators=(",", ":")
        )
        report = audit_rows(rows, manifest)
        assert report["ok"] is False, (field, value)
        assert "member_evidence" in _codes(report), (field, value)


def test_extra_evidence_key_fails(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    rows = copy.deepcopy(entities)
    target = _decorated(rows)
    evidence = json.loads(target["clang_shape_member_evidence"])
    evidence[0]["abi_offset"] = 0
    target["clang_shape_member_evidence"] = json.dumps(
        evidence, sort_keys=True, separators=(",", ":")
    )
    report = audit_rows(rows, manifest)
    assert report["ok"] is False
    assert "member_evidence" in _codes(report)


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def test_entry_index_outside_census_fails(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    for value, code in (
        ([1], "entry_index_census"),
        ([1, 0], "entry_index_census"),
        ([0, 0], "entry_index_census"),
        ([], "entry_index_census"),
        (["0"], "entry_index_census"),
    ):
        rows = copy.deepcopy(entities)
        _decorated(rows)["clang_shape_entry_indices"] = value
        report = audit_rows(rows, manifest)
        assert report["ok"] is False, value
        assert code in _codes(report), value


def test_compiler_and_digest_mismatch_fails(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)

    digest_rows = copy.deepcopy(entities)
    _decorated(digest_rows)["clang_shape_compile_commands_digest"] = "zzz"
    digest_report = audit_rows(digest_rows, manifest)
    assert digest_report["ok"] is False
    assert "digest_mismatch" in _codes(digest_report)

    other_rows = copy.deepcopy(entities)
    target = _decorated(other_rows)
    target["clang_shape_compilers"] = json.dumps(
        [
            {
                "compile_commands_digest": DIGEST,
                "compiler_id": "other clang",
                "compiler_path": "/usr/bin/other",
            }
        ],
        sort_keys=True,
        separators=(",", ":"),
    )
    other_report = audit_rows(other_rows, manifest)
    assert other_report["ok"] is False
    assert "compiler_mismatch" in _codes(other_report)

    singular_rows = copy.deepcopy(entities)
    _decorated(singular_rows)["clang_shape_compiler_id"] = "disagreeing id"
    singular_report = audit_rows(singular_rows, manifest)
    assert singular_report["ok"] is False
    assert "compiler_mismatch" in _codes(singular_report)

    manifest_rows = copy.deepcopy(entities)
    bad_manifest = copy.deepcopy(manifest)
    bad_manifest["clang_type_shapes"]["compile_commands_digest"] = "different"
    manifest_report = audit_rows(manifest_rows, bad_manifest)
    assert manifest_report["ok"] is False
    assert "digest_mismatch" in _codes(manifest_report)


def test_manifest_fact_and_count_mismatch_fails(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    for field, value in (
        ("n_facts", 2),
        ("n_decorated_entities", 99),
        ("n_facts_changed", 99),
        ("n_translation_units", 2),
        ("n_compile_entries", 0),
    ):
        broken = copy.deepcopy(manifest)
        broken["clang_type_shapes"][field] = value
        report = audit_rows(entities, broken)
        assert report["ok"] is False, field
        assert "manifest_count_mismatch" in _codes(report), field

    counts = copy.deepcopy(manifest)
    counts["clang_type_shapes"]["counts"]["matched_shape"] = 2
    report_counts = audit_rows(entities, counts)
    assert report_counts["ok"] is False
    assert "manifest_count_mismatch" in _codes(report_counts)

    entities_count = copy.deepcopy(manifest)
    entities_count["counts"]["entities"] = 999
    report_entities = audit_rows(entities, entities_count)
    assert report_entities["ok"] is False
    assert "manifest_count_mismatch" in _codes(report_entities)


def test_nonzero_fail_closed_residual_count_fails(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    for bucket in _FAIL_CLOSED_BUCKETS:
        broken = copy.deepcopy(manifest)
        broken["clang_type_shapes"]["counts"][bucket] = 1
        report = audit_rows(entities, broken)
        assert report["ok"] is False, bucket
        assert "residual_bucket_nonzero" in _codes(report), bucket


def test_observation_only_counts_stay_valid(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    ok_manifest = copy.deepcopy(manifest)
    counts = ok_manifest["clang_type_shapes"]["counts"]
    counts["outside_package_declarations"] = 54
    report = audit_rows(entities, ok_manifest)
    assert report["ok"] is True, report["anomalies"]
    assert report["counts"]["outside_package_declarations"] == 54

    negative = copy.deepcopy(manifest)
    negative["clang_type_shapes"]["counts"]["unsupported_member_form"] = -1
    report_negative = audit_rows(entities, negative)
    assert report_negative["ok"] is False
    assert "manifest_count_mismatch" in _codes(report_negative)


def test_derived_owner_census_must_agree(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    broken = copy.deepcopy(manifest)
    broken["clang_type_shapes"]["counts"]["shape_owners_classified"] = 9
    report = audit_rows(entities, broken)
    assert report["ok"] is False
    assert "manifest_count_mismatch" in _codes(report)


# ---------------------------------------------------------------------------
# Evidence-boundary claims
# ---------------------------------------------------------------------------


def test_description_boundary_must_survive(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    rows = copy.deepcopy(entities)
    _decorated(rows)["clang_shape_description"] = "configured Clang type shape"
    report = audit_rows(rows, manifest)
    assert report["ok"] is False
    assert "confidence_boundary" in _codes(report)


def test_forbidden_abi_claims_fail(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    for text in (
        "ordered direct member names only; proves the ABI of this struct; "
        "not ABI or layout equality; deterministic only relative to recorded "
        "Clang + compile_commands.json",
        "ordered direct member names only; layout-compatible with Rust; "
        "not ABI or layout equality; deterministic only relative to recorded "
        "Clang + compile_commands.json",
        "ordered direct member names only; FFI-safe; not ABI or layout "
        "equality; deterministic only relative to recorded Clang + "
        "compile_commands.json",
        "ordered direct member names only; repr-guaranteed; not ABI or layout "
        "equality; deterministic only relative to recorded Clang + "
        "compile_commands.json",
    ):
        rows = copy.deepcopy(entities)
        _decorated(rows)["clang_shape_description"] = text
        report = audit_rows(rows, manifest)
        assert report["ok"] is False, text
        assert "forbidden_claim" in _codes(report), text


def test_manifest_contract_text_is_pinned(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    for field, value in (
        ("hard_equality", "ABI-compatible layout"),
        ("confidence_boundary", "shapes are ABI proof"),
        ("limitations", ["ABI guaranteed"]),
        ("evidence_only", ["qualType"]),
        ("observation_only_buckets", []),
        ("fail_closed_buckets", ["owner_unmatched"]),
    ):
        broken = copy.deepcopy(manifest)
        broken["clang_type_shapes"][field] = value
        report = audit_rows(entities, broken)
        assert report["ok"] is False, field
        assert "manifest_contract_claim" in _codes(report), field

    claimed = copy.deepcopy(manifest)
    claimed["clang_type_shapes"]["limitations"] = ["ABI guaranteed"]
    report_claim = audit_rows(entities, claimed)
    assert "forbidden_claim" in _codes(report_claim)


# ---------------------------------------------------------------------------
# Determinism / read-only / CLI
# ---------------------------------------------------------------------------


def test_output_is_deterministic(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    first = audit_to_json(audit_rows(entities, manifest))
    second = audit_to_json(audit_rows(copy.deepcopy(entities), copy.deepcopy(manifest)))
    assert first == second
    assert json.loads(first)["ok"] is True
    # Anomaly ordering is stable regardless of entity order.
    broken = copy.deepcopy(entities)
    _decorated(broken)["clang_shape_member_count"] = 42
    a = audit_to_json(audit_rows(broken, manifest))
    b = audit_to_json(audit_rows(list(reversed(broken)), manifest))
    assert json.loads(a)["anomalies"] == json.loads(b)["anomalies"]


def test_audit_does_not_mutate_inputs(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    before_entities = json.dumps(entities, sort_keys=True, default=str)
    before_manifest = json.dumps(manifest, sort_keys=True, default=str)
    audit_rows(entities, manifest)
    assert json.dumps(entities, sort_keys=True, default=str) == before_entities
    assert json.dumps(manifest, sort_keys=True, default=str) == before_manifest


def _publish(tmp_path: Path, entities: list, manifest_block, *, name: str = "g") -> Path:
    """Write a minimal snapshot layout without invoking the indexer."""
    graph = tmp_path / name
    snap = graph / "snapshots" / "s1"
    snap.mkdir(parents=True)
    pd.DataFrame(entities).to_parquet(snap / "entities.parquet")
    pd.DataFrame([{"id": "r1", "source": "a", "target": "b", "type": "calls"}]).to_parquet(
        snap / "relationships.parquet"
    )
    pd.DataFrame([{"id": "t1", "title": "a"}]).to_parquet(snap / "text_units.parquet")
    manifest = {"counts": {"entities": len(entities)}}
    if manifest_block is not None:
        manifest["clang_type_shapes"] = manifest_block
    (snap / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    (graph / "current").write_text("s1", encoding="utf-8")
    return graph


def test_graph_root_audit_is_byte_for_byte_read_only(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    graph = _publish(tmp_path, entities, manifest["clang_type_shapes"])
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
    assert report["n_decorated_entities"] == 3
    assert after == before
    assert report["read_only_verification"]["verified"] is True
    assert report["read_only_verification"]["changed_inputs"] == []
    assert set(report["read_only_verification"]["inputs"]) == {
        "manifest.json",
        "entities.parquet",
        "relationships.parquet",
        "text_units.parquet",
        "current",
        "snapshots_dir",
    }
    fingerprint = read_only_fingerprint(graph, snap)
    assert fingerprint == report["read_only_verification"]["fingerprint"]


def test_cli_exit_codes_and_output(tmp_path: Path, capsys):
    entities, manifest = _cjson_style(tmp_path)
    good = _publish(tmp_path, entities, manifest["clang_type_shapes"], name="good")
    out_path = tmp_path / "report.json"
    assert audit_main(["--graph", str(good), "--output", str(out_path)]) == 0
    written = json.loads(out_path.read_text(encoding="utf-8"))
    assert written["ok"] is True
    assert written["audit_mode"] == AUDIT_MODE
    assert written["snapshot"] == "s1"
    capsys.readouterr()

    assert audit_main(["--graph", str(good), "--snapshot", "s1", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["snapshot"] == "s1"

    bad_entities = copy.deepcopy(entities)
    _decorated(bad_entities)["clang_shape_member_count"] = 42
    bad = _publish(tmp_path, bad_entities, manifest["clang_type_shapes"], name="bad")
    assert audit_main(["--graph", str(bad)]) == 1
    capsys.readouterr()

    assert audit_main(["--graph", str(tmp_path / "missing")]) == 2
    capsys.readouterr()

    assert audit_main(["--graph", str(good), "--snapshot", "../escape"]) == 2
    capsys.readouterr()
    assert audit_main(["--graph", str(good), "--snapshot", "nope"]) == 2
    capsys.readouterr()


def test_cli_refuses_output_inside_audited_graph(tmp_path: Path, capsys):
    entities, manifest = _cjson_style(tmp_path)
    graph = _publish(tmp_path, entities, manifest["clang_type_shapes"])
    before = {
        path.relative_to(graph).as_posix(): path.read_bytes()
        for path in sorted(graph.rglob("*"))
        if path.is_file()
    }
    forbidden = graph / "snapshots" / "s1" / "shape-audit.json"

    assert (
        audit_main(["--graph", str(graph), "--output", str(forbidden)]) == 2
    )
    assert "outside the audited graph root" in capsys.readouterr().err
    assert not forbidden.exists()
    after = {
        path.relative_to(graph).as_posix(): path.read_bytes()
        for path in sorted(graph.rglob("*"))
        if path.is_file()
    }
    assert after == before


def test_malformed_manifest_is_load_error(tmp_path: Path):
    entities, _manifest = _legacy_graph()
    graph = _publish(tmp_path, entities, None, name="nan")
    (graph / "snapshots" / "s1" / "manifest.json").write_text(
        '{"counts": {"entities": NaN}}', encoding="utf-8"
    )
    with pytest.raises(ClangTypeShapeGraphAuditError, match="malformed manifest"):
        resolve_snapshot(graph)
    assert audit_main(["--graph", str(graph)]) == 2


def test_format_report_is_human_readable(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    text = format_report(audit_rows(entities, manifest))
    assert "RESULT: PASS" in text
    assert "read-only" in text


# ---------------------------------------------------------------------------
# published_graph_health integration
# ---------------------------------------------------------------------------


def test_published_graph_health_states(tmp_path: Path):
    from published_graph_health import _type_shape_integrity  # type: ignore

    entities, manifest = _cjson_style(tmp_path)
    enabled = _type_shape_integrity(
        {"entities": entities}, manifest, indexer="c"
    )
    assert enabled is not None
    assert enabled["ok"] is True
    assert enabled["status"] == "enabled"
    assert enabled["n_decorated_entities"] == 3

    legacy_entities, legacy_manifest = _legacy_graph()
    legacy = _type_shape_integrity(
        {"entities": legacy_entities}, legacy_manifest, indexer="c"
    )
    assert legacy["ok"] is True and legacy["status"] == "legacy_absent"

    off_entities, off_manifest = _off_graph()
    off = _type_shape_integrity(
        {"entities": off_entities}, off_manifest, indexer="c"
    )
    assert off["ok"] is True and off["status"] == "off"

    # Configured fields without a legitimate manifest fail closed.
    orphan = _type_shape_integrity(
        {"entities": entities}, {"counts": {"entities": len(entities)}}, indexer="c"
    )
    assert orphan["ok"] is False
    assert orphan["n_anomalies"] >= 1

    # Python graphs are untouched by this check.
    assert (
        _type_shape_integrity({"entities": entities}, manifest, indexer="python")
        is None
    )


# ---------------------------------------------------------------------------
# Live disposable graphs (compiler-conditional; never touch published roots)
# ---------------------------------------------------------------------------


def _fail_if_clang_used(monkeypatch):
    """Make every compiler/capture entry point explode if the audit calls it."""
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


def _index(package: Path, graph: Path, *, shapes: bool) -> None:
    from index_c import main as index_c_main  # type: ignore

    index_c_main(
        package=package,
        graph=graph,
        keep_snapshots=2,
        compiler_builtins=False,
        compiler_dependencies=False,
        compiler_includes=False,
        clang_signatures=False,
        clang_calls=False,
        clang_types=False,
        clang_type_uses=False,
        clang_type_shapes=shapes,
        allow_toolchain_drift=False,
    )


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_cjson_shape_graph_audit_without_clang(tmp_path: Path, monkeypatch):
    pkg = ROOT / "examples" / "cjson"
    package_before = {p.name for p in pkg.iterdir()}
    graph = tmp_path / "byog_cjson_shapes"
    _index(pkg, graph, shapes=True)

    _fail_if_clang_used(monkeypatch)
    report = audit_graph_root(graph)
    assert report["ok"] is True, report["anomalies"]
    assert report["status"] == "enabled"
    assert report["n_decorated_entities"] == 3
    assert report["n_members_validated"] == 13
    assert report["counts"]["matched_shape"] == 3
    for bucket in _FAIL_CLOSED_BUCKETS:
        assert report["counts"][bucket] == 0
    assert report["read_only_verification"]["verified"] is True
    assert report["provenance"]["n_compile_entries"] == 1

    assert {p.name for p in pkg.iterdir()} == package_before
    for pattern in ("*.o", "*.ast", "*.d", "*.i"):
        assert not list(pkg.rglob(pattern))


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_inih_enabled_zero_decorated(tmp_path: Path, monkeypatch):
    pkg = ROOT / "examples" / "inih"
    graph = tmp_path / "byog_inih_shapes"
    _index(pkg, graph, shapes=True)

    snap = (graph / "current").read_text(encoding="utf-8").strip()
    block = json.loads(
        (graph / "snapshots" / snap / "manifest.json").read_text(encoding="utf-8")
    )["clang_type_shapes"]
    assert block["mode"] == MODE and block["enabled"] is True

    _fail_if_clang_used(monkeypatch)
    report = audit_graph_root(graph)
    assert report["ok"] is True, report["anomalies"]
    assert report["status"] == "enabled"
    assert report["n_decorated_entities"] == 0
    assert report["n_members_validated"] == 0
    assert report["read_only_verification"]["verified"] is True
    for pattern in ("*.o", "*.ast", "*.d", "*.i"):
        assert not list(pkg.rglob(pattern))


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_default_off_graph_passes(tmp_path: Path, monkeypatch):
    pkg = ROOT / "examples" / "inih"
    graph = tmp_path / "byog_inih_off"
    _index(pkg, graph, shapes=False)
    _fail_if_clang_used(monkeypatch)
    report = audit_graph_root(graph)
    assert report["ok"] is True
    assert report["status"] == "off"
    assert report["n_decorated_entities"] == 0


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_corrupted_temporary_copy_fails(tmp_path: Path):
    """Corrupt only a disposable copy; published byog_* roots are never touched."""
    pkg = ROOT / "examples" / "cjson"
    graph = tmp_path / "byog_cjson_shapes"
    _index(pkg, graph, shapes=True)
    snap_id = (graph / "current").read_text(encoding="utf-8").strip()
    snap = graph / "snapshots" / snap_id

    # 1) Corrupted entities parquet copy (member count no longer matches names).
    parquet_copy = tmp_path / "copy_entities"
    shutil.copytree(graph, parquet_copy)
    copy_snap = parquet_copy / "snapshots" / snap_id
    ents = pd.read_parquet(copy_snap / "entities.parquet")
    mask = ents["clang_shape_fact_kind"].astype(str) == FACT_KIND
    assert int(mask.sum()) == 3
    ents.loc[ents.index[mask][0], "clang_shape_member_count"] = 999
    ents.to_parquet(copy_snap / "entities.parquet")
    report_entities = audit_graph_root(parquet_copy)
    assert report_entities["ok"] is False
    assert "member_census" in _codes(report_entities)

    # 2) Corrupted manifest copy (block removed while fields remain).
    manifest_copy = tmp_path / "copy_manifest"
    shutil.copytree(graph, manifest_copy)
    manifest_snap = manifest_copy / "snapshots" / snap_id
    manifest = json.loads(
        (manifest_snap / "manifest.json").read_text(encoding="utf-8")
    )
    manifest.pop("clang_type_shapes")
    (manifest_snap / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    report_manifest = audit_graph_root(manifest_copy)
    assert report_manifest["ok"] is False
    assert "legacy_block_missing_with_fields" in _codes(report_manifest)

    # The disposable source graph is still clean and unmodified.
    assert audit_graph_root(graph)["ok"] is True
    assert snap.is_dir()


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_published_graph_health_on_disposable_graph(tmp_path: Path):
    """check_spec accepts an enabled shape overlay on a disposable C root."""
    from published_graph_health import PublishedGraphSpec, check_spec  # type: ignore

    graph = tmp_path / "byog_inih_shapes"
    _index(ROOT / "examples" / "inih", graph, shapes=True)
    spec = PublishedGraphSpec(
        ident="inih_tmp",
        source=Path("examples/inih"),
        graph=str(graph),
        indexer="c",
        mode="mutable",
    )
    result = check_spec(spec, root=ROOT, graph_root=graph)
    assert result["status"] == "pass", result
    shape = result["clang_type_shape_integrity"]
    assert shape["ok"] is True
    assert shape["status"] == "enabled"
    assert shape["n_decorated_entities"] == 0
