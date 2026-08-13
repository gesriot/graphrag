"""Read-only integrity audit for persisted compiler direct-include edges.

Pure synthetic/corruption tests always run. Live disposable-graph tests skip
when no C compiler is present. Nothing here writes a published byog_* root.

Run:
  uv run python -m pytest examples/cjson/tests/test_c_compiler_include_graph_audit.py -q
"""
from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

from c_compiler_common import CONFIDENCE_BOUNDARY as DEP_CONFIDENCE_BOUNDARY  # type: ignore
from c_compiler_facts import (  # type: ignore
    make_depends_on_relationship,
    validate_persisted_compiler_dependency_overlay,
)
from c_compiler_include_graph_audit import (  # type: ignore
    AUDIT_MODE,
    audit_graph_root,
    audit_rows,
    audit_to_json,
    format_report,
    main as audit_main,
    read_only_fingerprint,
)
from c_compiler_includes import (  # type: ignore
    CONFIDENCE_BOUNDARY,
    EXTRACTOR,
    FACT_KIND,
    MODE,
    build_disabled_provenance,
    include_relationship_id,
    make_includes_relationship,
    validate_persisted_compiler_include_overlay,
)
from c_preprocessor import find_c_compiler  # type: ignore

DIGEST = "a" * 64
COMPILER_PATH = "/usr/bin/clang"
COMPILER_ID = "Apple clang version test"
COMPILER_VERSION = "17.0.0"
SRC_MAIN = "/tmp/pkg/main.c"
SRC_DIRECT = "/tmp/pkg/include/direct.h"
SRC_TRANS = "/tmp/pkg/include/trans.h"
SRC_OTHER = "/tmp/pkg/other.c"

_INCLUDE_FIELDS = (
    "id",
    "source",
    "target",
    "type",
    "description",
    "weight",
    "text_unit_ids",
    "human_readable_id",
    "source_file",
    "span",
    "extractor",
    "confidence",
    "is_deterministic",
    "document_ids",
    "covariate_ids",
    "fact_kind",
    "compiler_path",
    "compiler_id",
    "compile_commands_digest",
    "preprocessor_dependent",
    "preprocessor_reasons",
)


def _cc():
    return find_c_compiler()


def _codes(report) -> set:
    return {a.get("code") for a in report.get("anomalies") or []}


def _file_entity(title: str, path: str) -> dict:
    return {
        "id": f"ent:file:{title}",
        "title": title,
        "type": "file",
        "source_file": path,
    }


def _contains(hid: int = 4) -> dict:
    return {
        "id": "rel:contains:1",
        "source": "main:main.c",
        "target": "main:main",
        "type": "contains",
        "description": "file contains function",
        "weight": 1.0,
        "text_unit_ids": [],
        "human_readable_id": hid,
        "source_file": SRC_MAIN,
        "span": "1:0",
        "extractor": "tree-sitter-c",
        "confidence": 1.0,
        "is_deterministic": True,
    }


def _inc(
    *,
    source: str = "main:main.c",
    target: str = "direct:direct.h",
    hid: int = 1,
    compiler_id: str | None = COMPILER_ID,
    source_file: str = SRC_MAIN,
    digest: str = DIGEST,
) -> dict:
    return make_includes_relationship(
        source_title=source,
        target_title=target,
        human_readable_id=hid,
        compiler_path=COMPILER_PATH,
        compiler_id=compiler_id,
        compile_commands_digest=digest,
        source_file=source_file,
    )


def _dep(
    *,
    source: str = "main:main.c",
    target: str = "trans:trans.h",
    hid: int = 10,
) -> dict:
    return make_depends_on_relationship(
        source_title=source,
        target_title=target,
        human_readable_id=hid,
        compiler_path=COMPILER_PATH,
        compiler_id=COMPILER_ID,
        compile_commands_digest=DIGEST,
        source_file=SRC_MAIN,
    )


def _enabled_block(**overrides) -> dict:
    block = {
        "mode": MODE,
        "enabled": True,
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
        "fact_kind": FACT_KIND,
        "n_facts": 2,
        "n_facts_added": 2,
        "n_facts_collected": 2,
        "n_translation_units": 1,
        "n_compile_entries": 1,
        "translation_unit_titles": ["main:main.c"],
        "confidence_boundary": CONFIDENCE_BOUNDARY,
    }
    block.update(overrides)
    return block


def _base_entities() -> list:
    return [
        _file_entity("main:main.c", SRC_MAIN),
        _file_entity("direct:direct.h", SRC_DIRECT),
        _file_entity("trans:trans.h", SRC_TRANS),
        {
            "id": "ent:fn:main",
            "title": "main:main",
            "type": "function",
            "source_file": SRC_MAIN,
        },
    ]


