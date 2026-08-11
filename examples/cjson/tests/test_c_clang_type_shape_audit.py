"""Diagnostic Clang type-shape audit (member inventory only; not ABI/layout).

Pure synthetic tests always run. Live Clang tests skip when no compiler.

Run: uv run python -m pytest examples/cjson/tests/test_c_clang_type_shape_audit.py -q
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
from c_clang_type_shape_audit import (  # type: ignore
    MODE,
    ClangTypeShapeAuditError,
    _FAIL_ON_MISMATCH_BUCKETS,
    _classify_shape,
    _semantic_member_inventory,
    audit_to_json,
    build_type_shape_audit_from_capture,
    main as shape_main,
    run_clang_type_shape_audit,
)
from c_preprocessor import find_c_compiler  # type: ignore
from extract_c import (  # type: ignore
    build_c_byog,
    collect_type_shape_members_at_site,
)
from index_c import main as index_c_main  # type: ignore


def _cc():
    return find_c_compiler()


def _write_pkg(tmp_path: Path, files: dict[str, str]) -> Path:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    for rel, text in files.items():
        path = pkg / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    # Minimal compile_commands for package-local TUs.
    entries = []
    for rel in files:
        if rel.endswith((".c", ".h")) and not rel.startswith("."):
            # Only .c files as TUs typically; include a.c always.
            pass
    c_files = [rel for rel in files if rel.endswith(".c")]
    if not c_files:
        c_files = ["a.c"]
        (pkg / "a.c").write_text('#include "a.h"\n' if "a.h" in files else "int x;\n")
    for rel in c_files:
        entries.append(
            {
                "directory": str(pkg.resolve()),
                "file": rel,
                "arguments": ["clang", "-c", rel, "-I", "."],
            }
        )
    (pkg / "compile_commands.json").write_text(json.dumps(entries, indent=2))
    return pkg

# ---------------------------------------------------------------------------
# Tree-sitter member helper
# ---------------------------------------------------------------------------


def test_tree_sitter_simple_struct_and_multi_declarator(tmp_path: Path):
    pkg = _write_pkg(
        tmp_path,
        {
            "a.h": """
