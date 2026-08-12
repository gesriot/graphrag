"""Optional Clang configured type-shape evidence overlay.

Pure application tests always run. Live Clang tests skip when no compiler.

Run: uv run python -m pytest examples/cjson/tests/test_c_clang_type_shapes.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

import c_clang_ast_capture as cap_mod  # type: ignore
from c_clang_ast_capture import capture_clang_ast_package  # type: ignore
from c_clang_type_audit import (  # type: ignore
    build_type_declaration_audit_from_capture,
)
from c_clang_type_shape_audit import (  # type: ignore
    audit_to_json,
    build_type_shape_audit_from_capture,
    run_clang_type_shape_audit,
)
from c_clang_type_shapes import (  # type: ignore
    FACT_KIND,
    MODE,
    ClangTypeShapeOverlayError,
    append_clang_type_shapes,
    apply_clang_type_shapes_from_reports,
    build_disabled_provenance,
)
from c_preprocessor import find_c_compiler  # type: ignore
from extract_c import build_c_byog  # type: ignore
from index_c import main as index_c_main  # type: ignore

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


def _patch_dump_counter(monkeypatch):
    counter = {"n": 0}
    real_dump = cap_mod.run_ast_dump_for_entry

    def wrapped_dump(*args, **kwargs):
        counter["n"] += 1
        return real_dump(*args, **kwargs)

    monkeypatch.setattr(cap_mod, "run_ast_dump_for_entry", wrapped_dump)
    return counter


# ---------------------------------------------------------------------------
# Synthetic report builders
# ---------------------------------------------------------------------------


def _base_entity(
    *,
    title: str,
    source_file: str,
    etype: str = "struct",
    span: str = "1:0-4:1",
    symbol_name: str | None = None,
    **extra,
) -> dict:
    name = symbol_name if symbol_name is not None else title.rsplit(":", 1)[-1]
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
        "symbol_name": name,
        "text_unit_ids": [f"tu:{title}"],
    }
    e.update(extra)
    return e


def _clang_member(
    name: str,
    order: int,
    *,
    form: str = "field",
    qual: str | None = "int",
    enum_value: int | None = None,
    reverse_keys: bool = False,
    **extra,
) -> dict:
    member = {
        "name": name,
        "order": order,
        "form": form,
        "is_bitfield": False,
        "bit_width": None,
        "qualType": qual,
        "desugaredQualType": None,
        "line": 2 + order,
        "col0": 2,
        "location_origin": "direct",
        "residual": None,
        "clang_kind": "FieldDecl" if form == "field" else "EnumConstantDecl",
    }
    if enum_value is not None:
        member["enum_value"] = enum_value
    member.update(extra)
    if reverse_keys:
        member = {k: member[k] for k in reversed(list(member))}
    return member


def _ts_member(name: str, order: int) -> dict:
    return {
        "name": name,
        "order": order,
        "form": "field",
        "is_bitfield": False,
        "bit_width": None,
        "line": 2 + order,
        "col0": 2,
        "span": f"{2 + order}:2-{2 + order}:10",
        "residual": None,
    }


def _shape_row(
    *,
    title: str,
    source_path: str,
    entity_kind: str = "struct",
    name: str | None = None,
    member_names: list[str] | None = None,
    clang_members: list[dict] | None = None,
    matched_span: str = "1:0-4:1",
    matched_line: int = 1,
    matched_col0: int = 0,
    entry_indices: list | None = None,
    compilers: list | None = None,
    **extra,
) -> dict:
    name = name or title.rsplit(":", 1)[-1]
    member_names = ["x", "y"] if member_names is None else member_names
    if clang_members is None:
        clang_members = [
            _clang_member(n, i) for i, n in enumerate(member_names)
        ]
    indices = entry_indices or [0]
    comps = compilers or [
        {
            "compiler_path": COMPILER_PATH,
            "compiler_id": COMPILER_ID,
            "compile_commands_digest": DIGEST,
        }
    ]
    row = {
        "classification": "matched_shape",
        "entity_kind": entity_kind,
        "name": name,
        "source_path": source_path,
        "tree_sitter_title": title,
        "matched_site_span": matched_span,
        "matched_site_line": matched_line,
        "matched_site_col0": matched_col0,
        "clang_line": matched_line,
        "clang_col0": matched_col0,
        "location_origin": "direct",
        "entry_indices": indices,
        "compiler_path": comps[0]["compiler_path"] if len(comps) == 1 else None,
        "compiler_id": comps[0]["compiler_id"] if len(comps) == 1 else None,
        "compilers": comps,
        "compile_commands_digest": DIGEST,
        "tree_sitter_members": [
            _ts_member(n, i) for i, n in enumerate(member_names)
        ],
        "clang_members": clang_members,
        "tree_sitter_member_names": list(member_names),
        "clang_member_names": list(member_names),
        "detail": {},
        "confidence_boundary": "test",
    }
    row.update(extra)
    return row


def _owner_row(
    *,
    title: str,
    source_path: str,
    entity_kind: str = "struct",
    name: str | None = None,
    graph_span: str = "1:0-4:1",
    matched_span: str = "1:0-4:1",
    matched_line: int = 1,
    matched_col0: int = 0,
    confirmed: bool = True,
    **extra,
) -> dict:
    name = name or title.rsplit(":", 1)[-1]
    span_start = graph_span.split("-", 1)[0].split(":")
    row = {
        "entity_kind": entity_kind,
        "name": name,
        "source_path": source_path,
        "tree_sitter_title": title,
        "graph_canonical_span": graph_span,
        "graph_canonical_line": int(span_start[0]),
        "graph_canonical_col0": int(span_start[1]),
        "graph_canonical_is_matched_site": graph_span == matched_span,
        "matched_site_span": matched_span,
        "matched_site_line": matched_line,
        "matched_site_col0": matched_col0,
        "matched_site_is_canonical": graph_span == matched_span,
        "line_column_confirmed": confirmed,
    }
    row.update(extra)
    return row


def _provenance_fields() -> dict:
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


def _type_report(owners: list) -> dict:
    report = {"mode": "clang_ast_json_type_declaration_audit"}
    report.update(_provenance_fields())
    report["matched"] = owners
    return report


def _shape_report(matched: list, owners: list | None = None, **counts_extra) -> dict:
    owners = matched if owners is None else owners
    counts = {
        "matched_shape": len(matched),
        "tree_sitter_only_members": 0,
        "clang_only_members": 0,
        "member_order_mismatch": 0,
        "duplicate_or_ambiguous_members": 0,
        "macro_location_unsupported": 0,
        "owner_unmatched": 0,
        "unsupported_member_form": 0,
        "outside_package_declarations": 0,
        "type_declaration_matched_struct_enum": len(owners),
        "type_declaration_matched_total": len(owners),
        "shape_owners_classified": len(matched),
    }
    counts.update(counts_extra)
    report = {"mode": "clang_ast_json_type_shape_audit"}
    report.update(_provenance_fields())
    report.update(
        {
            "type_declaration_audit_mode": "clang_ast_json_type_declaration_audit",
            "counts": counts,
            "matched_shape": matched,
            "tree_sitter_only_members": [],
            "clang_only_members": [],
            "member_order_mismatch": [],
            "duplicate_or_ambiguous_members": [],
            "macro_location_unsupported": [],
            "unsupported_member_form": [],
            "outside_package_declarations": [],
            "owner_unmatched": [],
            "limitations": [],
            "confidence_boundary": "test",
        }
    )
    return report


def _simple_case(tmp_path: Path, *, member_names: list[str] | None = None):
    """One struct entity plus agreeing shape/type reports."""
    pkg = tmp_path / "pkg"
    pkg.mkdir(parents=True)
    src = pkg / "a.c"
    src.write_text("struct S {\n  int x;\n  int y;\n};\n", encoding="utf-8")
    data = {
        "entities": [
            _base_entity(title="a:S", source_file=str(src), span="1:0-4:1")
        ],
        "relationships": [],
    }
    shape = _shape_report(
        [_shape_row(title="a:S", source_path="a.c", member_names=member_names)]
    )
    types = _type_report([_owner_row(title="a:S", source_path="a.c")])
    return pkg, data, shape, types


# ---------------------------------------------------------------------------
# Pure application
# ---------------------------------------------------------------------------


def test_apply_synthetic_matched_shape(tmp_path: Path):
    pkg, data, shape, types = _simple_case(tmp_path)
    prov = apply_clang_type_shapes_from_reports(data, shape, types, pkg)
    assert prov["enabled"] is True
    assert prov["mode"] == MODE
    assert prov["n_facts"] == 1
    assert prov["n_decorated_entities"] == 1
    assert prov["hard_equality"] == "ordered direct member names only"
    ent = data["entities"][0]
    assert ent["clang_shape_members_validated"] is True
    assert ent["clang_shape_fact_kind"] == FACT_KIND
    assert ent["clang_shape_entity_kind"] == "struct"
    assert ent["clang_shape_member_count"] == 2
    assert json.loads(ent["clang_shape_member_names"]) == ["x", "y"]
    assert ent["clang_shape_graph_canonical_span"] == "1:0-4:1"
    assert ent["clang_shape_matched_site_span"] == "1:0-4:1"
    assert ent["clang_shape_matched_site_is_canonical"] is True
    assert ent["clang_shape_confidence"] == 1.0
    assert ent["clang_shape_is_deterministic"] is True
    assert "not ABI or layout equality" in ent["clang_shape_description"]
    # Base identity fields untouched.
    assert ent["id"] == "ent:struct:a:S"
    assert ent["title"] == "a:S"
    assert ent["type"] == "struct"
    assert ent["span"] == "1:0-4:1"
    assert ent["extractor"] == "tree-sitter-c"
    assert ent["confidence"] == 1.0
    assert ent["text_unit_ids"] == ["tu:a:S"]


def test_no_entities_or_relationships_created_or_deleted(tmp_path: Path):
    pkg, data, shape, types = _simple_case(tmp_path)
    data["entities"].append(
        _base_entity(
            title="a:f",
            source_file=str(pkg / "a.c"),
            etype="function",
            span="6:0-6:9",
        )
    )
    data["relationships"] = [{"source": "a:f", "target": "a:S", "type": "calls"}]
    ids_before = [e["id"] for e in data["entities"]]
    rels_before = json.dumps(data["relationships"], sort_keys=True)
    apply_clang_type_shapes_from_reports(data, shape, types, pkg)
    assert [e["id"] for e in data["entities"]] == ids_before
    assert json.dumps(data["relationships"], sort_keys=True) == rels_before
    # Non-struct entity stays undecorated.
    assert not any(
        str(k).startswith("clang_shape_") for k in data["entities"][1]
    )


def test_member_evidence_is_deterministic_json(tmp_path: Path):
    pkg, data, shape, types = _simple_case(tmp_path)
    apply_clang_type_shapes_from_reports(data, shape, types, pkg)
    evidence = data["entities"][0]["clang_shape_member_evidence"]
    decoded = json.loads(evidence)
    assert [m["name"] for m in decoded] == ["x", "y"]
    assert [m["order"] for m in decoded] == [0, 1]
    assert decoded[0]["qualType"] == "int"
    # Canonical: sorted keys, no whitespace.
    assert evidence == json.dumps(
        decoded, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )

    # Member dict key insertion order must not change the published bytes.
    pkg2, data2, shape2, types2 = _simple_case(tmp_path / "second")
    shape2["matched_shape"][0]["clang_members"] = [
        _clang_member("x", 0, reverse_keys=True),
        _clang_member("y", 1, reverse_keys=True),
    ]
    apply_clang_type_shapes_from_reports(data2, shape2, types2, pkg2)
    assert data2["entities"][0]["clang_shape_member_evidence"] == evidence


def test_enum_members_publish_values_as_evidence(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    src = pkg / "a.h"
    src.write_text("enum E {\n  E_A = 1,\n  E_B = 2\n};\n", encoding="utf-8")
    data = {
        "entities": [
            _base_entity(
                title="a:E",
                source_file=str(src),
                etype="enum",
                span="1:0-4:1",
            )
        ],
        "relationships": [],
    }
    members = [
        _clang_member("E_A", 0, form="enumerator", qual="int", enum_value=1),
        _clang_member("E_B", 1, form="enumerator", qual="int", enum_value=2),
    ]
    shape = _shape_report(
        [
            _shape_row(
                title="a:E",
                source_path="a.h",
                entity_kind="enum",
                member_names=["E_A", "E_B"],
                clang_members=members,
            )
        ]
    )
    types = _type_report(
        [_owner_row(title="a:E", source_path="a.h", entity_kind="enum")]
    )
    apply_clang_type_shapes_from_reports(data, shape, types, pkg)
    ent = data["entities"][0]
    assert ent["clang_shape_entity_kind"] == "enum"
    assert [m["enum_value"] for m in json.loads(ent["clang_shape_member_evidence"])] == [1, 2]


def test_collision_safe_attachment_kind_qualified_titles(tmp_path: Path):
    """A same-named typedef beside the struct must never absorb shape fields."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    src = pkg / "a.h"
    src.write_text("typedef struct S {\n  int x;\n} S;\n", encoding="utf-8")
    data = {
        "entities": [
            _base_entity(
                title="a:struct:S",
                source_file=str(src),
                etype="struct",
                span="1:8-3:1",
                symbol_name="S",
            ),
            _base_entity(
                title="a:typedef:S",
                source_file=str(src),
                etype="typedef",
                span="1:0-3:3",
                symbol_name="S",
            ),
        ],
        "relationships": [],
    }
    shape = _shape_report(
        [
            _shape_row(
                title="a:struct:S",
                source_path="a.h",
                name="S",
                member_names=["x"],
                matched_span="1:8-3:1",
                matched_col0=8,
            )
        ],
        owners=[1],
    )
    types = _type_report(
        [
            _owner_row(
                title="a:struct:S",
                source_path="a.h",
                name="S",
                graph_span="1:8-3:1",
                matched_span="1:8-3:1",
                matched_col0=8,
            ),
            _owner_row(
                title="a:typedef:S",
                source_path="a.h",
                entity_kind="typedef",
                name="S",
                graph_span="1:0-3:3",
            ),
        ]
    )
    prov = apply_clang_type_shapes_from_reports(data, shape, types, pkg)
    assert prov["n_facts"] == 1
    assert data["entities"][0]["clang_shape_members_validated"] is True
    assert not any(
        str(k).startswith("clang_shape_") for k in data["entities"][1]
    )