def _enabled_graph(*, extra_rel=None, extra_ent=None):
    ents = _base_entities()
    if extra_ent is not None:
        ents.append(extra_ent)
    rels = [
        _inc(source="main:main.c", target="direct:direct.h", hid=1, source_file=SRC_MAIN),
        _inc(
            source="direct:direct.h",
            target="trans:trans.h",
            hid=2,
            source_file=SRC_DIRECT,
        ),
        _contains(),
    ]
    if extra_rel is not None:
        rels.append(extra_rel)
    return ents, rels, {"compiler_includes": _enabled_block()}


def _off_graph():
    return _base_entities(), [_contains()], {
        "compiler_includes": build_disabled_provenance()
    }


def _legacy_graph():
    return _base_entities(), [_contains()], {}


def _decorated(rels: list) -> dict:
    return next(
        r
        for r in rels
        if r.get("type") == "includes" and r.get("fact_kind") == FACT_KIND
    )


def _decorated_all(rels: list) -> list:
    return [
        r
        for r in rels
        if r.get("type") == "includes" and r.get("fact_kind") == FACT_KIND
    ]


def test_legacy_absent_passes():
    ents, rels, manifest = _legacy_graph()
    report = audit_rows(ents, rels, manifest)
    assert report["ok"] is True
    assert report["status"] == "legacy_absent"
    assert report["classification"] == "legacy_absent"
    assert report["n_decorated_relationships"] == 0
    assert audit_rows(ents, rels, None)["status"] == "legacy_absent"


def test_exact_off_passes():
    ents, rels, manifest = _off_graph()
    report = audit_rows(ents, rels, manifest)
    assert report["ok"] is True
    assert report["status"] == "off"
    assert manifest["compiler_includes"] == {
        "mode": "off",
        "enabled": False,
        "n_facts": 0,
        "n_translation_units": 0,
    }


def test_enabled_overlay_and_header_to_header_passes():
    ents, rels, manifest = _enabled_graph()
    report = audit_rows(ents, rels, manifest)
    assert report["ok"] is True, report["anomalies"]
    assert report["status"] == "enabled"
    assert report["n_decorated_relationships"] == 2
    assert report["n_include_carriers"] == 2
    assert report["n_translation_units"] == 1
    assert report["fact_kind"] == FACT_KIND
    assert report["extractor"] == EXTRACTOR
    assert report["overlay_mode"] == MODE
    assert report["audit_mode"] == AUDIT_MODE
    pairs = {(r["source"], r["target"]) for r in _decorated_all(rels)}
    assert ("direct:direct.h", "trans:trans.h") in pairs
    assert ("main:main.c", "direct:direct.h") in pairs
    for row in _decorated_all(rels):
        assert all(field in row for field in _INCLUDE_FIELDS)


def test_malformed_and_partial_and_extra_manifest():
    ents, rels, manifest = _enabled_graph()
    for value in (None, [], "off", 0):
        report = audit_rows(ents, rels, {"compiler_includes": value})
        assert report["ok"] is False, value
        assert "invalid_enabled_block" in _codes(report)

    missing = copy.deepcopy(manifest)
    missing["compiler_includes"].pop("n_facts_added")
    report_missing = audit_rows(ents, rels, missing)
    assert report_missing["ok"] is False
    assert "missing_manifest_key" in _codes(report_missing)

    extra = copy.deepcopy(manifest)
    extra["compiler_includes"]["unexpected"] = True
    report_extra = audit_rows(ents, rels, extra)
    assert report_extra["ok"] is False
    assert "extra_manifest_key" in _codes(report_extra)

    mixed_keys = copy.deepcopy(manifest)
    mixed_keys["compiler_includes"][1] = True
    report_mixed = audit_rows(ents, rels, mixed_keys)
    assert report_mixed["ok"] is False
    assert "extra_manifest_key" in _codes(report_mixed)


def test_evidence_without_manifest_and_off_with_evidence_fail():
    ents, rels, _manifest = _enabled_graph()
    report = audit_rows(ents, rels, {})
    assert report["ok"] is False
    assert "legacy_block_missing_with_fields" in _codes(report)

    off = audit_rows(ents, rels, {"compiler_includes": build_disabled_provenance()})
    assert off["ok"] is False
    assert "off_with_decorated_relationships" in _codes(off)


def test_partial_and_contradictory_carrier_markers_fail():
    ents, rels, manifest = _enabled_graph()
    for field, value in (
        ("type", "depends_on"),
        ("fact_kind", "translation_unit_dependency"),
        ("extractor", "c-compiler-deps"),
    ):
        rows = copy.deepcopy(rels)
        _decorated(rows)[field] = value
        report = audit_rows(ents, rows, manifest)
        assert report["ok"] is False, field
        assert "contradictory_carrier" in _codes(report), field


def test_dependency_rows_are_ignored_as_include_carriers():
    ents, rels, manifest = _enabled_graph(extra_rel=_dep())
    report = audit_rows(ents, rels, manifest)
    assert report["ok"] is True, report["anomalies"]
    assert report["n_decorated_relationships"] == 2
    assert report["n_include_carriers"] == 2


