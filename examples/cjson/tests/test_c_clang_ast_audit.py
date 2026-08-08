"""Clang AST JSON function-definition audit (diagnostic only).

Pure fixture tests always run. Live Clang integration skips when no compiler
is available. GCC identity is refused even if a binary named gcc is present.

Run: uv run python -m pytest examples/cjson/tests/test_c_clang_ast_audit.py -q
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
from c_clang_ast_audit import (  # type: ignore
    ClangAstAuditError,
    _has_function_body,
    audit_to_json,
    collect_function_definitions_from_ast,
    match_definitions,
    parse_ast_json_document,
    require_clang_identity,
    run_clang_ast_audit,
)
from c_compiler_common import (  # type: ignore
    CompilerOverlayError,
    require_clang_identity as common_require_clang,
)
from c_preprocessor import find_c_compiler  # type: ignore
from extract_c import build_c_byog  # type: ignore

FIXTURES = Path(__file__).parent / "fixtures" / "clang_ast"


def _cc():
    return find_c_compiler()


def _load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _collect(name: str, package_dir: Path, cwd: Path | None = None):
    root = _load_fixture(name)
    return collect_function_definitions_from_ast(
        root,
        package_dir=package_dir,
        cwd=cwd or package_dir,
        entry_index=0,
        compiler_path="/usr/bin/clang",
        compiler_id="Apple clang test",
        compile_commands_digest="deadbeef",
    )


# ---------------------------------------------------------------------------
# Pure parsing
# ---------------------------------------------------------------------------


def test_prototype_is_not_a_definition(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "main.c").write_text("int proto_only(void);\nint normal_fn(int x){return x;}\n")
    defs = _collect("proto_and_defn.json", pkg)
    names = {d.name for d in defs}
    assert "proto_only" not in names
    assert "normal_fn" in names


def test_function_decl_with_body_is_definition(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "main.c").write_text("int normal_fn(int x){return x;}\nstatic int static_helper(void){return 1;}\n")
    defs = _collect("proto_and_defn.json", pkg)
    by_name = {d.name: d for d in defs}
    assert by_name["normal_fn"].qual_type == "int (int)"
    assert by_name["static_helper"].storage_class == "static"
    assert by_name["static_helper"].source_path == "main.c"  # inherited file


def test_header_definition_and_omitted_file_context(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "main.c").write_text("int normal_fn(int x){return x;}\n")
    (pkg / "hdr.h").write_text("static inline int header_inline(int a){return a;}\n")
    defs = _collect("header_and_inherit.json", pkg)
    by_name = {d.name: d for d in defs}
    assert by_name["header_inline"].source_path == "hdr.h"
    assert by_name["header_inline"].inline is True
    assert by_name["static_helper"].source_path == "main.c"


def test_implicit_and_system_excluded(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "main.c").write_text("int normal_fn(int x){return x;}\nstatic int static_helper(void){return 1;}\n")
    defs = _collect("implicit_and_system.json", pkg)
    names = {d.name for d in defs}
    assert "__builtin_nanf" not in names
    assert "isascii" not in names
    # Builtin must not re-parent static_helper away from main.c
    assert {d.source_path for d in defs} == {"main.c"}
    assert "static_helper" in names


def test_macro_same_file_ok(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "main.c").write_text("int normal_fn(int x){return x;}\nint macro_gen(void){return 7;}\n")
    defs = _collect("macro_same_file.json", pkg)
    by_name = {d.name: d for d in defs}
    assert by_name["macro_gen"].classification_hint is None
    assert by_name["macro_gen"].source_path == "main.c"
    assert by_name["macro_gen"].location_origin == "expansion"


def test_macro_spelling_expansion_disagree(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "main.c").write_text("int x;\n")
    (pkg / "macros.h").write_text("#define X\n")
    defs = _collect("macro_disagree.json", pkg)
    assert len(defs) == 1
    assert defs[0].classification_hint == "macro_location_unsupported"
    assert defs[0].spelling_path == "macros.h"
    assert defs[0].expansion_path == "main.c"

    from c_clang_ast_audit import TreeSitterFunction

    ts = TreeSitterFunction(
        title="main:macro_other",
        name="macro_other",
        source_path="main.c",
        line=10,
        col=1,
        preprocessor_dependent=False,
        preprocessor_reasons=(),
        preprocessor_branches=(),
    )
    report = match_definitions(
        clang_defs=defs,
        tree_sitter=[ts],
        in_scope_paths={"main.c", "macros.h"},
    )
    assert report["counts"]["macro_location_unsupported"] == 1
    assert report["tree_sitter_only"] == []


def test_static_same_name_two_files_distinct(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.c").write_text("static int static_helper(void){return 1;}\n")
    (pkg / "b.c").write_text("static int static_helper(void){return 2;}\n")
    defs = _collect("two_files_static.json", pkg)
    keys = {(d.source_path, d.name) for d in defs}
    assert keys == {("a.c", "static_helper"), ("b.c", "static_helper")}


def test_has_function_body_helper():
    assert _has_function_body({"inner": [{"kind": "CompoundStmt"}]})
    assert not _has_function_body({"inner": [{"kind": "ParmVarDecl"}]})
    assert not _has_function_body({})


def test_parse_ast_json_rejects_empty_and_multi_root():
    with pytest.raises(ClangAstAuditError, match="empty"):
        parse_ast_json_document("")
    with pytest.raises(ClangAstAuditError, match="multiple"):
        parse_ast_json_document('{"kind":"A"}\n{"kind":"B"}\n')
    with pytest.raises(ClangAstAuditError, match="malformed"):
        parse_ast_json_document("{not json")


def test_match_and_dedup_deterministic(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "main.c").write_text("int normal_fn(int x){return x;}\n")
    d1 = _collect("proto_and_defn.json", pkg)
    d2 = _collect("proto_and_defn.json", pkg)
    for d in d2:
        d.entry_indices = [1]
    # Simulate merge via match_definitions with empty tree-sitter
    from c_clang_ast_audit import TreeSitterFunction, _merge_clang_defs

    merged = _merge_clang_defs(list(d1) + list(d2))
    normal = next(d for d in merged if d.name == "normal_fn")
    assert normal.entry_indices == [0, 1]
    assert normal.observation_variants[0]["entry_indices"] == [0, 1]
    report1 = match_definitions(
        clang_defs=merged,
        tree_sitter=[],
        in_scope_paths={"main.c"},
    )
    report2 = match_definitions(
        clang_defs=merged,
        tree_sitter=[],
        in_scope_paths={"main.c"},
    )
    assert report1 == report2

    # A materially different result from another compile entry must remain
    # visible instead of silently borrowing fields from the first result.
    conflicting = _collect("proto_and_defn.json", pkg)
    next(d for d in conflicting if d.name == "normal_fn").qual_type = "long (int)"
    for d in conflicting:
        d.entry_indices = [2]
    conflict_report = match_definitions(
        clang_defs=list(d1) + conflicting,
        tree_sitter=[],
        in_scope_paths={"main.c"},
    )
    conflict = next(
        row for row in conflict_report["ambiguous"]
        if row["name"] == "normal_fn"
    )
    assert conflict["reason"].startswith("compile entries produced")
    assert len(conflict["observations"]) == 2

    # Path+name is not enough when both parsers report incompatible lines;
    # the tree-sitter row belongs only to the ambiguity bucket.
    ts = TreeSitterFunction(
        title="main:normal_fn",
        name="normal_fn",
        source_path="main.c",
        line=99,
        col=5,
        preprocessor_dependent=False,
        preprocessor_reasons=(),
        preprocessor_branches=(),
    )
    line_report = match_definitions(
        clang_defs=[next(d for d in d1 if d.name == "normal_fn")],
        tree_sitter=[ts],
        in_scope_paths={"main.c"},
    )
    assert line_report["counts"]["ambiguous"] == 1
    assert line_report["tree_sitter_only"] == []


def test_require_clang_identity_rejects_gcc_string():
    # Monkeypatch compiler_identity via require_clang_identity path: call common
    # with a fake by patching.
    import c_compiler_common as common

    orig = common.compiler_identity
    try:
        common.compiler_identity = lambda p: ("gcc (Ubuntu 11.4.0)", "11.4.0")  # type: ignore
        with pytest.raises(CompilerOverlayError, match="Clang"):
            common.require_clang_identity("/usr/bin/gcc")
    finally:
        common.compiler_identity = orig


# ---------------------------------------------------------------------------
# Live Clang
# ---------------------------------------------------------------------------


def _write_live_package(root: Path, *, enable_alt: bool = False) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "hdr.h").write_text(
        "#ifndef HDR_H\n#define HDR_H\n"
        "static inline int header_inline(int a) { return a * 2; }\n"
        "#endif\n",
        encoding="utf-8",
    )
    alt_block = (
        "#ifdef ENABLE_ALT\nint alt_fn(void) { return 1; }\n#endif\n"
    )
    (root / "main.c").write_text(
        '#include "hdr.h"\n'
        "int normal_fn(int x) { return x + 1; }\n"
        "static int static_helper(void) { return 42; }\n"
        "#define MAKE_FN(name) int name(void) { return 7; }\n"
        "MAKE_FN(macro_gen)\n"
        f"{alt_block}"
        "int main(void) { return normal_fn(static_helper()); }\n",
        encoding="utf-8",
    )
    (root / "second.c").write_text(
        "static int static_helper(void) { return 99; }\n"
        "int second_tu(void) { return static_helper(); }\n",
        encoding="utf-8",
    )
    compiler = _cc() or "clang"
    flags_main = f"{compiler} -c -I. main.c -o main.o"
    if enable_alt:
        flags_main = f"{compiler} -c -I. -DENABLE_ALT main.c -o main.o"
    entries = [
        {"directory": str(root), "command": flags_main, "file": "main.c"},
        {
            "directory": str(root),
            "command": f"{compiler} -c -I. second.c -o second.o",
            "file": "second.c",
        },
        # Duplicate entry for main (dedup provenance)
        {"directory": str(root), "command": flags_main, "file": "main.c"},
    ]
    (root / "compile_commands.json").write_text(
        json.dumps(entries), encoding="utf-8"
    )
    return root


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_clang_audit_matches_and_scopes(tmp_path: Path):
    pkg = _write_live_package(tmp_path / "pkg")
    before = {p.relative_to(pkg).as_posix() for p in pkg.rglob("*") if p.is_file()}
    report = run_clang_ast_audit(pkg)
    after = {p.relative_to(pkg).as_posix() for p in pkg.rglob("*") if p.is_file()}
    assert before == after
    assert not any(p.suffix in {".o", ".d", ".i", ".ast"} for p in pkg.rglob("*"))

    names_matched = {m["name"] for m in report["matched"]}
    assert "normal_fn" in names_matched
    assert "static_helper" in names_matched  # at least one file
    assert "header_inline" in names_matched
    assert "second_tu" in names_matched
    # Two file-scoped static_helpers
    statics = [m for m in report["matched"] if m["name"] == "static_helper"]
    assert {m["source_path"] for m in statics} == {"main.c", "second.c"}
    # alt disabled
    assert "alt_fn" not in names_matched
    assert report["compiler_id"] and "clang" in report["compiler_id"].lower()
    assert report["package"] == "pkg"
    assert report["n_compile_entries"] == 3
    # Dedup keeps multi entry indices for main.c defs
    normal = next(m for m in report["matched"] if m["name"] == "normal_fn")
    assert 0 in normal["entry_indices"] and 2 in normal["entry_indices"]


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_enable_alt_configuration(tmp_path: Path):
    pkg = _write_live_package(tmp_path / "pkg", enable_alt=True)
    report = run_clang_ast_audit(pkg)
    names = {m["name"] for m in report["matched"]}
    assert "alt_fn" in names


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_gcc_identity_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pkg = _write_live_package(tmp_path / "pkg")
    import c_compiler_common as common

    real_from_entry = common.compiler_from_entry

    def fake_from_entry(entry, *, cwd):
        # Keep path resolution but force gcc identity check path
        return real_from_entry(entry, cwd=cwd)

    real_req = common.require_clang_identity

    def fake_req(path):
        raise CompilerOverlayError(
            f"compiler {path!r} is not a verified Clang/Apple Clang "
            f"toolchain (version identity='gcc (Ubuntu 11.4.0)')"
        )

    monkeypatch.setattr(common, "require_clang_identity", fake_req)
    # Audit imports require_clang_identity at module level - patch there too
    import c_clang_ast_audit as audit

    monkeypatch.setattr(audit, "require_clang_identity", fake_req)
    with pytest.raises(ClangAstAuditError, match="Clang"):
        run_clang_ast_audit(pkg)


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_cjson_audit_scope_and_match():
    report = run_clang_ast_audit(ROOT / "examples" / "cjson")
    assert report["counts"]["matched"] >= 100
    assert report["counts"]["clang_only"] == 0
    # Runner is out of compile DB scope
    out_paths = {r["source_path"] for r in report["out_of_compile_db_scope"]}
    assert any("runner" in p for p in out_paths)
    # MSVC-only internals may be tree_sitter_only with preprocessor evidence
    for row in report["tree_sitter_only"]:
        assert row["source_path"] == "cJSON.c"
        assert row["preprocessor_dependent"] or row["branch_unknown_evidence"]


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_inih_audit_matches_configured():
    report = run_clang_ast_audit(ROOT / "examples" / "inih")
    names = {m["name"] for m in report["matched"]}
    assert "ini_parse" in names
    assert "ini_parse_string" in names
    assert report["counts"]["tree_sitter_only"] == 0
    assert report["counts"]["clang_only"] == 0
    assert report["counts"]["out_of_compile_db_scope"] >= 1  # runner


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_audit_deterministic_across_hash_seeds():
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
                str(ROOT / "scripts" / "c_clang_ast_audit.py"),
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


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_audit_twice_byte_identical():
    a = audit_to_json(run_clang_ast_audit(ROOT / "examples" / "inih"))
    b = audit_to_json(run_clang_ast_audit(ROOT / "examples" / "inih"))
    assert a == b


def test_default_build_c_byog_unchanged_for_cjson():
    """Audit must not mutate extract defaults."""
    d1 = build_c_byog(ROOT / "examples" / "cjson")
    d2 = build_c_byog(ROOT / "examples" / "cjson")
    assert len(d1["entities"]) == len(d2["entities"])
    assert len(d1["relationships"]) == len(d2["relationships"])
    assert {e["title"] for e in d1["entities"]} == {
        e["title"] for e in d2["entities"]
    }
    # No includes/depends_on from audit
    types = {r["type"] for r in d1["relationships"]}
    assert "includes" not in types
    assert "depends_on" not in types
    assert not any(
        r.get("fact_kind") == "configured_direct_include"
        for r in d1["relationships"]
    )
