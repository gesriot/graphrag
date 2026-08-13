"""Read-only integrity audit for persisted configured Clang type-declaration fields.

Pure synthetic/corruption tests always run. Live disposable-graph tests skip
when no C compiler is present. Nothing here writes a published byog_* root.

Run:
  uv run python -m pytest examples/cjson/tests/test_c_clang_type_graph_audit.py -q
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

from c_clang_type_graph_audit import (  # type: ignore
    AUDIT_MODE,
    ClangTypeGraphAuditError,
    audit_graph_root,
    audit_rows,
    audit_to_json,
    format_report,
    main as audit_main,
    read_only_fingerprint,
    resolve_snapshot,
)
from c_clang_types import (  # type: ignore
    EXTRACTOR,
    FACT_KIND,
    MODE,
    apply_clang_types_from_report,
    build_disabled_provenance,
)
from c_preprocessor import find_c_compiler  # type: ignore

DIGEST = "abc123"
COMPILER_PATH = "/usr/bin/clang"
COMPILER_ID = "Apple clang version test"
COMPILER_VERSION = "17.0.0"

_FAIL_CLOSED_BUCKETS = (
    "tree_sitter_only",
    "clang_only",
    "ambiguous",
    "macro_location_unsupported",
)

_OBSERVATION_ONLY_BUCKETS = (
    "out_of_compile_db_scope",
    "anonymous_declarations",
    "unsupported_declarations",
    "outside_package_declarations",
    "alternate_declaration_sites",
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


def _matched_row(
    *,
    title: str,
    source_path: str,
    span: str,
    entity_kind: str = "struct",
    line: int = 1,
    col0: int = 0,
    matched_span: str | None = None,
    matched_line: int | None = None,
    matched_col0: int | None = None,
    qual: str | None = "int",
    desugared: str | None = None,
    fixed: str | None = None,
) -> dict:
    matched_span = matched_span if matched_span is not None else span
    matched_line = matched_line if matched_line is not None else line
    matched_col0 = matched_col0 if matched_col0 is not None else col0
    same = (
        matched_span == span
        and matched_line == line
        and matched_col0 == col0
    )
    return {
        "entity_kind": entity_kind,
        "name": title.rsplit(":", 1)[-1],
        "source_path": source_path,
        "tree_sitter_title": title,
        "graph_canonical_span": span,
        "graph_canonical_line": line,
        "graph_canonical_col0": col0,
        "graph_canonical_is_matched_site": same,
        "matched_site_span": matched_span,
        "matched_site_line": matched_line,
        "matched_site_col0": matched_col0,
        "matched_site_is_canonical": same,
        "tree_sitter_line": matched_line,
        "tree_sitter_col": matched_col0,
        "clang_line": matched_line,
        "clang_col0": matched_col0,
        "line_column_confirmed": True,
        "tag_kind": None,
        "is_complete": True,
        "qualType": qual,
        "desugaredQualType": desugared,
        "fixedUnderlyingType": fixed,
        "location_origin": "direct",
        "entry_indices": [0],
        "compiler_path": COMPILER_PATH,
        "compiler_id": COMPILER_ID,
        "compile_commands_digest": DIGEST,
        "compilers": [
            {
                "compiler_path": COMPILER_PATH,
                "compiler_id": COMPILER_ID,
                "compile_commands_digest": DIGEST,
            }
        ],
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


def _type_report(matched: list, **counts_extra) -> dict:
    counts = {
        "matched": len(matched),
        "out_of_compile_db_scope": 0,
        "anonymous_declarations": 0,
        "unsupported_declarations": 0,
        "outside_package_declarations": 0,
        "alternate_declaration_sites": 0,
    }
    for bucket in _FAIL_CLOSED_BUCKETS:
        counts[bucket] = 0
    counts.update(counts_extra)
    report = {"mode": "clang_ast_json_type_declaration_audit"}
    report.update(_provenance())
    report.update(
        {
            "counts": counts,
            "matched": matched,
            "out_of_compile_db_scope": [],
            "anonymous_declarations": [],
            "unsupported_declarations": [],
            "outside_package_declarations": [],
            "alternate_declaration_sites": [],
            "limitations": [],
            "confidence_boundary": "test",
        }
    )
    for bucket in _FAIL_CLOSED_BUCKETS:
        report[bucket] = []
    return report


def _cjson_style(tmp_path: Path):
    """cJSON-style mix: colliding struct/typedef plus another struct."""
    pkg = tmp_path / "pkg"
    pkg.mkdir(parents=True, exist_ok=True)
    src = pkg / "a.c"
    src.write_text("struct S { int x; };\ntypedef struct S S;\n", encoding="utf-8")
    specs = [
        ("a:struct:cJSON", "struct", "1:0-4:1", "struct cJSON"),
        ("a:typedef:cJSON", "typedef", "5:0-5:20", "struct cJSON"),
        ("a:struct:cJSON_Hooks", "struct", "7:0-10:1", "struct cJSON_Hooks"),
    ]
    entities = [
        _entity(title=title, etype=etype, span=span, source_file=str(src))
        for title, etype, span, _ in specs
    ]
    entities.append(
        _entity(title="a:f", etype="function", span="20:0-21:1", source_file=str(src))
    )
    data = {"entities": entities, "relationships": []}
    report = _type_report(
        [
            _matched_row(
                title=title,
                source_path="a.c",
                span=span,
                entity_kind=etype,
                line=int(span.split(":")[0]),
                qual=qual,
            )
            for title, etype, span, qual in specs
        ]
    )
    block = apply_clang_types_from_report(data, report, pkg)
    manifest = {
        "counts": {"entities": len(entities)},
        "clang_types": block,
    }
    return data["entities"], manifest


def _inih_style(tmp_path: Path):
    """inih-style: three typedefs; ini_handler has alternate matched site."""
    pkg = tmp_path / "pkg"
    pkg.mkdir(parents=True, exist_ok=True)
    src = pkg / "a.c"
    src.write_text("typedef int T;\n", encoding="utf-8")
    specs = [
        ("a:ini_parse_string_ctx", "50:0-50:40", 50, 50, "50:0-50:40"),
        ("a:ini_handler", "58:0-58:80", 58, 62, "62:0-62:80"),
        ("a:ini_reader", "70:0-70:30", 70, 70, "70:0-70:30"),
    ]
    entities = [
        _entity(
            title=title,
            etype="typedef",
            span=span,
            source_file=str(src),
        )
        for title, span, _canon, _matched, _mspan in specs
    ]
    data = {"entities": entities, "relationships": []}
    report = _type_report(
        [
            _matched_row(
                title=title,
                source_path="a.c",
                span=span,
                entity_kind="typedef",
                line=canon,
                matched_span=mspan,
                matched_line=matched,
                qual="int",
            )
            for title, span, canon, matched, mspan in specs
        ]
    )
    block = apply_clang_types_from_report(data, report, pkg)
    # Persisted observation-only count (the overlay stores counts, not rows).
    block["counts"]["alternate_declaration_sites"] = 1
    manifest = {
        "counts": {"entities": len(entities)},
        "clang_types": block,
    }
    return data["entities"], manifest


def _off_graph():
    entities = [
        _entity(title="a:struct:S", etype="struct", span="1:0-4:1", source_file="a.c")
    ]
    manifest = {
        "counts": {"entities": len(entities)},
        "clang_types": build_disabled_provenance(),
    }
    return entities, manifest


def _legacy_graph():
    entities = [
        _entity(title="a:struct:S", etype="struct", span="1:0-4:1", source_file="a.c")
    ]
    return entities, {"counts": {"entities": len(entities)}}


def _decorated(entities: list) -> dict:
    return next(e for e in entities if e.get("clang_type_fact_kind") == FACT_KIND)


def _named(entities: list, title: str) -> dict:
    return next(e for e in entities if e.get("title") == title)


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
    bare = audit_rows(entities, None)
    assert bare["ok"] is True
    assert bare["status"] == "legacy_absent"


def test_explicit_off_passes():
    entities, manifest = _off_graph()
    report = audit_rows(entities, manifest)
    assert report["ok"] is True
    assert report["status"] == "off"
    assert report["n_decorated_entities"] == 0
    assert manifest["clang_types"] == {
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
    assert report["counts"]["matched"] == 3
    assert report["provenance"]["compile_commands_digest"] == DIGEST
    assert report["provenance"]["n_compile_entries"] == 1
    assert report["fact_kind"] == FACT_KIND
    assert report["extractor"] == EXTRACTOR
    assert report["overlay_mode"] == MODE
    kinds = {
        e["title"]: e["clang_type_entity_kind"]
        for e in entities
        if e.get("clang_type_declaration_confirmed") is True
    }
    assert kinds["a:struct:cJSON"] == "struct"
    assert kinds["a:typedef:cJSON"] == "typedef"


def test_enabled_inih_style_passes(tmp_path: Path):
    entities, manifest = _inih_style(tmp_path)
    report = audit_rows(entities, manifest)
    assert report["ok"] is True, report["anomalies"]
    assert report["status"] == "enabled"
    assert report["n_decorated_entities"] == 3
    assert report["counts"]["alternate_declaration_sites"] == 1
    handler = _named(entities, "a:ini_handler")
    assert handler["clang_type_graph_canonical_line"] == 58
    assert handler["clang_type_matched_site_line"] == 62
    assert handler["clang_type_matched_site_is_canonical"] is False
    assert handler["type"] == "typedef"
    assert all(e["type"] == "typedef" for e in entities)


# ---------------------------------------------------------------------------
# Fail-closed states
# ---------------------------------------------------------------------------


def test_explicit_null_block_fails(tmp_path: Path):
    entities, _manifest = _legacy_graph()
    report = audit_rows(entities, {"counts": {"entities": 1}, "clang_types": None})
    assert report["ok"] is False
    assert report["status"] == "invalid"
    assert "invalid_enabled_block" in _codes(report)

    for value in ([], "off", 0):
        report_bad = audit_rows(
            entities, {"counts": {"entities": 1}, "clang_types": value}
        )
        assert report_bad["ok"] is False, value
        assert "invalid_enabled_block" in _codes(report_bad), value


def test_missing_manifest_with_fields_fails(tmp_path: Path):
    entities, _manifest = _cjson_style(tmp_path)
    report = audit_rows(entities, {"counts": {"entities": len(entities)}})
    assert report["ok"] is False
    assert report["status"] == "invalid"
    assert "legacy_block_missing_with_fields" in _codes(report)
    none_manifest = audit_rows(entities, None)
    assert none_manifest["ok"] is False
    assert "legacy_block_missing_with_fields" in _codes(none_manifest)
    assert none_manifest["violations"] == none_manifest["anomalies"]


def test_off_manifest_with_fields_fails(tmp_path: Path):
    entities, _manifest = _cjson_style(tmp_path)
    manifest = {
        "counts": {"entities": len(entities)},
        "clang_types": build_disabled_provenance(),
    }
    report = audit_rows(entities, manifest)
    assert report["ok"] is False
    assert report["status"] == "invalid"
    assert "off_with_decorated_entities" in _codes(report)


def test_inconsistent_enablement_fails(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    for mode, enabled in ((MODE, False), ("off", True), ("weird", True), (MODE, None)):
        broken = copy.deepcopy(manifest)
        broken["clang_types"]["mode"] = mode
        broken["clang_types"]["enabled"] = enabled
        report = audit_rows(entities, broken)
        assert report["ok"] is False, (mode, enabled)
        assert "invalid_enabled_block" in _codes(report)


def test_partial_entity_payload_fails(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    for field in (
        "clang_type_declaration_confirmed",
        "clang_type_fact_kind",
        "clang_type_extractor",
        "clang_type_entity_kind",
        "clang_type_graph_canonical_span",
        "clang_type_entry_indices",
        "clang_type_qual_type",
        "clang_type_desugared_qual_type",
        "clang_type_fixed_underlying_type",
        "clang_type_compiler_path",
        "clang_type_compiler_id",
        "clang_type_compilers",
        "clang_type_description",
        "clang_type_compile_commands_digest",
    ):
        rows = copy.deepcopy(entities)
        _decorated(rows).pop(field)
        report = audit_rows(rows, manifest)
        assert report["ok"] is False, field
        assert "partial_type_payload" in _codes(report), field


def test_null_required_payload_field_is_partial(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    rows = copy.deepcopy(entities)
    _decorated(rows)["clang_type_compilers"] = None
    report = audit_rows(rows, manifest)
    assert report["ok"] is False
    assert "partial_type_payload" in _codes(report)


def test_unknown_type_field_fails(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    rows = copy.deepcopy(entities)
    _decorated(rows)["clang_type_abi_layout_proof"] = True
    report = audit_rows(rows, manifest)
    assert report["ok"] is False
    assert "unknown_type_field" in _codes(report)


def test_non_type_decorated_entity_fails(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    rows = copy.deepcopy(entities)
    payload = {
        key: value
        for key, value in _decorated(rows).items()
        if str(key).startswith("clang_type_")
    }
    function = next(e for e in rows if e["type"] == "function")
    function.update(payload)
    report = audit_rows(rows, manifest)
    assert report["ok"] is False
    assert "stale_type_metadata" in _codes(report)


def test_duplicate_and_empty_entity_id_fails(tmp_path: Path):
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


def test_entity_kind_mismatch_fails(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    rows = copy.deepcopy(entities)
    _decorated(rows)["clang_type_entity_kind"] = "enum"
    report = audit_rows(rows, manifest)
    assert report["ok"] is False
    assert "identity_mismatch" in _codes(report)


def test_canonical_span_mismatch_fails(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    rows = copy.deepcopy(entities)
    _decorated(rows)["span"] = "99:0-99:1"
    report = audit_rows(rows, manifest)
    assert report["ok"] is False
    assert "canonical_span_mismatch" in _codes(report)


def test_graph_span_line_col_mismatch_fails(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    rows = copy.deepcopy(entities)
    _decorated(rows)["clang_type_graph_canonical_line"] = 99
    report = audit_rows(rows, manifest)
    assert report["ok"] is False
    assert "graph_span_mismatch" in _codes(report)

    col_rows = copy.deepcopy(entities)
    _decorated(col_rows)["clang_type_graph_canonical_col0"] = 9
    report_col = audit_rows(col_rows, manifest)
    assert report_col["ok"] is False
    assert "graph_span_mismatch" in _codes(report_col)


def test_matched_span_line_col_mismatch_fails(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    rows = copy.deepcopy(entities)
    _decorated(rows)["clang_type_matched_site_line"] = 99
    report = audit_rows(rows, manifest)
    assert report["ok"] is False
    assert "matched_span_mismatch" in _codes(report)

    col_rows = copy.deepcopy(entities)
    _decorated(col_rows)["clang_type_matched_site_col0"] = 9
    report_col = audit_rows(col_rows, manifest)
    assert report_col["ok"] is False
    assert "matched_span_mismatch" in _codes(report_col)


def test_incorrect_canonical_site_marker_fails(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    rows = copy.deepcopy(entities)
    target = _decorated(rows)
    assert target["clang_type_matched_site_is_canonical"] is True
    target["clang_type_matched_site_is_canonical"] = False
    report = audit_rows(rows, manifest)
    assert report["ok"] is False
    assert "canonical_site_marker" in _codes(report)

    inih_entities, inih_manifest = _inih_style(tmp_path)
    handler = _named(inih_entities, "a:ini_handler")
    handler["clang_type_matched_site_is_canonical"] = True
    report_inih = audit_rows(inih_entities, inih_manifest)
    assert report_inih["ok"] is False
    assert "canonical_site_marker" in _codes(report_inih)


def test_invalid_optional_type_string_forms_fail(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    for field, value in (
        ("clang_type_qual_type", ""),
        ("clang_type_qual_type", "   "),
        ("clang_type_qual_type", 7),
        ("clang_type_desugared_qual_type", ""),
        ("clang_type_desugared_qual_type", []),
        ("clang_type_fixed_underlying_type", ""),
        ("clang_type_fixed_underlying_type", 1),
    ):
        rows = copy.deepcopy(entities)
        _decorated(rows)[field] = value
        report = audit_rows(rows, manifest)
        assert report["ok"] is False, (field, value)
        assert "optional_type_string" in _codes(report), (field, value)


def test_null_optional_type_strings_are_accepted(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    rows = copy.deepcopy(entities)
    target = _decorated(rows)
    target["clang_type_qual_type"] = None
    target["clang_type_desugared_qual_type"] = None
    target["clang_type_fixed_underlying_type"] = None
    report = audit_rows(rows, manifest)
    assert report["ok"] is True, report["anomalies"]


def test_invalid_confidence_and_determinism_markers_fail(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    for field, value in (
        ("clang_type_declaration_confirmed", False),
        ("clang_type_declaration_confirmed", "true"),
        ("clang_type_is_deterministic", False),
        ("clang_type_is_deterministic", "yes"),
        ("clang_type_confidence", 0.5),
        ("clang_type_confidence", "1.0"),
        ("clang_type_matched_site_is_canonical", "yes"),
        ("clang_type_graph_canonical_line", 0),
        ("clang_type_matched_site_col0", -1),
        ("clang_type_location_origin", "  "),
        ("clang_type_matched_site_span", ""),
    ):
        rows = copy.deepcopy(entities)
        _decorated(rows)[field] = value
        report = audit_rows(rows, manifest)
        assert report["ok"] is False, (field, value)
        assert _codes(report) & {
            "type_field_type",
            "canonical_site_marker",
            "graph_span_mismatch",
            "matched_span_mismatch",
        }, (field, value, _codes(report))


def test_integral_float_scalars_are_accepted(tmp_path: Path):
    """Parquet widens int columns with nulls to float64; that is not corruption."""
    entities, manifest = _cjson_style(tmp_path)
    rows = copy.deepcopy(entities)
    target = _decorated(rows)
    target["clang_type_graph_canonical_line"] = float(
        target["clang_type_graph_canonical_line"]
    )
    target["clang_type_graph_canonical_col0"] = float(
        target["clang_type_graph_canonical_col0"]
    )
    target["clang_type_matched_site_line"] = float(
        target["clang_type_matched_site_line"]
    )
    target["clang_type_matched_site_col0"] = float(
        target["clang_type_matched_site_col0"]
    )
    target["clang_type_entry_indices"] = [
        float(i) for i in target["clang_type_entry_indices"]
    ]
    report = audit_rows(rows, manifest)
    assert report["ok"] is True, report["anomalies"]


# ---------------------------------------------------------------------------
# Compiler JSON / provenance
# ---------------------------------------------------------------------------


def test_invalid_non_canonical_compiler_json_fails(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    for value in (
        "not json",
        json.dumps(
            [
                {
                    "compiler_path": COMPILER_PATH,
                    "compiler_id": COMPILER_ID,
                    "compile_commands_digest": DIGEST,
                }
            ],
            indent=2,
        ),
        json.dumps(
            [
                {
                    "compiler_path": COMPILER_PATH,
                    "compiler_id": COMPILER_ID,
                    "compile_commands_digest": DIGEST,
                }
            ],
            separators=(", ", ": "),
        ),
    ):
        rows = copy.deepcopy(entities)
        _decorated(rows)["clang_type_compilers"] = value
        report = audit_rows(rows, manifest)
        assert report["ok"] is False, value
        assert "compiler_json" in _codes(report), (value, _codes(report))

    empty_rows = copy.deepcopy(entities)
    _decorated(empty_rows)["clang_type_compilers"] = "[]"
    empty_report = audit_rows(empty_rows, manifest)
    assert empty_report["ok"] is False
    assert _codes(empty_report) & {"compiler_json", "compiler_mismatch"}

    object_rows = copy.deepcopy(entities)
    _decorated(object_rows)["clang_type_compilers"] = (
        '{"compiler_path":"/usr/bin/clang"}'
    )
    object_report = audit_rows(object_rows, manifest)
    assert object_report["ok"] is False
    assert _codes(object_report) & {"compiler_json", "compiler_mismatch"}


def test_nan_infinity_and_duplicate_json_keys_are_refused(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    for raw in (
        '[{"compile_commands_digest":"abc123","compiler_id":"x",'
        '"compiler_path":NaN}]',
        '[{"compile_commands_digest":"abc123","compiler_id":"x",'
        '"compiler_path":Infinity}]',
        '[{"compile_commands_digest":"abc123","compiler_id":"Apple clang '
        'version test","compiler_path":"/usr/bin/clang","compiler_path":'
        '"/usr/bin/other"}]',
    ):
        rows = copy.deepcopy(entities)
        _decorated(rows)["clang_type_compilers"] = raw
        report = audit_rows(rows, manifest)
        assert report["ok"] is False, raw
        assert "compiler_json" in _codes(report), (raw, _codes(report))


def test_entry_indices_unsorted_duplicate_or_outside_census_fail(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    for value in ([1], [1, 0], [0, 0], [], ["0"]):
        rows = copy.deepcopy(entities)
        _decorated(rows)["clang_type_entry_indices"] = value
        report = audit_rows(rows, manifest)
        assert report["ok"] is False, value
        assert "entry_index_census" in _codes(report), value


def test_compiler_digest_and_singular_field_mismatch_fails(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)

    digest_rows = copy.deepcopy(entities)
    _decorated(digest_rows)["clang_type_compile_commands_digest"] = "zzz"
    digest_report = audit_rows(digest_rows, manifest)
    assert digest_report["ok"] is False
    assert "digest_mismatch" in _codes(digest_report)

    other_rows = copy.deepcopy(entities)
    target = _decorated(other_rows)
    target["clang_type_compilers"] = json.dumps(
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
    _decorated(singular_rows)["clang_type_compiler_id"] = "disagreeing id"
    singular_report = audit_rows(singular_rows, manifest)
    assert singular_report["ok"] is False
    assert "compiler_mismatch" in _codes(singular_report)

    manifest_rows = copy.deepcopy(entities)
    bad_manifest = copy.deepcopy(manifest)
    bad_manifest["clang_types"]["compile_commands_digest"] = "different"
    manifest_report = audit_rows(manifest_rows, bad_manifest)
    assert manifest_report["ok"] is False
    assert "digest_mismatch" in _codes(manifest_report)


def test_manifest_fact_and_count_mismatch_fails(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    for field, value in (
        ("n_facts", 2),
        ("n_facts_changed", 99),
        ("n_translation_units", 2),
        ("n_compile_entries", 0),
    ):
        broken = copy.deepcopy(manifest)
        broken["clang_types"][field] = value
        report = audit_rows(entities, broken)
        assert report["ok"] is False, field
        assert "manifest_count_mismatch" in _codes(report), field

    counts = copy.deepcopy(manifest)
    counts["clang_types"]["counts"]["matched"] = 2
    report_counts = audit_rows(entities, counts)
    assert report_counts["ok"] is False
    assert "manifest_count_mismatch" in _codes(report_counts)

    entities_count = copy.deepcopy(manifest)
    entities_count["counts"]["entities"] = 999
    report_entities = audit_rows(entities, entities_count)
    assert report_entities["ok"] is False
    assert "manifest_count_mismatch" in _codes(report_entities)


def test_nonzero_fail_closed_count_fails(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    for bucket in _FAIL_CLOSED_BUCKETS:
        broken = copy.deepcopy(manifest)
        broken["clang_types"]["counts"][bucket] = 1
        report = audit_rows(entities, broken)
        assert report["ok"] is False, bucket
        assert "residual_bucket_nonzero" in _codes(report), bucket


def test_negative_observation_only_count_fails(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    ok_manifest = copy.deepcopy(manifest)
    counts = ok_manifest["clang_types"]["counts"]
    counts["outside_package_declarations"] = 212
    report = audit_rows(entities, ok_manifest)
    assert report["ok"] is True, report["anomalies"]
    assert report["counts"]["outside_package_declarations"] == 212

    for bucket in _OBSERVATION_ONLY_BUCKETS:
        negative = copy.deepcopy(manifest)
        negative["clang_types"]["counts"][bucket] = -1
        report_negative = audit_rows(entities, negative)
        assert report_negative["ok"] is False, bucket
        assert "manifest_count_mismatch" in _codes(report_negative), bucket


def test_invalid_compiler_provenance_fails(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    relative = copy.deepcopy(manifest)
    relative["clang_types"]["compilers"][0]["compiler_path"] = "clang"
    report_rel = audit_rows(entities, relative)
    assert report_rel["ok"] is False
    assert "manifest_identity_mismatch" in _codes(report_rel)

    empty = copy.deepcopy(manifest)
    empty["clang_types"]["compilers"] = []
    report_empty = audit_rows(entities, empty)
    assert report_empty["ok"] is False
    assert "manifest_identity_mismatch" in _codes(report_empty)

    dup = copy.deepcopy(manifest)
    only = copy.deepcopy(dup["clang_types"]["compilers"][0])
    dup["clang_types"]["compilers"] = [only, copy.deepcopy(only)]
    report_dup = audit_rows(entities, dup)
    assert report_dup["ok"] is False
    assert "manifest_identity_mismatch" in _codes(report_dup)

    singular = copy.deepcopy(manifest)
    singular["clang_types"]["compiler_id"] = "other"
    report_singular = audit_rows(entities, singular)
    assert report_singular["ok"] is False
    assert "manifest_identity_mismatch" in _codes(report_singular)


def test_forbidden_abi_layout_type_use_claims_fail(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    for text in (
        "configured Clang type declaration for X; proves the ABI of this "
        "struct; deterministic only relative to recorded Clang + "
        "compile_commands.json",
        "configured Clang type declaration for X; layout-compatible with "
        "Rust; deterministic only relative to recorded Clang + "
        "compile_commands.json",
        "configured Clang type declaration for X; type-use proof of every "
        "site; deterministic only relative to recorded Clang + "
        "compile_commands.json",
        "configured Clang type declaration for X; multi-config proof; "
        "deterministic only relative to recorded Clang + "
        "compile_commands.json",
        "configured Clang type declaration for X; macro-complete; "
        "deterministic only relative to recorded Clang + "
        "compile_commands.json",
    ):
        rows = copy.deepcopy(entities)
        _decorated(rows)["clang_type_description"] = text
        report = audit_rows(rows, manifest)
        assert report["ok"] is False, text
        assert "forbidden_claim" in _codes(report), text

    claimed = copy.deepcopy(manifest)
    claimed["clang_types"]["confidence_boundary"] = "this is ABI proof"
    report_manifest = audit_rows(entities, claimed)
    assert report_manifest["ok"] is False
    assert "manifest_contract_claim" in _codes(report_manifest)
    assert "forbidden_claim" in _codes(report_manifest)


# ---------------------------------------------------------------------------
# Determinism / read-only / CLI
# ---------------------------------------------------------------------------


def test_output_is_deterministic(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    first = audit_to_json(audit_rows(entities, manifest))
    second = audit_to_json(audit_rows(copy.deepcopy(entities), copy.deepcopy(manifest)))
    assert first == second
    assert json.loads(first)["ok"] is True
    broken = copy.deepcopy(entities)
    _decorated(broken)["clang_type_confidence"] = 0.5
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
        manifest["clang_types"] = manifest_block
    (snap / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    (graph / "current").write_text("s1", encoding="utf-8")
    return graph


def test_graph_root_audit_is_byte_for_byte_read_only(tmp_path: Path):
    entities, manifest = _cjson_style(tmp_path)
    graph = _publish(tmp_path, entities, manifest["clang_types"])
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
    good = _publish(tmp_path, entities, manifest["clang_types"], name="good")
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
    _decorated(bad_entities)["clang_type_confidence"] = 0.5
    bad = _publish(tmp_path, bad_entities, manifest["clang_types"], name="bad")
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
    graph = _publish(tmp_path, entities, manifest["clang_types"])
    before = {
        path.relative_to(graph).as_posix(): path.read_bytes()
        for path in sorted(graph.rglob("*"))
        if path.is_file()
    }
    forbidden = graph / "snapshots" / "s1" / "type-audit.json"

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
    with pytest.raises(ClangTypeGraphAuditError, match="malformed manifest"):
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
    from published_graph_health import (  # type: ignore
        _type_integrity,
        _type_shape_integrity,
        _type_use_integrity,
    )

    entities, manifest = _cjson_style(tmp_path)
    enabled = _type_integrity(
        {"entities": entities}, manifest, indexer="c"
    )
    assert enabled is not None
    assert enabled["ok"] is True
    assert enabled["status"] == "enabled"
    assert enabled["n_decorated_entities"] == 3

    legacy_entities, legacy_manifest = _legacy_graph()
    legacy = _type_integrity(
        {"entities": legacy_entities}, legacy_manifest, indexer="c"
    )
    assert legacy["ok"] is True and legacy["status"] == "legacy_absent"

    off_entities, off_manifest = _off_graph()
    off = _type_integrity(
        {"entities": off_entities}, off_manifest, indexer="c"
    )
    assert off["ok"] is True and off["status"] == "off"

    orphan = _type_integrity(
        {"entities": entities}, {"counts": {"entities": len(entities)}}, indexer="c"
    )
    assert orphan["ok"] is False
    assert orphan["n_anomalies"] >= 1

    assert (
        _type_integrity({"entities": entities}, manifest, indexer="python")
        is None
    )

    # Existing overlay integrity helpers stay independent.
    type_use = _type_use_integrity(
        {"entities": entities, "relationships": []},
        {"counts": {"entities": len(entities)}},
        indexer="c",
    )
    assert type_use is not None
    assert type_use["ok"] is True
    assert type_use["status"] == "legacy_absent"
    type_shape = _type_shape_integrity(
        {"entities": entities},
        {"counts": {"entities": len(entities)}},
        indexer="c",
    )
    assert type_shape is not None
    assert type_shape["ok"] is True
    assert type_shape["status"] == "legacy_absent"


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


def _index(package: Path, graph: Path, *, types: bool) -> None:
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
        clang_types=types,
        clang_type_uses=False,
        clang_type_shapes=False,
        allow_toolchain_drift=False,
    )


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_cjson_type_graph_audit_without_clang(tmp_path: Path, monkeypatch):
    pkg = ROOT / "examples" / "cjson"
    package_before = {p.name for p in pkg.iterdir()}
    graph = tmp_path / "byog_cjson_types"
    _index(pkg, graph, types=True)

    _fail_if_clang_used(monkeypatch)
    report = audit_graph_root(graph)
    assert report["ok"] is True, report["anomalies"]
    assert report["status"] == "enabled"
    assert report["n_decorated_entities"] == 10
    assert report["counts"]["matched"] == 10
    for bucket in _FAIL_CLOSED_BUCKETS:
        assert report["counts"][bucket] == 0
    assert report["read_only_verification"]["verified"] is True
    assert report["provenance"]["n_compile_entries"] == 1

    assert {p.name for p in pkg.iterdir()} == package_before
    for pattern in ("*.o", "*.ast", "*.d", "*.i"):
        assert not list(pkg.rglob(pattern))


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_inih_enabled_typedefs(tmp_path: Path, monkeypatch):
    pkg = ROOT / "examples" / "inih"
    graph = tmp_path / "byog_inih_types"
    _index(pkg, graph, types=True)

    snap = (graph / "current").read_text(encoding="utf-8").strip()
    snap_dir = graph / "snapshots" / snap
    block = json.loads(
        (snap_dir / "manifest.json").read_text(encoding="utf-8")
    )["clang_types"]
    assert block["mode"] == MODE and block["enabled"] is True
    assert block["n_facts"] == 3

    ents = pd.read_parquet(snap_dir / "entities.parquet")
    typed = ents[ents["clang_type_declaration_confirmed"] == True]  # noqa: E712
    assert len(typed) == 3
    assert set(typed["type"].astype(str)) == {"typedef"}
    handler = ents[ents["title"].astype(str).str.endswith("ini_handler")].iloc[0]
    assert int(handler["clang_type_graph_canonical_line"]) == 58
    assert int(handler["clang_type_matched_site_line"]) == 62
    assert bool(handler["clang_type_matched_site_is_canonical"]) is False

    _fail_if_clang_used(monkeypatch)
    report = audit_graph_root(graph)
    assert report["ok"] is True, report["anomalies"]
    assert report["status"] == "enabled"
    assert report["n_decorated_entities"] == 3
    assert report["read_only_verification"]["verified"] is True
    for pattern in ("*.o", "*.ast", "*.d", "*.i"):
        assert not list(pkg.rglob(pattern))


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_default_off_graph_passes(tmp_path: Path, monkeypatch):
    pkg = ROOT / "examples" / "inih"
    graph = tmp_path / "byog_inih_off"
    _index(pkg, graph, types=False)
    _fail_if_clang_used(monkeypatch)
    report = audit_graph_root(graph)
    assert report["ok"] is True
    assert report["status"] == "off"
    assert report["n_decorated_entities"] == 0


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_corrupted_temporary_copy_fails(tmp_path: Path):
    """Corrupt only a disposable copy; published byog_* roots are never touched."""
    pkg = ROOT / "examples" / "cjson"
    graph = tmp_path / "byog_cjson_types"
    _index(pkg, graph, types=True)
    snap_id = (graph / "current").read_text(encoding="utf-8").strip()
    snap = graph / "snapshots" / snap_id

    parquet_copy = tmp_path / "copy_entities"
    shutil.copytree(graph, parquet_copy)
    copy_snap = parquet_copy / "snapshots" / snap_id
    ents = pd.read_parquet(copy_snap / "entities.parquet")
    mask = ents["clang_type_fact_kind"].astype(str) == FACT_KIND
    assert int(mask.sum()) == 10
    ents.loc[ents.index[mask][0], "clang_type_confidence"] = 0.1
    ents.to_parquet(copy_snap / "entities.parquet")
    report_entities = audit_graph_root(parquet_copy)
    assert report_entities["ok"] is False
    assert "type_field_type" in _codes(report_entities)

    manifest_copy = tmp_path / "copy_manifest"
    shutil.copytree(graph, manifest_copy)
    manifest_snap = manifest_copy / "snapshots" / snap_id
    manifest = json.loads(
        (manifest_snap / "manifest.json").read_text(encoding="utf-8")
    )
    manifest.pop("clang_types")
    (manifest_snap / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    report_manifest = audit_graph_root(manifest_copy)
    assert report_manifest["ok"] is False
    assert "legacy_block_missing_with_fields" in _codes(report_manifest)

    assert audit_graph_root(graph)["ok"] is True
    assert snap.is_dir()


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_published_graph_health_on_disposable_graph(tmp_path: Path):
    """check_spec accepts an enabled type overlay on a disposable C root."""
    from published_graph_health import PublishedGraphSpec, check_spec  # type: ignore

    graph = tmp_path / "byog_inih_types"
    _index(ROOT / "examples" / "inih", graph, types=True)
    spec = PublishedGraphSpec(
        ident="inih_tmp",
        source=Path("examples/inih"),
        graph=str(graph),
        indexer="c",
        mode="mutable",
    )
    result = check_spec(spec, root=ROOT, graph_root=graph)
    assert result["status"] == "pass", result
    type_decl = result["clang_type_integrity"]
    assert type_decl["ok"] is True
    assert type_decl["status"] == "enabled"
    assert type_decl["n_decorated_entities"] == 3
    assert result["clang_type_use_integrity"]["ok"] is True
    assert result["clang_type_shape_integrity"]["ok"] is True
