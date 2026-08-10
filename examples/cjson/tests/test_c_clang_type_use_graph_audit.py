"""Read-only integrity audit for persisted configured uses_type edges.

Pure corruption tests always run. Live Clang/inih smoke skips when no compiler.

Run:
  uv run python -m pytest examples/cjson/tests/test_c_clang_type_use_graph_audit.py -q
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

from byog_graph import publish_byog_snapshot  # type: ignore
from c_clang_type_use_graph_audit import (  # type: ignore
    AUDIT_MODE,
    ClangTypeUseGraphAuditError,
    audit_graph_root,
    audit_rows,
    audit_to_json,
    format_report,
    main as audit_main,
    resolve_current_snapshot,
)
from c_clang_type_uses import (  # type: ignore
    FACT_KIND,
    REL_TYPE,
    TYPE_USE_FIELDS,
    apply_clang_type_uses_from_report,
    build_disabled_provenance,
    relationship_id,
    _relationship_id,
    validate_persisted_type_use_overlay,
)
from c_clang_type_use_audit import MODE as AUDIT_MODE_DIAG  # type: ignore
from c_preprocessor import find_c_compiler  # type: ignore


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _entity(
    *,
    title: str,
    etype: str,
    eid: str | None = None,
    **extra,
) -> dict:
    e = {
        "id": eid or f"ent:{etype}:{title}",
        "title": title,
        "type": etype,
        "description": f"{etype} {title}",
        "source_file": "a.c",
        "span": "1:0-2:0",
        "extractor": "tree-sitter-c",
        "confidence": 1.0,
        "is_deterministic": True,
        "document_ids": ["doc:a"],
        "text_unit_ids": [],
        "symbol_name": title.rsplit(":", 1)[-1],
    }
    e.update(extra)
    return e


def _matched_row(
    *,
    owner_title: str,
    target_title: str,
    owner_kind: str = "function",
    target_kind: str = "typedef",
    use_kind: str = "parameter",
    qual: str = "T",
    source_path: str = "a.c",
    line: int | None = 1,
    col0: int | None = 0,
    entry_indices: list | None = None,
    resolver: str = "unique_typedef_spelling",
    owner_resolver: str = "exact_declaration_site",
    **extra,
) -> dict:
    name_owner = owner_title.rsplit(":", 1)[-1]
    name_target = target_title.rsplit(":", 1)[-1]
    indices = entry_indices or [0]
    row = {
        "classification": "matched_internal",
        "use_kind": use_kind,
        "owner_kind": owner_kind,
        "owner_name": name_owner,
        "owner_tree_sitter_title": owner_title,
        "owner_resolver": owner_resolver,
        "target_entity_kind": target_kind,
        "target_name": name_target,
        "target_tree_sitter_title": target_title,
        "qualType": qual,
        "desugaredQualType": None,
        "resolver": resolver,
        "source_path": source_path,
        "line": line,
        "col0": col0,
        "byte_offset": 10 if line is not None else None,
        "location_origin": "direct",
        "location_precision": "declaration_bearing_node",
        "entry_indices": indices,
        "compiler_path": "/usr/bin/clang",
        "compiler_id": "Apple clang version test",
        "compile_commands_digest": "abc123",
        "compilers": [
            {
                "compiler_path": "/usr/bin/clang",
                "compiler_id": "Apple clang version test",
                "compile_commands_digest": "abc123",
            }
        ],
    }
    row.update(extra)
    return row


def _clean_report(matched: list, **counts_extra) -> dict:
    counts = {
        "matched_internal": len(matched),
        "owner_unmatched": 0,
        "target_unresolved": 0,
        "ambiguous_target": 0,
        "macro_location_unsupported": 0,
        "external_or_system": 0,
        "unsupported_type_form": 0,
        "unowned_context": 0,
    }
    counts.update(counts_extra)
    counts["type_uses_deduped_total"] = sum(
        counts[key]
        for key in (
            "matched_internal",
            "owner_unmatched",
            "target_unresolved",
            "ambiguous_target",
            "macro_location_unsupported",
            "external_or_system",
            "unsupported_type_form",
            "unowned_context",
        )
    )
    counts["type_uses_raw_observations"] = counts["type_uses_deduped_total"]
    return {
        "mode": AUDIT_MODE_DIAG,
        "package": "pkg",
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
        "matched_internal": matched,
        "owner_unmatched": [],
        "target_unresolved": [],
        "ambiguous_target": [],
        "macro_location_unsupported": [],
        "external_or_system": [],
        "unsupported_type_form": [],
        "unowned_context": [],
        "confidence_boundary": "test",
    }


def _base_entities() -> list:
    return [
        _entity(title="a:f", etype="function"),
        _entity(title="a:T", etype="typedef"),
        _entity(title="a:a.c", etype="file", eid="ent:file:a:a.c"),
    ]


def _contains_rel() -> dict:
    return {
        "id": "rel:contains:a:f",
        "source": "a:a.c",
        "target": "a:f",
        "type": "contains",
        "description": "contains",
        "weight": 1.0,
        "text_unit_ids": [],
        "human_readable_id": 1,
        "source_file": "",
        "span": "",
        "extractor": "tree-sitter-c",
        "confidence": 1.0,
        "is_deterministic": True,
        "document_ids": ["doc:a"],
        "covariate_ids": [],
    }


def _valid_enabled_graph(
    *,
    self_edge: bool = False,
    extra_matched: list | None = None,
) -> tuple[list, list, dict]:
    """Return (entities, relationships, manifest) with one valid configured edge."""
    if self_edge:
        entities = [
            _entity(title="a:struct:T", etype="struct"),
            _entity(title="a:a.c", etype="file", eid="ent:file:a:a.c"),
        ]
        matched = [
            _matched_row(
                owner_title="a:struct:T",
                target_title="a:struct:T",
                owner_kind="struct",
                target_kind="struct",
                use_kind="field",
                qual="struct T *",
                resolver="exact_tag_spelling",
            )
        ]
    else:
        entities = _base_entities()
        matched = [
            _matched_row(owner_title="a:f", target_title="a:T", qual="T")
        ]
    if extra_matched:
        matched = list(matched) + list(extra_matched)
    data = {
        "entities": entities,
        "relationships": [_contains_rel()],
        "text_units": [],
    }
    # Fix contains endpoints when self-edge graph has no a:f
    if self_edge:
        data["relationships"][0]["target"] = "a:struct:T"
        data["relationships"][0]["id"] = "rel:contains:a:struct:T"
    pkg = Path("pkg")
    report = _clean_report(matched)
    if len(matched) != 1:
        report["counts"]["matched_internal"] = len(matched)
        report["counts"]["type_uses_deduped_total"] = len(matched)
        report["counts"]["type_uses_raw_observations"] = len(matched)
        report["matched_internal"] = matched
    prov = apply_clang_type_uses_from_report(data, report, pkg)
    manifest = {
        "counts": {
            "entities": len(data["entities"]),
            "relationships": len(data["relationships"]),
            "text_units": 0,
        },
        "clang_type_uses": prov,
    }
    return data["entities"], data["relationships"], manifest


def _codes(result: dict) -> list[str]:
    return [a["code"] for a in result["anomalies"]]


def _find_uses(rels: list) -> dict:
    for r in rels:
        if r.get("type") == REL_TYPE and r.get("fact_kind") == FACT_KIND:
            return r
    raise AssertionError("no configured uses_type edge")


# ---------------------------------------------------------------------------
# Producer helper byte identity
# ---------------------------------------------------------------------------


def test_relationship_id_public_alias_byte_identical():
    args = ("a:f", "a:T", "ent:function:a:f", "ent:typedef:a:T")
    assert relationship_id(*args) == _relationship_id(*args)
    assert relationship_id(*args).startswith("rel:uses_type:")
    # Distinct entity ids → distinct digests (regression for public surface).
    assert relationship_id("a:f", "a:T", "e1", "t1") != relationship_id(
        "a:f", "a:T", "e2", "t1"
    )


def test_type_use_fields_tuple_is_stable():
    assert "clang_type_use_observations_json" in TYPE_USE_FIELDS
    assert "clang_type_use_source_entity_id" in TYPE_USE_FIELDS


# ---------------------------------------------------------------------------
# Compatibility modes
# ---------------------------------------------------------------------------


def test_valid_legacy_graph():
    entities = _base_entities()
    rels = [_contains_rel()]
    result = validate_persisted_type_use_overlay(entities, rels, {})
    assert result["ok"] is True
    assert result["status"] == "legacy_absent"
    assert result["n_configured_edges"] == 0
    assert result["n_anomalies"] == 0
    # Inputs not mutated.
    assert rels == [_contains_rel()]


def test_valid_off_graph():
    entities = _base_entities()
    rels = [_contains_rel()]
    manifest = {"clang_type_uses": build_disabled_provenance()}
    result = validate_persisted_type_use_overlay(entities, rels, manifest)
    assert result["ok"] is True
    assert result["status"] == "off"
    assert result["n_anomalies"] == 0


def test_malformed_enablement_values_fail_without_crashing():
    result = validate_persisted_type_use_overlay(
        _base_entities(),
        [_contains_rel()],
        {"clang_type_uses": {"mode": [], "enabled": []}},
    )
    assert result["ok"] is False
    assert "invalid_enabled_block" in _codes(result)


def test_off_manifest_with_stale_configured_edge():
    entities, rels, _ = _valid_enabled_graph()
    manifest = {"clang_type_uses": build_disabled_provenance()}
    result = validate_persisted_type_use_overlay(entities, rels, manifest)
    assert result["ok"] is False
    assert "off_with_configured_edges" in _codes(result)


def test_legacy_missing_block_never_legitimizes_edges():
    entities, rels, _ = _valid_enabled_graph()
    result = validate_persisted_type_use_overlay(entities, rels, {})
    assert result["ok"] is False
    assert "legacy_block_missing_with_edges" in _codes(result)


def test_enabled_manifest_with_valid_normal_edge():
    entities, rels, manifest = _valid_enabled_graph()
    before = json.dumps(rels, sort_keys=True)
    result = validate_persisted_type_use_overlay(entities, rels, manifest)
    assert result["ok"] is True
    assert result["status"] == "enabled"
    assert result["n_configured_edges"] == 1
    assert result["n_observations_decoded"] == 1
    assert result["n_anomalies"] == 0
    assert json.dumps(rels, sort_keys=True) == before


def test_dataframe_api_matches_row_api():
    entities, rels, manifest = _valid_enabled_graph()
    row_report = audit_rows(entities, rels, manifest)
    dataframe_report = audit_rows(
        pd.DataFrame(entities), pd.DataFrame(rels), manifest
    )
    assert dataframe_report == row_report


def test_valid_self_edge():
    entities, rels, manifest = _valid_enabled_graph(self_edge=True)
    result = validate_persisted_type_use_overlay(entities, rels, manifest)
    assert result["ok"] is True
    edge = _find_uses(rels)
    assert edge["source"] == edge["target"] == "a:struct:T"


# ---------------------------------------------------------------------------
# Relationship integrity corruptions
# ---------------------------------------------------------------------------


def test_dangling_source_and_target():
    entities, rels, manifest = _valid_enabled_graph()
    edge = _find_uses(rels)
    edge["source"] = "missing:source"
    edge["target"] = "missing:target"
    result = validate_persisted_type_use_overlay(entities, rels, manifest)
    codes = set(_codes(result))
    assert "dangling_source" in codes
    assert "dangling_target" in codes
    assert result["ok"] is False


def test_stored_entity_id_mismatch():
    entities, rels, manifest = _valid_enabled_graph()
    edge = _find_uses(rels)
    edge["clang_type_use_source_entity_id"] = "ent:function:WRONG"
    result = validate_persisted_type_use_overlay(entities, rels, manifest)
    assert "entity_id_mismatch" in _codes(result)


def test_deterministic_relationship_id_mismatch():
    entities, rels, manifest = _valid_enabled_graph()
    edge = _find_uses(rels)
    edge["id"] = "rel:uses_type:tampered:deadbeef"
    result = validate_persisted_type_use_overlay(entities, rels, manifest)
    assert "relationship_id_mismatch" in _codes(result)


def test_duplicate_endpoint_pair():
    entities, rels, manifest = _valid_enabled_graph()
    edge = _find_uses(rels)
    clone = copy.deepcopy(edge)
    clone["id"] = edge["id"] + ":dup"
    clone["human_readable_id"] = int(edge["human_readable_id"]) + 100
    rels.append(clone)
    manifest["clang_type_uses"]["n_facts"] = 2
    manifest["counts"]["relationships"] = len(rels)
    result = validate_persisted_type_use_overlay(entities, rels, manifest)
    codes = set(_codes(result))
    assert "duplicate_endpoint_pair" in codes
    assert "relationship_id_mismatch" in codes
    assert result["ok"] is False


def test_stale_type_use_metadata_on_other_relationship_type():
    entities, rels, manifest = _valid_enabled_graph()
    for r in rels:
        if r["type"] == "contains":
            r["clang_type_use_status"] = "matched"
            r["clang_type_use_fact_kind"] = FACT_KIND
    result = validate_persisted_type_use_overlay(entities, rels, manifest)
    assert "stale_type_use_metadata" in _codes(result)


def test_unknown_material_clang_type_use_field():
    entities, rels, manifest = _valid_enabled_graph()
    edge = _find_uses(rels)
    edge["clang_type_use_extra_secret"] = "invented"
    result = validate_persisted_type_use_overlay(entities, rels, manifest)
    assert "unknown_type_use_field" in _codes(result)


def test_duplicate_and_empty_relationship_ids():
    entities = _base_entities()
    r1 = _contains_rel()
    r2 = copy.deepcopy(r1)
    r2["target"] = "a:T"
    rels = [r1, r2]
    result = validate_persisted_type_use_overlay(entities, rels, {})
    assert "duplicate_relationship_id" in _codes(result)

    r1["id"] = ""
    r2["id"] = "rel:other"
    result2 = validate_persisted_type_use_overlay(entities, rels, {})
    assert "empty_relationship_id" in _codes(result2)


# ---------------------------------------------------------------------------
# Aggregated evidence integrity
# ---------------------------------------------------------------------------


def test_malformed_observations_json():
    entities, rels, manifest = _valid_enabled_graph()
    edge = _find_uses(rels)
    edge["clang_type_use_observations_json"] = "{not-json"
    result = validate_persisted_type_use_overlay(entities, rels, manifest)
    assert "malformed_observations_json" in _codes(result)


def test_non_object_observation_item():
    entities, rels, manifest = _valid_enabled_graph()
    edge = _find_uses(rels)
    edge["clang_type_use_observations_json"] = json.dumps(["scalar", 1])
    edge["clang_type_use_observation_count"] = 2
    result = validate_persisted_type_use_overlay(entities, rels, manifest)
    assert "non_object_observation" in _codes(result)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda observation: observation.pop("source_path"),
        lambda observation: observation.__setitem__("entry_indices", [0.5]),
        lambda observation: observation.__setitem__("invented", "field"),
    ],
    ids=["missing-required", "lossy-entry-index", "unknown-field"],
)
def test_invalid_observation_schema_fails_closed(mutation):
    entities, rels, manifest = _valid_enabled_graph()
    edge = _find_uses(rels)
    observations = json.loads(edge["clang_type_use_observations_json"])
    mutation(observations[0])
    edge["clang_type_use_observations_json"] = json.dumps(
        observations, sort_keys=True, separators=(",", ":")
    )
    result = validate_persisted_type_use_overlay(entities, rels, manifest)
    assert "invalid_observation" in _codes(result)


def test_nested_non_finite_observation_is_rejected():
    entities, rels, manifest = _valid_enabled_graph()
    edge = _find_uses(rels)
    observations = json.loads(edge["clang_type_use_observations_json"])
    observations[0]["byte_offset"] = float("nan")
    edge["clang_type_use_observations_json"] = json.dumps(
        observations, sort_keys=True, separators=(",", ":")
    )
    result = validate_persisted_type_use_overlay(entities, rels, manifest)
    assert "nan_or_infinity" in _codes(result)


def test_observation_count_mismatch():
    entities, rels, manifest = _valid_enabled_graph()
    edge = _find_uses(rels)
    edge["clang_type_use_observation_count"] = 99
    result = validate_persisted_type_use_overlay(entities, rels, manifest)
    assert "observation_count_mismatch" in _codes(result)


def test_non_canonical_observation_order():
    entities, rels, manifest = _valid_enabled_graph()
    edge = _find_uses(rels)
    # Two observations, deliberately reverse-sorted.
    obs = [
        {
            "use_kind": "parameter",
            "entry_indices": [0],
            "qualType": "Z",
            "source_path": "z.c",
            "location_precision": "declaration_bearing_node",
            "location_origin": "direct",
            "resolver": "unique_typedef_spelling",
            "owner_resolver": "exact_declaration_site",
            "desugaredQualType": None,
        },
        {
            "use_kind": "function_return",
            "entry_indices": [0],
            "qualType": "A",
            "source_path": "a.c",
            "location_precision": "declaration_bearing_node",
            "location_origin": "direct",
            "resolver": "unique_typedef_spelling",
            "owner_resolver": "exact_declaration_site",
            "desugaredQualType": None,
        },
    ]
    # Ensure they are NOT in canonical order.
    canon = sorted(obs, key=lambda o: json.dumps(o, sort_keys=True, separators=(",", ":")))
    assert obs != canon
    edge["clang_type_use_observations_json"] = json.dumps(obs, separators=(",", ":"))
    edge["clang_type_use_observation_count"] = 2
    edge["clang_type_use_use_kinds"] = ["function_return", "parameter"]
    edge["clang_type_use_entry_indices"] = [0]
    manifest["clang_type_uses"]["n_observations"] = 2
    result = validate_persisted_type_use_overlay(entities, rels, manifest)
    assert "non_canonical_observation_order" in _codes(result)


def test_use_kind_and_entry_index_aggregate_mismatch():
    entities, rels, manifest = _valid_enabled_graph()
    edge = _find_uses(rels)
    edge["clang_type_use_use_kinds"] = ["field"]  # wrong
    edge["clang_type_use_entry_indices"] = [7]  # wrong
    result = validate_persisted_type_use_overlay(entities, rels, manifest)
    codes = set(_codes(result))
    assert "use_kinds_mismatch" in codes
    assert "entry_indices_mismatch" in codes


def test_malformed_compiler_json():
    entities, rels, manifest = _valid_enabled_graph()
    edge = _find_uses(rels)
    edge["clang_type_use_compilers"] = "not-json"
    result = validate_persisted_type_use_overlay(entities, rels, manifest)
    assert "malformed_compilers_json" in _codes(result)


def test_digest_compiler_mismatch():
    entities, rels, manifest = _valid_enabled_graph()
    edge = _find_uses(rels)
    edge["clang_type_use_compile_commands_digest"] = "OTHERDIGEST"
    # compilers still have abc123
    result = validate_persisted_type_use_overlay(entities, rels, manifest)
    codes = set(_codes(result))
    assert "digest_mismatch" in codes


def test_manifest_fact_observation_count_mismatch():
    entities, rels, manifest = _valid_enabled_graph()
    manifest["clang_type_uses"]["n_facts"] = 99
    manifest["clang_type_uses"]["n_observations"] = 99
    result = validate_persisted_type_use_overlay(entities, rels, manifest)
    assert _codes(result).count("manifest_count_mismatch") >= 1


@pytest.mark.parametrize(
    "field",
    ["fact_kind", "extractor", "confidence_boundary"],
)
def test_enabled_manifest_requires_producer_identity(field: str):
    entities, rels, manifest = _valid_enabled_graph()
    manifest["clang_type_uses"].pop(field)
    result = validate_persisted_type_use_overlay(entities, rels, manifest)
    assert result["ok"] is False
    assert set(_codes(result)) & {
        "manifest_identity_mismatch",
        "confidence_boundary",
    }


def test_nan_or_infinity_input():
    entities, rels, manifest = _valid_enabled_graph()
    edge = _find_uses(rels)
    edge["confidence"] = float("nan")
    edge["clang_type_use_confidence"] = float("inf")
    result = validate_persisted_type_use_overlay(entities, rels, manifest)
    codes = set(_codes(result))
    assert "nan_or_infinity" in codes


def test_ndarray_list_parquet_normalization():
    """Parquet-style list/ndarray aggregates must still validate."""
    entities, rels, manifest = _valid_enabled_graph()
    edge = _find_uses(rels)
    # Simulate parquet ndarray for use_kinds / entry_indices.
    edge["clang_type_use_use_kinds"] = pd.Series(["parameter"]).values
    edge["clang_type_use_entry_indices"] = pd.Series([0]).values
    result = validate_persisted_type_use_overlay(entities, rels, manifest)
    assert result["ok"] is True, result["anomalies"]


def test_validator_does_not_mutate_inputs():
    entities, rels, manifest = _valid_enabled_graph()
    edge = _find_uses(rels)
    edge["clang_type_use_observations_json"] = "{bad"
    ents_before = copy.deepcopy(entities)
    rels_before = copy.deepcopy(rels)
    man_before = copy.deepcopy(manifest)
    validate_persisted_type_use_overlay(entities, rels, manifest)
    assert entities == ents_before
    assert rels == rels_before
    assert manifest == man_before


# ---------------------------------------------------------------------------
# Deterministic JSON / report ordering
# ---------------------------------------------------------------------------


def test_deterministic_json_report_ordering():
    entities, rels, manifest = _valid_enabled_graph()
    edge = _find_uses(rels)
    edge["id"] = "rel:uses_type:tampered"
    edge["clang_type_use_use_kinds"] = ["zzz"]
    r1 = audit_rows(entities, rels, manifest)
    r2 = audit_rows(entities, rels, manifest)
    j1 = audit_to_json(r1)
    j2 = audit_to_json(r2)
    assert j1 == j2
    assert r1["anomalies"] == sorted(
        r1["anomalies"],
        key=lambda a: (
            a.get("code") or "",
            a.get("relationship_id") or "",
            a.get("message") or "",
        ),
    )
    # audit_mode present
    assert r1["audit_mode"] == AUDIT_MODE
    # Human report is stable enough to include RESULT
    text = format_report(r1)
    assert "RESULT: FAIL" in text
    assert "relationship_id_mismatch" in text
    assert "use_kinds_mismatch" in text


# ---------------------------------------------------------------------------
# CLI exit codes
# ---------------------------------------------------------------------------


def test_cli_exit_codes(tmp_path: Path):
    # Exit 2: missing graph
    rc = audit_main(["--graph", str(tmp_path / "nope")])
    assert rc == 2

    # Exit 0: valid legacy flat graph
    entities = _base_entities()
    rels = [_contains_rel()]
    tus = [
        {
            "id": "tu:1",
            "text": "x",
            "n_tokens": 1,
            "document_ids": [],
            "entity_ids": [],
            "relationship_ids": [],
        }
    ]
    graph = tmp_path / "byog_legacy"
    publish_byog_snapshot(
        pd.DataFrame(entities),
        pd.DataFrame(rels),
        pd.DataFrame(tus),
        graph,
        "audit-test",
    )
    # Remove clang_type_uses from manifest to force legacy_absent.
    snap = (graph / "current").read_text().strip()
    man_path = graph / "snapshots" / snap / "manifest.json"
    man = json.loads(man_path.read_text())
    man.pop("clang_type_uses", None)
    man_path.write_text(json.dumps(man, indent=2))
    rc0 = audit_main(["--graph", str(graph), "--json"])
    assert rc0 == 0

    # Exit 1: enabled-looking corruption via in-memory publish with bad edge
    ents, rels2, manifest = _valid_enabled_graph()
    edge = _find_uses(rels2)
    edge["id"] = "rel:uses_type:bad"
    graph2 = tmp_path / "byog_bad"
    publish_byog_snapshot(
        pd.DataFrame(ents),
        pd.DataFrame(rels2),
        pd.DataFrame(tus),
        graph2,
        "audit-test",
        extra_manifest={"clang_type_uses": manifest["clang_type_uses"]},
    )
    rc1 = audit_main(["--graph", str(graph2)])
    assert rc1 == 1


def test_resolve_current_snapshot_malformed_manifest(tmp_path: Path):
    graph = tmp_path / "g"
    snap = graph / "snapshots" / "s1"
    snap.mkdir(parents=True)
    (graph / "current").write_text("s1")
    (snap / "entities.parquet").write_bytes(b"not-parquet")
    (snap / "manifest.json").write_text("{bad")
    with pytest.raises(ClangTypeUseGraphAuditError, match="malformed manifest"):
        resolve_current_snapshot(graph)


def test_resolve_current_snapshot_requires_manifest(tmp_path: Path):
    graph = tmp_path / "g"
    snap = graph / "snapshots" / "s1"
    snap.mkdir(parents=True)
    (graph / "current").write_text("s1")
    with pytest.raises(ClangTypeUseGraphAuditError, match="missing manifest"):
        resolve_current_snapshot(graph)


def test_resolve_current_snapshot_rejects_path_escape(tmp_path: Path):
    graph = tmp_path / "g"
    (graph / "snapshots").mkdir(parents=True)
    (graph / "current").write_text("../../outside")
    with pytest.raises(ClangTypeUseGraphAuditError, match="unsafe current"):
        resolve_current_snapshot(graph)


# ---------------------------------------------------------------------------
# published_graph_health integration
# ---------------------------------------------------------------------------


def test_published_graph_health_integration_legacy_c(tmp_path: Path):
    entities = _base_entities()
    rels = [_contains_rel()]
    tus = [
        {
            "id": "tu:1",
            "text": "x",
            "n_tokens": 1,
            "document_ids": [],
            "entity_ids": [],
            "relationship_ids": [],
        }
    ]
    graph = tmp_path / "byog_c_health"
    publish_byog_snapshot(
        pd.DataFrame(entities),
        pd.DataFrame(rels),
        pd.DataFrame(tus),
        graph,
        "health",
        extra_manifest={"clang_type_uses": build_disabled_provenance()},
    )
    # C indexer path: integrity should attach and pass (off).
    # We call _type_use_integrity via a minimal check that only needs published data.
    from published_graph_health import _type_use_integrity  # type: ignore

    published = {
        "entities": entities,
        "relationships": rels,
        "text_units": tus,
    }
    snap_id = (graph / "current").read_text().strip()
    stored_manifest = json.loads(
        (graph / "snapshots" / snap_id / "manifest.json").read_text()
    )
    type_use = _type_use_integrity(
        published, stored_manifest, indexer="c"
    )
    assert type_use is not None
    assert type_use["ok"] is True
    assert type_use["status"] in {"off", "legacy_absent"}

    # Non-C skips.
    assert (
        _type_use_integrity(published, stored_manifest, indexer="python")
        is None
    )


def test_published_graph_health_fails_on_type_use_anomalies(tmp_path: Path):
    """When extractor matches, type-use anomalies still fail health for C."""
    ents, rels, manifest = _valid_enabled_graph()
    edge = _find_uses(rels)
    edge["clang_type_use_source_entity_id"] = "WRONG"
    tus = [
        {
            "id": "tu:1",
            "text": "x",
            "n_tokens": 1,
            "document_ids": [],
            "entity_ids": [],
            "relationship_ids": [],
        }
    ]
    graph = tmp_path / "byog_c_bad_tu"
    publish_byog_snapshot(
        pd.DataFrame(ents),
        pd.DataFrame(rels),
        pd.DataFrame(tus),
        graph,
        "health",
        extra_manifest={"clang_type_uses": manifest["clang_type_uses"]},
    )
    from published_graph_health import _type_use_integrity  # type: ignore

    snap_id = (graph / "current").read_text().strip()
    stored_manifest = json.loads(
        (graph / "snapshots" / snap_id / "manifest.json").read_text()
    )
    type_use = _type_use_integrity(
        {"entities": ents, "relationships": rels, "text_units": tus},
        stored_manifest,
        indexer="c",
    )
    assert type_use is not None
    assert type_use["ok"] is False
    assert type_use["n_anomalies"] >= 1


def test_published_graph_health_rejects_malformed_manifest(tmp_path: Path):
    from published_graph_health import _published_data  # type: ignore

    graph = tmp_path / "g"
    snap = graph / "snapshots" / "s1"
    snap.mkdir(parents=True)
    (graph / "current").write_text("s1")
    (snap / "manifest.json").write_text('{"counts": NaN}')
    with pytest.raises(ValueError, match="non-finite JSON constant"):
        _published_data(graph)


# ---------------------------------------------------------------------------
# Live inih smoke (compiler-conditional)
# ---------------------------------------------------------------------------


def _cc():
    return find_c_compiler()


@pytest.mark.skipif(_cc() is None, reason="no C compiler for live inih smoke")
def test_live_inih_type_use_graph_audit_smoke(tmp_path: Path):
    """Default/off passes; --clang-type-uses temp snapshot passes; corruption fails.

    Deliberately corrupts an in-memory/dataframe copy only — never the package
    or published byog_inih snapshot. No .o/.ast/.d/.i artifacts in the package.
    """
    from index_c import main as index_c_main  # type: ignore

    inih = ROOT / "examples" / "inih"
    package_before = {
        p.name for p in inih.iterdir() if p.is_file() or p.is_dir()
    }

    # 1) Default/off temporary snapshot
    off_graph = tmp_path / "byog_inih_off"
    index_c_main(
        package=inih,
        graph=off_graph,
        keep_snapshots=2,
        compiler_builtins=False,
        compiler_dependencies=False,
        compiler_includes=False,
        clang_signatures=False,
        clang_calls=False,
        clang_types=False,
        clang_type_uses=False,
        allow_toolchain_drift=False,
    )
    report_off = audit_graph_root(off_graph)
    assert report_off["ok"] is True, report_off
    assert report_off["status"] in {"off", "legacy_absent"}
    assert report_off["n_configured_edges"] == 0

    # 2) Enabled temporary snapshot
    on_graph = tmp_path / "byog_inih_on"
    index_c_main(
        package=inih,
        graph=on_graph,
        keep_snapshots=2,
        compiler_builtins=False,
        compiler_dependencies=False,
        compiler_includes=False,
        clang_signatures=False,
        clang_calls=False,
        clang_types=False,
        clang_type_uses=True,
        allow_toolchain_drift=False,
    )
    report_on = audit_graph_root(on_graph)
    assert report_on["ok"] is True, report_on
    assert report_on["status"] == "enabled"
    assert report_on["n_configured_edges"] == 8, report_on

    # 3) Corrupt a copied dataframe row (not the package / not published root)
    snap_id = (on_graph / "current").read_text().strip()
    snap = on_graph / "snapshots" / snap_id
    rels_df = pd.read_parquet(snap / "relationships.parquet")
    ents_df = pd.read_parquet(snap / "entities.parquet")
    man = json.loads((snap / "manifest.json").read_text())
    # Corrupt first uses_type id in a copy.
    corrupted = rels_df.copy()
    mask = corrupted["type"].astype(str) == REL_TYPE
    assert mask.any()
    idx = corrupted.index[mask][0]
    corrupted.at[idx, "id"] = "rel:uses_type:CORRUPTED"
    bad = validate_persisted_type_use_overlay(
        ents_df.to_dict("records"),
        corrupted.to_dict("records"),
        man,
    )
    assert bad["ok"] is False
    assert "relationship_id_mismatch" in _codes(bad)

    # Package must not gain compiler/AST artifacts.
    package_after = {
        p.name for p in inih.iterdir() if p.is_file() or p.is_dir()
    }
    assert package_after == package_before
    for pattern in ("*.o", "*.ast", "*.d", "*.i"):
        assert not list(inih.glob(pattern))
        assert not list(inih.glob(f"**/{pattern}"))

    # Published byog_inih was never rewritten.
    # (This smoke only wrote under tmp_path.)
    assert not str(on_graph).startswith(str(ROOT / "byog_inih"))
