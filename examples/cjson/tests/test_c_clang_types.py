"""Optional Clang configured type-declaration evidence overlay.

Pure application tests always run. Live Clang tests skip when no compiler.

Run: uv run python -m pytest examples/cjson/tests/test_c_clang_types.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
from c_clang_types import (  # type: ignore
    FACT_KIND,
    MODE,
    ClangTypeOverlayError,
    append_clang_types,
    apply_clang_types_from_report,
    build_disabled_provenance,
)
from c_preprocessor import find_c_compiler  # type: ignore
from extract_c import build_c_byog  # type: ignore
from index_c import main as index_c_main  # type: ignore


def _cc():
    return find_c_compiler()


def _base_entity(
    *,
    title: str,
    source_file: str,
    etype: str = "typedef",
    span: str = "10:0-12:1",
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


def _matched_row(
    *,
    title: str,
    source_path: str,
    entity_kind: str = "typedef",
    name: str | None = None,
    graph_span: str = "10:0-12:1",
    graph_line: int = 10,
    graph_col0: int = 0,
    matched_span: str | None = None,
    matched_line: int | None = None,
    matched_col0: int | None = None,
    matched_is_canonical: bool | None = None,
    qual: str | None = "int",
    desugared: str | None = None,
    fixed: str | None = None,
    confirmed: bool = True,
    entry_indices: list | None = None,
    compilers: list | None = None,
    **extra,
) -> dict:
    name = name or title.rsplit(":", 1)[-1]
    matched_span = matched_span if matched_span is not None else graph_span
    matched_line = matched_line if matched_line is not None else graph_line
    matched_col0 = matched_col0 if matched_col0 is not None else graph_col0
    if matched_is_canonical is None:
        matched_is_canonical = (
            matched_span == graph_span
            and matched_line == graph_line
            and matched_col0 == graph_col0
        )
    indices = entry_indices or [0]
    comps = compilers or [
        {
            "compiler_path": "/usr/bin/clang",
            "compiler_id": "Apple clang version test",
            "compile_commands_digest": "abc123",
        }
    ]
    row = {
        "entity_kind": entity_kind,
        "name": name,
        "source_path": source_path,
        "tree_sitter_title": title,
        "graph_canonical_span": graph_span,
        "graph_canonical_line": graph_line,
        "graph_canonical_col0": graph_col0,
        "graph_canonical_is_matched_site": matched_is_canonical,
        "matched_site_span": matched_span,
        "matched_site_line": matched_line,
        "matched_site_col0": matched_col0,
        "matched_site_is_canonical": matched_is_canonical,
        "tree_sitter_line": matched_line,
        "tree_sitter_col": matched_col0,
        "clang_line": matched_line,
        "clang_col0": matched_col0,
        "line_column_confirmed": confirmed,
        "tag_kind": None,
        "is_complete": True,
        "qualType": qual,
        "desugaredQualType": desugared,
        "fixedUnderlyingType": fixed,
        "location_origin": "direct",
        "entry_indices": indices,
        "compiler_path": comps[0]["compiler_path"] if len(comps) == 1 else None,
        "compiler_id": comps[0]["compiler_id"] if len(comps) == 1 else None,
        "compile_commands_digest": "abc123",
        "compilers": comps,
        "observations": [
            {
                "entry_indices": indices,
                "compiler_path": c["compiler_path"],
                "compiler_id": c["compiler_id"],
                "compile_commands_digest": "abc123",
            }
            for c in comps
        ],
    }
    row.update(extra)
    return row


def _clean_report(matched: list, **counts_extra) -> dict:
    counts = {
        "matched": len(matched),
        "tree_sitter_only": 0,
        "clang_only": 0,
        "ambiguous": 0,
        "macro_location_unsupported": 0,
        "out_of_compile_db_scope": 0,
        "anonymous_declarations": 0,
        "unsupported_declarations": 0,
        "outside_package_declarations": 0,
        "alternate_declaration_sites": 0,
        "clang_type_declarations_package_local": len(matched),
        "tree_sitter_type_entities_total": len(matched),
        "tree_sitter_type_entities_in_scope": len(matched),
        "tree_sitter_declaration_sites_total": len(matched),
    }
    counts.update(counts_extra)
    return {
        "mode": "clang_ast_json_type_declaration_audit",
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
        "in_scope_source_paths": sorted({m["source_path"] for m in matched}),
        "counts": counts,
        "matched": matched,
        "tree_sitter_only": [],
        "clang_only": [],
        "ambiguous": [],
        "macro_location_unsupported": [],
        "out_of_compile_db_scope": [],
        "anonymous_declarations": [],
        "unsupported_declarations": [],
        "outside_package_declarations": [],
        "alternate_declaration_sites": [],
        "limitations": [],
        "confidence_boundary": "test",
    }


# ---------------------------------------------------------------------------
# Pure application
# ---------------------------------------------------------------------------


def test_apply_synthetic_matched_type(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    src = pkg / "a.c"
    src.write_text("typedef int T;\n", encoding="utf-8")
    data = {
        "entities": [
            _base_entity(
                title="a:T",
                source_file=str(src),
                etype="typedef",
                span="1:0-1:13",
            ),
            _base_entity(
                title="a:a.c",
                source_file=str(src),
                etype="file",
                symbol_name="a.c",
            ),
        ],
        "relationships": [],
    }
    report = _clean_report(
        [
            _matched_row(
                title="a:T",
                source_path="a.c",
                graph_span="1:0-1:13",
                graph_line=1,
                qual="int",
            )
        ]
    )
    prov = apply_clang_types_from_report(data, report, pkg)
    assert prov["enabled"] is True
    assert prov["n_facts"] == 1
    assert prov["mode"] == MODE
    ent = data["entities"][0]
    assert ent["clang_type_declaration_confirmed"] is True
    assert ent["clang_type_fact_kind"] == FACT_KIND
    assert ent["clang_type_entity_kind"] == "typedef"
    assert ent["clang_type_qual_type"] == "int"
    assert ent["clang_type_confidence"] == 1.0
    assert ent["clang_type_is_deterministic"] is True
    assert ent["clang_type_graph_canonical_span"] == "1:0-1:13"
    assert ent["clang_type_matched_site_is_canonical"] is True
    # Base identity unchanged
    assert ent["title"] == "a:T"
    assert ent["type"] == "typedef"
    assert ent["extractor"] == "tree-sitter-c"
    assert ent["confidence"] == 1.0
    assert ent["span"] == "1:0-1:13"
    assert ent["id"] == "ent:typedef:a:T"
    assert ent["text_unit_ids"] == ["tu:a:T"]
    # File entity untouched
    assert "clang_type_declaration_confirmed" not in data["entities"][1]


def test_ini_handler_canonical_vs_matched_site(tmp_path: Path):
    """ini_handler: attach to graph line 58 while recording match at 62."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    src = pkg / "ini.h"
    src.write_text("typedef int ini_handler;\n", encoding="utf-8")
    data = {
        "entities": [
            _base_entity(
                title="ini:ini_handler",
                source_file=str(src),
                etype="typedef",
                span="58:0-60:39",
                symbol_name="ini_handler",
            )
        ],
        "relationships": [],
    }
    report = _clean_report(
        [
            _matched_row(
                title="ini:ini_handler",
                source_path="ini.h",
                name="ini_handler",
                graph_span="58:0-60:39",
                graph_line=58,
                graph_col0=0,
                matched_span="62:0-63:64",
                matched_line=62,
                matched_col0=0,
                matched_is_canonical=False,
                qual="int (*)(void *, const char *, const char *, const char *)",
            )
        ]
    )
    apply_clang_types_from_report(data, report, pkg)
    ent = data["entities"][0]
    assert ent["span"] == "58:0-60:39"
    assert ent["clang_type_graph_canonical_line"] == 58
    assert ent["clang_type_graph_canonical_span"] == "58:0-60:39"
    assert ent["clang_type_matched_site_line"] == 62
    assert ent["clang_type_matched_site_span"] == "62:0-63:64"
    assert ent["clang_type_matched_site_is_canonical"] is False