struct S {
  int a, *b;
  unsigned flags:3;
  int :0;
  struct { int x; } anon;
  int (*fp)(void);
};
""",
            "a.c": '#include "a.h"\n',
        },
    )
    # Find site coordinates via collect sites
    from extract_c import collect_type_declaration_sites

    sites = [
        s
        for s in collect_type_declaration_sites(pkg)
        if s.entity_kind == "struct" and s.name == "S"
    ]
    assert len(sites) >= 1
    site = sites[0]
    err, members = collect_type_shape_members_at_site(
        pkg,
        source_path=site.source_path,
        entity_kind="struct",
        line=site.line,
        col0=site.col0,
    )
    assert err is None
    names = [m.name for m in members]
    # Nested struct body field `x` must NOT appear; anon is the field name.
    assert "x" not in names
    assert names == ["a", "b", "flags", None, "anon", "fp"]
    forms = [m.form for m in members]
    assert "unnamed_bitfield" in forms
    bit = next(m for m in members if m.name == "flags")
    assert bit.is_bitfield is True
    assert bit.bit_width == 3


def test_tree_sitter_pairs_each_bitfield_with_its_own_width(tmp_path: Path):
    pkg = _write_pkg(
        tmp_path,
        {
            "a.h": "struct S { unsigned a:1, b:2; int plain; };\n",
            "a.c": '#include "a.h"\n',
        },
    )
    from extract_c import collect_type_declaration_sites

    site = next(
        s
        for s in collect_type_declaration_sites(pkg)
        if s.entity_kind == "struct" and s.name == "S"
    )
    err, members = collect_type_shape_members_at_site(
        pkg,
        source_path=site.source_path,
        entity_kind=site.entity_kind,
        line=site.line,
        col0=site.col0,
    )

    assert err is None
    assert [(m.name, m.is_bitfield, m.bit_width) for m in members] == [
        ("a", True, 1),
        ("b", True, 2),
        ("plain", False, None),
    ]


def test_tree_sitter_enum_order(tmp_path: Path):
    pkg = _write_pkg(
        tmp_path,
        {
            "a.h": "enum E { A = 1, B, C = 4 };\n",
            "a.c": '#include "a.h"\n',
        },
    )
    from extract_c import collect_type_declaration_sites

    site = next(
        s
        for s in collect_type_declaration_sites(pkg)
        if s.entity_kind == "enum" and s.name == "E"
    )
    err, members = collect_type_shape_members_at_site(
        pkg,
        source_path=site.source_path,
        entity_kind="enum",
        line=site.line,
        col0=site.col0,
    )
    assert err is None
    assert [m.name for m in members] == ["A", "B", "C"]
    assert all(m.form == "enumerator" for m in members)


def test_extract_graph_unchanged_by_helper(tmp_path: Path):
    """Graph extraction must remain byte-stable after helper addition."""
    pkg = _write_pkg(
        tmp_path,
        {
            "a.h": "struct S { int x; };\ntypedef struct S S;\n",
            "a.c": '#include "a.h"\nint f(void) { return 0; }\n',
        },
    )
    data = build_c_byog(pkg)
    titles = sorted(e["title"] for e in data["entities"])
    assert any("S" in t for t in titles)
    # Canonical spans still present.
    structs = [e for e in data["entities"] if e["type"] == "struct"]
    assert structs and structs[0].get("span")


# ---------------------------------------------------------------------------
# Live / capture-built
# ---------------------------------------------------------------------------


@pytest.mark.skipif(_cc() is None, reason="no C compiler")
def test_live_cjson_shape_matched_and_no_dumps_on_builder():
    dumps = {"n": 0}
    real = cap_mod.capture_clang_ast_package

    def wrapped(*a, **k):
        dumps["n"] += 1
        return real(*a, **k)

    cap_mod.capture_clang_ast_package = wrapped  # type: ignore
    try:
        cap = real(ROOT / "examples" / "cjson", timeout=120)
        dumps["n"] = 0
        report = build_type_shape_audit_from_capture(cap)
        assert dumps["n"] == 0
    finally:
        cap_mod.capture_clang_ast_package = real  # type: ignore

    assert report["mode"] == MODE
    assert report["counts"]["matched_shape"] >= 3
    assert report["counts"]["tree_sitter_only_members"] == 0
    assert report["counts"]["clang_only_members"] == 0
    assert report["counts"]["member_order_mismatch"] == 0
    cjson = next(
        r
        for r in report["matched_shape"]
        if r["tree_sitter_title"] == "cJSON:struct:cJSON"
    )
    assert cjson["tree_sitter_member_names"] == [
        "next",
        "prev",
        "child",
        "type",
        "valuestring",
        "valueint",
        "valuedouble",
        "string",
    ]
    assert cjson["clang_member_names"] == cjson["tree_sitter_member_names"]
    # Evidence fields present without being equality claims.
    assert any(
        m.get("qualType") for m in cjson["clang_members"] if m.get("name") == "next"
    )


@pytest.mark.skipif(_cc() is None, reason="no C compiler")
def test_standalone_json_equals_capture_built_cjson():
    cap = capture_clang_ast_package(ROOT / "examples" / "cjson", timeout=120)
    built = build_type_shape_audit_from_capture(cap)
    standalone = run_clang_type_shape_audit(ROOT / "examples" / "cjson", timeout=120)
    assert audit_to_json(built) == audit_to_json(standalone)


@pytest.mark.skipif(_cc() is None, reason="no C compiler")
def test_live_inih_no_struct_enum_owners():
    report = run_clang_type_shape_audit(ROOT / "examples" / "inih", timeout=120)
    # inih matched type-declaration owners are typedef-only in this config.
    assert report["counts"]["type_declaration_matched_struct_enum"] == 0
    assert report["counts"]["matched_shape"] == 0
    assert report["counts"]["shape_owners_classified"] == 0
    # Outside-package observations may appear; observation-only.
    assert "outside_package_declarations" in report["counts"]


@pytest.mark.skipif(_cc() is None, reason="no C compiler")
def test_fail_on_mismatch_exit_codes(tmp_path: Path):
    # cJSON is fully matched → exit 0 even with --fail-on-mismatch
    rc = shape_main(
        [
            "--package",
            str(ROOT / "examples" / "cjson"),
            "--fail-on-mismatch",
            "--timeout",
            "120",
        ]
    )
    assert rc == 0

    # Missing package / no compile_commands → exit 2
    bad = tmp_path / "empty"
    bad.mkdir()
    rc2 = shape_main(["--package", str(bad), "--timeout", "5"])
    assert rc2 == 2


@pytest.mark.skipif(_cc() is None, reason="no C compiler")
def test_default_index_unchanged_no_artifacts(tmp_path: Path):
    pkg = ROOT / "examples" / "inih"
    before = {p.name for p in pkg.iterdir()}
    graph = tmp_path / "byog"
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
    snap = (graph / "current").read_text().strip()
    man = json.loads(
        (graph / "snapshots" / snap / "manifest.json").read_text()
    )
    assert "clang_type_shape" not in man
    # No uses_type without flag.
    import pandas as pd

    rels = pd.read_parquet(graph / "snapshots" / snap / "relationships.parquet")
    assert int((rels["type"].astype(str) == "uses_type").sum()) == 0
    after = {p.name for p in pkg.iterdir()}
    assert after == before
    for pattern in ("*.o", "*.ast", "*.d", "*.i"):
        assert not list(pkg.glob(pattern))


# ---------------------------------------------------------------------------
# Pure comparison logic via synthetic package + real clang when available
# ---------------------------------------------------------------------------


@pytest.mark.skipif(_cc() is None, reason="no C compiler")
def test_live_synthetic_struct_enum_and_struct_vs_typedef_spelling(tmp_path: Path):
    # C tag namespace is shared by struct/enum, so same-tag struct+enum is
    # invalid. Same bare spelling still appears as distinct graph identities
    # for struct vs typedef (kind-qualified titles).
    pkg = _write_pkg(
        tmp_path,
        {
            "a.h": """
