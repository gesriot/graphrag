"""Optional configured Clang uses_type graph overlay.

Pure application tests always run. Live Clang tests skip when no compiler.

Run: uv run python -m pytest examples/cjson/tests/test_c_clang_type_uses.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

import c_clang_ast_capture as cap_mod  # type: ignore
from c_clang_type_use_audit import MODE as AUDIT_MODE  # type: ignore
from c_clang_type_uses import (  # type: ignore
    FACT_KIND,
    MODE,
    REL_TYPE,
    ClangTypeUseOverlayError,
    append_clang_type_uses,
    apply_clang_type_uses_from_report,
    build_disabled_provenance,
    _relationship_id,
)
from c_preprocessor import find_c_compiler  # type: ignore
from extract_c import build_c_byog  # type: ignore
from index_c import main as index_c_main  # type: ignore


def _cc():
    return find_c_compiler()


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
        "mode": AUDIT_MODE,
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


def _base_data(entities: list) -> dict:
    return {
        "entities": entities,
        "relationships": [
            {
                "id": "rel:contains:a:f",
                "source": "ent:file:a:a.c",
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
        ],
        "text_units": [],
    }


# ---------------------------------------------------------------------------
# Pure application
# ---------------------------------------------------------------------------


def test_apply_one_matched_row_creates_uses_type(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    data = _base_data(
        [
            _entity(title="a:f", etype="function"),
            _entity(title="a:T", etype="typedef"),
            _entity(title="a:a.c", etype="file", eid="ent:file:a:a.c"),
        ]
    )
    report = _clean_report(
        [
            _matched_row(
                owner_title="a:f",
                target_title="a:T",
                qual="T",
            )
        ]
    )
    prov = apply_clang_type_uses_from_report(data, report, pkg)
    assert prov["enabled"] is True
    assert prov["mode"] == MODE
    assert prov["n_facts"] == 1
    assert prov["n_observations"] == 1
    uses = [r for r in data["relationships"] if r["type"] == REL_TYPE]
    assert len(uses) == 1
    edge = uses[0]
    assert edge["source"] == "a:f"
    assert edge["target"] == "a:T"
    assert edge["fact_kind"] == FACT_KIND
    assert edge["extractor"] == "clang-ast-json"
    assert edge["confidence"] == 1.0
    assert edge["is_deterministic"] is True
    assert edge["clang_type_use_observation_count"] == 1
    assert edge["clang_type_use_use_kinds"] == ["parameter"]
    assert "configuration" in edge["description"].lower() or "configured" in edge[
        "description"
    ].lower()
    # Base contains edge untouched
    assert data["relationships"][0]["id"] == "rel:contains:a:f"


def test_multiple_observations_aggregate_one_edge(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    data = _base_data(
        [
            _entity(title="a:f", etype="function"),
            _entity(title="a:T", etype="typedef"),
        ]
    )
    report = _clean_report(
        [
            _matched_row(
                owner_title="a:f",
                target_title="a:T",
                use_kind="parameter",
                line=1,
                qual="T",
            ),
            _matched_row(
                owner_title="a:f",
                target_title="a:T",
                use_kind="function_return",
                line=2,
                qual="T *",
            ),
            _matched_row(
                owner_title="a:f",
                target_title="a:T",
                use_kind="local_variable",
                line=3,
                qual="T",
            ),
        ]
    )
    prov = apply_clang_type_uses_from_report(data, report, pkg)
    assert prov["n_facts"] == 1
    assert prov["n_observations"] == 3
    edge = next(r for r in data["relationships"] if r["type"] == REL_TYPE)
    assert edge["clang_type_use_observation_count"] == 3
    assert edge["clang_type_use_use_kinds"] == [
        "function_return",
        "local_variable",
        "parameter",
    ]
    obs = json.loads(edge["clang_type_use_observations_json"])
    assert len(obs) == 3
    # Deterministic order
    assert obs == sorted(obs, key=lambda o: json.dumps(o, sort_keys=True))


def test_distinct_targets_distinct_edges(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    data = _base_data(
        [
            _entity(title="a:f", etype="function"),
            _entity(title="a:T", etype="typedef"),
            _entity(title="a:U", etype="typedef"),
        ]
    )
    report = _clean_report(
        [
            _matched_row(owner_title="a:f", target_title="a:T"),
            _matched_row(owner_title="a:f", target_title="a:U", qual="U"),
        ]
    )
    prov = apply_clang_type_uses_from_report(data, report, pkg)
    assert prov["n_facts"] == 2
    targets = sorted(
        r["target"] for r in data["relationships"] if r["type"] == REL_TYPE
    )
    assert targets == ["a:T", "a:U"]


def test_recursive_self_edge_retained(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    data = _base_data(
        [
            _entity(title="a:struct:T", etype="struct"),
        ]
    )
    report = _clean_report(
        [
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
    )
    prov = apply_clang_type_uses_from_report(data, report, pkg)
    assert prov["n_facts"] == 1
    edge = next(r for r in data["relationships"] if r["type"] == REL_TYPE)
    assert edge["source"] == edge["target"] == "a:struct:T"


def test_struct_vs_typedef_targets_distinct(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    data = _base_data(
        [
            _entity(title="m:f", etype="function"),
            _entity(title="m:struct:T", etype="struct"),
            _entity(title="m:typedef:T", etype="typedef"),
        ]
    )
    report = _clean_report(
        [
            _matched_row(
                owner_title="m:f",
                target_title="m:struct:T",
                target_kind="struct",
                qual="struct T *",
                resolver="exact_tag_spelling",
            ),
            _matched_row(
                owner_title="m:f",
                target_title="m:typedef:T",
                target_kind="typedef",
                qual="T",
                resolver="unique_typedef_spelling",
            ),
        ]
    )
    prov = apply_clang_type_uses_from_report(data, report, pkg)
    assert prov["n_facts"] == 2
    edges = [r for r in data["relationships"] if r["type"] == REL_TYPE]
    assert {e["target"] for e in edges} == {"m:struct:T", "m:typedef:T"}
    assert {e["clang_type_use_target_entity_id"] for e in edges} == {
        "ent:struct:m:struct:T",
        "ent:typedef:m:typedef:T",
    }


def test_same_name_different_modules_by_title(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    data = _base_data(
        [
            _entity(title="left:f", etype="function"),
            _entity(title="right:f", etype="function"),
            _entity(title="left:T", etype="typedef"),
            _entity(title="right:T", etype="typedef"),
        ]
    )
    report = _clean_report(
        [
            _matched_row(
                owner_title="left:f",
                target_title="left:T",
                source_path="left/a.c",
            ),
            _matched_row(
                owner_title="right:f",
                target_title="right:T",
                source_path="right/a.c",
            ),
        ]
    )
    prov = apply_clang_type_uses_from_report(data, report, pkg)
    assert prov["n_facts"] == 2
    pairs = {
        (r["source"], r["target"])
        for r in data["relationships"]
        if r["type"] == REL_TYPE
    }
    assert pairs == {("left:f", "left:T"), ("right:f", "right:T")}


def test_entry_scoped_provenance_in_observations(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    data = _base_data(
        [
            _entity(title="a:f", etype="function"),
            _entity(title="a:T", etype="typedef"),
        ]
    )
    report = _clean_report(
        [
            _matched_row(
                owner_title="a:f",
                target_title="a:T",
                entry_indices=[0, 1],
            )
        ]
    )
    report["n_compile_entries"] = 2
    report["translation_units"] = [
        {
            "entry_index": 0,
            "file": "a.c",
            "package_local": True,
            "compiler_path": "/usr/bin/clang",
            "compiler_id": "Apple clang version test",
        },
        {
            "entry_index": 1,
            "file": "b.c",
            "package_local": True,
            "compiler_path": "/usr/bin/clang",
            "compiler_id": "Apple clang version test",
        },
    ]
    apply_clang_type_uses_from_report(data, report, pkg)
    edge = next(r for r in data["relationships"] if r["type"] == REL_TYPE)
    assert edge["clang_type_use_entry_indices"] == [0, 1]
    obs = json.loads(edge["clang_type_use_observations_json"])
    assert obs[0]["entry_indices"] == [0, 1]
    assert obs[0]["resolver"] == "unique_typedef_spelling"
    assert obs[0]["owner_resolver"] == "exact_declaration_site"


@pytest.mark.parametrize(
    "bucket",
    [
        "owner_unmatched",
        "target_unresolved",
        "ambiguous_target",
        "macro_location_unsupported",
    ],
)
def test_fail_closed_residual_buckets(tmp_path: Path, bucket: str):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    data = _base_data(
        [
            _entity(title="a:f", etype="function"),
            _entity(title="a:T", etype="typedef"),
        ]
    )
    report = _clean_report(
        [_matched_row(owner_title="a:f", target_title="a:T")]
    )
    report["counts"][bucket] = 1
    report[bucket] = [{"name": "bad"}]
    report["counts"]["type_uses_deduped_total"] += 1
    report["counts"]["type_uses_raw_observations"] += 1
    before = json.dumps(data, sort_keys=True)
    with pytest.raises(ClangTypeUseOverlayError, match="unclean type-use residuals"):
        apply_clang_type_uses_from_report(data, report, pkg)
    assert json.dumps(data, sort_keys=True) == before


def test_observation_only_buckets_allowed(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    data = _base_data(
        [
            _entity(title="a:f", etype="function"),
            _entity(title="a:T", etype="typedef"),
        ]
    )
    report = _clean_report(
        [_matched_row(owner_title="a:f", target_title="a:T")],
        external_or_system=2,
        unsupported_type_form=1,
        unowned_context=3,
    )
    report["external_or_system"] = [{"x": 1}, {"x": 2}]
    report["unsupported_type_form"] = [{"x": 1}]
    report["unowned_context"] = [{"x": 1}, {"x": 2}, {"x": 3}]
    prov = apply_clang_type_uses_from_report(data, report, pkg)
    assert prov["n_facts"] == 1
    assert prov["counts"]["external_or_system"] == 2
    assert prov["counts"]["unowned_context"] == 3


def test_missing_endpoint_fails(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    data = _base_data([_entity(title="a:f", etype="function")])
    report = _clean_report(
        [_matched_row(owner_title="a:f", target_title="a:T")]
    )
    before = json.dumps(data, sort_keys=True)
    with pytest.raises(ClangTypeUseOverlayError, match="no entity for target"):
        apply_clang_type_uses_from_report(data, report, pkg)
    assert json.dumps(data, sort_keys=True) == before


def test_non_unique_endpoint_fails(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    data = _base_data(
        [
            _entity(title="a:f", etype="function"),
            _entity(title="a:T", etype="typedef"),
            _entity(title="a:T", etype="typedef", eid="ent:typedef:dup"),
        ]
    )
    report = _clean_report(
        [_matched_row(owner_title="a:f", target_title="a:T")]
    )
    with pytest.raises(ClangTypeUseOverlayError, match="non-unique target"):
        apply_clang_type_uses_from_report(data, report, pkg)


def test_type_mismatch_fails(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    data = _base_data(
        [
            _entity(title="a:f", etype="function"),
            _entity(title="a:T", etype="struct"),  # graph says struct
        ]
    )
    report = _clean_report(
        [
            _matched_row(
                owner_title="a:f",
                target_title="a:T",
                target_kind="typedef",  # audit says typedef
            )
        ]
    )
    with pytest.raises(ClangTypeUseOverlayError, match="target type mismatch"):
        apply_clang_type_uses_from_report(data, report, pkg)


def test_conflicting_preexisting_relationship_fails(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    data = _base_data(
        [
            _entity(title="a:f", etype="function"),
            _entity(title="a:T", etype="typedef"),
        ]
    )
    # Seed a uses_type edge with wrong fact payload then re-apply conflicting.
    data["relationships"].append(
        {
            "id": "rel:uses_type:wrong",
            "source": "a:f",
            "target": "a:T",
            "type": REL_TYPE,
            "fact_kind": FACT_KIND,
            "clang_type_use_source_entity_id": "ent:function:a:f",
            "clang_type_use_target_entity_id": "ent:typedef:a:T",
            "clang_type_use_qual_type": "WRONG",  # unknown field style
            "human_readable_id": 99,
            "description": "stale",
            "weight": 1.0,
            "extractor": "other",
            "confidence": 0.5,
            "is_deterministic": False,
            "text_unit_ids": [],
            "document_ids": [],
            "covariate_ids": [],
            "source_file": "",
            "span": "",
        }
    )
    report = _clean_report(
        [_matched_row(owner_title="a:f", target_title="a:T")]
    )
    with pytest.raises(
        ClangTypeUseOverlayError,
        match="conflicting pre-existing relationship id|unknown pre-existing",
    ):
        apply_clang_type_uses_from_report(data, report, pkg)


def test_atomic_no_partial_mutation(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    data = _base_data(
        [
            _entity(title="a:f", etype="function"),
            _entity(title="a:T", etype="typedef"),
            _entity(title="a:g", etype="function"),
        ]
    )
    report = _clean_report(
        [
            _matched_row(owner_title="a:f", target_title="a:T"),
            _matched_row(owner_title="a:g", target_title="a:Missing"),
        ]
    )
    before = json.dumps(data, sort_keys=True)
    with pytest.raises(ClangTypeUseOverlayError, match="no entity for target"):
        apply_clang_type_uses_from_report(data, report, pkg)
    assert json.dumps(data, sort_keys=True) == before
    assert not any(r.get("type") == REL_TYPE for r in data["relationships"])


def test_missing_relationships_shape_is_atomic(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    data = {
        "entities": [
            _entity(title="a:f", etype="function"),
            _entity(title="a:T", etype="typedef"),
        ],
        "text_units": [],
    }
    report = _clean_report(
        [_matched_row(owner_title="a:f", target_title="a:T")]
    )
    before = json.dumps(data, sort_keys=True)
    with pytest.raises(
        ClangTypeUseOverlayError, match="data.relationships must be"
    ):
        apply_clang_type_uses_from_report(data, report, pkg)
    assert json.dumps(data, sort_keys=True) == before


def test_unrelated_relationship_id_collision_fails_atomically(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    source = _entity(title="a:f", etype="function")
    target = _entity(title="a:T", etype="typedef")
    data = _base_data([source, target])
    data["relationships"][0]["id"] = _relationship_id(
        source["title"], target["title"], source["id"], target["id"]
    )
    report = _clean_report(
        [_matched_row(owner_title="a:f", target_title="a:T")]
    )
    before = json.dumps(data, sort_keys=True)
    with pytest.raises(ClangTypeUseOverlayError, match="already exists"):
        apply_clang_type_uses_from_report(data, report, pkg)
    assert json.dumps(data, sort_keys=True) == before


def test_relationship_id_includes_entity_ids():
    first = _relationship_id("a:f", "a:T", "ent:function:one", "ent:type:one")
    second = _relationship_id("a:f", "a:T", "ent:function:two", "ent:type:one")
    assert first != second


def test_translation_unit_census_fails_closed(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    data = _base_data(
        [
            _entity(title="a:f", etype="function"),
            _entity(title="a:T", etype="typedef"),
        ]
    )
    report = _clean_report(
        [_matched_row(owner_title="a:f", target_title="a:T")]
    )
    report["translation_units"][0]["entry_index"] = 1
    before = json.dumps(data, sort_keys=True)
    with pytest.raises(ClangTypeUseOverlayError, match="entry_index"):
        apply_clang_type_uses_from_report(data, report, pkg)
    assert json.dumps(data, sort_keys=True) == before


def test_row_compiler_must_match_entry_compiler(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    data = _base_data(
        [
            _entity(title="a:f", etype="function"),
            _entity(title="a:T", etype="typedef"),
        ]
    )
    row = _matched_row(owner_title="a:f", target_title="a:T")
    row["compilers"] = [
        {
            "compiler_path": "/usr/bin/other-clang",
            "compiler_id": "other clang",
            "compile_commands_digest": "abc123",
        }
    ]
    report = _clean_report([row])
    report["compilers"].append(
        {
            "compiler_path": "/usr/bin/other-clang",
            "compiler_id": "other clang",
            "compiler_version": "17.0.0",
        }
    )
    report["compiler_path"] = None
    report["compiler_id"] = None
    report["compiler_version"] = None
    before = json.dumps(data, sort_keys=True)
    with pytest.raises(
        ClangTypeUseOverlayError, match="compile entry indices"
    ):
        apply_clang_type_uses_from_report(data, report, pkg)
    assert json.dumps(data, sort_keys=True) == before


def test_endpoint_symbol_name_mismatch_fails_closed(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    target = _entity(title="a:T", etype="typedef")
    target["symbol_name"] = "Wrong"
    data = _base_data(
        [_entity(title="a:f", etype="function"), target]
    )
    report = _clean_report(
        [_matched_row(owner_title="a:f", target_title="a:T")]
    )
    before = json.dumps(data, sort_keys=True)
    with pytest.raises(ClangTypeUseOverlayError, match="symbol_name mismatch"):
        apply_clang_type_uses_from_report(data, report, pkg)
    assert json.dumps(data, sort_keys=True) == before


def test_duplicate_matched_observation_fails_closed(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    data = _base_data(
        [
            _entity(title="a:f", etype="function"),
            _entity(title="a:T", etype="typedef"),
        ]
    )
    row = _matched_row(owner_title="a:f", target_title="a:T")
    report = _clean_report([row, dict(row)])
    before = json.dumps(data, sort_keys=True)
    with pytest.raises(
        ClangTypeUseOverlayError, match="duplicate matched_internal observation"
    ):
        apply_clang_type_uses_from_report(data, report, pkg)
    assert json.dumps(data, sort_keys=True) == before


def test_idempotent_reapplication(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    data = _base_data(
        [
            _entity(title="a:f", etype="function"),
            _entity(title="a:T", etype="typedef"),
        ]
    )
    report = _clean_report(
        [
            _matched_row(owner_title="a:f", target_title="a:T"),
            _matched_row(
                owner_title="a:f",
                target_title="a:T",
                use_kind="function_return",
                line=2,
            ),
        ]
    )
    p1 = apply_clang_type_uses_from_report(data, report, pkg)
    snap = json.dumps(data, sort_keys=True)
    p2 = apply_clang_type_uses_from_report(data, report, pkg)
    assert p1["n_facts"] == 1
    assert p2["n_facts"] == 1
    assert p2["n_facts_changed"] == 0
    assert json.dumps(data, sort_keys=True) == snap


def test_disabled_provenance_shape():
    assert build_disabled_provenance() == {
        "mode": "off",
        "enabled": False,
        "n_facts": 0,
        "n_observations": 0,
        "n_translation_units": 0,
    }


def test_default_build_c_byog_unchanged():
    d = build_c_byog(ROOT / "examples" / "inih")
    assert not any(r.get("type") == REL_TYPE for r in d["relationships"])
    assert d == build_c_byog(ROOT / "examples" / "inih")


# ---------------------------------------------------------------------------
# CLI / index_c
# ---------------------------------------------------------------------------


def test_cli_default_off_manifest(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.c").write_text("typedef int T;\nint f(T x){return 0;}\n")
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
        allow_toolchain_drift=False,
    )
    snap = (graph / "current").read_text(encoding="utf-8").strip()
    manifest = json.loads(
        (graph / "snapshots" / snap / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["clang_type_uses"] == {
        "mode": "off",
        "enabled": False,
        "n_facts": 0,
        "n_observations": 0,
        "n_translation_units": 0,
    }
    assert manifest["counts"]["entities"] == len(baseline["entities"])
    assert manifest["counts"]["relationships"] == len(baseline["relationships"])
    import pandas as pd

    rels = pd.read_parquet(graph / "snapshots" / snap / "relationships.parquet")
    assert not any(str(t) == REL_TYPE for t in rels["type"].astype(str))


# ---------------------------------------------------------------------------
# Live
# ---------------------------------------------------------------------------


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_inih_type_uses():
    pkg = ROOT / "examples" / "inih"
    data = build_c_byog(pkg)
    n_ent = len(data["entities"])
    base_ids = [r["id"] for r in data["relationships"]]
    base_endpoints = [(r["source"], r["target"], r["type"]) for r in data["relationships"]]
    before = {p.relative_to(pkg).as_posix() for p in pkg.rglob("*") if p.is_file()}
    prov = append_clang_type_uses(data, pkg)
    after = {p.relative_to(pkg).as_posix() for p in pkg.rglob("*") if p.is_file()}
    assert before == after
    assert not any(pkg.rglob("*.o"))
    assert not any(pkg.rglob("*.ast"))
    assert prov["n_observations"] == 14
    assert prov["n_facts"] == 8
    assert prov["counts"]["matched_internal"] == 14
    assert prov["counts"]["ambiguous_target"] == 0
    assert prov["counts"]["owner_unmatched"] == 0
    assert prov["counts"]["target_unresolved"] == 0
    assert prov["counts"]["macro_location_unsupported"] == 0
    assert len(data["entities"]) == n_ent == 21
    non_use = [r for r in data["relationships"] if r.get("type") != REL_TYPE]
    assert [r["id"] for r in non_use] == base_ids
    assert [
        (r["source"], r["target"], r["type"]) for r in non_use
    ] == base_endpoints
    uses = [r for r in data["relationships"] if r["type"] == REL_TYPE]
    assert len(uses) == 8
    # Idempotent
    prov2 = append_clang_type_uses(data, pkg)
    assert prov2["n_facts"] == 8
    assert prov2["n_facts_changed"] == 0


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_cjson_type_uses():
    pkg = ROOT / "examples" / "cjson"
    data = build_c_byog(pkg)
    n_ent = len(data["entities"])
    base_ids = [r["id"] for r in data["relationships"]]
    before = {p.relative_to(pkg).as_posix() for p in pkg.rglob("*") if p.is_file()}
    prov = append_clang_type_uses(data, pkg)
    after = {p.relative_to(pkg).as_posix() for p in pkg.rglob("*") if p.is_file()}
    assert before == after
    assert prov["n_observations"] == 439
    assert prov["n_facts"] == 172
    assert prov["counts"]["matched_internal"] == 439
    assert prov["counts"]["ambiguous_target"] == 0
    assert prov["counts"]["owner_unmatched"] == 0
    assert prov["counts"]["target_unresolved"] == 0
    assert len(data["entities"]) == n_ent == 148
    non_use = [r for r in data["relationships"] if r.get("type") != REL_TYPE]
    assert [r["id"] for r in non_use] == base_ids
    uses = [r for r in data["relationships"] if r["type"] == REL_TYPE]
    assert len(uses) == 172
    self_edges = [r for r in uses if r["source"] == r["target"]]
    assert len(self_edges) == 1
    assert self_edges[0]["source"] == "cJSON:struct:cJSON"
    # struct vs typedef targets both present among edges
    targets = {r["target"] for r in uses}
    assert "cJSON:struct:cJSON" in targets
    assert "cJSON:typedef:cJSON" in targets or "cJSON:typedef:cJSON" in str(targets)


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_index_type_use_only(tmp_path: Path):
    pkg = ROOT / "examples" / "inih"
    graph = tmp_path / "g-tu"
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
        clang_type_uses=True,
        allow_toolchain_drift=False,
    )
    snap = (graph / "current").read_text(encoding="utf-8").strip()
    snap_dir = graph / "snapshots" / snap
    import pandas as pd

    ents = pd.read_parquet(snap_dir / "entities.parquet")
    rels = pd.read_parquet(snap_dir / "relationships.parquet")
    assert len(ents) == len(baseline["entities"]) == 21
    uses = rels[rels["type"].astype(str) == REL_TYPE]
    assert len(uses) == 8
    # No signature/type fields published
    assert not any(str(c).startswith("clang_signature_") for c in ents.columns)
    assert not any(str(c).startswith("clang_type_declaration") for c in ents.columns)
    assert not any(str(c).startswith("clang_call_") for c in rels.columns)
    manifest = json.loads((snap_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["clang_type_uses"]["mode"] == MODE
    assert manifest["clang_type_uses"]["n_facts"] == 8
    assert manifest["clang_type_uses"]["n_observations"] == 14
    assert manifest["clang_signatures"]["mode"] == "off"
    assert manifest["clang_calls"]["mode"] == "off"
    assert manifest["clang_types"]["mode"] == "off"
    assert not any(pkg.rglob("*.o"))


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_combined_all_four_flags_inih(tmp_path: Path):
    pkg = ROOT / "examples" / "inih"
    baseline = build_c_byog(pkg)
    graph = tmp_path / "g-all"
    index_c_main(
        package=pkg,
        graph=graph,
        keep_snapshots=2,
        compiler_builtins=False,
        compiler_dependencies=False,
        compiler_includes=False,
        clang_signatures=True,
        clang_calls=True,
        clang_types=True,
        clang_type_uses=True,
        allow_toolchain_drift=False,
    )
    snap = (graph / "current").read_text(encoding="utf-8").strip()
    snap_dir = graph / "snapshots" / snap
    import pandas as pd

    ents = pd.read_parquet(snap_dir / "entities.parquet")
    rels = pd.read_parquet(snap_dir / "relationships.parquet")
    assert len(ents) == len(baseline["entities"])
    # base relationship ids preserved
    base_ids = {r["id"] for r in baseline["relationships"]}
    assert base_ids.issubset(set(rels["id"].astype(str)))
    uses = rels[rels["type"].astype(str) == REL_TYPE]
    assert len(uses) == 8
    manifest = json.loads((snap_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["clang_signatures"]["enabled"] is True
    assert manifest["clang_calls"]["enabled"] is True
    assert manifest["clang_types"]["enabled"] is True
    assert manifest["clang_type_uses"]["enabled"] is True
    assert manifest["clang_type_uses"]["n_facts"] == 8
    assert "clang_ast_capture" not in manifest


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_index_failure_no_snapshot(tmp_path: Path, monkeypatch):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.c").write_text("typedef int T;\nint f(T x){return 0;}\n")
    cc = _cc() or "clang"
    (pkg / "compile_commands.json").write_text(
        json.dumps(
            [
                {
                    "directory": str(pkg),
                    "file": str(pkg / "a.c"),
                    "arguments": [cc, "-c", "a.c", "-o", "a.o"],
                }
            ]
        ),
        encoding="utf-8",
    )
    graph = tmp_path / "g"
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
        allow_toolchain_drift=False,
    )
    prior_current = (graph / "current").read_text(encoding="utf-8")
    prior_snaps = sorted(p.name for p in (graph / "snapshots").iterdir())

    import c_clang_type_uses as tu_mod
    import index_c as index_mod

    def boom(*_a, **_k):
        raise tu_mod.ClangTypeUseOverlayError("forced type-use overlay failure")

    monkeypatch.setattr(tu_mod, "append_clang_type_uses", boom)
    monkeypatch.setattr(index_mod, "append_clang_type_uses", boom)

    import typer

    with pytest.raises((SystemExit, typer.Exit)) as ei:
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
            clang_type_uses=True,
            allow_toolchain_drift=False,
        )
    code = getattr(ei.value, "exit_code", None)
    if code is None:
        code = getattr(ei.value, "code", None)
    assert code == 2
    assert (graph / "current").read_text(encoding="utf-8") == prior_current
    assert sorted(p.name for p in (graph / "snapshots").iterdir()) == prior_snaps


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_shared_capture_four_flags_n_dumps(tmp_path: Path, monkeypatch):
    n = 2
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    cc = _cc() or "clang"
    for i in range(n):
        (pkg / f"f{i}.c").write_text(
            f"typedef int T{i};\nint g{i}(T{i} x){{return (int)x;}}\n"
        )
    entries = [
        {
            "directory": str(pkg),
            "file": str(pkg / f"f{i}.c"),
            "arguments": [cc, "-c", f"f{i}.c", "-o", f"f{i}.o"],
        }
        for i in range(n)
    ]
    (pkg / "compile_commands.json").write_text(
        json.dumps(entries, indent=2) + "\n", encoding="utf-8"
    )
    counter = {"n": 0, "loads": 0}
    real_dump = cap_mod.run_ast_dump_for_entry
    real_load = cap_mod.load_compile_entries

    def wrapped_dump(*a, **k):
        counter["n"] += 1
        return real_dump(*a, **k)

    def wrapped_load(*a, **k):
        counter["loads"] += 1
        return real_load(*a, **k)

    monkeypatch.setattr(cap_mod, "run_ast_dump_for_entry", wrapped_dump)
    monkeypatch.setattr(cap_mod, "load_compile_entries", wrapped_load)

    combos = [
        (False, False, False, False, 0),
        (True, False, False, False, n),
        (False, True, False, False, n),
        (False, False, True, False, n),
        (False, False, False, True, n),
        (True, True, True, True, n),
        (True, False, False, True, n),
        (False, False, True, True, n),
    ]
    for sigs, calls, types, tuses, expect in combos:
        counter["n"] = 0
        counter["loads"] = 0
        graph = tmp_path / f"g_{int(sigs)}{int(calls)}{int(types)}{int(tuses)}"
        index_c_main(
            package=pkg,
            graph=graph,
            keep_snapshots=2,
            compiler_builtins=False,
            compiler_dependencies=False,
            compiler_includes=False,
            clang_signatures=sigs,
            clang_calls=calls,
            clang_types=types,
            clang_type_uses=tuses,
            allow_toolchain_drift=False,
        )
        assert counter["n"] == expect, (sigs, calls, types, tuses, counter["n"])
        if expect:
            assert counter["loads"] == 1
        else:
            assert counter["loads"] == 0
    assert not any(pkg.rglob("*.o"))
    assert not any(pkg.rglob("*.ast"))
    assert not any(pkg.rglob("*.d"))
    assert not any(pkg.rglob("*.i"))


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_optional_reports_reuse_byte_identical():
    from c_clang_ast_capture import capture_clang_ast_package  # type: ignore
    from c_clang_ast_audit import build_function_audit_from_capture  # type: ignore
    from c_clang_type_audit import (  # type: ignore
        build_type_declaration_audit_from_capture,
    )
    from c_clang_type_use_audit import (  # type: ignore
        audit_to_json,
        build_type_use_audit_from_capture,
    )

    pkg = ROOT / "examples" / "inih"
    cap = capture_clang_ast_package(pkg)
    fn = build_function_audit_from_capture(cap)
    ty = build_type_declaration_audit_from_capture(cap)
    a = build_type_use_audit_from_capture(cap)
    b = build_type_use_audit_from_capture(
        cap, function_report=fn, type_report=ty
    )
    assert audit_to_json(a) == audit_to_json(b)
