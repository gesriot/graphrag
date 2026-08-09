"""Clang AST JSON type-use evidence audit (diagnostic only).

Pure helper tests always run. Live Clang tests skip when no compiler.

Run: uv run python -m pytest examples/cjson/tests/test_c_clang_type_use_audit.py -q
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
from c_clang_type_use_audit import (  # type: ignore
    MODE,
    ClangTypeUseAuditError,
    audit_to_json,
    build_type_use_audit_from_capture,
    is_function_pointer_qual_type,
    main as type_use_main,
    resolve_target,
    run_clang_type_use_audit,
    split_function_return_qual_type,
    strip_pointers_and_qualifiers,
)
from c_preprocessor import find_c_compiler  # type: ignore
from extract_c import build_c_byog  # type: ignore
from index_c import main as index_c_main  # type: ignore


def _cc():
    return find_c_compiler()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_split_function_return_qual_type():
    assert split_function_return_qual_type("cJSON *(const char *)") == "cJSON *"
    assert split_function_return_qual_type("void (cJSON *)") == "void"
    assert split_function_return_qual_type("int (void)") == "int"
    assert (
        split_function_return_qual_type(
            "void *(*)(size_t)"  # not a FunctionProto of a definition style
        )
        is not None
    )  # parses as return void * with (*)(size_t) — still a string form
    assert split_function_return_qual_type("int") is None


def test_strip_pointers_and_qualifiers():
    assert strip_pointers_and_qualifiers("cJSON *const") == "cJSON"
    assert strip_pointers_and_qualifiers("const cJSON *const") == "cJSON"
    assert strip_pointers_and_qualifiers("struct cJSON *") == "struct cJSON"
    assert strip_pointers_and_qualifiers("char * restrict") == "char"
    assert is_function_pointer_qual_type("void *(*)(size_t)") is True
    assert is_function_pointer_qual_type("cJSON *") is False


def test_resolve_target_struct_vs_typedef_collision():
    """C's bare typedef and explicit tag spellings stay distinct."""
    from c_clang_type_use_audit import RawTypeUse  # type: ignore

    use = RawTypeUse(
        use_kind="parameter",
        owner_kind="function",
        owner_name="f",
        owner_source_path="a.c",
        owner_line=1,
        owner_col0=0,
        owner_clang_id=None,
        target_qual_type="cJSON *",
        target_desugared_qual_type=None,
        type_alias_decl_id=None,
        source_path="a.c",
        line=1,
        col0=0,
        byte_offset=None,
        location_origin="direct",
        location_precision="declaration_bearing_node",
        is_package_local=True,
        classification_hint=None,
        entry_index=0,
        compiler_path="/usr/bin/clang",
        compiler_id="test",
        compile_commands_digest="d",
    )
    matched = {
        ("struct", "cJSON"): [
            {
                "entity_kind": "struct",
                "name": "cJSON",
                "tree_sitter_title": "cJSON:struct:cJSON",
            }
        ],
        ("typedef", "cJSON"): [
            {
                "entity_kind": "typedef",
                "name": "cJSON",
                "tree_sitter_title": "cJSON:typedef:cJSON",
                "source_path": "cJSON.h",
                "matched_site_line": 1,
                "matched_site_col0": 0,
            }
        ],
    }
    bucket, row, resolver = resolve_target(
        use, decl_by_id={}, matched_types=matched
    )
    assert bucket == "matched"
    assert row["tree_sitter_title"] == "cJSON:typedef:cJSON"
    assert resolver == "unique_typedef_spelling"

    # Exact tag spelling selects struct only.
    use2 = RawTypeUse(**{**use.__dict__, "target_qual_type": "struct cJSON *"})
    bucket2, row2, resolver2 = resolve_target(
        use2, decl_by_id={}, matched_types=matched
    )
    assert bucket2 == "matched"
    assert row2["tree_sitter_title"] == "cJSON:struct:cJSON"
    assert resolver2 == "exact_tag_spelling"

    # typeAliasDeclId selects typedef.
    from c_clang_type_use_audit import PackageTypeDecl  # type: ignore

    decls = {
        (0, "0xabc"): PackageTypeDecl(
            clang_id="0xabc",
            entity_kind="typedef",
            name="cJSON",
            source_path="cJSON.h",
            line=1,
            col0=0,
        )
    }
    use3 = RawTypeUse(
        **{**use.__dict__, "type_alias_decl_id": "0xabc", "target_qual_type": "cJSON"}
    )
    bucket3, row3, resolver3 = resolve_target(
        use3, decl_by_id=decls, matched_types=matched
    )
    assert bucket3 == "matched"
    assert row3["tree_sitter_title"] == "cJSON:typedef:cJSON"
    assert resolver3 == "type_alias_decl_id"


