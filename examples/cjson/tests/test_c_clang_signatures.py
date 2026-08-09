"""Optional Clang configured function-signature overlay.

Pure application tests always run. Live Clang tests skip when no compiler.

Run: uv run python -m pytest examples/cjson/tests/test_c_clang_signatures.py -q
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
from c_clang_signatures import (  # type: ignore
    FACT_KIND,
    ClangSignatureError,
    append_clang_signatures,
    apply_clang_signatures_from_report,
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
    etype: str = "function",
    **extra,
) -> dict:
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
    confirmed: bool = True,
    entry_indices: list | None = None,
    observations: list | None = None,
    **extra,
) -> dict:
    name = name or title.rsplit(":", 1)[-1]
    row = {
        "name": name,
        "source_path": source_path,
        "tree_sitter_title": title,
        "tree_sitter_line": 1,
        "tree_sitter_col": 0,
        "clang_line": 1,
        "clang_col": 0,
        "line_column_confirmed": confirmed,
        "qualType": qual,
        "storageClass": "static",
        "inline": False,
        "variadic": False,
        "mangledName": f"_{name}",
        "location_origin": "direct",
        "entry_indices": entry_indices or [0],
        "compiler_path": "/usr/bin/clang",
        "compiler_id": "Apple clang version test",
        "compile_commands_digest": "abc123",
        "observations": observations
        or [
            {
                "entry_indices": entry_indices or [0],
                "compiler_path": "/usr/bin/clang",
                "compiler_id": "Apple clang version test",
                "compile_commands_digest": "abc123",
                "qualType": qual,
                "storageClass": "static",
            }
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
        "clang_definitions_package_local": len(matched),
        "tree_sitter_definitions_total": len(matched),
        "tree_sitter_definitions_in_scope": len(matched),
    }
    counts.update(counts_extra)
    return {
        "mode": "clang_ast_json_audit",
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
            {"entry_index": 0, "file": "a.c", "package_local": True}
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


# ---------------------------------------------------------------------------
# Pure application
# ---------------------------------------------------------------------------


def test_apply_synthetic_matched_signature(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    src = pkg / "a.c"
    src.write_text("static int helper(void) { return 1; }\n", encoding="utf-8")
    data = {
        "entities": [
            _base_entity(title="a:helper", source_file=str(src)),
            _base_entity(
                title="a:a.c", source_file=str(src), etype="file"
            ),
        ],
        "relationships": [],
    }
    report = _clean_report(
        [_matched_row(title="a:helper", source_path="a.c", qual="int (void)")]
    )
    prov = apply_clang_signatures_from_report(data, report, pkg)
    assert prov["enabled"] is True
    assert prov["n_facts"] == 1
    assert prov["mode"] == "clang_ast_signatures"
    helper = data["entities"][0]
    assert helper["clang_signature_status"] == "matched"
    assert helper["clang_qual_type"] == "int (void)"
    assert helper["clang_storage_class"] == "static"
    assert helper["clang_signature_fact_kind"] == FACT_KIND
    assert helper["clang_signature_extractor"] == "clang-ast-json"
    assert helper["clang_signature_confidence"] == 1.0
    assert helper["clang_signature_is_deterministic"] is True
    assert helper["extractor"] == "tree-sitter-c"  # base unchanged
    assert helper["confidence"] == 1.0
    assert "configured Clang function signature" in helper["clang_signature_description"]
    # File entity untouched
    assert "clang_qual_type" not in data["entities"][1]


def test_collision_safe_same_basename(tmp_path: Path):
    pkg = tmp_path / "pkg"
    left = pkg / "src" / "left"
    right = pkg / "src" / "right"
    left.mkdir(parents=True)
    right.mkdir(parents=True)
    (left / "util.c").write_text("static int work(void){return 1;}\n")
    (right / "util.c").write_text("static int work(void){return 2;}\n")
    data = {
        "entities": [
            _base_entity(
                title="src/left/util:work",
                source_file=str(left / "util.c"),
            ),
            _base_entity(
                title="src/right/util:work",
                source_file=str(right / "util.c"),
            ),
        ],
        "relationships": [],
    }
    report = _clean_report(
        [
            _matched_row(
                title="src/left/util:work",
                source_path="src/left/util.c",
                name="work",
                qual="int (void)",
            ),
            _matched_row(
                title="src/right/util:work",
                source_path="src/right/util.c",
                name="work",
                qual="int (void)",
            ),
        ]
    )
    apply_clang_signatures_from_report(data, report, pkg)
    assert data["entities"][0]["clang_qual_type"] == "int (void)"
    assert data["entities"][1]["clang_qual_type"] == "int (void)"
    assert data["entities"][0]["title"] != data["entities"][1]["title"]


def test_source_path_mismatch_fails(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    src = pkg / "a.c"
    src.write_text("int f(void){return 0;}\n")
    second = pkg / "b.c"
    second.write_text("int g(void){return 0;}\n")
    data = {
        "entities": [
            _base_entity(title="a:f", source_file=str(src)),
            _base_entity(title="b:g", source_file=str(second)),
        ],
        "relationships": [],
    }
    report = _clean_report(
        [
            _matched_row(title="a:f", source_path="a.c", name="f"),
            _matched_row(title="b:g", source_path="z.c", name="g"),
        ]
    )
    before = json.dumps(data, sort_keys=True)
    with pytest.raises(ClangSignatureError, match="source-path mismatch"):
        apply_clang_signatures_from_report(data, report, pkg)
    # The earlier valid row must not be applied before the later failure.
    assert json.dumps(data, sort_keys=True) == before


def test_missing_entity_fails(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    data = {"entities": [], "relationships": []}
    report = _clean_report(
        [_matched_row(title="a:f", source_path="a.c", name="f")]
    )
    with pytest.raises(ClangSignatureError, match="no function entity"):
        apply_clang_signatures_from_report(data, report, pkg)


def test_non_unique_title_fails(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    src = pkg / "a.c"
    src.write_text("int f(void){return 0;}\n")
    data = {
        "entities": [
            _base_entity(title="a:f", source_file=str(src)),
            _base_entity(title="a:f", source_file=str(src)),
        ],
        "relationships": [],
    }
    report = _clean_report(
        [_matched_row(title="a:f", source_path="a.c", name="f")]
    )
    with pytest.raises(ClangSignatureError, match="non-unique"):
        apply_clang_signatures_from_report(data, report, pkg)


def test_unconfirmed_location_fails(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    src = pkg / "a.c"
    src.write_text("int f(void){return 0;}\n")
    data = {
        "entities": [_base_entity(title="a:f", source_file=str(src))],
        "relationships": [],
    }
    report = _clean_report(
        [
            _matched_row(
                title="a:f", source_path="a.c", name="f", confirmed=False
            )
        ]
    )
    before = json.dumps(data, sort_keys=True)
    with pytest.raises(ClangSignatureError, match="line_column_confirmed"):
        apply_clang_signatures_from_report(data, report, pkg)
    assert json.dumps(data, sort_keys=True) == before


def test_empty_qualtype_fails(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    src = pkg / "a.c"
    src.write_text("int f(void){return 0;}\n")
    data = {
        "entities": [_base_entity(title="a:f", source_file=str(src))],
        "relationships": [],
    }
    report = _clean_report(
        [_matched_row(title="a:f", source_path="a.c", name="f", qual="")]
    )
    with pytest.raises(ClangSignatureError, match="empty qualType"):
        apply_clang_signatures_from_report(data, report, pkg)

    missing_observations = _clean_report(
        [_matched_row(title="a:f", source_path="a.c", name="f")]
    )
    missing_observations["matched"][0]["observations"] = None
    with pytest.raises(ClangSignatureError, match="observations list"):
        apply_clang_signatures_from_report(data, missing_observations, pkg)


@pytest.mark.parametrize(
    "bucket",
    ["clang_only", "ambiguous", "macro_location_unsupported"],
)
def test_fail_closed_audit_buckets(tmp_path: Path, bucket: str):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    src = pkg / "a.c"
    src.write_text("int f(void){return 0;}\n")
    data = {
        "entities": [_base_entity(title="a:f", source_file=str(src))],
        "relationships": [],
    }
    report = _clean_report(
        [_matched_row(title="a:f", source_path="a.c", name="f")]
    )
    report["counts"][bucket] = 1
    report[bucket] = [{"name": "bad"}]
    before = json.dumps(data, sort_keys=True)
    with pytest.raises(ClangSignatureError, match="unclean audit residuals"):
        apply_clang_signatures_from_report(data, report, pkg)
    assert json.dumps(data, sort_keys=True) == before


def test_tree_sitter_only_and_out_of_scope_allowed(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    src = pkg / "a.c"
    src.write_text("int f(void){return 0;}\n")
    data = {
        "entities": [_base_entity(title="a:f", source_file=str(src))],
        "relationships": [],
    }
    report = _clean_report(
        [_matched_row(title="a:f", source_path="a.c", name="f")],
        tree_sitter_only=2,
        out_of_compile_db_scope=3,
    )
    report["tree_sitter_only"] = [{"name": "g1"}, {"name": "g2"}]
    report["out_of_compile_db_scope"] = [
        {"name": "h1"},
        {"name": "h2"},
        {"name": "h3"},
    ]
    prov = apply_clang_signatures_from_report(data, report, pkg)
    assert prov["n_facts"] == 1
    assert prov["counts"]["tree_sitter_only"] == 2
    assert prov["counts"]["out_of_compile_db_scope"] == 3

    inconsistent = json.loads(json.dumps(report))
    inconsistent["counts"]["tree_sitter_only"] = 1
    with pytest.raises(ClangSignatureError, match="count/list mismatch"):
        apply_clang_signatures_from_report(data, inconsistent, pkg)


def test_observations_json_preserves_multiple_variants(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    src = pkg / "a.c"
    src.write_text("int f(void){return 0;}\n")
    data = {
        "entities": [_base_entity(title="a:f", source_file=str(src))],
        "relationships": [],
    }
    obs = [
        {
            "entry_indices": [1],
            "compiler_path": "/usr/bin/clang",
            "compiler_id": "Apple clang version test",
            "qualType": "int (void)",
            "compile_commands_digest": "abc123",
        },
        {
            "entry_indices": [0],
            "compiler_path": "/usr/bin/clang",
            "compiler_id": "Apple clang version test",
            "qualType": "int (void)",
            "compile_commands_digest": "abc123",
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
        ]
    )
    apply_clang_signatures_from_report(data, report, pkg)
    raw = data["entities"][0]["clang_signature_observations_json"]
    parsed = json.loads(raw)
    assert isinstance(parsed, list) and len(parsed) == 2
    # Canonical: re-dump matches
    assert raw == json.dumps(
        parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )

    reversed_data = {
        "entities": [_base_entity(title="a:f", source_file=str(src))],
        "relationships": [],
    }
    reversed_report = _clean_report(
        [
            _matched_row(
                title="a:f",
                source_path="a.c",
                name="f",
                entry_indices=[0, 1],
                observations=list(reversed(obs)),
            )
        ]
    )
    apply_clang_signatures_from_report(reversed_data, reversed_report, pkg)
    assert (
        reversed_data["entities"][0]["clang_signature_observations_json"]
        == raw
    )


def test_idempotent_identical_application(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    src = pkg / "a.c"
    src.write_text("int f(void){return 0;}\n")
    data = {
        "entities": [_base_entity(title="a:f", source_file=str(src))],
        "relationships": [],
    }
    report = _clean_report(
        [_matched_row(title="a:f", source_path="a.c", name="f")]
    )
    p1 = apply_clang_signatures_from_report(data, report, pkg)
    snap = json.dumps(data["entities"][0], sort_keys=True)
    p2 = apply_clang_signatures_from_report(data, report, pkg)
    assert p1["n_facts"] == 1
    assert p2["n_facts"] == 1
    assert p2["n_facts_changed"] == 0
    assert json.dumps(data["entities"][0], sort_keys=True) == snap


def test_conflicting_preexisting_metadata_fails(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    src = pkg / "a.c"
    src.write_text("int f(void){return 0;}\n")
    data = {
        "entities": [
            _base_entity(
                title="a:f",
                source_file=str(src),
                clang_qual_type="void (void)",
            )
        ],
        "relationships": [],
    }
    report = _clean_report(
        [
            _matched_row(
                title="a:f", source_path="a.c", name="f", qual="int (void)"
            )
        ]
    )
    with pytest.raises(ClangSignatureError, match="conflicting pre-existing"):
        apply_clang_signatures_from_report(data, report, pkg)
    # Conflicting field remains the old value; no partial overwrite of others.
    assert data["entities"][0]["clang_qual_type"] == "void (void)"
    assert "clang_signature_status" not in data["entities"][0]

    stale = {
        "entities": [
            _base_entity(
                title="a:f",
                source_file=str(src),
                clang_qual_type="int (void)",
            )
        ],
        "relationships": [],
    }
    with pytest.raises(ClangSignatureError, match="stale Clang signature"):
        apply_clang_signatures_from_report(stale, _clean_report([]), pkg)


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
        assert "clang_qual_type" not in e
        assert "clang_signature_status" not in e
    assert d == build_c_byog(ROOT / "examples" / "inih")


# ---------------------------------------------------------------------------
# CLI / parquet
# ---------------------------------------------------------------------------


def test_cli_default_off_manifest(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.c").write_text("int f(void){return 0;}\n")
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
    snapshot = (graph / "current").read_text(encoding="utf-8").strip()
    manifest = json.loads(
        (graph / "snapshots" / snapshot / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["clang_signatures"] == {
        "mode": "off",
        "enabled": False,
        "n_facts": 0,
        "n_translation_units": 0,
    }
    assert manifest["counts"]["entities"] == len(baseline["entities"])
    assert manifest["counts"]["relationships"] == len(baseline["relationships"])
    import pandas as pd

    ents = pd.read_parquet(graph / "snapshots" / snapshot / "entities.parquet")
    assert not any(str(column).startswith("clang_") for column in ents.columns)


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_parquet_roundtrip_signature_fields(tmp_path: Path):
    pkg = ROOT / "examples" / "inih"
    graph = tmp_path / "g-sig"
    index_c_main(
        package=pkg,
        graph=graph,
        keep_snapshots=2,
        compiler_builtins=False,
        compiler_dependencies=False,
        compiler_includes=False,
        clang_signatures=True,
        clang_calls=False,
        clang_types=False,
        clang_type_uses=False,
        allow_toolchain_drift=False,
    )
    snapshot = (graph / "current").read_text(encoding="utf-8").strip()
    snap = graph / "snapshots" / snapshot
    import pandas as pd

    ents = pd.read_parquet(snap / "entities.parquet")
    fns = ents[ents["type"].astype(str) == "function"]
    signed = fns[fns["clang_signature_status"].astype(str) == "matched"]
    assert len(signed) == 10
    assert signed["clang_qual_type"].notna().all()
    assert (signed["clang_signature_fact_kind"] == FACT_KIND).all()
    # relationship count unchanged vs baseline extract
    baseline = build_c_byog(pkg)
    rels = pd.read_parquet(snap / "relationships.parquet")
    assert len(rels) == len(baseline["relationships"])
    assert len(ents) == len(baseline["entities"])
    assert not any(pkg.rglob("*.o"))
    assert not any(pkg.rglob("*.ast"))


# ---------------------------------------------------------------------------
# Live packages
# ---------------------------------------------------------------------------


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_inih_signatures():
    pkg = ROOT / "examples" / "inih"
    data = build_c_byog(pkg)
    n_ent = len(data["entities"])
    n_rel = len(data["relationships"])
    before = {p.relative_to(pkg).as_posix() for p in pkg.rglob("*") if p.is_file()}
    prov = append_clang_signatures(data, pkg)
    after = {p.relative_to(pkg).as_posix() for p in pkg.rglob("*") if p.is_file()}
    assert before == after
    assert prov["n_facts"] == 10
    assert prov["counts"]["matched"] == 10
    assert prov["counts"]["tree_sitter_only"] == 0
    assert prov["counts"]["out_of_compile_db_scope"] == 5
    assert len(data["entities"]) == n_ent
    assert len(data["relationships"]) == n_rel
    signed = [
        e
        for e in data["entities"]
        if e.get("clang_signature_status") == "matched"
    ]
    assert len(signed) == 10
    # Idempotent
    prov2 = append_clang_signatures(data, pkg)
    assert prov2["n_facts"] == 10
    assert prov2["n_facts_changed"] == 0


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_cjson_signatures():
    pkg = ROOT / "examples" / "cjson"
    data = build_c_byog(pkg)
    n_ent = len(data["entities"])
    n_rel = len(data["relationships"])
    before = {p.relative_to(pkg).as_posix() for p in pkg.rglob("*") if p.is_file()}
    prov = append_clang_signatures(data, pkg)
    after = {p.relative_to(pkg).as_posix() for p in pkg.rglob("*") if p.is_file()}
    assert before == after
    assert prov["n_facts"] == 113
    assert prov["counts"]["matched"] == 113
    assert prov["counts"]["tree_sitter_only"] == 3
    assert prov["counts"]["out_of_compile_db_scope"] == 19
    assert len(data["entities"]) == n_ent
    assert len(data["relationships"]) == n_rel
    signed = [
        e
        for e in data["entities"]
        if e.get("clang_signature_status") == "matched"
    ]
    assert len(signed) == 113
    # Residuals get no invented signatures
    residual_titles = {
        "cJSON:internal_malloc",
        "cJSON:internal_free",
        "cJSON:internal_realloc",
    }
    for e in data["entities"]:
        if e["title"] in residual_titles:
            assert e.get("clang_signature_status") is None


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_signature_application_deterministic():
    pkg = ROOT / "examples" / "inih"
    d1 = build_c_byog(pkg)
    d2 = build_c_byog(pkg)
    p1 = append_clang_signatures(d1, pkg)
    p2 = append_clang_signatures(d2, pkg)
    assert p1["n_facts"] == p2["n_facts"]
    sig1 = sorted(
        (
            e["title"],
            e.get("clang_qual_type"),
            e.get("clang_signature_observations_json"),
        )
        for e in d1["entities"]
        if e.get("clang_signature_status") == "matched"
    )
    sig2 = sorted(
        (
            e["title"],
            e.get("clang_qual_type"),
            e.get("clang_signature_observations_json"),
        )
        for e in d2["entities"]
        if e.get("clang_signature_status") == "matched"
    )
    assert sig1 == sig2