def test_missing_target_entity_fails_closed(tmp_path: Path):
    pkg, data, shape, types = _simple_case(tmp_path)
    data["entities"] = []
    with pytest.raises(
        ClangTypeShapeOverlayError, match="no struct/enum entity"
    ):
        apply_clang_type_shapes_from_reports(data, shape, types, pkg)


def test_non_unique_target_entity_fails_closed(tmp_path: Path):
    pkg, data, shape, types = _simple_case(tmp_path)
    data["entities"].append(
        _base_entity(
            title="a:S", source_file=str(pkg / "a.c"), span="1:0-4:1"
        )
    )
    before = json.dumps(data, sort_keys=True)
    with pytest.raises(ClangTypeShapeOverlayError, match="non-unique"):
        apply_clang_type_shapes_from_reports(data, shape, types, pkg)
    assert json.dumps(data, sort_keys=True) == before


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda e: e.update(type="enum"), "entity type mismatch"),
        (lambda e: e.update(symbol_name="Other"), "symbol_name mismatch"),
        (lambda e: e.update(span="9:0-9:1"), "canonical-span mismatch"),
    ],
)
def test_entity_identity_mismatch_fails_closed(tmp_path: Path, mutate, message):
    pkg, data, shape, types = _simple_case(tmp_path)
    mutate(data["entities"][0])
    before = json.dumps(data, sort_keys=True)
    with pytest.raises(ClangTypeShapeOverlayError, match=message):
        apply_clang_type_shapes_from_reports(data, shape, types, pkg)
    assert json.dumps(data, sort_keys=True) == before