def test_function_owner_resolution_is_site_and_linkage_aware():
    from c_clang_type_use_audit import RawTypeUse, resolve_owner  # type: ignore

    use = RawTypeUse(
        use_kind="parameter",
        owner_kind="function",
        owner_name="same",
        owner_source_path="b.c",
        owner_line=20,
        owner_col0=0,
        owner_clang_id=None,
        target_qual_type="int",
        target_desugared_qual_type=None,
        type_alias_decl_id=None,
        source_path="b.c",
        line=20,
        col0=9,
        byte_offset=None,
        location_origin="direct",
        location_precision="declaration_bearing_node",
        is_package_local=True,
        classification_hint=None,
        entry_index=0,
        compiler_path="/usr/bin/clang",
        compiler_id="test",
        compile_commands_digest="d",
        enclosing_function_name="same",
        owner_storage_class="static",
    )
    functions = {
        "same": [
            {
                "tree_sitter_title": "a:same",
                "source_path": "a.c",
                "tree_sitter_line": 10,
                "tree_sitter_col": 0,
                "storageClass": "static",
            },
            {
                "tree_sitter_title": "b:same",
                "source_path": "b.c",
                "tree_sitter_line": 20,
                "tree_sitter_col": 0,
                "storageClass": "static",
            },
        ]
    }
    status, title, kind, resolver = resolve_owner(
        use, matched_functions=functions, matched_types={}
    )
    assert (status, title, kind, resolver) == (
        "ok",
        "b:same",
        "function",
        "exact_declaration_site",
    )

    prototype = RawTypeUse(
        **{
            **use.__dict__,
            "owner_source_path": "api.h",
            "owner_line": 3,
            "owner_storage_class": None,
        }
    )
    one_external = {
        "same": [
            {
                "tree_sitter_title": "b:same",
                "source_path": "b.c",
                "tree_sitter_line": 20,
                "tree_sitter_col": 0,
                "storageClass": None,
            }
        ]
    }
    assert resolve_owner(
        prototype, matched_functions=one_external, matched_types={}
    ) == ("ok", "b:same", "function", "unique_external_function_name")
    static_prototype = RawTypeUse(
        **{**prototype.__dict__, "owner_storage_class": "static"}
    )
    assert resolve_owner(
        static_prototype, matched_functions=one_external, matched_types={}
    ) == ("unmatched", None, None, None)
    one_static_same_file = {
        "same": [
            {
                "tree_sitter_title": "b:same",
                "source_path": "b.c",
                "tree_sitter_line": 20,
                "tree_sitter_col": 0,
                "storageClass": "static",
            }
        ]
    }
    same_file_forward = RawTypeUse(
        **{
            **static_prototype.__dict__,
            "owner_source_path": "b.c",
        }
    )
    assert resolve_owner(
        same_file_forward,
        matched_functions=one_static_same_file,
        matched_types={},
    ) == (
        "ok",
        "b:same",
        "function",
        "unique_internal_function_name_same_file",
    )


