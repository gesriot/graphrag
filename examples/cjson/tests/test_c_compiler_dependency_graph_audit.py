"""Read-only integrity audit for persisted compiler TU dependency edges.

Pure synthetic/corruption tests always run. Live disposable-graph tests skip
when no C compiler is present. Nothing here writes a published byog_* root.

Run:
  uv run python -m pytest examples/cjson/tests/test_c_compiler_dependency_graph_audit.py -q
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

from c_compiler_common import CONFIDENCE_BOUNDARY  # type: ignore
from c_compiler_dependency_graph_audit import (  # type: ignore
    AUDIT_MODE,
    audit_graph_root,
    audit_rows,
    audit_to_json,
    format_report,
    main as audit_main,
    read_only_fingerprint,
)
from c_compiler_facts import (  # type: ignore
    EXTRACTOR,
    FACT_KIND,
    MODE,
    build_disabled_provenance,
    dependency_relationship_id,
    make_depends_on_relationship,
    validate_persisted_compiler_dependency_overlay,
)
from c_preprocessor import find_c_compiler  # type: ignore

DIGEST = "a" * 64
COMPILER_PATH = "/usr/bin/clang"
COMPILER_ID = "Apple clang version test"
COMPILER_VERSION = "17.0.0"
SRC_MAIN = "/tmp/pkg/main.c"
SRC_DIRECT = "/tmp/pkg/direct.h"
SRC_TRANS = "/tmp/pkg/trans.h"
SRC_OTHER = "/tmp/pkg/other.c"

_DEP_FIELDS = (
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


def _contains(hid: int = 3) -> dict:
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


def _includes(hid: int = 4) -> dict:
    return {
        "id": "rel:includes:1",
        "source": "main:main.c",
        "target": "direct:direct.h",
        "type": "includes",
        "description": "configured direct include",
        "weight": 1.0,
        "text_unit_ids": [],
        "human_readable_id": hid,
        "source_file": SRC_MAIN,
        "span": "",
        "extractor": "c-compiler-includes",
        "confidence": 1.0,
        "is_deterministic": True,
        "document_ids": [],
        "covariate_ids": [],
        "fact_kind": "configured_direct_include",
        "compiler_path": COMPILER_PATH,
        "compiler_id": COMPILER_ID,
        "compile_commands_digest": DIGEST,
        "preprocessor_dependent": True,
        "preprocessor_reasons": ["compiler_configuration_include"],
    }


def _dep(
    *,
    source: str = "main:main.c",
    target: str = "direct:direct.h",
    hid: int = 1,
    compiler_id: str | None = COMPILER_ID,
    source_file: str = SRC_MAIN,
    digest: str = DIGEST,
) -> dict:
    return make_depends_on_relationship(
        source_title=source,
        target_title=target,
        human_readable_id=hid,
        compiler_path=COMPILER_PATH,
        compiler_id=compiler_id,
        compile_commands_digest=digest,
        source_file=source_file,
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


def _enabled_graph(*, extra_rel=None):
    ents = _base_entities()
    rels = [
        _dep(target="direct:direct.h", hid=1),
        _dep(target="trans:trans.h", hid=2),
        _contains(),
    ]
    if extra_rel is not None:
        rels.append(extra_rel)
    manifest = {"compiler_dependencies": _enabled_block()}
    return ents, rels, manifest


def _off_graph():
    return _base_entities(), [_contains()], {
        "compiler_dependencies": build_disabled_provenance()
    }


def _legacy_graph():
    return _base_entities(), [_contains()], {}


def _decorated(rels: list) -> dict:
    return next(
        r
        for r in rels
        if r.get("type") == "depends_on" and r.get("fact_kind") == FACT_KIND
    )


def _decorated_all(rels: list) -> list:
    return [
        r
        for r in rels
        if r.get("type") == "depends_on" and r.get("fact_kind") == FACT_KIND
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
    assert manifest["compiler_dependencies"] == {
        "mode": "off",
        "enabled": False,
        "n_facts": 0,
        "n_translation_units": 0,
    }


def test_enabled_overlay_passes():
    ents, rels, manifest = _enabled_graph()
    report = audit_rows(ents, rels, manifest)
    assert report["ok"] is True, report["anomalies"]
    assert report["status"] == "enabled"
    assert report["n_decorated_relationships"] == 2
    assert report["n_translation_units"] == 1
    assert report["n_dependency_carriers"] == 2
    assert report["counts"]["n_facts"] == 2
    assert report["fact_kind"] == FACT_KIND
    assert report["extractor"] == EXTRACTOR
    assert report["overlay_mode"] == MODE
    assert report["audit_mode"] == AUDIT_MODE
    assert report["confidence_boundary"] == CONFIDENCE_BOUNDARY
    for row in _decorated_all(rels):
        assert all(field in row for field in _DEP_FIELDS)
    includes_ents, includes_rels, includes_manifest = _enabled_graph(
        extra_rel=_includes()
    )
    includes_report = audit_rows(includes_ents, includes_rels, includes_manifest)
    assert includes_report["ok"] is True, includes_report["anomalies"]
    assert includes_report["n_decorated_relationships"] == 2


def test_malformed_and_partial_and_extra_manifest():
    ents, rels, manifest = _enabled_graph()
    for value in (None, [], "off", 0):
        report = audit_rows(ents, rels, {"compiler_dependencies": value})
        assert report["ok"] is False, value
        assert "invalid_enabled_block" in _codes(report)

    missing = copy.deepcopy(manifest)
    missing["compiler_dependencies"].pop("n_facts_added")
    report_missing = audit_rows(ents, rels, missing)
    assert report_missing["ok"] is False
    assert "missing_manifest_key" in _codes(report_missing)

    extra = copy.deepcopy(manifest)
    extra["compiler_dependencies"]["unexpected"] = True
    report_extra = audit_rows(ents, rels, extra)
    assert report_extra["ok"] is False
    assert "extra_manifest_key" in _codes(report_extra)

    non_string_extra = copy.deepcopy(manifest)
    non_string_extra["compiler_dependencies"][7] = True
    report_non_string = audit_rows(ents, rels, non_string_extra)
    assert report_non_string["ok"] is False
    assert "extra_manifest_key" in _codes(report_non_string)

    inconsistent = copy.deepcopy(manifest)
    inconsistent["compiler_dependencies"]["enabled"] = False
    report_inconsistent = audit_rows(ents, rels, inconsistent)
    assert report_inconsistent["ok"] is False
    assert "invalid_enabled_block" in _codes(report_inconsistent)


def test_evidence_without_manifest_fails():
    ents, rels, _manifest = _enabled_graph()
    report = audit_rows(ents, rels, {})
    assert report["ok"] is False
    assert "legacy_block_missing_with_fields" in _codes(report)


def test_manifest_without_evidence_fails():
    ents, _rels, manifest = _enabled_graph()
    clean_ents, clean_rels, _ = _legacy_graph()
    report = audit_rows(clean_ents, clean_rels, manifest)
    assert report["ok"] is False
    assert "manifest_count_mismatch" in _codes(report)


def test_off_with_evidence_fails():
    ents, rels, _manifest = _enabled_graph()
    report = audit_rows(
        ents, rels, {"compiler_dependencies": build_disabled_provenance()}
    )
    assert report["ok"] is False
    assert "off_with_decorated_relationships" in _codes(report)


def test_every_missing_relationship_producer_field_fails():
    ents, rels, manifest = _enabled_graph()
    for field in _DEP_FIELDS:
        rows = copy.deepcopy(rels)
        _decorated(rows).pop(field)
        report = audit_rows(ents, rows, manifest)
        assert report["ok"] is False, field
        codes = _codes(report)
        if field in {"type", "fact_kind", "extractor"}:
            assert "contradictory_carrier" in codes, (field, codes)
        elif field == "id":
            assert "empty_relationship_id" in codes or (
                "partial_dependency_payload" in codes
            ), (field, codes)
        else:
            assert "partial_dependency_payload" in codes, (field, codes)


def test_nullable_compiler_id_present_as_null_passes():
    ents, rels, manifest = _enabled_graph()
    rows = copy.deepcopy(rels)
    for row in _decorated_all(rows):
        row["compiler_id"] = None
    block = copy.deepcopy(manifest["compiler_dependencies"])
    block["compiler_id"] = None
    block["compilers"][0]["compiler_id"] = None
    report = audit_rows(ents, rows, {"compiler_dependencies": block})
    assert report["ok"] is True, report["anomalies"]
    assert "compiler_id" in _decorated(rows)


def test_dropped_nullable_parquet_column_fails_without_repair(tmp_path: Path):
    ents, rels, manifest = _enabled_graph()
    graph = _publish(tmp_path, ents, rels, manifest["compiler_dependencies"])
    snap = graph / "snapshots" / "s1"
    df = pd.read_parquet(snap / "relationships.parquet")
    assert "compiler_id" in df.columns
    df = df.drop(columns=["compiler_id"])
    df.to_parquet(snap / "relationships.parquet")
    report = audit_graph_root(graph)
    assert report["ok"] is False
    assert "partial_dependency_payload" in _codes(report)
    import c_compiler_facts as facts  # type: ignore

    assert not hasattr(facts, "restore_missing_dependency_keys")
    assert not hasattr(facts, "repair_persisted_compiler_dependencies")


def test_contradictory_carrier_markers_fail():
    ents, rels, manifest = _enabled_graph()
    for field, value in (
        ("type", "includes"),
        ("fact_kind", "configured_direct_include"),
        ("extractor", "c-compiler-includes"),
    ):
        rows = copy.deepcopy(rels)
        _decorated(rows)[field] = value
        report = audit_rows(ents, rows, manifest)
        assert report["ok"] is False, field
        assert "contradictory_carrier" in _codes(report), field


def test_wrong_type_fact_kind_extractor_fail():
    ents, rels, manifest = _enabled_graph()
    contains = next(r for r in rels if r["type"] == "contains")
    rows = copy.deepcopy(rels)
    payload = {
        key: _decorated(rows)[key]
        for key in ("fact_kind", "extractor")
    }
    target = next(r for r in rows if r["type"] == "contains")
    target.update(payload)
    report = audit_rows(ents, rows, manifest)
    assert report["ok"] is False
    assert "contradictory_carrier" in _codes(report)
    assert contains["type"] == "contains"


def test_incompatible_material_overlay_fields_fail():
    ents, rels, manifest = _enabled_graph()
    rows = copy.deepcopy(rels)
    target = _decorated(rows)
    target["clang_call_status"] = "matched"
    target["clang_type_use_status"] = "matched"
    report = audit_rows(ents, rows, manifest)
    assert report["ok"] is False
    assert "incompatible_overlay_field" in _codes(report)

    null_ok = copy.deepcopy(rels)
    _decorated(null_ok)["clang_call_status"] = None
    report_null = audit_rows(ents, null_ok, manifest)
    assert report_null["ok"] is True, report_null["anomalies"]

    unknown = copy.deepcopy(rels)
    _decorated(unknown)["compiler_version"] = "17.0.0"
    report_unknown = audit_rows(ents, unknown, manifest)
    assert report_unknown["ok"] is False
    assert "unknown_dependency_field" in _codes(report_unknown)


def test_empty_and_duplicate_relationship_ids_fail():
    ents, rels, manifest = _enabled_graph()
    dup = copy.deepcopy(rels)
    dup[1]["id"] = dup[0]["id"]
    report_dup = audit_rows(ents, dup, manifest)
    assert report_dup["ok"] is False
    assert "duplicate_relationship_id" in _codes(report_dup)

    empty = copy.deepcopy(rels)
    empty[0]["id"] = ""
    report_empty = audit_rows(ents, empty, manifest)
    assert report_empty["ok"] is False
    assert "empty_relationship_id" in _codes(report_empty)


def test_duplicate_endpoint_pairs_fail():
    ents, rels, manifest = _enabled_graph()
    rows = copy.deepcopy(rels)
    first = _decorated(rows)
    clone = copy.deepcopy(first)
    clone["id"] = "rel:depends_on:duplicate"
    clone["human_readable_id"] = 99
    rows.append(clone)
    report = audit_rows(ents, rows, manifest)
    assert report["ok"] is False
    assert "duplicate_endpoint_pair" in _codes(report)


def test_missing_and_non_file_endpoints_fail():
    ents, rels, manifest = _enabled_graph()
    missing = copy.deepcopy(rels)
    _decorated(missing)["target"] = "ghost:ghost.h"
    missing[0]["id"] = dependency_relationship_id("main:main.c", "ghost:ghost.h")
    report_missing = audit_rows(ents, missing, manifest)
    assert report_missing["ok"] is False
    assert "endpoint_mismatch" in _codes(report_missing)

    non_file = copy.deepcopy(rels)
    _decorated(non_file)["target"] = "main:main"
    non_file[0]["id"] = dependency_relationship_id("main:main.c", "main:main")
    report_non_file = audit_rows(ents, non_file, manifest)
    assert report_non_file["ok"] is False
    assert "endpoint_mismatch" in _codes(report_non_file)

    same = copy.deepcopy(rels)
    _decorated(same)["target"] = "main:main.c"
    same[0]["id"] = dependency_relationship_id("main:main.c", "main:main.c")
    report_same = audit_rows(ents, same, manifest)
    assert report_same["ok"] is False
    assert "endpoint_mismatch" in _codes(report_same)


def test_source_file_disagreement_fails():
    ents, rels, manifest = _enabled_graph()
    rows = copy.deepcopy(rels)
    _decorated(rows)["source_file"] = "/tmp/pkg/other.c"
    report = audit_rows(ents, rows, manifest)
    assert report["ok"] is False
    assert "source_file_mismatch" in _codes(report)

    relative = copy.deepcopy(rels)
    _decorated(relative)["source_file"] = "main.c"
    report_rel = audit_rows(ents, relative, manifest)
    assert report_rel["ok"] is False
    assert "source_file_mismatch" in _codes(report_rel)


def test_invalid_deterministic_id_fails():
    ents, rels, manifest = _enabled_graph()
    rows = copy.deepcopy(rels)
    _decorated(rows)["id"] = "rel:depends_on:wrong"
    report = audit_rows(ents, rows, manifest)
    assert report["ok"] is False
    assert "identity_mismatch" in _codes(report)
    expected = dependency_relationship_id("main:main.c", "direct:direct.h")
    assert expected.startswith("rel:depends_on:")
    assert expected != "rel:depends_on:wrong"


def test_invalid_human_readable_id_fails():
    ents, rels, manifest = _enabled_graph()
    for value in (0, -1, 1.0, 1.5, "1", True, None):
        rows = copy.deepcopy(rels)
        _decorated(rows)["human_readable_id"] = value
        report = audit_rows(ents, rows, manifest)
        assert report["ok"] is False, value
        assert "human_readable_id" in _codes(report), value

    dup = copy.deepcopy(rels)
    _decorated_all(dup)[1]["human_readable_id"] = 1
    report_dup = audit_rows(ents, dup, manifest)
    assert report_dup["ok"] is False
    assert "human_readable_id" in _codes(report_dup)


def test_invalid_exact_description_fails():
    ents, rels, manifest = _enabled_graph()
    rows = copy.deepcopy(rels)
    _decorated(rows)["description"] = "depends on header"
    report = audit_rows(ents, rows, manifest)
    assert report["ok"] is False
    assert "description_mismatch" in _codes(report)


def test_invalid_weight_confidence_determinism_fail():
    ents, rels, manifest = _enabled_graph()
    for field, value in (
        ("weight", 0.9),
        ("confidence", 0.5),
        ("is_deterministic", False),
        ("is_deterministic", 1),
        ("weight", True),
    ):
        rows = copy.deepcopy(rels)
        _decorated(rows)[field] = value
        report = audit_rows(ents, rows, manifest)
        assert report["ok"] is False, (field, value)
        assert "dependency_field_type" in _codes(report), (field, value)


def test_nonempty_span_or_metadata_lists_fail():
    ents, rels, manifest = _enabled_graph()
    for field, value in (
        ("span", "1:0"),
        ("text_unit_ids", ["tu:1"]),
        ("document_ids", ["doc:1"]),
        ("covariate_ids", ["cov:1"]),
    ):
        rows = copy.deepcopy(rels)
        _decorated(rows)[field] = value
        report = audit_rows(ents, rows, manifest)
        assert report["ok"] is False, field
        assert "dependency_field_type" in _codes(report), field


def test_invalid_preprocessor_provenance_fails():
    ents, rels, manifest = _enabled_graph()
    rows = copy.deepcopy(rels)
    _decorated(rows)["preprocessor_dependent"] = False
    report = audit_rows(ents, rows, manifest)
    assert report["ok"] is False
    assert "preprocessor_provenance" in _codes(report)

    reasons = copy.deepcopy(rels)
    _decorated(reasons)["preprocessor_reasons"] = ["textual_include"]
    report_reasons = audit_rows(ents, reasons, manifest)
    assert report_reasons["ok"] is False
    assert "preprocessor_provenance" in _codes(report_reasons)


def test_digest_mismatch_and_malformed_sha256_fail():
    ents, rels, manifest = _enabled_graph()
    mismatch = copy.deepcopy(rels)
    _decorated(mismatch)["compile_commands_digest"] = "b" * 64
    report_mismatch = audit_rows(ents, mismatch, manifest)
    assert report_mismatch["ok"] is False
    assert "digest_mismatch" in _codes(report_mismatch)

    for value in ("ABC" + "a" * 61, "a" * 63, "", None, 1):
        broken = copy.deepcopy(manifest)
        broken["compiler_dependencies"]["compile_commands_digest"] = value
        report = audit_rows(ents, rels, broken)
        assert report["ok"] is False, value
        assert "digest_mismatch" in _codes(report), value


def test_compiler_record_missing_and_extra_keys_fail():
    ents, rels, manifest = _enabled_graph()
    missing = copy.deepcopy(manifest)
    missing["compiler_dependencies"]["compilers"][0].pop("compiler_version")
    report_missing = audit_rows(ents, rels, missing)
    assert report_missing["ok"] is False
    assert "compiler_mismatch" in _codes(report_missing)

    extra = copy.deepcopy(manifest)
    extra["compiler_dependencies"]["compilers"][0]["unexpected"] = True
    report_extra = audit_rows(ents, rels, extra)
    assert report_extra["ok"] is False
    assert "compiler_mismatch" in _codes(report_extra)


def test_duplicate_noncanonical_relative_compiler_paths_fail():
    ents, rels, manifest = _enabled_graph()

    relative = copy.deepcopy(manifest)
    relative["compiler_dependencies"]["compiler_path"] = "clang"
    relative["compiler_dependencies"]["compilers"][0]["compiler_path"] = "clang"
    report_rel = audit_rows(ents, rels, relative)
    assert report_rel["ok"] is False
    assert "compiler_mismatch" in _codes(report_rel)

    dotted = copy.deepcopy(manifest)
    dotted["compiler_dependencies"]["compiler_path"] = "/usr/bin/../bin/clang"
    dotted["compiler_dependencies"]["compilers"][0]["compiler_path"] = (
        "/usr/bin/../bin/clang"
    )
    report_dot = audit_rows(ents, rels, dotted)
    assert report_dot["ok"] is False
    assert "compiler_mismatch" in _codes(report_dot)

    dup = copy.deepcopy(manifest)
    only = copy.deepcopy(dup["compiler_dependencies"]["compilers"][0])
    dup["compiler_dependencies"]["compilers"] = [only, copy.deepcopy(only)]
    report_dup = audit_rows(ents, rels, dup)
    assert report_dup["ok"] is False
    assert "compiler_mismatch" in _codes(report_dup)


def test_compiler_id_and_version_type_and_whitespace_errors():
    ents, rels, manifest = _enabled_graph()
    for field, value in (
        ("compiler_id", "  spaced  "),
        ("compiler_id", ""),
        ("compiler_id", 17),
        ("compiler_version", " 17.0.0 "),
        ("compiler_version", ""),
        ("compiler_version", 17),
    ):
        broken = copy.deepcopy(manifest)
        broken["compiler_dependencies"][field] = value
        broken["compiler_dependencies"]["compilers"][0][field] = value
        report = audit_rows(ents, rels, broken)
        assert report["ok"] is False, (field, value)
        assert "compiler_mismatch" in _codes(report), (field, value)


def test_singular_and_multi_compiler_contradictions_fail():
    ents, rels, manifest = _enabled_graph()
    singular = copy.deepcopy(manifest)
    singular["compiler_dependencies"]["compiler_id"] = "other"
    report_singular = audit_rows(ents, rels, singular)
    assert report_singular["ok"] is False
    assert "compiler_mismatch" in _codes(report_singular)

    second = {
        "compiler_path": "/usr/bin/gcc",
        "compiler_id": "gcc version test",
        "compiler_version": "14.0.0",
    }
    multi = copy.deepcopy(manifest)
    first = multi["compiler_dependencies"]["compilers"][0]
    multi["compiler_dependencies"]["compilers"] = sorted(
        [first, second], key=lambda item: item["compiler_path"]
    )
    report_multi = audit_rows(ents, rels, multi)
    assert report_multi["ok"] is False
    assert "compiler_mismatch" in _codes(report_multi)

    multi_ok = copy.deepcopy(multi)
    for field in ("compiler_path", "compiler_id", "compiler_version"):
        multi_ok["compiler_dependencies"][field] = None
    report_multi_ok = audit_rows(ents, rels, multi_ok)
    assert report_multi_ok["ok"] is True, report_multi_ok["anomalies"]

    unordered = copy.deepcopy(multi_ok)
    unordered["compiler_dependencies"]["compilers"] = [second, first]
    report_order = audit_rows(ents, rels, unordered)
    assert report_order["ok"] is False
    assert "compiler_mismatch" in _codes(report_order)


def test_translation_unit_titles_contract_errors():
    ents, rels, manifest = _enabled_graph()
    for value, _note in (
        ("main:main.c", "not a list"),
        (["main:main.c", "direct:direct.h"], "unsorted extra"),
    ):
        broken = copy.deepcopy(manifest)
        broken["compiler_dependencies"]["translation_unit_titles"] = value
        if isinstance(value, list):
            broken["compiler_dependencies"]["n_translation_units"] = len(value)
        report = audit_rows(ents, rels, broken)
        assert report["ok"] is False, _note
        assert "translation_unit_titles" in _codes(report), _note

    unsorted = copy.deepcopy(manifest)
    unsorted["compiler_dependencies"]["translation_unit_titles"] = [
        "z:z.c",
        "main:main.c",
    ]
    unsorted["compiler_dependencies"]["n_translation_units"] = 2
    ents_unsorted = _base_entities() + [_file_entity("z:z.c", "/tmp/pkg/z.c")]
    report_unsorted = audit_rows(ents_unsorted, rels, unsorted)
    assert report_unsorted["ok"] is False
    assert "translation_unit_titles" in _codes(report_unsorted)

    dup = copy.deepcopy(manifest)
    dup["compiler_dependencies"]["translation_unit_titles"] = [
        "main:main.c",
        "main:main.c",
    ]
    dup["compiler_dependencies"]["n_translation_units"] = 2
    report_dup = audit_rows(ents, rels, dup)
    assert report_dup["ok"] is False
    assert "translation_unit_titles" in _codes(report_dup)

    empty = copy.deepcopy(manifest)
    empty["compiler_dependencies"]["translation_unit_titles"] = [""]
    empty["compiler_dependencies"]["n_translation_units"] = 1
    report_empty = audit_rows(ents, rels, empty)
    assert report_empty["ok"] is False
    assert "translation_unit_titles" in _codes(report_empty)

    for invalid_title in (" main:main.c", "main:main.c ", 1, None):
        invalid = copy.deepcopy(manifest)
        invalid["compiler_dependencies"]["translation_unit_titles"] = [
            invalid_title
        ]
        report_invalid = audit_rows(ents, rels, invalid)
        assert report_invalid["ok"] is False, invalid_title
        assert "translation_unit_titles" in _codes(report_invalid), invalid_title

    missing_ent = copy.deepcopy(manifest)
    missing_ent["compiler_dependencies"]["translation_unit_titles"] = [
        "ghost:ghost.c"
    ]
    report_missing = audit_rows(ents, rels, missing_ent)
    assert report_missing["ok"] is False
    assert "translation_unit_titles" in _codes(report_missing)

    ambiguous_ents = _base_entities() + [
        {
            **_file_entity("main:main.c", SRC_MAIN),
            "id": "ent:file:duplicate-main",
        }
    ]
    report_ambiguous = audit_rows(ambiguous_ents, rels, manifest)
    assert report_ambiguous["ok"] is False
    assert "translation_unit_titles" in _codes(report_ambiguous)

    count = copy.deepcopy(manifest)
    count["compiler_dependencies"]["n_translation_units"] = 2
    report_count = audit_rows(ents, rels, count)
    assert report_count["ok"] is False
    assert "translation_unit_titles" in _codes(report_count)

    no_titles = copy.deepcopy(manifest)
    no_titles["compiler_dependencies"]["translation_unit_titles"] = []
    no_titles["compiler_dependencies"]["n_translation_units"] = 0
    report_no_titles = audit_rows(ents, rels, no_titles)
    assert report_no_titles["ok"] is False
    assert "translation_unit_titles" in _codes(report_no_titles)

    zero_edge_ok = copy.deepcopy(manifest)
    extra_ents = _base_entities() + [_file_entity("other:other.c", SRC_OTHER)]
    zero_edge_ok["compiler_dependencies"]["translation_unit_titles"] = [
        "main:main.c",
        "other:other.c",
    ]
    zero_edge_ok["compiler_dependencies"]["n_translation_units"] = 2
    report_ok = audit_rows(extra_ents, rels, zero_edge_ok)
    assert report_ok["ok"] is True, report_ok["anomalies"]

    source_missing = copy.deepcopy(manifest)
    source_missing["compiler_dependencies"]["translation_unit_titles"] = [
        "other:other.c"
    ]
    report_source = audit_rows(extra_ents, rels, source_missing)
    assert report_source["ok"] is False
    assert "translation_unit_titles" in _codes(report_source)


def test_n_facts_and_collected_and_added_mismatches_fail():
    ents, rels, manifest = _enabled_graph()
    for field, value in (
        ("n_facts", 9),
        ("n_facts_collected", 1),
        ("n_facts_added", 9),
        ("n_facts_added", -1),
        ("n_facts", True),
        ("n_compile_entries", 0),
    ):
        broken = copy.deepcopy(manifest)
        broken["compiler_dependencies"][field] = value
        if field == "n_facts":
            broken["compiler_dependencies"]["n_facts_collected"] = value
        report = audit_rows(ents, rels, broken)
        assert report["ok"] is False, (field, value)
        assert "manifest_count_mismatch" in _codes(report), (field, value)

    counted = copy.deepcopy(manifest)
    counted["counts"] = {"relationships": 99}
    report_counts = audit_rows(ents, rels, counted)
    assert report_counts["ok"] is False
    assert "manifest_count_mismatch" in _codes(report_counts)

    zero_facts = {
        "compiler_dependencies": _enabled_block(
            n_facts=0,
            n_facts_added=0,
            n_facts_collected=False,
        )
    }
    report_false_collected = audit_rows(ents, [_contains()], zero_facts)
    assert report_false_collected["ok"] is False
    assert "manifest_count_mismatch" in _codes(report_false_collected)


def test_output_is_deterministic():
    ents, rels, manifest = _enabled_graph()
    first = audit_to_json(audit_rows(ents, rels, manifest))
    second = audit_to_json(
        audit_rows(copy.deepcopy(ents), copy.deepcopy(rels), copy.deepcopy(manifest))
    )
    assert first == second
    parsed = json.loads(first)
    assert parsed["ok"] is True
    assert list(parsed.keys()) == sorted(parsed.keys())


def _publish(
    tmp_path: Path,
    entities: list,
    relationships: list,
    block,
    *,
    name: str = "g",
) -> Path:
    graph = tmp_path / name
    snap = graph / "snapshots" / "s1"
    snap.mkdir(parents=True)
    pd.DataFrame(entities).to_parquet(snap / "entities.parquet")
    pd.DataFrame(relationships).to_parquet(snap / "relationships.parquet")
    pd.DataFrame([{"id": "t1", "title": "a"}]).to_parquet(snap / "text_units.parquet")
    manifest = {}
    if block is not None:
        manifest["compiler_dependencies"] = block
    (snap / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    (graph / "current").write_text("s1", encoding="utf-8")
    return graph


def test_graph_root_is_read_only(tmp_path: Path):
    ents, rels, manifest = _enabled_graph()
    graph = _publish(tmp_path, ents, rels, manifest["compiler_dependencies"])
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
    ents, rels, manifest = _enabled_graph()
    graph = _publish(tmp_path, ents, rels, manifest["compiler_dependencies"])
    forbidden = graph / "snapshots" / "s1" / "dep-audit.json"
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
    ents, rels, manifest = _enabled_graph()
    good = _publish(
        tmp_path, ents, rels, manifest["compiler_dependencies"], name="good"
    )
    out_path = tmp_path / "report.json"
    assert audit_main(["--graph", str(good), "--output", str(out_path), "--json"]) == 0
    written = json.loads(out_path.read_text(encoding="utf-8"))
    assert written["ok"] is True
    stdout = capsys.readouterr().out
    assert json.loads(stdout)["ok"] is True

    bad_rels = copy.deepcopy(rels)
    _decorated(bad_rels)["confidence"] = 0.5
    bad = _publish(
        tmp_path, ents, bad_rels, manifest["compiler_dependencies"], name="bad"
    )
    assert audit_main(["--graph", str(bad)]) == 1
    capsys.readouterr()
    assert audit_main(["--graph", str(tmp_path / "missing")]) == 2


def test_format_report_is_human_readable():
    ents, rels, manifest = _enabled_graph()
    text = format_report(audit_rows(ents, rels, manifest))
    assert "RESULT: PASS" in text
    assert "read-only" in text


def test_published_graph_health_states():
    from published_graph_health import (  # type: ignore
        _call_integrity,
        _compiler_dependency_integrity,
        _signature_integrity,
        _type_integrity,
        _type_shape_integrity,
        _type_use_integrity,
    )

    ents, rels, manifest = _enabled_graph()
    enabled = _compiler_dependency_integrity(
        {"entities": ents, "relationships": rels}, manifest, indexer="c"
    )
    assert enabled is not None
    assert enabled["ok"] is True
    assert enabled["status"] == "enabled"
    assert enabled["n_decorated_relationships"] == 2

    legacy_ents, legacy_rels, legacy_manifest = _legacy_graph()
    legacy = _compiler_dependency_integrity(
        {"entities": legacy_ents, "relationships": legacy_rels},
        legacy_manifest,
        indexer="c",
    )
    assert legacy["ok"] is True and legacy["status"] == "legacy_absent"

    off_ents, off_rels, off_manifest = _off_graph()
    off = _compiler_dependency_integrity(
        {"entities": off_ents, "relationships": off_rels},
        off_manifest,
        indexer="c",
    )
    assert off["ok"] is True and off["status"] == "off"

    orphan = _compiler_dependency_integrity(
        {"entities": ents, "relationships": rels}, {}, indexer="c"
    )
    assert orphan["ok"] is False
    assert (
        _compiler_dependency_integrity(
            {"entities": ents, "relationships": rels}, manifest, indexer="python"
        )
        is None
    )

    empty = {"relationships": rels, "entities": ents}
    assert _type_use_integrity(empty, {}, indexer="c")["ok"] is True
    assert _type_shape_integrity({"entities": []}, {}, indexer="c")["ok"] is True
    assert _type_integrity({"entities": []}, {}, indexer="c")["ok"] is True
    assert _signature_integrity({"entities": []}, {}, indexer="c")["ok"] is True
    assert _call_integrity({"relationships": []}, {}, indexer="c")["ok"] is True


def test_validator_is_pure_and_does_not_invoke_compiler(monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("read-only audit must not invoke a compiler")

    import c_compiler_common as common_mod  # type: ignore
    import c_compiler_facts as facts_mod  # type: ignore

    monkeypatch.setattr(facts_mod, "collect_translation_unit_dependencies", boom)
    monkeypatch.setattr(facts_mod, "append_compiler_dependencies", boom)
    monkeypatch.setattr(common_mod, "load_compile_entries", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "check_output", boom)
    ents, rels, manifest = _enabled_graph()
    report = validate_persisted_compiler_dependency_overlay(ents, rels, manifest)
    assert report["ok"] is True, report["anomalies"]


def _fail_if_compiler_used(monkeypatch):
    import c_compiler_common as common_mod  # type: ignore
    import c_compiler_facts as facts_mod  # type: ignore

    def boom(*_args, **_kwargs):
        raise AssertionError("read-only audit must not invoke a compiler")

    monkeypatch.setattr(facts_mod, "collect_translation_unit_dependencies", boom)
    monkeypatch.setattr(facts_mod, "append_compiler_dependencies", boom)
    monkeypatch.setattr(facts_mod, "collect_tu_dependencies_for_entry", boom)
    monkeypatch.setattr(common_mod, "load_compile_entries", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "check_output", boom)
    monkeypatch.setattr(subprocess, "check_call", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)


def _write_fixture_package(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "main.c").write_text(
        '#include "direct.h"\n'
        "#include <stdio.h>\n"
        "int main(void) { return direct_fn(); }\n",
        encoding="utf-8",
    )
    (root / "direct.h").write_text(
        '#include "trans.h"\n'
        "int direct_fn(void);\n",
        encoding="utf-8",
    )
    (root / "trans.h").write_text(
        "int trans_fn(void);\n",
        encoding="utf-8",
    )
    return root


def _write_compile_commands(root: Path, *, compiler: str) -> None:
    entry = {
        "directory": str(root),
        "file": "main.c",
        "command": f"{compiler} -c -isystem . main.c -o main.o",
    }
    (root / "compile_commands.json").write_text(
        json.dumps([entry]), encoding="utf-8"
    )


def _index(package: Path, graph: Path, *, deps: bool) -> None:
    from index_c import main as index_c_main  # type: ignore

    index_c_main(
        package=package,
        graph=graph,
        keep_snapshots=2,
        compiler_builtins=False,
        compiler_dependencies=deps,
        compiler_includes=False,
        clang_signatures=False,
        clang_calls=False,
        clang_types=False,
        clang_type_uses=False,
        clang_type_shapes=False,
        allow_toolchain_drift=False,
    )


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_fixture_dependency_graph_audit(tmp_path: Path, monkeypatch):
    compiler = _cc()
    assert compiler is not None
    pkg = _write_fixture_package(tmp_path / "pkg")
    _write_compile_commands(pkg, compiler=compiler)
    graph = tmp_path / "byog_fixture_deps"
    before = {p.name for p in pkg.iterdir()}
    _index(pkg, graph, deps=True)
    snap = (graph / "current").read_text(encoding="utf-8").strip()
    block = json.loads(
        (graph / "snapshots" / snap / "manifest.json").read_text(encoding="utf-8")
    )["compiler_dependencies"]
    assert block["n_facts"] == 2
    assert block["n_translation_units"] == 1
    _fail_if_compiler_used(monkeypatch)
    report = audit_graph_root(graph)
    assert report["ok"] is True, report["anomalies"]
    assert report["status"] == "enabled"
    assert report["n_decorated_relationships"] == 2 == block["n_facts"]
    assert report["n_translation_units"] == 1 == block["n_translation_units"]
    assert report["read_only_verification"]["verified"] is True
    assert {p.name for p in pkg.iterdir()} == before
    for pattern in ("*.o", "*.ast", "*.d", "*.i", "*.ii", "*.pch"):
        assert not list(pkg.rglob(pattern))


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_cjson_dependency_graph_audit(tmp_path: Path, monkeypatch):
    pkg = ROOT / "examples" / "cjson"
    package_before = {p.name for p in pkg.iterdir()}
    graph = tmp_path / "byog_cjson_deps"
    _index(pkg, graph, deps=True)
    snap = (graph / "current").read_text(encoding="utf-8").strip()
    block = json.loads(
        (graph / "snapshots" / snap / "manifest.json").read_text(encoding="utf-8")
    )["compiler_dependencies"]
    _fail_if_compiler_used(monkeypatch)
    report = audit_graph_root(graph)
    assert report["ok"] is True, report["anomalies"]
    assert report["status"] == "enabled"
    assert report["n_decorated_relationships"] == block["n_facts"]
    assert report["n_translation_units"] == block["n_translation_units"]
    assert report["n_decorated_relationships"] >= 0
    assert report["read_only_verification"]["verified"] is True
    assert {p.name for p in pkg.iterdir()} == package_before
    for pattern in ("*.o", "*.ast", "*.d", "*.i", "*.ii", "*.pch"):
        assert not list(pkg.rglob(pattern))


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_inih_dependency_graph_audit(tmp_path: Path, monkeypatch):
    pkg = ROOT / "examples" / "inih"
    graph = tmp_path / "byog_inih_deps"
    _index(pkg, graph, deps=True)
    snap = (graph / "current").read_text(encoding="utf-8").strip()
    block = json.loads(
        (graph / "snapshots" / snap / "manifest.json").read_text(encoding="utf-8")
    )["compiler_dependencies"]
    _fail_if_compiler_used(monkeypatch)
    report = audit_graph_root(graph)
    assert report["ok"] is True, report["anomalies"]
    assert report["status"] == "enabled"
    assert report["n_decorated_relationships"] == block["n_facts"]
    assert report["n_translation_units"] == block["n_translation_units"]
    assert report["read_only_verification"]["verified"] is True
    for pattern in ("*.o", "*.ast", "*.d", "*.i", "*.ii", "*.pch"):
        assert not list(pkg.rglob(pattern))


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_default_off_and_health(tmp_path: Path, monkeypatch):
    from published_graph_health import PublishedGraphSpec, check_spec  # type: ignore

    pkg = ROOT / "examples" / "inih"
    off_graph = tmp_path / "byog_inih_off"
    _index(pkg, off_graph, deps=False)
    _fail_if_compiler_used(monkeypatch)
    off_report = audit_graph_root(off_graph)
    assert off_report["ok"] is True
    assert off_report["status"] == "off"

    spec = PublishedGraphSpec(
        ident="inih_tmp_off",
        source=Path("examples/inih"),
        graph=str(off_graph),
        indexer="c",
        mode="mutable",
    )
    result = check_spec(spec, root=ROOT, graph_root=off_graph)
    assert result["status"] == "pass", result
    deps = result["compiler_dependency_integrity"]
    assert deps["ok"] is True
    assert deps["status"] == "off"
    assert result["clang_call_integrity"]["ok"] is True
    assert result["clang_signature_integrity"]["ok"] is True
    assert result["clang_type_integrity"]["ok"] is True
    assert result["clang_type_use_integrity"]["ok"] is True
    assert result["clang_type_shape_integrity"]["ok"] is True

    monkeypatch.undo()
    enabled_graph = tmp_path / "byog_inih_deps"
    _index(pkg, enabled_graph, deps=True)
    enabled_spec = PublishedGraphSpec(
        ident="inih_tmp_on",
        source=Path("examples/inih"),
        graph=str(enabled_graph),
        indexer="c",
        mode="mutable",
    )
    enabled_result = check_spec(spec=enabled_spec, root=ROOT, graph_root=enabled_graph)
    assert enabled_result["compiler_dependency_integrity"]["ok"] is True
    assert enabled_result["compiler_dependency_integrity"]["status"] == "enabled"
    assert enabled_result["clang_call_integrity"]["ok"] is True
    assert enabled_result["clang_signature_integrity"]["ok"] is True
    assert enabled_result["clang_type_integrity"]["ok"] is True
    assert enabled_result["clang_type_use_integrity"]["ok"] is True
    assert enabled_result["clang_type_shape_integrity"]["ok"] is True
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