def test_source_path_mismatch_fails_closed(tmp_path: Path):
    pkg, data, shape, types = _simple_case(tmp_path)
    other = pkg / "b.c"
    other.write_text("struct S { int x; };\n", encoding="utf-8")
    data["entities"][0]["source_file"] = str(other)
    with pytest.raises(ClangTypeShapeOverlayError, match="source-path mismatch"):
        apply_clang_type_shapes_from_reports(data, shape, types, pkg)


def test_missing_type_declaration_owner_fails_closed(tmp_path: Path):
    pkg, data, shape, types = _simple_case(tmp_path)
    types["matched"] = []
    shape["counts"]["type_declaration_matched_struct_enum"] = 0
    with pytest.raises(
        ClangTypeShapeOverlayError, match="no matched type-declaration owner"
    ):
        apply_clang_type_shapes_from_reports(data, shape, types, pkg)


@pytest.mark.parametrize(
    "field", ["matched_site_span", "matched_site_line", "matched_site_col0"]
)
def test_owner_site_divergence_fails_closed(tmp_path: Path, field: str):
    pkg, data, shape, types = _simple_case(tmp_path)
    types["matched"][0][field] = (
        "77:0-79:1" if field.endswith("span") else 77
    )
    before = json.dumps(data, sort_keys=True)
    with pytest.raises(
        ClangTypeShapeOverlayError, match="disagrees with its type-declaration"
    ):
        apply_clang_type_shapes_from_reports(data, shape, types, pkg)
    assert json.dumps(data, sort_keys=True) == before