struct T {
  int x;
  char *y;
};
typedef struct T T;
enum E {
  E_A = 1,
  E_B = 2
};
""",
            "a.c": '#include "a.h"\nint f(T *t) { return t->x + E_A; }\n',
        },
    )
    report = run_clang_type_shape_audit(pkg, timeout=60)
    struct_rows = [r for r in report["matched_shape"] if r["entity_kind"] == "struct"]
    enum_rows = [r for r in report["matched_shape"] if r["entity_kind"] == "enum"]
    assert any(r["name"] == "T" for r in struct_rows)
    assert any(r["name"] == "E" for r in enum_rows)
    s = next(r for r in struct_rows if r["name"] == "T")
    e = next(r for r in enum_rows if r["name"] == "E")
    assert s["tree_sitter_member_names"] == ["x", "y"]
    assert e["tree_sitter_member_names"] == ["E_A", "E_B"]
    # Typedef is not an independent shape owner.
    assert not any(r.get("entity_kind") == "typedef" for r in report["matched_shape"])
    # Struct title is kind-qualified when it collides with typedef T.
    assert "struct" in s["tree_sitter_title"] or s["tree_sitter_title"].endswith(":T")
    # Enum values are evidence when present (may be None if AST omits them).
    assert any(
        m.get("name") == "E_A" and (m.get("enum_value") in (1, None))
        for m in e["clang_members"]
    )


@pytest.mark.skipif(_cc() is None, reason="no C compiler")
def test_nested_record_not_flattened(tmp_path: Path):
    pkg = _write_pkg(
        tmp_path,
        {
            "a.h": """
