"""Clang AST JSON call-site audit (diagnostic only).

Pure fixture tests always run. Live package audits skip when no compiler.

Run: uv run python -m pytest examples/cjson/tests/test_c_clang_call_audit.py -q
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
from c_clang_call_audit import (  # type: ignore
    ClangCallAuditError,
    audit_to_json,
    clang_col_to_zero_based,
    collect_calls_from_ast,
    compare_calls,
    merge_clang_calls,
    parse_tree_sitter_call_span,
    resolve_callee_expression,
    run_clang_call_audit,
    TSCallEdge,
)
from c_preprocessor import find_c_compiler  # type: ignore
from extract_c import build_c_byog  # type: ignore

FIXTURES = Path(__file__).parent / "fixtures" / "clang_call"


def _cc():
    return find_c_compiler()


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _titles_for(pkg: Path, pairs: list[tuple[str, str, str]]):
    """pairs: (path, name, title)"""
    m: dict[tuple[str, str], list[str]] = {}
    for path, name, title in pairs:
        m.setdefault((path, name), []).append(title)
    return m


def _collect(fixture: str, pkg: Path, titles: dict):
    root = _load(fixture)
    return collect_calls_from_ast(
        root,
        package_dir=pkg,
        cwd=pkg,
        entry_index=0,
        compiler_path="/usr/bin/clang",
        compiler_id="Apple clang test",
        compile_commands_digest="deadbeef",
        title_by_path_name=titles,
    )


def _prep_pkg(tmp_path: Path, files: dict[str, str]) -> Path:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    for rel, text in files.items():
        p = pkg / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return pkg


# ---------------------------------------------------------------------------
# Pure unit helpers
# ---------------------------------------------------------------------------


def test_column_normalization():
    assert clang_col_to_zero_based(1) == 0
    assert clang_col_to_zero_based(28) == 27
    assert clang_col_to_zero_based(0) is None
    assert parse_tree_sitter_call_span("187:16") == (187, 16)
    assert parse_tree_sitter_call_span("2:25") == (2, 25)
    assert parse_tree_sitter_call_span("0:25") == (None, None)
    with pytest.raises(ClangCallAuditError, match="timeout must be a positive"):
        run_clang_call_audit(FIXTURES, timeout=0)


def test_resolve_callee_direct_vs_argument_not_scanned(tmp_path: Path):
    # Callee is ImplicitCast(DeclRef FunctionDecl); arguments not passed in.
    callee = {
        "kind": "ImplicitCastExpr",
        "inner": [
            {
                "kind": "DeclRefExpr",
                "referencedDecl": {
                    "id": "1",
                    "kind": "FunctionDecl",
                    "name": "helper",
                    "type": {"qualType": "int (int)"},
                },
            }
        ],
    }
    r = resolve_callee_expression(callee)
    assert r.kind == "direct_function"
    assert r.ref_name == "helper"

    parm = {
        "kind": "DeclRefExpr",
        "referencedDecl": {
            "kind": "ParmVarDecl",
            "name": "fp",
            "type": {"qualType": "int (*)(int)"},
        },
    }
    r2 = resolve_callee_expression(parm)
    assert r2.kind == "indirect"
    assert r2.ref_kind == "ParmVarDecl"

    pkg = _prep_pkg(tmp_path, {"main.c": "int caller(void) { return missing(); }\n"})
    root = {
        "kind": "TranslationUnitDecl",
        "inner": [
            {
                "id": "caller",
                "kind": "FunctionDecl",
                "name": "caller",
                "loc": {"file": "main.c", "line": 1, "col": 1},
                "inner": [
                    {
                        "kind": "CompoundStmt",
                        "inner": [
                            {
                                "kind": "CallExpr",
                                "range": {
                                    "begin": {
                                        "file": "main.c",
                                        "line": 1,
                                        "col": 27,
                                        "offset": 26,
                                    }
                                },
                                "inner": [
                                    {
                                        "kind": "DeclRefExpr",
                                        "referencedDecl": {
                                            "id": "missing-decl-id",
                                            "kind": "FunctionDecl",
                                            "name": "missing",
                                        },
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
    }
    calls, _ = collect_calls_from_ast(
        root,
        package_dir=pkg,
        cwd=pkg,
        entry_index=0,
        compiler_path="/cc",
        compiler_id="clang",
        compile_commands_digest="d",
        title_by_path_name={('main.c', 'caller'): ['main:caller']},
    )
    assert len(calls) == 1
    assert calls[0].classification == "ambiguous"
    assert calls[0].resolve_reason == "referenced FunctionDecl id not in AST index"

    from c_clang_call_audit import FnDeclInfo, resolve_function_decl_to_package_def

    external = FnDeclInfo(
        decl_id="external",
        name="collides",
        has_body=False,
        previous_decl=None,
        source_path=None,
        is_package_local=False,
        line=1,
        col=1,
        qual_type="int (void)",
        is_implicit=False,
    )
    local = FnDeclInfo(
        decl_id="local",
        name="collides",
        has_body=True,
        previous_decl=None,
        source_path="main.c",
        is_package_local=True,
        line=1,
        col=1,
        qual_type="int (void)",
        is_implicit=False,
    )
    resolved, reason = resolve_function_decl_to_package_def(
        "external",
        by_id={"external": external, "local": local},
        prev_to_ids={},
        path_name_to_def_ids={("main.c", "collides"): ["local"]},
    )
    assert resolved is None
    assert reason == "no_package_local_definition"


def test_direct_internal_and_kinds(tmp_path: Path):
    pkg = _prep_pkg(
        tmp_path,
        {
            "main.c": "int x;\n",
            "macros.h": "#define X\n",
        },
    )
    titles = _titles_for(
        pkg,
        [
            ("main.c", "helper", "main:helper"),
            ("main.c", "target", "main:target"),
            ("main.c", "recurse", "main:recurse"),
            ("main.c", "external_user", "main:external_user"),
            ("main.c", "uses_proto", "main:uses_proto"),
            ("main.c", "proto_only", "main:proto_only"),
            ("main.c", "param_ptr", "main:param_ptr"),
            ("main.c", "local_ptr", "main:local_ptr"),
            ("main.c", "member_call", "main:member_call"),
            ("main.c", "arg_not_callee", "main:arg_not_callee"),
            ("main.c", "macro_user", "main:macro_user"),
        ],
    )
    calls, scope = _collect("direct_and_kinds.json", pkg, titles)
    assert "main.c" in scope
    by_cls: dict[str, list] = {}
    for c in calls:
        by_cls.setdefault(c.classification, []).append(c)

    internals = {
        (c.caller_title, c.target_title) for c in by_cls.get("internal_direct", [])
    }
    assert ("main:target", "main:helper") in internals
    assert ("main:recurse", "main:recurse") in internals
    assert ("main:uses_proto", "main:proto_only") in internals
    # Nested argument call still produces internal target call
    assert ("main:arg_not_callee", "main:helper") in internals
    assert ("main:arg_not_callee", "main:target") in internals

    ext = by_cls.get("external_direct", [])
    assert any(c.target_name == "printf" for c in ext)

    ind = by_cls.get("indirect", [])
    kinds = {c.ref_kind for c in ind}
    assert "ParmVarDecl" in kinds
    assert "VarDecl" in kinds
    assert any(c.member_name == "m" or c.ref_kind == "FieldDecl" for c in ind)

    macros = by_cls.get("macro_location_unsupported", [])
    assert any(c.caller_name == "macro_user" for c in macros)


def test_two_file_static_helpers(tmp_path: Path):
    pkg = _prep_pkg(
        tmp_path,
        {
            "a.c": "static int helper(int x){return x;}\n",
            "b.c": "static int helper(int x){return x;}\n",
        },
    )
    titles = _titles_for(
        pkg,
        [
            ("a.c", "helper", "a:helper"),
            ("a.c", "uses_a", "a:uses_a"),
            ("b.c", "helper", "b:helper"),
            ("b.c", "uses_b", "b:uses_b"),
        ],
    )
    calls, _ = _collect("two_file_static.json", pkg, titles)
    pairs = {
        (c.caller_title, c.target_title)
        for c in calls
        if c.classification == "internal_direct"
    }
    assert ("a:uses_a", "a:helper") in pairs
    assert ("b:uses_b", "b:helper") in pairs
    assert ("a:uses_a", "b:helper") not in pairs


def test_merge_duplicate_entries_and_conflict():
    from c_clang_call_audit import RawClangCall

    a = RawClangCall(
        caller_name="t",
        caller_path="a.c",
        caller_title="a:t",
        line=1,
        col0=5,
        clang_col1=6,
        location_origin="direct",
        classification="internal_direct",
        byte_offset=10,
        target_name="h",
        target_path="a.c",
        target_title="a:h",
        ref_kind="FunctionDecl",
        resolve_reason="unique",
        entry_index=0,
        compiler_path="/cc",
        compiler_id="clang",
        compile_commands_digest="d",
    )
    b = RawClangCall(
        caller_name="t",
        caller_path="a.c",
        caller_title="a:t",
        line=1,
        col0=5,
        clang_col1=6,
        location_origin="direct",
        classification="internal_direct",
        byte_offset=10,
        target_name="h",
        target_path="a.c",
        target_title="a:h",
        ref_kind="FunctionDecl",
        resolve_reason="redeclaration chain",
        entry_index=1,
        compiler_path="/other-cc",
        compiler_id="other clang",
        compile_commands_digest="d",
    )
    merged = merge_clang_calls([a, b])
    assert len(merged) == 1
    assert merged[0]["entry_indices"] == [0, 1]
    assert len(merged[0]["observations"]) == 2
    assert len(merged[0]["compilers"]) == 2
    assert merged[0]["resolve_reason"] is None

    c = RawClangCall(
        caller_name="t",
        caller_path="a.c",
        caller_title="a:t",
        line=1,
        col0=5,
        clang_col1=6,
        location_origin="direct",
        classification="indirect",
        byte_offset=10,
        target_name="fp",
        ref_kind="VarDecl",
        resolve_reason="ptr",
        entry_index=1,
        compiler_path="/cc",
        compiler_id="clang",
        compile_commands_digest="d",
    )
    merged2 = merge_clang_calls([a, c])
    assert len(merged2) == 1
    assert merged2[0]["classification"] == "ambiguous"

    repeated = [
        RawClangCall(
            caller_name="t",
            caller_path="a.c",
            caller_title="a:t",
            line=None,
            col0=5,
            clang_col1=6,
            location_origin="inherited",
            classification="internal_direct",
            byte_offset=offset,
            target_name="h",
            target_path="a.c",
            target_title="a:h",
            ref_kind="FunctionDecl",
            entry_index=0,
            observation_index=index,
        )
        for index, offset in enumerate((30, 50))
    ]
    assert len(merge_clang_calls(repeated)) == 2

    macro_nested = [
        RawClangCall(
            caller_name="t",
            caller_path="a.c",
            caller_title="a:t",
            line=7,
            col0=5,
            clang_col1=6,
            location_origin="expansion",
            classification="external_direct",
            byte_offset=70,
            target_name=target,
            ref_kind="FunctionDecl",
            entry_index=0,
            observation_index=index,
        )
        for index, target in enumerate(("__builtin_outer", "__builtin_inner"))
    ]
    nested_merged = merge_clang_calls(macro_nested)
    assert len(nested_merged) == 2
    assert {row["target_name"] for row in nested_merged} == {
        "__builtin_outer",
        "__builtin_inner",
    }


def test_compare_mutually_exclusive_and_repeated_sites():
    clang_rows = [
        {
            "classification": "internal_direct",
            "caller_title": "a:f",
            "caller_path": "a.c",
            "target_title": "a:g",
            "line": 10,
            "col0": 5,
            "byte_offset": 100,
            "clang_col1": 6,
            "entry_indices": [0],
            "resolve_reason": "x",
            "ref_kind": "FunctionDecl",
            "ref_type": "int (void)",
            "compiler_path": "/cc",
            "compiler_id": "clang",
            "compile_commands_digest": "d",
        },
        {
            "classification": "internal_direct",
            "caller_title": "a:f",
            "caller_path": "a.c",
            "target_title": "a:g",
            "line": 12,
            "col0": 5,
            "byte_offset": 200,
            "clang_col1": 6,
            "entry_indices": [0],
            "resolve_reason": "x",
            "ref_kind": "FunctionDecl",
            "ref_type": "int (void)",
            "compiler_path": "/cc",
            "compiler_id": "clang",
            "compile_commands_digest": "d",
        },
        {
            "classification": "indirect",
            "caller_title": "a:f",
            "caller_path": "a.c",
            "line": 20,
            "col0": 3,
            "byte_offset": 300,
            "clang_col1": 4,
            "entry_indices": [0],
            "ref_kind": "ParmVarDecl",
            "target_name": "fp",
            "compiler_path": "/cc",
            "compiler_id": "clang",
            "compile_commands_digest": "d",
        },
        {
            "classification": "internal_direct",
            "caller_title": "a:f",
            "caller_path": "a.c",
            "target_title": "a:g",
            "line": None,
            "col0": 5,
            "byte_offset": 500,
            "clang_col1": 6,
            "entry_indices": [0],
        },
        {
            "classification": "internal_direct",
            "caller_title": "a:f",
            "caller_path": "a.c",
            "target_title": "a:g",
            "line": None,
            "col0": 5,
            "byte_offset": 600,
            "clang_col1": 6,
            "entry_indices": [0],
        },
    ]
    ts = [
        TSCallEdge("a:f", "a:g", "a.c", 10, 5, "10:5", byte_offset=100),
        TSCallEdge("a:f", "a:g", "a.c", 12, 5, "12:5", byte_offset=200),
        TSCallEdge("a:f", "a:g", "a.c", 20, 3, "20:3", byte_offset=300),
        # Same column as the indirect call, but a different physical site.
        TSCallEdge("a:f", "a:g", "a.c", 21, 3, "21:3", byte_offset=400),
        # Clang omitted lines; byte offsets still distinguish repeated calls.
        TSCallEdge("a:f", "a:g", "a.c", 30, 5, "30:5", byte_offset=500),
        TSCallEdge("a:f", "a:g", "a.c", 40, 5, "40:5", byte_offset=600),
        TSCallEdge("a:h", "a:g", "other.c", 1, 0, "1:0"),  # out of scope
    ]
    out = compare_calls(
        clang_rows=clang_rows,
        ts_edges=ts,
        in_scope_paths={"a.c"},
    )
    assert out["counts"]["matched_internal"] == 4
    assert out["counts"]["indirect"] == 1
    assert out["counts"]["tree_sitter_only_internal"] == 1
    assert out["counts"]["out_of_compile_db_scope"] == 1
    assert out["tree_sitter_accounting"] == {
        "total_calls": 7,
        "matched_internal": 4,
        "covered_by_noninternal_clang_observation": 1,
        "tree_sitter_only_internal": 1,
        "out_of_compile_db_scope": 1,
    }
    # mutual exclusion: no row in two buckets by identity
    matched_ids = {
        (r["caller_title"], r["target_title"], r["line"], r["col0"])
        for r in out["buckets"]["matched_internal"]
    }
    ts_only_ids = {
        (r["caller_title"], r["target_title"], r["line"], r["col0"])
        for r in out["buckets"]["tree_sitter_only_internal"]
    }
    assert matched_ids.isdisjoint(ts_only_ids)


def test_default_build_c_byog_unchanged():
    d1 = build_c_byog(ROOT / "examples" / "inih")
    d2 = build_c_byog(ROOT / "examples" / "inih")
    assert d1 == d2


# ---------------------------------------------------------------------------
# Live packages — measured counts (not prescribed)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_inih_call_audit_measured():
    pkg = ROOT / "examples" / "inih"
    before = {p.relative_to(pkg).as_posix() for p in pkg.rglob("*") if p.is_file()}
    report = run_clang_call_audit(pkg)
    after = {p.relative_to(pkg).as_posix() for p in pkg.rglob("*") if p.is_file()}
    assert before == after
    assert report["mode"] == "clang_ast_json_call_audit"
    counts = report["counts"]
    # Sanity: partitions of tree-sitter internal edges
    ts_calls = sum(
        1
        for r in build_c_byog(pkg)["relationships"]
        if r["type"] == "calls"
    )
    accounted = (
        counts["matched_internal"]
        + counts["tree_sitter_only_internal"]
        + counts["out_of_compile_db_scope"]
        + report["tree_sitter_accounting"][
            "covered_by_noninternal_clang_observation"
        ]
    )
    assert accounted == ts_calls
    assert report["tree_sitter_accounting"]["total_calls"] == ts_calls
    assert counts["matched_internal"] >= 1
    assert counts["external_direct"] >= 1
    assert counts["out_of_compile_db_scope"] >= 1  # runner
    # Deterministic
    assert audit_to_json(report) == audit_to_json(run_clang_call_audit(pkg))


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_cjson_call_audit_measured():
    pkg = ROOT / "examples" / "cjson"
    before = {p.relative_to(pkg).as_posix() for p in pkg.rglob("*") if p.is_file()}
    report = run_clang_call_audit(pkg)
    after = {p.relative_to(pkg).as_posix() for p in pkg.rglob("*") if p.is_file()}
    assert before == after
    counts = report["counts"]
    assert counts["matched_internal"] >= 50
    assert counts["out_of_compile_db_scope"] >= 100  # runner heavy
    assert counts["external_direct"] >= 1
    ts_calls = sum(
        1 for r in build_c_byog(pkg)["relationships"] if r["type"] == "calls"
    )
    accounting = report["tree_sitter_accounting"]
    assert accounting["total_calls"] == ts_calls
    assert (
        accounting["matched_internal"]
        + accounting["covered_by_noninternal_clang_observation"]
        + accounting["tree_sitter_only_internal"]
        + accounting["out_of_compile_db_scope"]
        == ts_calls
    )
    # No package artifacts
    assert not any(p.suffix in {".o", ".ast", ".d", ".i"} for p in pkg.rglob("*"))
    assert audit_to_json(report) == audit_to_json(run_clang_call_audit(pkg))


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_hash_seed_determinism_inih():
    pkg = ROOT / "examples" / "inih"
    texts = []
    for seed in ("0", "1", "42"):
        env = os.environ.copy()
        env["PYTHONHASHSEED"] = seed
        env["PYTHONPATH"] = str(ROOT / "scripts") + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
        )
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "c_clang_call_audit.py"),
                "--package",
                str(pkg),
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        texts.append(proc.stdout)
    assert texts[0] == texts[1] == texts[2]