def test_unconfirmed_owner_fails_closed(tmp_path: Path):
    pkg, data, shape, types = _simple_case(tmp_path)
    types["matched"][0]["line_column_confirmed"] = False
    with pytest.raises(
        ClangTypeShapeOverlayError, match="line_column_confirmed"
    ):
        apply_clang_type_shapes_from_reports(data, shape, types, pkg)


@pytest.mark.parametrize("bucket", _FAIL_CLOSED_BUCKETS)
def test_residual_bucket_blocks_publication(tmp_path: Path, bucket: str):
    pkg, data, shape, types = _simple_case(tmp_path)
    shape["counts"][bucket] = 1
    shape[bucket] = [{"name": "bad"}]
    before = json.dumps(data, sort_keys=True)
    with pytest.raises(
        ClangTypeShapeOverlayError, match="unclean shape residuals"
    ):
        apply_clang_type_shapes_from_reports(data, shape, types, pkg)
    assert json.dumps(data, sort_keys=True) == before
    assert not any(
        str(k).startswith("clang_shape_") for k in data["entities"][0]
    )


def test_observation_only_buckets_do_not_block(tmp_path: Path):
    pkg, data, shape, types = _simple_case(tmp_path)
    shape["counts"]["unsupported_member_form"] = 2
    shape["unsupported_member_form"] = [{"name": "u1"}, {"name": "u2"}]
    shape["counts"]["outside_package_declarations"] = 5
    shape["outside_package_declarations"] = [{"name": f"o{i}"} for i in range(5)]
    prov = apply_clang_type_shapes_from_reports(data, shape, types, pkg)
    assert prov["n_facts"] == 1
    assert prov["counts"]["unsupported_member_form"] == 2
    assert prov["counts"]["outside_package_declarations"] == 5
    assert prov["observation_only_buckets"] == [
        "unsupported_member_form",
        "outside_package_declarations",
    ]
    # Observation-only rows never become metadata.
    assert data["entities"][0]["clang_shape_member_count"] == 2
    assert len(data["entities"]) == 1


def test_count_list_mismatch_fails_closed(tmp_path: Path):
    pkg, data, shape, types = _simple_case(tmp_path)
    shape["counts"]["outside_package_declarations"] = 3
    with pytest.raises(ClangTypeShapeOverlayError, match="count/list mismatch"):
        apply_clang_type_shapes_from_reports(data, shape, types, pkg)