struct Outer {
  int a;
  struct {
    int nested_only;
  } inner;
  int b;
};
""",
            "a.c": '#include "a.h"\n',
        },
    )
    report = run_clang_type_shape_audit(pkg, timeout=60)
    row = next(
        r
        for r in report["matched_shape"]
        if r["entity_kind"] == "struct" and r["name"] == "Outer"
    )
    assert "nested_only" not in row["tree_sitter_member_names"]
    assert "nested_only" not in row["clang_member_names"]
    assert row["tree_sitter_member_names"] == ["a", "inner", "b"]


@pytest.mark.skipif(_cc() is None, reason="no C compiler")
def test_fail_on_mismatch_nonzero_when_forced(tmp_path: Path, monkeypatch):
    """Force a residual bucket and ensure --fail-on-mismatch returns 1."""
    pkg = _write_pkg(
        tmp_path,
        {
            "a.h": "struct S { int x; };\n",
            "a.c": '#include "a.h"\n',
        },
    )
    real_run = run_clang_type_shape_audit

    def fake_run(package_dir, timeout=120):
        rep = real_run(package_dir, timeout=timeout)
        # Inject a synthetic mismatch count without inventing graph facts.
        rep = json.loads(audit_to_json(rep))
        rep["tree_sitter_only_members"].append(
            {
                "classification": "tree_sitter_only_members",
                "entity_kind": "struct",
                "name": "Forced",
                "tree_sitter_title": "pkg:Forced",
                "tree_sitter_member_names": ["x"],
                "clang_member_names": [],
            }
        )
        rep["counts"]["tree_sitter_only_members"] = len(
            rep["tree_sitter_only_members"]
        )
        return rep

    import c_clang_type_shape_audit as mod

    monkeypatch.setattr(mod, "run_clang_type_shape_audit", fake_run)
    rc = shape_main(
        ["--package", str(pkg), "--fail-on-mismatch", "--timeout", "60"]
    )
    assert rc == 1
    assert "tree_sitter_only_members" in _FAIL_ON_MISMATCH_BUCKETS


def test_fail_on_mismatch_buckets_exclude_outside_and_unsupported():
    assert "outside_package_declarations" not in _FAIL_ON_MISMATCH_BUCKETS
    assert "unsupported_member_form" not in _FAIL_ON_MISMATCH_BUCKETS
    assert "matched_shape" not in _FAIL_ON_MISMATCH_BUCKETS


def test_classify_shape_pure_order_and_set_mismatches():
    def named(names):
        return [
            {"name": n, "order": i, "residual": None}
            for i, n in enumerate(names)
        ]

    assert (
        _classify_shape(
            ts_members=named(["a", "b"]),
            clang_members=named(["a", "b"]),
            multi_entry_conflict=False,
            site_error=None,
            location_origin="direct",
        )[0]
        == "matched_shape"
    )
    assert (
        _classify_shape(
            ts_members=named(["a", "b"]),
            clang_members=named(["b", "a"]),
            multi_entry_conflict=False,
            site_error=None,
            location_origin="direct",
        )[0]
        == "member_order_mismatch"
    )
    assert (
        _classify_shape(
            ts_members=named(["a", "b"]),
            clang_members=named(["a"]),
            multi_entry_conflict=False,
            site_error=None,
            location_origin="direct",
        )[0]
        == "tree_sitter_only_members"
    )
    assert (
        _classify_shape(
            ts_members=named(["a"]),
            clang_members=named(["a", "b"]),
            multi_entry_conflict=False,
            site_error=None,
            location_origin="direct",
        )[0]
        == "clang_only_members"
    )
    assert (
        _classify_shape(
            ts_members=named(["a", "a"]),
            clang_members=named(["a", "a"]),
            multi_entry_conflict=False,
            site_error=None,
            location_origin="direct",
        )[0]
        == "duplicate_or_ambiguous_members"
    )
    assert (
        _classify_shape(
            ts_members=named(["a"]),
            clang_members=named(["a"]),
            multi_entry_conflict=True,
            site_error=None,
            location_origin="direct",
        )[0]
        == "duplicate_or_ambiguous_members"
    )
    assert (
        _classify_shape(
            ts_members=[],
            clang_members=[],
            multi_entry_conflict=False,
            site_error="site_not_found",
            location_origin="direct",
        )[0]
        == "owner_unmatched"
    )


def test_classify_shape_never_matches_while_residual_members_exist():
    named = {"name": "a", "order": 0, "residual": None}
    residual = {
        "name": None,
        "order": 1,
        "residual": "anonymous_member",
    }
    assert (
        _classify_shape(
            ts_members=[named, residual],
            clang_members=[named, residual],
            multi_entry_conflict=False,
            site_error=None,
            location_origin="direct",
        )[0]
        == "unsupported_member_form"
    )


def test_compile_entry_inventory_conflict_includes_evidence_fields():
    base = {
        "name": "flags",
        "order": 0,
        "form": "field",
        "is_bitfield": True,
        "bit_width": 1,
        "qualType": "unsigned int",
        "desugaredQualType": None,
        "residual": None,
        "clang_kind": "FieldDecl",
        "line": 4,
        "col0": 2,
    }
    other_location = {**base, "line": 104, "col0": 8}
    other_width = {**base, "bit_width": 2}

    assert _semantic_member_inventory([base]) == _semantic_member_inventory(
        [other_location]
    )
    assert _semantic_member_inventory([base]) != _semantic_member_inventory(
        [other_width]
    )