def test_mixed_dependency_and_include_graph_passes_both_validators():
    ents, rels, include_manifest = _enabled_graph(extra_rel=_dep())
    dep_block = {
        "mode": "compiler_m",
        "enabled": True,
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
        "fact_kind": "translation_unit_dependency",
        "n_facts": 1,
        "n_facts_added": 1,
        "n_facts_collected": 1,
        "n_translation_units": 1,
        "n_compile_entries": 1,
        "translation_unit_titles": ["main:main.c"],
        "confidence_boundary": DEP_CONFIDENCE_BOUNDARY,
    }
    manifest = {
        "compiler_includes": include_manifest["compiler_includes"],
        "compiler_dependencies": dep_block,
    }
    include_report = validate_persisted_compiler_include_overlay(ents, rels, manifest)
    dep_report = validate_persisted_compiler_dependency_overlay(ents, rels, manifest)
    assert include_report["ok"] is True, include_report["anomalies"]
    assert dep_report["ok"] is True, dep_report["anomalies"]
    assert include_report["n_decorated_relationships"] == 2
    assert dep_report["n_decorated_relationships"] == 1


def test_every_missing_relationship_producer_field_fails():
    ents, rels, manifest = _enabled_graph()
    for field in _INCLUDE_FIELDS:
        rows = copy.deepcopy(rels)
        _decorated(rows).pop(field)
        report = audit_rows(ents, rows, manifest)
        assert report["ok"] is False, field
        codes = _codes(report)
        if field in {"type", "fact_kind", "extractor"}:
            assert "contradictory_carrier" in codes, (field, codes)
        elif field == "id":
            assert "empty_relationship_id" in codes or (
                "partial_include_payload" in codes
            ), (field, codes)
        else:
            assert "partial_include_payload" in codes, (field, codes)


def test_nullable_compiler_id_present_as_null_passes():
    ents, rels, manifest = _enabled_graph()
    rows = copy.deepcopy(rels)
    for row in _decorated_all(rows):
        row["compiler_id"] = None
    block = copy.deepcopy(manifest["compiler_includes"])
    block["compiler_id"] = None
    block["compilers"][0]["compiler_id"] = None
    report = audit_rows(ents, rows, {"compiler_includes": block})
    assert report["ok"] is True, report["anomalies"]
    assert "compiler_id" in _decorated(rows)


def test_unrelated_nullable_schema_bleed_is_ignored():
    ents, rels, manifest = _enabled_graph()
    rows = copy.deepcopy(rels)
    _decorated(rows)["clang_call_status"] = None
    _decorated(rows)["unrelated_union_column"] = None
    report = audit_rows(ents, rows, manifest)
    assert report["ok"] is True, report["anomalies"]


def test_material_incompatible_and_unknown_provenance_fail():
    ents, rels, manifest = _enabled_graph()
    rows = copy.deepcopy(rels)
    target = _decorated(rows)
    target["clang_call_status"] = "matched"
    target["clang_type_use_status"] = "matched"
    report = audit_rows(ents, rows, manifest)
    assert report["ok"] is False
    assert "incompatible_overlay_field" in _codes(report)

    unknown = copy.deepcopy(rels)
    _decorated(unknown)["compiler_version"] = "17.0.0"
    report_unknown = audit_rows(ents, unknown, manifest)
    assert report_unknown["ok"] is False
    assert "unknown_include_field" in _codes(report_unknown)


def test_endpoint_errors_and_header_sources_are_not_required_tus():
    ents, rels, manifest = _enabled_graph()
    missing = copy.deepcopy(rels)
    _decorated(missing)["target"] = "ghost:ghost.h"
    missing[0]["id"] = include_relationship_id("main:main.c", "ghost:ghost.h")
    report_missing = audit_rows(ents, missing, manifest)
    assert report_missing["ok"] is False
    assert "endpoint_mismatch" in _codes(report_missing)

    same = copy.deepcopy(rels)
    _decorated(same)["target"] = "main:main.c"
    same[0]["id"] = include_relationship_id("main:main.c", "main:main.c")
    report_same = audit_rows(ents, same, manifest)
    assert report_same["ok"] is False
    assert "endpoint_mismatch" in _codes(report_same)

    # Header -> header source is not a TU; that remains valid.
    extra = _file_entity("other:other.c", SRC_OTHER)
    zero_edge = copy.deepcopy(manifest)
    zero_edge["compiler_includes"]["translation_unit_titles"] = [
        "main:main.c",
        "other:other.c",
    ]
    zero_edge["compiler_includes"]["n_translation_units"] = 2
    zero_edge["compiler_includes"]["n_compile_entries"] = 2
    report_ok = audit_rows(_base_entities() + [extra], rels, zero_edge)
    assert report_ok["ok"] is True, report_ok["anomalies"]