def test_cross_kind_qualified_type_title(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    src = pkg / "cJSON.c"
    src.write_text("typedef struct cJSON cJSON;\n", encoding="utf-8")
    data = {
        "entities": [
            _base_entity(
                title="cJSON:typedef:cJSON",
                source_file=str(src),
                etype="typedef",
                span="103:0-123:8",
                symbol_name="cJSON",
            ),
            _base_entity(
                title="cJSON:struct:cJSON",
                source_file=str(src),
                etype="struct",
                span="103:8-123:1",
                symbol_name="cJSON",
            ),
        ],
        "relationships": [],
    }
    report = _clean_report(
        [
            _matched_row(
                title="cJSON:typedef:cJSON",
                source_path="cJSON.c",
                entity_kind="typedef",
                name="cJSON",
                graph_span="103:0-123:8",
                graph_line=103,
                qual="struct cJSON",
            ),
            _matched_row(
                title="cJSON:struct:cJSON",
                source_path="cJSON.c",
                entity_kind="struct",
                name="cJSON",
                graph_span="103:8-123:1",
                graph_line=103,
                graph_col0=8,
                qual=None,
            ),
        ]
    )
    apply_clang_types_from_report(data, report, pkg)
    td = next(e for e in data["entities"] if e["type"] == "typedef")
    st = next(e for e in data["entities"] if e["type"] == "struct")
    assert td["clang_type_entity_kind"] == "typedef"
    assert td["clang_type_qual_type"] == "struct cJSON"
    assert st["clang_type_entity_kind"] == "struct"
    assert st["clang_type_qual_type"] is None
    assert td["title"] == "cJSON:typedef:cJSON"
    assert st["title"] == "cJSON:struct:cJSON"


@pytest.mark.parametrize("kind", ["struct", "enum", "typedef"])
def test_struct_enum_typedef_type_validation(tmp_path: Path, kind: str):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    src = pkg / "a.c"
    src.write_text("/* x */\n", encoding="utf-8")
    data = {
        "entities": [
            _base_entity(
                title=f"a:{kind}:X",
                source_file=str(src),
                etype=kind,
                span="1:0-1:1",
                symbol_name="X",
            )
        ],
        "relationships": [],
    }
    report = _clean_report(
        [
            _matched_row(
                title=f"a:{kind}:X",
                source_path="a.c",
                entity_kind=kind,
                name="X",
                graph_span="1:0-1:1",
                graph_line=1,
                qual="int" if kind == "typedef" else None,
            )
        ]
    )
    apply_clang_types_from_report(data, report, pkg)
    assert data["entities"][0]["clang_type_entity_kind"] == kind


def test_entity_type_mismatch_fails(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    src = pkg / "a.c"
    src.write_text("/* x */\n", encoding="utf-8")
    data = {
        "entities": [
            _base_entity(
                title="a:T",
                source_file=str(src),
                etype="struct",  # graph says struct
                span="1:0-1:1",
                symbol_name="T",
            )
        ],
        "relationships": [],
    }
    report = _clean_report(
        [
            _matched_row(
                title="a:T",
                source_path="a.c",
                entity_kind="typedef",  # audit says typedef
                graph_span="1:0-1:1",
                graph_line=1,
            )
        ]
    )
    before = json.dumps(data, sort_keys=True)
    with pytest.raises(ClangTypeOverlayError, match="entity type mismatch"):
        apply_clang_types_from_report(data, report, pkg)
    assert json.dumps(data, sort_keys=True) == before


def test_source_path_mismatch_fails(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    src = pkg / "a.c"
    src.write_text("typedef int T;\n", encoding="utf-8")
    second = pkg / "b.c"
    second.write_text("typedef int U;\n", encoding="utf-8")
    data = {
        "entities": [
            _base_entity(
                title="a:T",
                source_file=str(src),
                span="1:0-1:13",
            ),
            _base_entity(
                title="b:U",
                source_file=str(second),
                span="1:0-1:13",
            ),
        ],
        "relationships": [],
    }
    report = _clean_report(
        [
            _matched_row(
                title="a:T",
                source_path="a.c",
                graph_span="1:0-1:13",
                graph_line=1,
            ),
            _matched_row(
                title="b:U",
                source_path="z.c",  # wrong
                graph_span="1:0-1:13",
                graph_line=1,
            ),
        ]
    )
    before = json.dumps(data, sort_keys=True)
    with pytest.raises(ClangTypeOverlayError, match="source-path mismatch|invalid package-relative"):
        apply_clang_types_from_report(data, report, pkg)
    assert json.dumps(data, sort_keys=True) == before


def test_canonical_span_mismatch_fails(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    src = pkg / "a.c"
    src.write_text("typedef int T;\n", encoding="utf-8")
    data = {
        "entities": [
            _base_entity(
                title="a:T",
                source_file=str(src),
                span="1:0-1:13",
            )
        ],
        "relationships": [],
    }
    report = _clean_report(
        [
            _matched_row(
                title="a:T",
                source_path="a.c",
                graph_span="99:0-99:1",  # disagrees with entity.span
                graph_line=99,
            )
        ]
    )
    before = json.dumps(data, sort_keys=True)
    with pytest.raises(ClangTypeOverlayError, match="canonical-span mismatch"):
        apply_clang_types_from_report(data, report, pkg)
    assert json.dumps(data, sort_keys=True) == before


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("graph_canonical_line", 2, "span/start disagreement"),
        ("matched_site_col0", 1, "span/start disagreement"),
        ("tree_sitter_line", 2, "tree-sitter coordinates"),
        ("clang_col0", 1, "Clang coordinates"),
        ("matched_site_is_canonical", False, "canonical-site markers"),
        ("graph_canonical_is_matched_site", False, "canonical-site markers"),
    ],
)
def test_inconsistent_exact_site_evidence_fails(
    tmp_path: Path, field: str, value, message: str
):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    src = pkg / "a.c"
    src.write_text("typedef int T;\n", encoding="utf-8")
    data = {
        "entities": [
            _base_entity(title="a:T", source_file=str(src), span="1:0-1:13")
        ],
        "relationships": [],
    }
    row = _matched_row(
        title="a:T",
        source_path="a.c",
        graph_span="1:0-1:13",
        graph_line=1,
    )
    row[field] = value
    before = json.dumps(data, sort_keys=True)
    with pytest.raises(ClangTypeOverlayError, match=message):
        apply_clang_types_from_report(data, _clean_report([row]), pkg)
    assert json.dumps(data, sort_keys=True) == before


def test_compile_entry_provenance_is_bound_to_census(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    src = pkg / "a.c"
    src.write_text("typedef int T;\n", encoding="utf-8")
    data = {
        "entities": [
            _base_entity(title="a:T", source_file=str(src), span="1:0-1:13")
        ],
        "relationships": [],
    }
    row = _matched_row(
        title="a:T",
        source_path="a.c",
        graph_span="1:0-1:13",
        graph_line=1,
        entry_indices=[1],
    )
    before = json.dumps(data, sort_keys=True)
    with pytest.raises(ClangTypeOverlayError, match="compile entry index"):
        apply_clang_types_from_report(data, _clean_report([row]), pkg)
    assert json.dumps(data, sort_keys=True) == before

    report = _clean_report(
        [
            _matched_row(
                title="a:T",
                source_path="a.c",
                graph_span="1:0-1:13",
                graph_line=1,
            )
        ]
    )
    report["translation_units"][0]["entry_index"] = 1
    with pytest.raises(ClangTypeOverlayError, match="entry_index"):
        apply_clang_types_from_report(data, report, pkg)
    assert json.dumps(data, sort_keys=True) == before


def test_missing_symbol_name_fails(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    src = pkg / "a.c"
    src.write_text("typedef int T;\n", encoding="utf-8")
    entity = _base_entity(title="a:T", source_file=str(src), span="1:0-1:13")
    entity.pop("symbol_name")
    data = {"entities": [entity], "relationships": []}
    report = _clean_report(
        [
            _matched_row(
                title="a:T",
                source_path="a.c",
                graph_span="1:0-1:13",
                graph_line=1,
            )
        ]
    )
    with pytest.raises(ClangTypeOverlayError, match="symbol_name mismatch"):
        apply_clang_types_from_report(data, report, pkg)


def test_duplicate_matched_title_fails(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    src = pkg / "a.c"
    src.write_text("typedef int T;\n", encoding="utf-8")
    data = {
        "entities": [
            _base_entity(
                title="a:T",
                source_file=str(src),
                span="1:0-1:13",
            )
        ],
        "relationships": [],
    }
    report = _clean_report(
        [
            _matched_row(
                title="a:T",
                source_path="a.c",
                graph_span="1:0-1:13",
                graph_line=1,
            ),
            _matched_row(
                title="a:T",
                source_path="a.c",
                graph_span="1:0-1:13",
                graph_line=1,
                qual="long",
            ),
        ]
    )
    report["counts"]["matched"] = 2
    before = json.dumps(data, sort_keys=True)
    with pytest.raises(ClangTypeOverlayError, match="duplicate matched title"):
        apply_clang_types_from_report(data, report, pkg)
    assert json.dumps(data, sort_keys=True) == before


def test_non_unique_entity_title_fails(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    src = pkg / "a.c"
    src.write_text("typedef int T;\n", encoding="utf-8")
    data = {
        "entities": [
            _base_entity(
                title="a:T", source_file=str(src), span="1:0-1:13"
            ),
            _base_entity(
                title="a:T", source_file=str(src), span="1:0-1:13"
            ),
        ],
        "relationships": [],
    }
    report = _clean_report(
        [
            _matched_row(
                title="a:T",
                source_path="a.c",
                graph_span="1:0-1:13",
                graph_line=1,
            )
        ]
    )
    with pytest.raises(ClangTypeOverlayError, match="non-unique"):
        apply_clang_types_from_report(data, report, pkg)


def test_report_mode_and_provenance_mismatch_fails(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    src = pkg / "a.c"
    src.write_text("typedef int T;\n", encoding="utf-8")
    data = {
        "entities": [
            _base_entity(
                title="a:T", source_file=str(src), span="1:0-1:13"
            )
        ],
        "relationships": [],
    }
    bad_mode = _clean_report(
        [
            _matched_row(
                title="a:T",
                source_path="a.c",
                graph_span="1:0-1:13",
                graph_line=1,
            )
        ]
    )
    bad_mode["mode"] = "wrong"
    with pytest.raises(ClangTypeOverlayError, match="unexpected type audit mode"):
        apply_clang_types_from_report(data, bad_mode, pkg)

    bad_pkg = _clean_report(
        [
            _matched_row(
                title="a:T",
                source_path="a.c",
                graph_span="1:0-1:13",
                graph_line=1,
            )
        ]
    )
    bad_pkg["package"] = "other"
    with pytest.raises(ClangTypeOverlayError, match="does not match"):
        apply_clang_types_from_report(data, bad_pkg, pkg)

    bad_digest = _clean_report(
        [
            _matched_row(
                title="a:T",
                source_path="a.c",
                graph_span="1:0-1:13",
                graph_line=1,
            )
        ]
    )
    bad_digest["matched"][0]["compile_commands_digest"] = "zzz"
    bad_digest["matched"][0]["compilers"][0]["compile_commands_digest"] = "zzz"
    with pytest.raises(
        ClangTypeOverlayError, match="compile_commands_digest disagrees"
    ):
        apply_clang_types_from_report(data, bad_digest, pkg)


def test_stale_clang_type_fields_fail(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    src = pkg / "a.c"
    src.write_text("typedef int T;\n", encoding="utf-8")
    stale = {
        "entities": [
            _base_entity(
                title="a:T",
                source_file=str(src),
                span="1:0-1:13",
                clang_type_declaration_confirmed=True,
            )
        ],
        "relationships": [],
    }
    with pytest.raises(ClangTypeOverlayError, match="stale Clang type fields"):
        apply_clang_types_from_report(stale, _clean_report([]), pkg)

    non_type = {
        "entities": [
            _base_entity(
                title="a:f",
                source_file=str(src),
                etype="function",
                span="1:0-1:13",
                clang_type_qual_type="int",
            )
        ],
        "relationships": [],
    }
    with pytest.raises(ClangTypeOverlayError, match="non-type entity"):
        apply_clang_types_from_report(non_type, _clean_report([]), pkg)


def test_atomic_no_partial_mutation(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    a = pkg / "a.c"
    b = pkg / "b.c"
    a.write_text("typedef int A;\n", encoding="utf-8")
    b.write_text("typedef int B;\n", encoding="utf-8")
    data = {
        "entities": [
            _base_entity(
                title="a:A", source_file=str(a), span="1:0-1:13"
            ),
            _base_entity(
                title="b:B", source_file=str(b), span="1:0-1:13"
            ),
        ],
        "relationships": [],
    }
    report = _clean_report(
        [
            _matched_row(
                title="a:A",
                source_path="a.c",
                name="A",
                graph_span="1:0-1:13",
                graph_line=1,
            ),
            _matched_row(
                title="b:B",
                source_path="b.c",
                name="B",
                graph_span="99:0-99:1",  # mismatch on second row
                graph_line=99,
            ),
        ]
    )
    before = json.dumps(data, sort_keys=True)
    with pytest.raises(ClangTypeOverlayError, match="canonical-span mismatch"):
        apply_clang_types_from_report(data, report, pkg)
    assert json.dumps(data, sort_keys=True) == before
    assert "clang_type_declaration_confirmed" not in data["entities"][0]


def test_idempotent_reapplication(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    src = pkg / "a.c"
    src.write_text("typedef int T;\n", encoding="utf-8")
    data = {
        "entities": [
            _base_entity(
                title="a:T", source_file=str(src), span="1:0-1:13"
            )
        ],
        "relationships": [],
    }
    report = _clean_report(
        [
            _matched_row(
                title="a:T",
                source_path="a.c",
                graph_span="1:0-1:13",
                graph_line=1,
            )
        ]
    )
    p1 = apply_clang_types_from_report(data, report, pkg)
    snap = json.dumps(data["entities"][0], sort_keys=True)
    p2 = apply_clang_types_from_report(data, report, pkg)
    assert p1["n_facts"] == 1
    assert p2["n_facts"] == 1
    assert p2["n_facts_changed"] == 0
    assert json.dumps(data["entities"][0], sort_keys=True) == snap


def test_alternate_declaration_sites_allowed(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    src = pkg / "a.c"
    src.write_text("typedef int T;\n", encoding="utf-8")
    data = {
        "entities": [
            _base_entity(
                title="a:T", source_file=str(src), span="1:0-1:13"
            )
        ],
        "relationships": [],
    }
    report = _clean_report(
        [
            _matched_row(
                title="a:T",
                source_path="a.c",
                graph_span="1:0-1:13",
                graph_line=1,
            )
        ],
        alternate_declaration_sites=1,
        anonymous_declarations=2,
        out_of_compile_db_scope=1,
    )
    report["alternate_declaration_sites"] = [
        {
            "classification": "alternate_declaration_sites",
            "name": "T",
            "line": 5,
        }
    ]
    report["anonymous_declarations"] = [{"name": None}, {"name": None}]
    report["out_of_compile_db_scope"] = [{"name": "U"}]
    prov = apply_clang_types_from_report(data, report, pkg)
    assert prov["n_facts"] == 1
    assert prov["counts"]["alternate_declaration_sites"] == 1
    assert prov["counts"]["anonymous_declarations"] == 2
    assert prov["counts"]["out_of_compile_db_scope"] == 1


@pytest.mark.parametrize(
    "bucket",
    ["tree_sitter_only", "clang_only", "ambiguous", "macro_location_unsupported"],
)
def test_standard_residual_blocks_publication(tmp_path: Path, bucket: str):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    src = pkg / "a.c"
    src.write_text("typedef int T;\n", encoding="utf-8")
    data = {
        "entities": [
            _base_entity(
                title="a:T", source_file=str(src), span="1:0-1:13"
            )
        ],
        "relationships": [],
    }
    report = _clean_report(
        [
            _matched_row(
                title="a:T",
                source_path="a.c",
                graph_span="1:0-1:13",
                graph_line=1,
            )
        ]
    )
    report["counts"][bucket] = 1
    report[bucket] = [{"name": "bad"}]
    before = json.dumps(data, sort_keys=True)
    with pytest.raises(ClangTypeOverlayError, match="unclean type-audit residuals"):
        apply_clang_types_from_report(data, report, pkg)
    assert json.dumps(data, sort_keys=True) == before


def test_conflicting_preexisting_metadata_fails(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    src = pkg / "a.c"
    src.write_text("typedef int T;\n", encoding="utf-8")
    data = {
        "entities": [
            _base_entity(
                title="a:T",
                source_file=str(src),
                span="1:0-1:13",
                clang_type_qual_type="void",
            )
        ],
        "relationships": [],
    }
    report = _clean_report(
        [
            _matched_row(
                title="a:T",
                source_path="a.c",
                graph_span="1:0-1:13",
                graph_line=1,
                qual="int",
            )
        ]
    )
    with pytest.raises(ClangTypeOverlayError, match="conflicting pre-existing"):
        apply_clang_types_from_report(data, report, pkg)
    assert data["entities"][0]["clang_type_qual_type"] == "void"
    assert "clang_type_declaration_confirmed" not in data["entities"][0]


def test_disabled_provenance_shape():
    assert build_disabled_provenance() == {
        "mode": "off",
        "enabled": False,
        "n_facts": 0,
        "n_translation_units": 0,
    }


def test_default_build_c_byog_unchanged():
    d = build_c_byog(ROOT / "examples" / "inih")
    for e in d["entities"]:
        assert not any(str(k).startswith("clang_type_") for k in e)
    assert d == build_c_byog(ROOT / "examples" / "inih")


# ---------------------------------------------------------------------------
# CLI / parquet
# ---------------------------------------------------------------------------


def test_cli_default_off_manifest(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.c").write_text("typedef int T;\nint f(void){return 0;}\n")
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
        allow_toolchain_drift=False,
    )
    snapshot = (graph / "current").read_text(encoding="utf-8").strip()
    manifest = json.loads(
        (graph / "snapshots" / snapshot / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["clang_types"] == {
        "mode": "off",
        "enabled": False,
        "n_facts": 0,
        "n_translation_units": 0,
    }
    assert manifest["counts"]["entities"] == len(baseline["entities"])
    import pandas as pd

    ents = pd.read_parquet(graph / "snapshots" / snapshot / "entities.parquet")
    assert not any(str(c).startswith("clang_type_") for c in ents.columns)


# ---------------------------------------------------------------------------
# Live packages
# ---------------------------------------------------------------------------


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_inih_types():
    pkg = ROOT / "examples" / "inih"
    data = build_c_byog(pkg)
    n_ent = len(data["entities"])
    n_rel = len(data["relationships"])
    n_tu = len(data["text_units"])
    n_calls = sum(1 for r in data["relationships"] if r.get("type") == "calls")
    n_obs = len(data.get("call_observations") or [])
    before = {p.relative_to(pkg).as_posix() for p in pkg.rglob("*") if p.is_file()}
    prov = append_clang_types(data, pkg)
    after = {p.relative_to(pkg).as_posix() for p in pkg.rglob("*") if p.is_file()}
    assert before == after
    assert not any(pkg.rglob("*.o"))
    assert not any(pkg.rglob("*.ast"))
    assert not any(pkg.rglob("*.d"))
    assert not any(pkg.rglob("*.i"))
    assert prov["mode"] == MODE
    assert prov["n_facts"] == 3
    assert prov["counts"]["matched"] == 3
    assert prov["counts"]["alternate_declaration_sites"] == 1
    assert prov["counts"]["tree_sitter_only"] == 0
    assert prov["counts"]["clang_only"] == 0
    assert prov["counts"]["ambiguous"] == 0
    assert prov["counts"]["macro_location_unsupported"] == 0
    assert len(data["entities"]) == n_ent == 21
    assert len(data["relationships"]) == n_rel == 56
    assert len(data["text_units"]) == n_tu == 21
    assert n_calls == 38
    assert n_obs == 35
    typed = [
        e
        for e in data["entities"]
        if e.get("clang_type_declaration_confirmed") is True
    ]
    assert len(typed) == 3
    names = {e.get("symbol_name") for e in typed}
    assert names == {"ini_parse_string_ctx", "ini_handler", "ini_reader"}
    handler = next(e for e in typed if e.get("symbol_name") == "ini_handler")
    assert handler["span"].startswith("58:")
    assert handler["clang_type_graph_canonical_line"] == 58
    assert handler["clang_type_matched_site_line"] == 62
    assert handler["clang_type_matched_site_is_canonical"] is False
    # Base fields untouched
    assert handler["extractor"] == "tree-sitter-c"
    assert handler["type"] == "typedef"
    # Idempotent
    prov2 = append_clang_types(data, pkg)
    assert prov2["n_facts"] == 3
    assert prov2["n_facts_changed"] == 0


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_cjson_types():
    pkg = ROOT / "examples" / "cjson"
    data = build_c_byog(pkg)
    n_ent = len(data["entities"])
    n_rel = len(data["relationships"])
    n_calls = sum(1 for r in data["relationships"] if r.get("type") == "calls")
    n_obs = len(data.get("call_observations") or [])
    before = {p.relative_to(pkg).as_posix() for p in pkg.rglob("*") if p.is_file()}
    prov = append_clang_types(data, pkg)
    after = {p.relative_to(pkg).as_posix() for p in pkg.rglob("*") if p.is_file()}
    assert before == after
    assert prov["n_facts"] == 10
    assert prov["counts"]["matched"] == 10
    assert prov["counts"]["tree_sitter_only"] == 0
    assert prov["counts"]["clang_only"] == 0
    assert prov["counts"]["ambiguous"] == 0
    assert prov["counts"]["macro_location_unsupported"] == 0
    assert len(data["entities"]) == n_ent == 148
    assert len(data["relationships"]) == n_rel == 640
    assert n_calls == 495
    assert n_obs == 144
    typed = [
        e
        for e in data["entities"]
        if e.get("clang_type_declaration_confirmed") is True
    ]
    assert len(typed) == 10
    # Cross-kind titles preserved
    titles = {e["title"] for e in typed}
    assert "cJSON:struct:cJSON" in titles
    assert "cJSON:typedef:cJSON" in titles


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_index_c_types_parquet(tmp_path: Path):
    pkg = ROOT / "examples" / "inih"
    graph = tmp_path / "g-types"
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
        clang_types=True,
        allow_toolchain_drift=False,
    )
    snap = (graph / "current").read_text(encoding="utf-8").strip()
    snap_dir = graph / "snapshots" / snap
    import pandas as pd

    ents = pd.read_parquet(snap_dir / "entities.parquet")
    rels = pd.read_parquet(snap_dir / "relationships.parquet")
    assert len(ents) == len(baseline["entities"]) == 21
    assert len(rels) == len(baseline["relationships"]) == 56
    typed = ents[ents["clang_type_declaration_confirmed"] == True]  # noqa: E712
    assert len(typed) == 3
    assert (typed["clang_type_fact_kind"] == FACT_KIND).all()
    manifest = json.loads((snap_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["clang_types"]["mode"] == MODE
    assert manifest["clang_types"]["enabled"] is True
    assert manifest["clang_types"]["n_facts"] == 3
    assert manifest["clang_types"]["counts"]["matched"] == 3
    assert manifest["clang_types"]["counts"]["alternate_declaration_sites"] == 1
    assert manifest["clang_types"]["compile_commands_digest"]
    assert manifest["clang_signatures"]["mode"] == "off"
    assert manifest["clang_calls"]["mode"] == "off"
    assert not any(pkg.rglob("*.o"))
    assert not any(pkg.rglob("*.ast"))


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_index_failure_leaves_snapshot_unchanged(tmp_path: Path, monkeypatch):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.c").write_text("typedef int T;\nint f(void){return 0;}\n")
    # Minimal compile_commands so capture could succeed if not forced to fail.
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
        allow_toolchain_drift=False,
    )
    prior_current = (graph / "current").read_text(encoding="utf-8")
    prior_snapshots = sorted(p.name for p in (graph / "snapshots").iterdir())

    import c_clang_types as types_mod
    import index_c as index_mod

    def boom(*_a, **_k):
        raise types_mod.ClangTypeOverlayError("forced type overlay failure")

    monkeypatch.setattr(types_mod, "append_clang_types", boom)
    monkeypatch.setattr(index_mod, "append_clang_types", boom)

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
            clang_types=True,
            allow_toolchain_drift=False,
        )
    code = getattr(ei.value, "exit_code", None)
    if code is None:
        code = getattr(ei.value, "code", None)
    assert code == 2
    assert (graph / "current").read_text(encoding="utf-8") == prior_current
    assert sorted(
        p.name for p in (graph / "snapshots").iterdir()
    ) == prior_snapshots


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_overlays_independent_of_compiler_deps_includes(tmp_path: Path):
    """Type overlay does not require or invent depends_on/includes edges."""
    pkg = ROOT / "examples" / "inih"
    data = build_c_byog(pkg)
    n_rel = len(data["relationships"])
    dep_before = sum(
        1 for r in data["relationships"] if r.get("type") == "depends_on"
    )
    inc_before = sum(
        1 for r in data["relationships"] if r.get("type") == "includes"
    )
    append_clang_types(data, pkg)
    assert len(data["relationships"]) == n_rel
    assert (
        sum(1 for r in data["relationships"] if r.get("type") == "depends_on")
        == dep_before
    )
    assert (
        sum(1 for r in data["relationships"] if r.get("type") == "includes")
        == inc_before
    )