def test_atomic_no_partial_mutation(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    src = pkg / "a.c"
    src.write_text("struct A {int x;};\nstruct B {int y;};\n", encoding="utf-8")
    data = {
        "entities": [
            _base_entity(title="a:A", source_file=str(src), span="1:0-1:18"),
            _base_entity(title="a:B", source_file=str(src), span="2:0-2:18"),
        ],
        "relationships": [],
    }
    shape = _shape_report(
        [
            _shape_row(
                title="a:A",
                source_path="a.c",
                member_names=["x"],
                matched_span="1:0-1:18",
            ),
            _shape_row(
                title="a:B",
                source_path="a.c",
                member_names=["y"],
                matched_span="2:0-2:18",
                matched_line=2,
            ),
        ]
    )
    types = _type_report(
        [
            _owner_row(
                title="a:A",
                source_path="a.c",
                graph_span="1:0-1:18",
                matched_span="1:0-1:18",
            ),
            # Second owner disagrees with the graph span -> whole overlay aborts.
            _owner_row(
                title="a:B",
                source_path="a.c",
                graph_span="99:0-99:1",
                matched_span="2:0-2:18",
                matched_line=2,
            ),
        ]
    )
    before = json.dumps(data, sort_keys=True)
    with pytest.raises(ClangTypeShapeOverlayError, match="canonical-span mismatch"):
        apply_clang_type_shapes_from_reports(data, shape, types, pkg)
    assert json.dumps(data, sort_keys=True) == before
    for entity in data["entities"]:
        assert not any(str(k).startswith("clang_shape_") for k in entity)


def test_report_pair_must_share_capture_provenance(tmp_path: Path):
    pkg, data, shape, types = _simple_case(tmp_path)
    types["compile_commands_digest"] = "zzz"
    with pytest.raises(ClangTypeShapeOverlayError, match="reports disagree"):
        apply_clang_type_shapes_from_reports(data, shape, types, pkg)

    # An internally consistent type report built from a different toolchain is
    # still refused: the pair must describe one capture.
    pkg, data, shape, types = _simple_case(tmp_path / "b")
    types["compilers"][0]["compiler_id"] = "other clang"
    types["compiler_id"] = "other clang"
    types["translation_units"][0]["compiler_id"] = "other clang"
    with pytest.raises(ClangTypeShapeOverlayError, match="reports disagree"):
        apply_clang_type_shapes_from_reports(data, shape, types, pkg)


def test_report_pair_rejects_multi_compiler_version_drift(tmp_path: Path):
    pkg, data, shape, types = _simple_case(tmp_path)

    def make_multi(report: dict, *, second_version: str) -> None:
        second_path = "/usr/bin/clang-2"
        second_id = "Apple clang version second"
        report["compiler_path"] = None
        report["compiler_id"] = None
        report["compiler_version"] = None
        report["compilers"] = [
            {
                "compiler_path": COMPILER_PATH,
                "compiler_id": COMPILER_ID,
                "compiler_version": COMPILER_VERSION,
            },
            {
                "compiler_path": second_path,
                "compiler_id": second_id,
                "compiler_version": second_version,
            },
        ]
        report["n_compile_entries"] = 2
        report["translation_units"] = [
            {
                "entry_index": 0,
                "file": "a.c",
                "package_local": True,
                "compiler_path": COMPILER_PATH,
                "compiler_id": COMPILER_ID,
            },
            {
                "entry_index": 1,
                "file": "b.c",
                "package_local": True,
                "compiler_path": second_path,
                "compiler_id": second_id,
            },
        ]

    make_multi(shape, second_version="18.0.0")
    make_multi(types, second_version="19.0.0")
    with pytest.raises(ClangTypeShapeOverlayError, match="reports disagree"):
        apply_clang_type_shapes_from_reports(data, shape, types, pkg)


def test_report_mode_and_package_mismatch_fails(tmp_path: Path):
    pkg, data, shape, types = _simple_case(tmp_path)
    shape["mode"] = "wrong"
    with pytest.raises(
        ClangTypeShapeOverlayError, match="unexpected type-shape audit report mode"
    ):
        apply_clang_type_shapes_from_reports(data, shape, types, pkg)

    pkg, data, shape, types = _simple_case(tmp_path / "b")
    types["mode"] = "wrong"
    with pytest.raises(
        ClangTypeShapeOverlayError,
        match="unexpected type-declaration audit report mode",
    ):
        apply_clang_type_shapes_from_reports(data, shape, types, pkg)

    pkg, data, shape, types = _simple_case(tmp_path / "c")
    shape["package"] = "other"
    types["package"] = "other"
    with pytest.raises(ClangTypeShapeOverlayError, match="does not match"):
        apply_clang_type_shapes_from_reports(data, shape, types, pkg)

    pkg, data, shape, types = _simple_case(tmp_path / "d")
    shape["type_declaration_audit_mode"] = "something_else"
    with pytest.raises(
        ClangTypeShapeOverlayError, match="does not record the type-declaration"
    ):
        apply_clang_type_shapes_from_reports(data, shape, types, pkg)


def test_row_provenance_bound_to_compile_entry_census(tmp_path: Path):
    pkg, data, shape, types = _simple_case(tmp_path)
    shape["matched_shape"][0]["entry_indices"] = [1]
    with pytest.raises(ClangTypeShapeOverlayError, match="compile entry index"):
        apply_clang_type_shapes_from_reports(data, shape, types, pkg)

    pkg, data, shape, types = _simple_case(tmp_path / "b")
    shape["matched_shape"][0]["compilers"][0]["compile_commands_digest"] = "zzz"
    with pytest.raises(
        ClangTypeShapeOverlayError, match="compile_commands_digest disagrees"
    ):
        apply_clang_type_shapes_from_reports(data, shape, types, pkg)


def test_member_disagreement_inside_matched_row_fails(tmp_path: Path):
    pkg, data, shape, types = _simple_case(tmp_path)
    shape["matched_shape"][0]["tree_sitter_member_names"] = ["y", "x"]
    with pytest.raises(
        ClangTypeShapeOverlayError, match="member names disagree"
    ):
        apply_clang_type_shapes_from_reports(data, shape, types, pkg)

    pkg, data, shape, types = _simple_case(tmp_path / "b")
    shape["matched_shape"][0]["clang_members"][1]["residual"] = "anonymous_member"
    with pytest.raises(ClangTypeShapeOverlayError, match="carries residual"):
        apply_clang_type_shapes_from_reports(data, shape, types, pkg)

    pkg, data, shape, types = _simple_case(tmp_path / "c")
    shape["matched_shape"][0]["clang_members"].pop()
    with pytest.raises(ClangTypeShapeOverlayError, match="census"):
        apply_clang_type_shapes_from_reports(data, shape, types, pkg)

    pkg, data, shape, types = _simple_case(tmp_path / "d")
    shape["matched_shape"][0]["tree_sitter_members"][0]["name"] = "wrong"
    with pytest.raises(
        ClangTypeShapeOverlayError, match="tree-sitter member 0 name"
    ):
        apply_clang_type_shapes_from_reports(data, shape, types, pkg)
def test_duplicate_matched_title_fails(tmp_path: Path):
    pkg, data, shape, types = _simple_case(tmp_path)
    shape["matched_shape"].append(_shape_row(title="a:S", source_path="a.c"))
    shape["counts"]["matched_shape"] = 2
    shape["counts"]["shape_owners_classified"] = 2
    with pytest.raises(ClangTypeShapeOverlayError, match="duplicate matched title"):
        apply_clang_type_shapes_from_reports(data, shape, types, pkg)


def test_stale_and_conflicting_shape_fields_fail(tmp_path: Path):
    pkg, data, shape, types = _simple_case(tmp_path)
    data["entities"].append(
        _base_entity(
            title="a:T",
            source_file=str(pkg / "a.c"),
            span="6:0-8:1",
            clang_shape_member_count=3,
        )
    )
    with pytest.raises(ClangTypeShapeOverlayError, match="stale Clang shape fields"):
        apply_clang_type_shapes_from_reports(data, shape, types, pkg)

    pkg, data, shape, types = _simple_case(tmp_path / "b")
    data["entities"].append(
        _base_entity(
            title="a:f",
            source_file=str(pkg / "a.c"),
            etype="function",
            span="6:0-6:9",
            clang_shape_member_count=1,
        )
    )
    with pytest.raises(ClangTypeShapeOverlayError, match="non-struct/enum entity"):
        apply_clang_type_shapes_from_reports(data, shape, types, pkg)

    pkg, data, shape, types = _simple_case(tmp_path / "c")
    data["entities"][0]["clang_shape_member_count"] = 99
    with pytest.raises(ClangTypeShapeOverlayError, match="conflicting pre-existing"):
        apply_clang_type_shapes_from_reports(data, shape, types, pkg)
    assert data["entities"][0]["clang_shape_member_count"] == 99
    assert "clang_shape_members_validated" not in data["entities"][0]

    pkg, data, shape, types = _simple_case(tmp_path / "d")
    data["entities"][0]["clang_shape_unknown_field"] = "x"
    with pytest.raises(ClangTypeShapeOverlayError, match="unknown pre-existing"):
        apply_clang_type_shapes_from_reports(data, shape, types, pkg)


def test_idempotent_reapplication(tmp_path: Path):
    pkg, data, shape, types = _simple_case(tmp_path)
    p1 = apply_clang_type_shapes_from_reports(data, shape, types, pkg)
    snap = json.dumps(data["entities"][0], sort_keys=True)
    p2 = apply_clang_type_shapes_from_reports(data, shape, types, pkg)
    assert p1["n_facts"] == p2["n_facts"] == 1
    assert p1["n_facts_changed"] == 1
    assert p2["n_facts_changed"] == 0
    assert json.dumps(data["entities"][0], sort_keys=True) == snap


def test_reports_must_be_paired(tmp_path: Path):
    pkg, data, shape, _types = _simple_case(tmp_path)
    with pytest.raises(ClangTypeShapeOverlayError, match="provided together"):
        append_clang_type_shapes(data, pkg, report=shape)


def test_disabled_provenance_shape():
    assert build_disabled_provenance() == {
        "mode": "off",
        "enabled": False,
        "n_facts": 0,
        "n_translation_units": 0,
    }


def test_default_build_c_byog_unchanged():
    d = build_c_byog(ROOT / "examples" / "cjson")
    for e in d["entities"]:
        assert not any(str(k).startswith("clang_shape_") for k in e)
    assert d == build_c_byog(ROOT / "examples" / "cjson")


# ---------------------------------------------------------------------------
# CLI / parquet
# ---------------------------------------------------------------------------


def test_cli_default_off_manifest_and_stability(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.c").write_text(
        "struct S { int x; };\nint f(void){return 0;}\n", encoding="utf-8"
    )
    baseline = build_c_byog(pkg)

    def run(graph: Path) -> Path:
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
        snap = (graph / "current").read_text(encoding="utf-8").strip()
        return graph / "snapshots" / snap

    first = run(tmp_path / "g1")
    second = run(tmp_path / "g2")
    manifest = json.loads((first / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["clang_type_shapes"] == {
        "mode": "off",
        "enabled": False,
        "n_facts": 0,
        "n_translation_units": 0,
    }
    assert manifest["counts"]["entities"] == len(baseline["entities"])

    import pandas as pd

    ents = pd.read_parquet(first / "entities.parquet")
    assert not any(str(c).startswith("clang_shape_") for c in ents.columns)
    # Byte-stable default-off publication.
    assert (first / "entities.parquet").read_bytes() == (
        second / "entities.parquet"
    ).read_bytes()
    assert (first / "relationships.parquet").read_bytes() == (
        second / "relationships.parquet"
    ).read_bytes()
    assert list(ents["id"]) == [e["id"] for e in baseline["entities"]]


# ---------------------------------------------------------------------------
# Live packages
# ---------------------------------------------------------------------------


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_cjson_three_matched_struct_owners():
    pkg = ROOT / "examples" / "cjson"
    data = build_c_byog(pkg)
    n_ent = len(data["entities"])
    n_rel = len(data["relationships"])
    ids_before = [e["id"] for e in data["entities"]]
    spans_before = [e.get("span") for e in data["entities"]]
    before = {p.relative_to(pkg).as_posix() for p in pkg.rglob("*") if p.is_file()}

    prov = append_clang_type_shapes(data, pkg)

    after = {p.relative_to(pkg).as_posix() for p in pkg.rglob("*") if p.is_file()}
    assert before == after
    for pattern in ("*.o", "*.ast", "*.d", "*.i"):
        assert not list(pkg.rglob(pattern))
    assert prov["mode"] == MODE
    assert prov["n_facts"] == 3
    assert prov["counts"]["matched_shape"] == 3
    assert prov["counts"]["type_declaration_matched_struct_enum"] == 3
    for bucket in _FAIL_CLOSED_BUCKETS:
        assert prov["counts"][bucket] == 0
    assert len(data["entities"]) == n_ent == 148
    assert len(data["relationships"]) == n_rel == 640
    assert [e["id"] for e in data["entities"]] == ids_before
    assert [e.get("span") for e in data["entities"]] == spans_before

    decorated = [
        e
        for e in data["entities"]
        if e.get("clang_shape_members_validated") is True
    ]
    assert len(decorated) == 3
    assert {e["title"] for e in decorated} == {
        "cJSON:struct:cJSON",
        "cJSON:struct:cJSON_Hooks",
        "cJSON:struct:internal_hooks",
    }
    assert all(e["type"] == "struct" for e in decorated)
    cjson = next(e for e in decorated if e["title"] == "cJSON:struct:cJSON")
    assert json.loads(cjson["clang_shape_member_names"]) == [
        "next",
        "prev",
        "child",
        "type",
        "valuestring",
        "valueint",
        "valuedouble",
        "string",
    ]
    assert cjson["clang_shape_member_count"] == 8
    assert cjson["extractor"] == "tree-sitter-c"
    assert cjson["confidence"] == 1.0
    evidence = json.loads(cjson["clang_shape_member_evidence"])
    assert evidence[0]["qualType"] == "struct cJSON *"

    prov2 = append_clang_type_shapes(data, pkg)
    assert prov2["n_facts"] == 3
    assert prov2["n_facts_changed"] == 0


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_inih_zero_decorated_shape_owners():
    pkg = ROOT / "examples" / "inih"
    data = build_c_byog(pkg)
    n_ent = len(data["entities"])
    n_rel = len(data["relationships"])
    prov = append_clang_type_shapes(data, pkg)
    assert prov["n_facts"] == 0
    assert prov["counts"]["matched_shape"] == 0
    assert prov["counts"]["type_declaration_matched_struct_enum"] == 0
    assert prov["counts"]["outside_package_declarations"] >= 0
    assert len(data["entities"]) == n_ent == 21
    assert len(data["relationships"]) == n_rel == 56
    assert not any(
        any(str(k).startswith("clang_shape_") for k in e)
        for e in data["entities"]
    )
    for pattern in ("*.o", "*.ast", "*.d", "*.i"):
        assert not list(pkg.rglob(pattern))


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_index_c_shapes_parquet(tmp_path: Path):
    pkg = ROOT / "examples" / "cjson"
    baseline = build_c_byog(pkg)
    graph = tmp_path / "g-shapes"
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
        clang_type_shapes=True,
        allow_toolchain_drift=False,
    )
    snap = (graph / "current").read_text(encoding="utf-8").strip()
    snap_dir = graph / "snapshots" / snap
    import pandas as pd

    ents = pd.read_parquet(snap_dir / "entities.parquet")
    rels = pd.read_parquet(snap_dir / "relationships.parquet")
    assert len(ents) == len(baseline["entities"]) == 148
    assert len(rels) == len(baseline["relationships"]) == 640
    assert list(ents["id"]) == [e["id"] for e in baseline["entities"]]
    assert int((rels["type"].astype(str) == "uses_type").sum()) == 0
    decorated = ents[ents["clang_shape_members_validated"] == True]  # noqa: E712
    assert len(decorated) == 3
    assert (decorated["clang_shape_fact_kind"] == FACT_KIND).all()

    manifest = json.loads((snap_dir / "manifest.json").read_text(encoding="utf-8"))
    block = manifest["clang_type_shapes"]
    assert block["mode"] == MODE
    assert block["enabled"] is True
    assert block["n_facts"] == 3
    assert block["n_decorated_entities"] == 3
    assert block["counts"]["matched_shape"] == 3
    assert block["counts"]["unsupported_member_form"] == 0
    assert "outside_package_declarations" in block["counts"]
    assert block["compile_commands_digest"]
    assert block["compiler_id"]
    assert "do not mean size, alignment, offsets" in block["confidence_boundary"]
    assert "Not ABI evidence" in block["limitations"]
    assert "Not Rust representation (repr) proof" in block["limitations"]
    assert "Not C++" in block["limitations"]
    assert manifest["clang_types"]["mode"] == "off"
    assert manifest["clang_type_uses"]["mode"] == "off"
    for pattern in ("*.o", "*.ast", "*.d", "*.i"):
        assert not list(pkg.rglob(pattern))


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_shape_only_and_all_five_flags_dump_once_per_entry(
    tmp_path: Path, monkeypatch
):
    n = 2
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    cc = _cc() or "clang"
    entries = []
    for i in range(n):
        (pkg / f"f{i}.c").write_text(
            f"struct S{i} {{ int x; }};\n"
            f"static int h{i}(struct S{i} *s) {{ return s->x; }}\n"
            f"int g{i}(struct S{i} *s) {{ return h{i}(s); }}\n",
            encoding="utf-8",
        )
        entries.append(
            {
                "directory": str(pkg),
                "file": str(pkg / f"f{i}.c"),
                "arguments": [cc, "-c", f"f{i}.c", "-o", f"f{i}.o"],
            }
        )
    (pkg / "compile_commands.json").write_text(
        json.dumps(entries, indent=2) + "\n", encoding="utf-8"
    )
    counter = _patch_dump_counter(monkeypatch)

    for label, flags in (
        ("shape_only", dict(
            clang_signatures=False,
            clang_calls=False,
            clang_types=False,
            clang_type_uses=False,
            clang_type_shapes=True,
        )),
        ("all_five", dict(
            clang_signatures=True,
            clang_calls=True,
            clang_types=True,
            clang_type_uses=True,
            clang_type_shapes=True,
        )),
    ):
        counter["n"] = 0
        index_c_main(
            package=pkg,
            graph=tmp_path / f"g-{label}",
            keep_snapshots=2,
            compiler_builtins=False,
            compiler_dependencies=False,
            compiler_includes=False,
            allow_toolchain_drift=False,
            **flags,
        )
        assert counter["n"] == n, (label, counter["n"])
        assert counter["n"] != 5 * n
    for pattern in ("*.o", "*.ast", "*.d", "*.i"):
        assert not list(pkg.rglob(pattern))


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_standalone_audit_output_unchanged_by_overlay():
    pkg = ROOT / "examples" / "cjson"
    capture = capture_clang_ast_package(pkg, timeout=120)
    type_report = build_type_declaration_audit_from_capture(capture)
    reused = build_type_shape_audit_from_capture(
        capture, type_report=type_report
    )
    built = build_type_shape_audit_from_capture(capture)
    standalone = run_clang_type_shape_audit(pkg, timeout=120)
    assert audit_to_json(reused) == audit_to_json(built)
    assert audit_to_json(built) == audit_to_json(standalone)

    # Applying the overlay must not mutate the reports it consumed.
    shape_json = audit_to_json(reused)
    type_json = json.dumps(type_report, sort_keys=True)
    data = build_c_byog(pkg)
    apply_clang_type_shapes_from_reports(data, reused, type_report, pkg)
    assert audit_to_json(reused) == shape_json
    assert json.dumps(type_report, sort_keys=True) == type_json