def test_empty_tu_list_cannot_conceal_invalid_census():
    ents, rels, manifest = _enabled_graph()
    hidden = copy.deepcopy(manifest)
    hidden["compiler_includes"]["translation_unit_titles"] = []
    hidden["compiler_includes"]["n_translation_units"] = False
    report = audit_rows(ents, rels, hidden)
    assert report["ok"] is False
    assert "manifest_count_mismatch" in _codes(report)

    empty_ok_block = _enabled_block(
        n_facts=0,
        n_facts_added=0,
        n_facts_collected=0,
        n_translation_units=0,
        translation_unit_titles=[],
    )
    report_empty = audit_rows(
        ents, [_contains()], {"compiler_includes": empty_ok_block}
    )
    assert report_empty["ok"] is True, report_empty["anomalies"]


def test_deterministic_id_mismatch_and_normalization_collision():
    dotted = include_relationship_id("a-b", "c.h")
    underscored = include_relationship_id("a_b", "c.h")
    assert dotted != underscored
    assert dotted.startswith("rel:includes:")
    assert "configured_direct_include" not in dotted

    ents, rels, manifest = _enabled_graph()
    rows = copy.deepcopy(rels)
    _decorated(rows)["id"] = "rel:includes:wrong"
    report = audit_rows(ents, rows, manifest)
    assert report["ok"] is False
    assert "identity_mismatch" in _codes(report)


def test_exact_description_weight_confidence_determinism():
    ents, rels, manifest = _enabled_graph()
    rows = copy.deepcopy(rels)
    _decorated(rows)["description"] = "includes header"
    assert "description_mismatch" in _codes(audit_rows(ents, rows, manifest))

    for field, value in (
        ("weight", 0.9),
        ("weight", True),
        ("confidence", 0.5),
        ("is_deterministic", False),
        ("is_deterministic", 1),
    ):
        broken = copy.deepcopy(rels)
        _decorated(broken)[field] = value
        report = audit_rows(ents, broken, manifest)
        assert report["ok"] is False, (field, value)
        assert "include_field_type" in _codes(report), (field, value)


def test_human_readable_id_rejects_bool_and_float():
    ents, rels, manifest = _enabled_graph()
    for value in (0, -1, True, False, 1.0, "1", None):
        rows = copy.deepcopy(rels)
        _decorated(rows)["human_readable_id"] = value
        report = audit_rows(ents, rows, manifest)
        assert report["ok"] is False, value
        assert "human_readable_id" in _codes(report), value

    dup = copy.deepcopy(rels)
    _decorated_all(dup)[1]["human_readable_id"] = 1
    assert "human_readable_id" in _codes(audit_rows(ents, dup, manifest))


def test_duplicate_ids_and_endpoint_pairs_fail():
    ents, rels, manifest = _enabled_graph()
    dup = copy.deepcopy(rels)
    dup[1]["id"] = dup[0]["id"]
    assert "duplicate_relationship_id" in _codes(audit_rows(ents, dup, manifest))

    empty = copy.deepcopy(rels)
    empty[0]["id"] = ""
    assert "empty_relationship_id" in _codes(audit_rows(ents, empty, manifest))

    pair = copy.deepcopy(rels)
    clone = copy.deepcopy(_decorated(pair))
    clone["id"] = "rel:includes:duplicate"
    clone["human_readable_id"] = 99
    pair.append(clone)
    assert "duplicate_endpoint_pair" in _codes(audit_rows(ents, pair, manifest))


def test_source_file_canonicality_and_entity_disagreement():
    ents, rels, manifest = _enabled_graph()
    rows = copy.deepcopy(rels)
    _decorated(rows)["source_file"] = "/tmp/pkg/other.c"
    assert "source_file_mismatch" in _codes(audit_rows(ents, rows, manifest))

    relative = copy.deepcopy(rels)
    _decorated(relative)["source_file"] = "main.c"
    assert "source_file_mismatch" in _codes(audit_rows(ents, relative, manifest))


def test_digest_format_and_agreement():
    ents, rels, manifest = _enabled_graph()
    mismatch = copy.deepcopy(rels)
    _decorated(mismatch)["compile_commands_digest"] = "b" * 64
    assert "digest_mismatch" in _codes(audit_rows(ents, mismatch, manifest))
    for value in ("ABC" + "a" * 61, "a" * 63, "", None, 1):
        broken = copy.deepcopy(manifest)
        broken["compiler_includes"]["compile_commands_digest"] = value
        assert "digest_mismatch" in _codes(audit_rows(ents, rels, broken)), value