def test_type_alias_decl_ids_are_scoped_to_compile_entry():
    from c_clang_type_use_audit import RawTypeUse, PackageTypeDecl  # type: ignore

    base = RawTypeUse(
        use_kind="parameter",
        owner_kind="function",
        owner_name="f",
        owner_source_path="a.c",
        owner_line=1,
        owner_col0=0,
        owner_clang_id=None,
        target_qual_type="T",
        target_desugared_qual_type="int",
        type_alias_decl_id="0xsame",
        source_path="a.c",
        line=1,
        col0=0,
        byte_offset=None,
        location_origin="direct",
        location_precision="declaration_bearing_node",
        is_package_local=True,
        classification_hint=None,
        entry_index=1,
        compiler_path="/usr/bin/clang",
        compiler_id="test",
        compile_commands_digest="d",
    )
    decls = {
        (0, "0xsame"): PackageTypeDecl(
            "0xsame", "typedef", "T", "a.h", 1, 0
        ),
        (1, "0xsame"): PackageTypeDecl(
            "0xsame", "typedef", "T", "b.h", 2, 0
        ),
    }
    matched = {
        ("typedef", "T"): [
            {
                "entity_kind": "typedef",
                "name": "T",
                "tree_sitter_title": "a:T",
                "source_path": "a.h",
                "matched_site_line": 1,
                "matched_site_col0": 0,
            },
            {
                "entity_kind": "typedef",
                "name": "T",
                "tree_sitter_title": "b:T",
                "source_path": "b.h",
                "matched_site_line": 2,
                "matched_site_col0": 0,
            }
        ],
    }
    bucket, row, resolver = resolve_target(
        base, decl_by_id=decls, matched_types=matched
    )
    assert bucket == "matched"
    assert row["tree_sitter_title"] == "b:T"
    assert resolver == "type_alias_decl_id"
    bare = RawTypeUse(
        **{**base.__dict__, "type_alias_decl_id": None}
    )
    bucket_bare, row_bare, resolver_bare = resolve_target(
        bare, decl_by_id=decls, matched_types=matched
    )
    assert bucket_bare == "ambiguous_target"
    assert row_bare is None and resolver_bare is None


def test_resolve_unique_typedef_and_external():
    from c_clang_type_use_audit import RawTypeUse, PackageTypeDecl  # type: ignore

    use = RawTypeUse(
        use_kind="parameter",
        owner_kind="function",
        owner_name="f",
        owner_source_path="a.c",
        owner_line=1,
        owner_col0=0,
        owner_clang_id=None,
        target_qual_type="cJSON_bool",
        target_desugared_qual_type="int",
        type_alias_decl_id=None,
        source_path="a.c",
        line=1,
        col0=0,
        byte_offset=None,
        location_origin="direct",
        location_precision="declaration_bearing_node",
        is_package_local=True,
        classification_hint=None,
        entry_index=0,
        compiler_path="/usr/bin/clang",
        compiler_id="test",
        compile_commands_digest="d",
    )
    matched = {
        ("typedef", "cJSON_bool"): [
            {
                "entity_kind": "typedef",
                "name": "cJSON_bool",
                "tree_sitter_title": "cJSON:cJSON_bool",
            }
        ]
    }
    bucket, row, resolver = resolve_target(
        use, decl_by_id={}, matched_types=matched
    )
    assert bucket == "matched"
    assert resolver == "unique_typedef_spelling"
    assert row["name"] == "cJSON_bool"

    use_ext = RawTypeUse(**{**use.__dict__, "target_qual_type": "int"})
    bucket_e, _, _ = resolve_target(
        use_ext, decl_by_id={}, matched_types=matched
    )
    assert bucket_e == "external_or_system"

    use_fp = RawTypeUse(
        **{**use.__dict__, "target_qual_type": "void *(*)(size_t)"}
    )
    bucket_fp, _, _ = resolve_target(
        use_fp, decl_by_id={}, matched_types=matched
    )
    assert bucket_fp == "unsupported_type_form"

    use_macro = RawTypeUse(
        **{
            **use.__dict__,
            "classification_hint": "macro_location_unsupported",
        }
    )
    bucket_m, _, _ = resolve_target(
        use_macro, decl_by_id={}, matched_types=matched
    )
    assert bucket_m == "macro_location_unsupported"