def test_compiler_census_ordering_duplicates_and_singular_rules():
    ents, rels, manifest = _enabled_graph()
    relative = copy.deepcopy(manifest)
    relative["compiler_includes"]["compiler_path"] = "clang"
    relative["compiler_includes"]["compilers"][0]["compiler_path"] = "clang"
    assert "compiler_mismatch" in _codes(audit_rows(ents, rels, relative))

    dup = copy.deepcopy(manifest)
    only = copy.deepcopy(dup["compiler_includes"]["compilers"][0])
    dup["compiler_includes"]["compilers"] = [only, copy.deepcopy(only)]
    assert "compiler_mismatch" in _codes(audit_rows(ents, rels, dup))

    singular = copy.deepcopy(manifest)
    singular["compiler_includes"]["compiler_id"] = "other"
    assert "compiler_mismatch" in _codes(audit_rows(ents, rels, singular))

    second = {
        "compiler_path": "/usr/bin/gcc",
        "compiler_id": "gcc version test",
        "compiler_version": "14.0.0",
    }
    multi = copy.deepcopy(manifest)
    first = multi["compiler_includes"]["compilers"][0]
    multi["compiler_includes"]["compilers"] = sorted(
        [first, second], key=lambda item: item["compiler_path"]
    )
    assert "compiler_mismatch" in _codes(audit_rows(ents, rels, multi))

    multi_ok = copy.deepcopy(multi)
    for field in ("compiler_path", "compiler_id", "compiler_version"):
        multi_ok["compiler_includes"][field] = None
    multi_ok["compiler_includes"]["n_compile_entries"] = 2
    assert audit_rows(ents, rels, multi_ok)["ok"] is True

    impossible_census = copy.deepcopy(multi_ok)
    impossible_census["compiler_includes"]["n_compile_entries"] = 1
    report_impossible = audit_rows(ents, rels, impossible_census)
    assert report_impossible["ok"] is False
    assert "manifest_count_mismatch" in _codes(report_impossible)

    unordered = copy.deepcopy(multi_ok)
    unordered["compiler_includes"]["compilers"] = [second, first]
    assert "compiler_mismatch" in _codes(audit_rows(ents, rels, unordered))


def test_strict_count_types_reject_false_versus_zero():
    ents, rels, manifest = _enabled_graph()
    for field, value in (
        ("n_facts", False),
        ("n_facts_added", False),
        ("n_facts_collected", False),
        ("n_translation_units", False),
        ("n_compile_entries", False),
        ("n_facts", 2.0),
        ("n_compile_entries", 0),
    ):
        broken = copy.deepcopy(manifest)
        broken["compiler_includes"][field] = value
        report = audit_rows(ents, rels, broken)
        assert report["ok"] is False, (field, value)
        assert "manifest_count_mismatch" in _codes(report), (field, value)

    extra_tu = _file_entity("other:other.c", SRC_OTHER)
    impossible_tus = copy.deepcopy(manifest)
    impossible_tus["compiler_includes"]["translation_unit_titles"] = [
        "main:main.c",
        "other:other.c",
    ]
    impossible_tus["compiler_includes"]["n_translation_units"] = 2
    report_tus = audit_rows(ents + [extra_tu], rels, impossible_tus)
    assert report_tus["ok"] is False
    assert "manifest_count_mismatch" in _codes(report_tus)

    null_relationship_count = copy.deepcopy(manifest)
    null_relationship_count["counts"] = {"relationships": None}
    report_null_count = audit_rows(ents, rels, null_relationship_count)
    assert report_null_count["ok"] is False
    assert "manifest_count_mismatch" in _codes(report_null_count)


def test_translation_unit_title_sorting_uniqueness_and_ambiguous_entities():
    ents, rels, manifest = _enabled_graph()
    unsorted = copy.deepcopy(manifest)
    extra = _file_entity("z:z.c", "/tmp/pkg/z.c")
    unsorted["compiler_includes"]["translation_unit_titles"] = [
        "z:z.c",
        "main:main.c",
    ]
    unsorted["compiler_includes"]["n_translation_units"] = 2
    assert "translation_unit_titles" in _codes(
        audit_rows(ents + [extra], rels, unsorted)
    )

    mixed = copy.deepcopy(manifest)
    mixed["compiler_includes"]["translation_unit_titles"] = ["main:main.c", 1]
    mixed["compiler_includes"]["n_translation_units"] = 2
    report_mixed = audit_rows(ents, rels, mixed)
    assert report_mixed["ok"] is False
    assert "translation_unit_titles" in _codes(report_mixed)

    for title in (" main:main.c", "main:main.c ", ""):
        noncanonical = copy.deepcopy(manifest)
        noncanonical["compiler_includes"]["translation_unit_titles"] = [title]
        report_noncanonical = audit_rows(ents, rels, noncanonical)
        assert report_noncanonical["ok"] is False, title
        assert "translation_unit_titles" in _codes(report_noncanonical), title

    ambiguous = copy.deepcopy(ents)
    ambiguous.append(_file_entity("main:main.c", "/tmp/pkg/copy.c"))
    report_amb = audit_rows(ambiguous, rels, manifest)
    assert report_amb["ok"] is False
    assert "translation_unit_titles" in _codes(report_amb)


def test_exact_preprocessor_provenance():
    ents, rels, manifest = _enabled_graph()
    rows = copy.deepcopy(rels)
    _decorated(rows)["preprocessor_dependent"] = False
    assert "preprocessor_provenance" in _codes(audit_rows(ents, rows, manifest))
    reasons = copy.deepcopy(rels)
    _decorated(reasons)["preprocessor_reasons"] = ["compiler_configuration_dependency"]
    assert "preprocessor_provenance" in _codes(audit_rows(ents, reasons, manifest))


def test_deterministic_output_and_anomaly_truncation():
    ents, rels, manifest = _enabled_graph()
    first = audit_to_json(audit_rows(ents, rels, manifest))
    second = audit_to_json(
        audit_rows(copy.deepcopy(ents), copy.deepcopy(rels), copy.deepcopy(manifest))
    )
    assert first == second
    assert list(json.loads(first).keys()) == sorted(json.loads(first).keys())

    broken = copy.deepcopy(rels)
    for row in _decorated_all(broken):
        row["confidence"] = 0.1
        row["weight"] = 0.2
        row["description"] = "bad"
    truncated = audit_rows(ents, broken, manifest, max_anomaly_samples=1)
    assert truncated["ok"] is False
    assert truncated["n_anomalies"] > 1
    assert truncated["n_anomaly_samples"] == 1
    assert truncated["anomalies_truncated"] is True


def _write_tables(snap: Path, ents, rels, block, *, counts=None) -> None:
    snap.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(ents).to_parquet(snap / "entities.parquet")
    pd.DataFrame(rels).to_parquet(snap / "relationships.parquet")
    pd.DataFrame([{"id": "t1", "title": "a"}]).to_parquet(snap / "text_units.parquet")
    manifest = {"compiler_includes": block} if block is not None else {}
    if counts is not None:
        manifest["counts"] = counts
    (snap / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )


def _publish(tmp_path: Path, ents, rels, block, *, name: str = "g") -> Path:
    graph = tmp_path / name
    _write_tables(graph / "snapshots" / "s1", ents, rels, block)
    (graph / "current").write_text("s1", encoding="utf-8")
    return graph


def test_flat_current_and_explicit_snapshot_resolution(tmp_path: Path):
    ents, rels, manifest = _enabled_graph()
    current = _publish(tmp_path, ents, rels, manifest["compiler_includes"], name="cur")
    assert audit_graph_root(current)["ok"] is True
    assert audit_graph_root(current, snapshot="s1")["ok"] is True

    flat = tmp_path / "flat"
    _write_tables(flat, ents, rels, manifest["compiler_includes"])
    report_flat = audit_graph_root(flat)
    assert report_flat["ok"] is True, report_flat["anomalies"]
    assert report_flat.get("snapshot") is None


def test_malformed_parquet_and_manifest_exit_2(tmp_path: Path, capsys):
    ents, rels, manifest = _enabled_graph()
    graph = _publish(tmp_path, ents, rels, manifest["compiler_includes"])
    (graph / "snapshots" / "s1" / "manifest.json").write_text(
        '{"compiler_includes": NaN}', encoding="utf-8"
    )
    assert audit_main(["--graph", str(graph)]) == 2
    capsys.readouterr()

    graph2 = _publish(tmp_path, ents, rels, manifest["compiler_includes"], name="g2")
    (graph2 / "snapshots" / "s1" / "relationships.parquet").write_bytes(b"not parquet")
    assert audit_main(["--graph", str(graph2)]) == 2

    graph3 = _publish(tmp_path, ents, rels, manifest["compiler_includes"], name="g3")
    (graph3 / "snapshots" / "s1" / "manifest.json").write_text(
        '{"compiler_includes": {}, "compiler_includes": {}}', encoding="utf-8"
    )
    assert audit_main(["--graph", str(graph3)]) == 2


def test_output_containment_and_read_only_fingerprint(tmp_path: Path, capsys):
    ents, rels, manifest = _enabled_graph()
    graph = _publish(tmp_path, ents, rels, manifest["compiler_includes"])
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
    assert report["ok"] is True
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

    forbidden = graph / "snapshots" / "s1" / "include-audit.json"
    assert audit_main(["--graph", str(graph), "--output", str(forbidden)]) == 2
    assert "outside the audited graph root" in capsys.readouterr().err
    assert not forbidden.exists()

    alias = tmp_path / "alias"
    alias.symlink_to(graph)
    via_link = alias / "via-symlink.json"
    assert audit_main(["--graph", str(graph), "--output", str(via_link)]) == 2
    assert not via_link.exists()


def test_cli_exit_codes_and_format_report(tmp_path: Path, capsys):
    ents, rels, manifest = _enabled_graph()
    good = _publish(tmp_path, ents, rels, manifest["compiler_includes"], name="good")
    out_path = tmp_path / "report.json"
    assert audit_main(["--graph", str(good), "--output", str(out_path), "--json"]) == 0
    assert json.loads(out_path.read_text(encoding="utf-8"))["ok"] is True
    capsys.readouterr()

    bad_rels = copy.deepcopy(rels)
    _decorated(bad_rels)["confidence"] = 0.5
    bad = _publish(tmp_path, ents, bad_rels, manifest["compiler_includes"], name="bad")
    assert audit_main(["--graph", str(bad)]) == 1
    capsys.readouterr()
    assert audit_main(["--graph", str(tmp_path / "missing")]) == 2
    text = format_report(audit_rows(ents, rels, manifest))
    assert "RESULT: PASS" in text
    assert "read-only" in text