# ---------------------------------------------------------------------------
# Builder purity / capture identity
# ---------------------------------------------------------------------------


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_builder_zero_dumps_and_db_reloads(tmp_path: Path, monkeypatch):
    pkg = ROOT / "examples" / "inih"
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

    capture = capture_clang_ast_package(pkg)
    n_after = counter["n"]
    loads_after = counter["loads"]
    assert n_after >= 1 and loads_after == 1
    build_type_use_audit_from_capture(capture)
    build_type_use_audit_from_capture(capture)
    assert counter["n"] == n_after
    assert counter["loads"] == loads_after


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_builder_does_not_mutate_ast_capture():
    pkg = ROOT / "examples" / "inih"
    capture = capture_clang_ast_package(pkg)
    before = json.dumps(
        [entry.ast_root for entry in capture.entries],
        sort_keys=True,
        separators=(",", ":"),
    )
    ids_before = [id(e.ast_root) for e in capture.entries]
    r1 = build_type_use_audit_from_capture(capture)
    r2 = build_type_use_audit_from_capture(capture)
    after = json.dumps(
        [entry.ast_root for entry in capture.entries],
        sort_keys=True,
        separators=(",", ":"),
    )
    assert before == after
    assert [id(e.ast_root) for e in capture.entries] == ids_before
    assert audit_to_json(r1) == audit_to_json(r2)
    assert r1["mode"] == MODE


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_standalone_equals_capture_built_inih_and_cjson():
    for name in ("inih", "cjson"):
        pkg = ROOT / "examples" / name
        standalone = run_clang_type_use_audit(pkg)
        capture = capture_clang_ast_package(pkg)
        from_cap = build_type_use_audit_from_capture(capture)
        assert audit_to_json(standalone) == audit_to_json(from_cap)


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_dedup_across_compile_entries_retains_provenance(tmp_path: Path):
    """Two compile entries seeing the same header use merge entry_indices."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "t.h").write_text(
        "typedef int T;\nvoid f(T x);\n",
        encoding="utf-8",
    )
    (pkg / "a.c").write_text('#include "t.h"\nvoid f(T x) { (void)x; }\n')
    (pkg / "b.c").write_text('#include "t.h"\nint g(void) { return 0; }\n')
    cc = _cc() or "clang"
    entries = [
        {
            "directory": str(pkg),
            "file": str(pkg / "a.c"),
            "arguments": [cc, "-c", "a.c", "-o", "a.o"],
        },
        {
            "directory": str(pkg),
            "file": str(pkg / "b.c"),
            "arguments": [cc, "-c", "b.c", "-o", "b.o"],
        },
    ]
    (pkg / "compile_commands.json").write_text(
        json.dumps(entries, indent=2) + "\n", encoding="utf-8"
    )
    report = run_clang_type_use_audit(pkg)
    # Header prototype parameter uses may appear once after dedup with both entries.
    params = [
        r
        for r in report["matched_internal"]
        if r.get("use_kind") == "parameter" and r.get("target_name") == "T"
    ]
    # At least the definition parameter is matched.
    assert any(r.get("target_name") == "T" for r in report["matched_internal"])
    multi = [
        r
        for r in report["matched_internal"] + report["external_or_system"]
        if isinstance(r.get("entry_indices"), list) and len(r["entry_indices"]) > 1
    ]
    header_params = [
        r
        for bucket in (
            "matched_internal",
            "owner_unmatched",
            "target_unresolved",
            "ambiguous_target",
        )
        for r in report[bucket]
        if r.get("use_kind") == "parameter"
        and r.get("source_path") == "t.h"
        and r.get("target_name") == "T"
    ]
    assert len(header_params) == 1
    assert header_params[0]["entry_indices"] == [0, 1]
    header_returns = [
        r
        for r in report["external_or_system"]
        if r.get("use_kind") == "function_return"
        and r.get("source_path") == "t.h"
        and r.get("owner_name") == "f"
    ]
    assert len(header_returns) == 1
    assert header_returns[0]["owner_tree_sitter_title"] == "a:f"
    assert header_returns[0]["owner_resolver"] == "unique_external_function_name"
    assert report["counts"]["type_uses_raw_observations"] > report["counts"][
        "type_uses_deduped_total"
    ]
    assert report["n_compile_entries"] == 2
    assert report["compile_commands_digest"]
    assert len(report["compilers"]) >= 1
    assert not any(pkg.rglob("*.o"))
    assert not any(pkg.rglob("*.ast"))
    assert not any(pkg.rglob("*.d"))
    assert not any(pkg.rglob("*.i"))
    _ = params, multi


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_report_provenance_fields_present():
    report = run_clang_type_use_audit(ROOT / "examples" / "inih")
    assert report["mode"] == MODE
    assert report["package"] == "inih"
    assert report["compile_commands_digest"]
    assert report["n_compile_entries"] >= 1
    assert isinstance(report["compilers"], list) and report["compilers"]
    for bucket in (
        "matched_internal",
        "owner_unmatched",
        "target_unresolved",
        "ambiguous_target",
        "external_or_system",
        "macro_location_unsupported",
        "unsupported_type_form",
        "unowned_context",
    ):
        assert bucket in report
        assert report["counts"][bucket] == len(report[bucket])
    for row in report["matched_internal"]:
        assert row["location_precision"] == "declaration_bearing_node"
        assert row["resolver"] in {
            "type_alias_decl_id",
            "exact_tag_spelling",
            "unique_typedef_spelling",
        }
        assert row["owner_tree_sitter_title"]
        assert row["target_tree_sitter_title"]
        assert row["entry_indices"]
        assert row["compile_commands_digest"] == report["compile_commands_digest"]


# ---------------------------------------------------------------------------
# Live package pins
# ---------------------------------------------------------------------------


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_inih_type_use_counts():
    pkg = ROOT / "examples" / "inih"
    before = {p.relative_to(pkg).as_posix() for p in pkg.rglob("*") if p.is_file()}
    report = run_clang_type_use_audit(pkg)
    after = {p.relative_to(pkg).as_posix() for p in pkg.rglob("*") if p.is_file()}
    assert before == after
    c = report["counts"]
    assert c["matched_internal"] == 14
    assert c["owner_unmatched"] == 0
    assert c["target_unresolved"] == 0
    assert c["ambiguous_target"] == 0
    assert c["macro_location_unsupported"] == 0
    assert c["unsupported_type_form"] == 2
    assert c["unowned_context"] == 0
    assert c["external_or_system"] == 72
    assert c["function_audit_matched"] == 10
    assert c["type_declaration_audit_matched"] == 3
    # Typedef uses of ini_handler / ini_reader / ctx.
    targets = {r["target_name"] for r in report["matched_internal"]}
    assert "ini_handler" in targets
    assert "ini_reader" in targets or "ini_parse_string_ctx" in targets
    # Location honesty label.
    assert all(
        r["location_precision"] == "declaration_bearing_node"
        for r in report["matched_internal"]
    )


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_inih_fail_on_mismatch_exit0(tmp_path: Path):
    out = tmp_path / "inih-type-use.json"
    assert (
        type_use_main(
            [
                "--package",
                str(ROOT / "examples" / "inih"),
                "--output",
                str(out),
                "--fail-on-mismatch",
            ]
        )
        == 0
    )
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["counts"]["matched_internal"] == 14


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_cjson_type_use_counts():
    pkg = ROOT / "examples" / "cjson"
    before = {p.relative_to(pkg).as_posix() for p in pkg.rglob("*") if p.is_file()}
    report = run_clang_type_use_audit(pkg)
    after = {p.relative_to(pkg).as_posix() for p in pkg.rglob("*") if p.is_file()}
    assert before == after
    c = report["counts"]
    assert c["matched_internal"] == 439
    assert c["owner_unmatched"] == 0
    assert c["target_unresolved"] == 0
    assert c["ambiguous_target"] == 0
    assert c["macro_location_unsupported"] == 0
    assert c["unsupported_type_form"] == 5
    assert c["unowned_context"] == 2
    assert c["external_or_system"] == 269
    assert c["function_audit_matched"] == 113
    assert c["type_declaration_audit_matched"] == 10
    # Struct vs typedef distinction among matched rows.
    kinds = {r["target_entity_kind"] for r in report["matched_internal"]}
    assert "typedef" in kinds
    titles = {r["target_tree_sitter_title"] for r in report["matched_internal"]}
    assert "cJSON:struct:cJSON" in titles or any(
        "struct:cJSON" in t for t in titles
    )
    # Field / return / parameter use kinds present.
    use_kinds = {r["use_kind"] for r in report["matched_internal"]}
    assert "function_return" in use_kinds
    assert "parameter" in use_kinds
    assert "field" in use_kinds
    assert "typedef_underlying" in use_kinds


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_cjson_fail_on_mismatch_exit0(tmp_path: Path):
    out = tmp_path / "cjson-type-use.json"
    assert (
        type_use_main(
            [
                "--package",
                str(ROOT / "examples" / "cjson"),
                "--output",
                str(out),
                "--fail-on-mismatch",
            ]
        )
        == 0
    )


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_no_artifacts_after_audit():
    for name in ("inih", "cjson"):
        pkg = ROOT / "examples" / name
        run_clang_type_use_audit(pkg)
        assert not any(pkg.rglob("*.o"))
        assert not any(pkg.rglob("*.ast"))
        assert not any(pkg.rglob("*.d"))
        assert not any(pkg.rglob("*.i"))


# ---------------------------------------------------------------------------
# No graph / index_c / manifest mutation
# ---------------------------------------------------------------------------


def test_default_index_c_unchanged_by_type_use_audit(tmp_path: Path):
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
        clang_type_uses=False,
        allow_toolchain_drift=False,
    )
    snap = (graph / "current").read_text(encoding="utf-8").strip()
    manifest = json.loads(
        (graph / "snapshots" / snap / "manifest.json").read_text(encoding="utf-8")
    )
    assert "clang_type_use" not in manifest
    assert "uses_type" not in manifest
    assert manifest["clang_types"]["mode"] == "off"
    assert manifest["counts"]["entities"] == len(baseline["entities"])
    assert manifest["counts"]["relationships"] == len(baseline["relationships"])
    import pandas as pd

    rels = pd.read_parquet(graph / "snapshots" / snap / "relationships.parquet")
    assert not any(str(t) == "uses_type" for t in rels["type"].astype(str))


def test_default_build_c_byog_has_no_uses_type():
    d = build_c_byog(ROOT / "examples" / "inih")
    assert not any(r.get("type") == "uses_type" for r in d["relationships"])
    for e in d["entities"]:
        assert not any(str(k).startswith("clang_type_use") for k in e)


def test_invalid_timeout_fails():
    for timeout in (0, -1, True, 1.5, "1"):
        with pytest.raises(ClangTypeUseAuditError, match="timeout"):
            run_clang_type_use_audit(
                ROOT / "examples" / "inih", timeout=timeout  # type: ignore[arg-type]
            )


def test_cli_missing_compile_db_exit2(tmp_path: Path):
    pkg = tmp_path / "empty"
    pkg.mkdir()
    (pkg / "a.c").write_text("int f(void){return 0;}\n")
    assert type_use_main(["--package", str(pkg)]) == 2