def test_health_c_and_non_c_and_prior_keys():
    from published_graph_health import (  # type: ignore
        _call_integrity,
        _compiler_dependency_integrity,
        _compiler_include_integrity,
        _signature_integrity,
        _type_integrity,
        _type_shape_integrity,
        _type_use_integrity,
    )

    ents, rels, manifest = _enabled_graph()
    enabled = _compiler_include_integrity(
        {"entities": ents, "relationships": rels}, manifest, indexer="c"
    )
    assert enabled is not None and enabled["ok"] is True
    assert enabled["status"] == "enabled"

    legacy_ents, legacy_rels, legacy_manifest = _legacy_graph()
    assert (
        _compiler_include_integrity(
            {"entities": legacy_ents, "relationships": legacy_rels},
            legacy_manifest,
            indexer="c",
        )["status"]
        == "legacy_absent"
    )
    off_ents, off_rels, off_manifest = _off_graph()
    assert (
        _compiler_include_integrity(
            {"entities": off_ents, "relationships": off_rels},
            off_manifest,
            indexer="c",
        )["status"]
        == "off"
    )
    assert (
        _compiler_include_integrity(
            {"entities": ents, "relationships": rels}, manifest, indexer="python"
        )
        is None
    )

    empty = {"relationships": rels, "entities": ents}
    assert _compiler_dependency_integrity(empty, {}, indexer="c")["ok"] is True
    assert _type_use_integrity(empty, {}, indexer="c")["ok"] is True
    assert _type_shape_integrity({"entities": []}, {}, indexer="c")["ok"] is True
    assert _type_integrity({"entities": []}, {}, indexer="c")["ok"] is True
    assert _signature_integrity({"entities": []}, {}, indexer="c")["ok"] is True
    assert _call_integrity({"relationships": []}, {}, indexer="c")["ok"] is True


def test_validator_does_not_invoke_compiler_or_read_sources(monkeypatch, tmp_path: Path):
    def boom(*_args, **_kwargs):
        raise AssertionError("read-only audit must not invoke a compiler or sources")

    import c_compiler_common as common_mod  # type: ignore
    import c_compiler_includes as inc_mod  # type: ignore

    monkeypatch.setattr(inc_mod, "collect_configured_direct_includes", boom)
    monkeypatch.setattr(inc_mod, "append_compiler_includes", boom)
    monkeypatch.setattr(inc_mod, "collect_includes_for_entry", boom)
    monkeypatch.setattr(inc_mod, "reconstruct_direct_include_edges", boom)
    monkeypatch.setattr(common_mod, "load_compile_entries", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    ents, rels, manifest = _enabled_graph()
    report = validate_persisted_compiler_include_overlay(ents, rels, manifest)
    assert report["ok"] is True, report["anomalies"]


def _fail_if_compiler_used(monkeypatch):
    import c_compiler_common as common_mod  # type: ignore
    import c_compiler_includes as inc_mod  # type: ignore

    def boom(*_args, **_kwargs):
        raise AssertionError("read-only audit must not invoke a compiler")

    monkeypatch.setattr(inc_mod, "collect_configured_direct_includes", boom)
    monkeypatch.setattr(inc_mod, "append_compiler_includes", boom)
    monkeypatch.setattr(inc_mod, "collect_includes_for_entry", boom)
    monkeypatch.setattr(inc_mod, "reconstruct_direct_include_edges", boom)
    monkeypatch.setattr(common_mod, "load_compile_entries", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "check_output", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)


def _write_include_fixture(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    include_dir = root / "include"
    include_dir.mkdir()
    (root / "main.c").write_text(
        '#include "direct.h"\n'
        '#include "with space.h"\n'
        "#include <stdio.h>\n"
        "int main(void) { return 0; }\n",
        encoding="utf-8",
    )
    (include_dir / "direct.h").write_text(
        '#include "transitive.h"\n'
        "int direct_fn(void);\n",
        encoding="utf-8",
    )
    (include_dir / "transitive.h").write_text(
        "int trans_fn(void);\n",
        encoding="utf-8",
    )
    (include_dir / "with space.h").write_text(
        "int spaced_fn(void);\n",
        encoding="utf-8",
    )
    return root


def _write_cc(root: Path, *, compiler: str) -> None:
    (root / "compile_commands.json").write_text(
        json.dumps(
            [
                {
                    "directory": str(root),
                    "command": f"{compiler} -c -Iinclude main.c -o main.o",
                    "file": "main.c",
                }
            ]
        ),
        encoding="utf-8",
    )


def _index(package: Path, graph: Path, *, includes: bool, deps: bool = False) -> None:
    from index_c import main as index_c_main  # type: ignore

    index_c_main(
        package=package,
        graph=graph,
        keep_snapshots=2,
        compiler_builtins=False,
        compiler_dependencies=deps,
        compiler_includes=includes,
        clang_signatures=False,
        clang_calls=False,
        clang_types=False,
        clang_type_uses=False,
        clang_type_shapes=False,
        allow_toolchain_drift=False,
    )


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_fixture_include_graph_audit(tmp_path: Path, monkeypatch):
    compiler = _cc()
    assert compiler is not None
    pkg = _write_include_fixture(tmp_path / "pkg")
    _write_cc(pkg, compiler=compiler)
    graph = tmp_path / "byog_fixture_inc"
    before = {p.name for p in pkg.iterdir()}
    _index(pkg, graph, includes=True)
    snap = (graph / "current").read_text(encoding="utf-8").strip()
    block = json.loads(
        (graph / "snapshots" / snap / "manifest.json").read_text(encoding="utf-8")
    )["compiler_includes"]
    _fail_if_compiler_used(monkeypatch)
    report = audit_graph_root(graph)
    assert report["ok"] is True, report["anomalies"]
    assert report["status"] == "enabled"
    assert report["n_decorated_relationships"] == block["n_facts"]
    assert report["n_translation_units"] == block["n_translation_units"]
    assert report["n_decorated_relationships"] >= 2
    assert report["read_only_verification"]["verified"] is True
    assert {p.name for p in pkg.iterdir()} == before
    for pattern in ("*.o", "*.ast", "*.d", "*.i", "*.ii", "*.pch"):
        assert not list(pkg.rglob(pattern))


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_cjson_and_inih_include_graph_audit(tmp_path: Path, monkeypatch):
    for name in ("cjson", "inih"):
        pkg = ROOT / "examples" / name
        package_before = {p.name for p in pkg.iterdir()}
        graph = tmp_path / f"byog_{name}_inc"
        _index(pkg, graph, includes=True)
        snap = (graph / "current").read_text(encoding="utf-8").strip()
        block = json.loads(
            (graph / "snapshots" / snap / "manifest.json").read_text(encoding="utf-8")
        )["compiler_includes"]
        monkeypatch.undo()
        _fail_if_compiler_used(monkeypatch)
        report = audit_graph_root(graph)
        assert report["ok"] is True, (name, report["anomalies"])
        assert report["status"] == "enabled"
        assert report["n_decorated_relationships"] == block["n_facts"]
        assert report["n_translation_units"] == block["n_translation_units"]
        assert report["read_only_verification"]["verified"] is True
        assert {p.name for p in pkg.iterdir()} == package_before
        for pattern in ("*.o", "*.ast", "*.d", "*.i", "*.ii", "*.pch"):
            assert not list(pkg.rglob(pattern))
        monkeypatch.undo()


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_default_off_and_health(tmp_path: Path, monkeypatch):
    from published_graph_health import PublishedGraphSpec, check_spec  # type: ignore

    pkg = ROOT / "examples" / "inih"
    off_graph = tmp_path / "byog_inih_off"
    _index(pkg, off_graph, includes=False)
    _fail_if_compiler_used(monkeypatch)
    off_report = audit_graph_root(off_graph)
    assert off_report["ok"] is True
    assert off_report["status"] == "off"

    spec = PublishedGraphSpec(
        ident="inih_tmp_off",
        source=Path("examples/inih"),
        indexer="c",
        graph=str(off_graph),
        mode="mutable",
    )
    result = check_spec(spec, root=ROOT, graph_root=off_graph)
    assert result["status"] == "pass", result
    assert result["compiler_include_integrity"]["status"] == "off"
    assert result["compiler_dependency_integrity"]["ok"] is True
    assert result["clang_call_integrity"]["ok"] is True
    assert result["clang_signature_integrity"]["ok"] is True
    assert result["clang_type_integrity"]["ok"] is True
    assert result["clang_type_use_integrity"]["ok"] is True
    assert result["clang_type_shape_integrity"]["ok"] is True

    monkeypatch.undo()
    enabled_graph = tmp_path / "byog_inih_inc"
    _index(pkg, enabled_graph, includes=True)
    enabled_spec = PublishedGraphSpec(
        ident="inih_tmp_on",
        source=Path("examples/inih"),
        indexer="c",
        graph=str(enabled_graph),
        mode="mutable",
    )
    enabled_result = check_spec(enabled_spec, root=ROOT, graph_root=enabled_graph)
    assert enabled_result["compiler_include_integrity"]["ok"] is True
    assert enabled_result["compiler_include_integrity"]["status"] == "enabled"
    assert enabled_result["compiler_dependency_integrity"]["ok"] is True
    assert enabled_result["clang_call_integrity"]["ok"] is True
    assert enabled_result["status"] == "fail"
    assert "relationships" in enabled_result.get("mismatches", {})


def test_published_byog_roots_are_off_and_read_only():
    for name in ("byog_cjson", "byog_inih"):
        graph = ROOT / name
        assert graph.is_dir(), name
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
        assert after == before, name
        assert report["ok"] is True, (name, report["anomalies"])
        assert report["status"] == "off", name
        assert report["read_only_verification"]["verified"] is True
        assert report["n_decorated_relationships"] == 0
