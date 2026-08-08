"""Clang type-declaration audit (diagnostic only).

Pure synthetic AST tests always run. Live Clang tests skip when no compiler.

Run: uv run python -m pytest examples/cjson/tests/test_c_clang_type_audit.py -q
"""
from __future__ import annotations

import json
import hashlib
import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

import c_clang_ast_capture as cap_mod  # type: ignore
from c_clang_ast_audit import (  # type: ignore
    audit_to_json as function_audit_to_json,
    build_function_audit_from_capture,
    run_clang_ast_audit,
)
from c_clang_ast_capture import capture_clang_ast_package  # type: ignore
from c_clang_call_audit import (  # type: ignore
    audit_to_json as call_audit_to_json,
    build_call_audit_from_capture,
    run_clang_call_audit,
)
from c_clang_type_audit import (  # type: ignore
    MODE,
    ClangTypeAuditError,
    audit_to_json,
    build_type_declaration_audit_from_capture,
    collect_type_declarations_from_ast,
    match_type_declarations,
    main as type_audit_main,
    merge_clang_type_declarations,
    run_clang_type_audit,
    TreeSitterTypeEntity,
    ClangTypeDeclaration,
)
from c_preprocessor import find_c_compiler  # type: ignore
from extract_c import build_c_byog  # type: ignore
from index_c import main as index_c_main  # type: ignore


def _cc():
    return find_c_compiler()


def _loc(file=None, line=None, col=None, **extra):
    d = {}
    if file is not None:
        d["file"] = file
    if line is not None:
        d["line"] = line
    if col is not None:
        d["col"] = col
    d.update(extra)
    return d


def _range_begin(file=None, line=None, col=None):
    return {"begin": _loc(file=file, line=line, col=col)}


def _record(
    *,
    name=None,
    tag="struct",
    complete=True,
    file="a.c",
    line=1,
    col=1,
    name_col=8,
):
    node = {
        "kind": "RecordDecl",
        "name": name,
        "tagUsed": tag,
        "loc": _loc(file=file, line=line, col=name_col),
        "range": _range_begin(file=file, line=line, col=col),
    }
    if complete:
        node["completeDefinition"] = True
    return node


def _enum(*, name=None, file="a.c", line=1, col=1, name_col=6, constants=True):
    node = {
        "kind": "EnumDecl",
        "name": name,
        "loc": _loc(file=file, line=line, col=name_col),
        "range": _range_begin(file=file, line=line, col=col),
    }
    if constants:
        node["inner"] = [
            {"kind": "EnumConstantDecl", "name": "A"},
            {"kind": "EnumConstantDecl", "name": "B"},
        ]
    return node


def _typedef(
    *,
    name="T",
    file="a.c",
    line=1,
    col=1,
    name_col=13,
    qual="int",
    desug=None,
):
    t: dict = {"qualType": qual}
    if desug is not None:
        t["desugaredQualType"] = desug
    return {
        "kind": "TypedefDecl",
        "name": name,
        "loc": _loc(file=file, line=line, col=name_col),
        "range": _range_begin(file=file, line=line, col=col),
        "type": t,
    }


def _tu(*decls):
    return {"kind": "TranslationUnitDecl", "inner": list(decls)}


def _collect(pkg: Path, root: dict, cwd: Path | None = None):
    return collect_type_declarations_from_ast(
        root,
        package_dir=pkg,
        cwd=cwd or pkg,
        entry_index=0,
        compiler_path="/usr/bin/clang",
        compiler_id="Apple clang test",
        compile_commands_digest="deadbeef",
    )


# ---------------------------------------------------------------------------
# Pure synthetic AST extraction
# ---------------------------------------------------------------------------


def test_extract_struct_enum_typedef(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.c").write_text("/* placeholder */\n", encoding="utf-8")
    root = _tu(
        _record(name="S", line=1, col=1, name_col=8),
        _enum(name="E", line=2, col=1, name_col=6),
        _typedef(name="T", line=3, col=1, name_col=13, qual="int"),
    )
    decls, scope = _collect(pkg, root)
    kinds = {(d.entity_kind, d.name) for d in decls if d.is_package_local and not d.classification_hint}
    assert ("struct", "S") in kinds
    assert ("enum", "E") in kinds
    assert ("typedef", "T") in kinds
    assert "a.c" in scope
    td = next(d for d in decls if d.name == "T" and d.entity_kind == "typedef")
    assert td.qual_type == "int"
    st = next(d for d in decls if d.name == "S")
    assert st.is_complete is True and st.tag_kind == "struct"
    en = next(d for d in decls if d.name == "E")
    assert en.is_complete is True
    invalid = _tu(_typedef(name="Bad", line=4, col=0, qual="int"))
    invalid_decls, _ = _collect(pkg, invalid)
    assert invalid_decls[0].col0 is None
    assert invalid_decls[0].matchable_identity() is None

    # Clang may omit line on a typedef immediately following its anonymous
    # owned RecordDecl, while retaining an exact range.begin byte offset.
    source = b"typedef struct { int x; } box;\n"
    (pkg / "a.c").write_bytes(source)
    offset_typedef = _typedef(name="box", line=None, col=1, qual="struct box")
    offset_typedef["loc"] = {"offset": 26, "col": 27, "tokLen": 3}
    offset_typedef["range"] = {
        "begin": {"offset": 0, "col": 1, "tokLen": 7},
        "end": {"offset": 26, "col": 27, "tokLen": 3},
    }
    offset_decls, _ = _collect(
        pkg,
        _tu(
            _record(name=None, file="a.c", line=1, col=9, name_col=9),
            offset_typedef,
        ),
    )
    offset_decl = next(d for d in offset_decls if d.name == "box")
    assert offset_decl.line == 1
    assert offset_decl.col0 == 0
    assert offset_decl.matchable_identity() == (
        "typedef",
        "a.c",
        "box",
        1,
        0,
    )


def test_struct_and_typedef_same_name_are_distinct(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.c").write_text("x\n", encoding="utf-8")
    # Same name, same path, different kinds and columns.
    root = _tu(
        _record(name="Item", line=1, col=1, name_col=8),
        _typedef(name="Item", line=5, col=1, name_col=13, qual="struct Item"),
    )
    decls, _ = _collect(pkg, root)
    matchable = [d for d in decls if d.matchable_identity()]
    ids = {d.matchable_identity() for d in matchable}
    assert len(ids) == 2
    kinds = {i[0] for i in ids}
    assert kinds == {"struct", "typedef"}


def test_anonymous_explicitly_accounted(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.c").write_text("x\n", encoding="utf-8")
    root = _tu(
        _record(name=None, line=1, col=1),
        _enum(name=None, line=2, col=1),
    )
    decls, _ = _collect(pkg, root)
    assert all(d.classification_hint == "anonymous" for d in decls if d.is_package_local)
    assert all(d.is_anonymous for d in decls if d.is_package_local)


def test_union_and_incomplete_unsupported(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.c").write_text("x\n", encoding="utf-8")
    root = _tu(
        _record(name="U", tag="union", line=1, col=1),
        _record(name="Fwd", complete=False, line=2, col=1),
        _enum(name="Empty", constants=False, line=3, col=1),
    )
    decls, _ = _collect(pkg, root)
    hints = {d.name: d.classification_hint for d in decls}
    assert hints["U"] == "unsupported"
    assert hints["Fwd"] == "unsupported"
    assert hints["Empty"] == "unsupported"
    assert any(d.is_union for d in decls if d.name == "U")


def test_system_outside_never_matchable(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.c").write_text("x\n", encoding="utf-8")
    first = {
            "kind": "RecordDecl",
            "name": "FILE",
            "tagUsed": "struct",
            "completeDefinition": True,
            "loc": {
                "file": "/usr/include/stdio.h",
                "line": 50,
                "col": 8,
            },
            "range": {
                "begin": {"file": "/usr/include/stdio.h", "line": 50, "col": 1}
            },
            "inner": [
                {
                    "kind": "TypedefDecl",
                    "name": "Nested",
                    "loc": {"line": 51, "col": 9},
                    "range": {"begin": {"line": 51, "col": 1}},
                    "type": {"qualType": "int"},
                }
            ],
        }
    second = {
        **first,
        "loc": {"file": "/opt/sdk/stdio.h", "line": 50, "col": 8},
        "range": {
            "begin": {"file": "/opt/sdk/stdio.h", "line": 50, "col": 1}
        },
        "inner": [],
    }
    root = _tu(first, second)
    decls, _ = _collect(pkg, root)
    assert all(d.classification_hint == "outside_package" for d in decls)
    assert all(d.matchable_identity() is None for d in decls)
    assert len(decls) == 3  # includes the nested typedef under the first record
    merged = merge_clang_type_declarations(decls)
    file_rows = [d for d in merged if d.name == "FILE"]
    assert len(file_rows) == 2
    assert {d.source_path for d in file_rows} == {
        "/usr/include/stdio.h",
        "/opt/sdk/stdio.h",
    }

def test_macro_spelling_expansion_disagreement(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.c").write_text("x\n", encoding="utf-8")
    (pkg / "b.h").write_text("x\n", encoding="utf-8")
    root = _tu(
        {
            "kind": "RecordDecl",
            "name": "M",
            "tagUsed": "struct",
            "completeDefinition": True,
            "loc": {
                "spellingLoc": {"file": "a.c", "line": 1, "col": 8},
                "expansionLoc": {"file": "b.h", "line": 2, "col": 8},
            },
            "range": {"begin": {"file": "a.c", "line": 1, "col": 1}},
        }
    )
    decls, _ = _collect(pkg, root)
    assert any(d.classification_hint == "macro_location_unsupported" for d in decls)


def test_multi_entry_merge_and_conflict(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.c").write_text("x\n", encoding="utf-8")
    root = _tu(_typedef(name="T", line=1, col=1, qual="int"))
    d0, _ = collect_type_declarations_from_ast(
        root,
        package_dir=pkg,
        cwd=pkg,
        entry_index=0,
        compiler_path="/usr/bin/clang",
        compiler_id="clang-A",
        compile_commands_digest="deadbeef",
    )
    d1, _ = collect_type_declarations_from_ast(
        root,
        package_dir=pkg,
        cwd=pkg,
        entry_index=1,
        compiler_path="/usr/bin/clang",
        compiler_id="clang-B",
        compile_commands_digest="deadbeef",
    )
    merged = merge_clang_type_declarations(d0 + d1)
    m = next(x for x in merged if x.name == "T")
    assert m.entry_indices == [0, 1]
    assert len(m.observation_variants) == 2
    multi_compiler_match = match_type_declarations(
        clang_decls=d0 + d1,
        tree_sitter=[
            TreeSitterTypeEntity(
                title="a:T",
                entity_kind="typedef",
                name="T",
                source_path="a.c",
                line=1,
                col0=0,
                preprocessor_dependent=False,
                preprocessor_reasons=(),
                preprocessor_branches=(),
            )
        ],
        in_scope_paths={"a.c"},
    )["buckets"]["matched"][0]
    assert multi_compiler_match["compiler_path"] is None
    assert multi_compiler_match["compiler_id"] is None
    assert len(multi_compiler_match["compilers"]) == 2
    # Conflicting qualType → ambiguous classification
    root2 = _tu(_typedef(name="T", line=1, col=1, qual="long"))
    d2, _ = collect_type_declarations_from_ast(
        root2,
        package_dir=pkg,
        cwd=pkg,
        entry_index=2,
        compiler_path="/usr/bin/clang",
        compiler_id="clang-C",
        compile_commands_digest="deadbeef",
    )
    merged2 = merge_clang_type_declarations(d0 + d2)
    m2 = next(x for x in merged2 if x.name == "T")
    assert m2.classification_hint == "conflicting_compile_observations"
    conflict_match = match_type_declarations(
        clang_decls=d0 + d2,
        tree_sitter=[
            TreeSitterTypeEntity(
                title="a:T",
                entity_kind="typedef",
                name="T",
                source_path="a.c",
                line=1,
                col0=0,
                preprocessor_dependent=False,
                preprocessor_reasons=(),
                preprocessor_branches=(),
            )
        ],
        in_scope_paths={"a.c"},
    )
    assert conflict_match["counts"]["ambiguous"] == 1
    assert conflict_match["counts"]["tree_sitter_only"] == 0
    # A classification disagreement for the same declaration is also a
    # conflict, not two independently publishable classifications.
    macro_variant = replace(d0[0], classification_hint="macro_location_unsupported")
    merged3 = merge_clang_type_declarations(d0 + [macro_variant])
    assert len(merged3) == 1
    assert merged3[0].classification_hint == "conflicting_compile_observations"


def test_wrong_path_kind_line_col_cannot_match(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.c").write_text("x\n", encoding="utf-8")
    clang = [
        ClangTypeDeclaration(
            entity_kind="typedef",
            name="T",
            source_path="a.c",
            line=1,
            col0=0,
            clang_col1=1,
            location_origin="direct",
            is_package_local=True,
            entry_indices=[0],
            compiler_path="/usr/bin/clang",
            compiler_id="c",
            compile_commands_digest="d",
            observation_variants=[],
        )
    ]
    # Wrong kind
    ts_kind = [
        TreeSitterTypeEntity(
            title="a:T",
            entity_kind="struct",
            name="T",
            source_path="a.c",
            line=1,
            col0=0,
            preprocessor_dependent=False,
            preprocessor_reasons=(),
            preprocessor_branches=(),
        )
    ]
    r = match_type_declarations(
        clang_decls=clang, tree_sitter=ts_kind, in_scope_paths={"a.c"}
    )
    assert r["counts"]["matched"] == 0
    assert r["counts"]["clang_only"] == 1

    # Wrong column → ambiguous (same kind/path/name)
    ts_col = [
        TreeSitterTypeEntity(
            title="a:T",
            entity_kind="typedef",
            name="T",
            source_path="a.c",
            line=1,
            col0=5,
            preprocessor_dependent=False,
            preprocessor_reasons=(),
            preprocessor_branches=(),
        )
    ]
    r2 = match_type_declarations(
        clang_decls=clang, tree_sitter=ts_col, in_scope_paths={"a.c"}
    )
    assert r2["counts"]["matched"] == 0
    assert r2["counts"]["ambiguous"] == 1
    assert "line/column" in r2["buckets"]["ambiguous"][0]["reason"]

    # Exact match
    ts_ok = [
        TreeSitterTypeEntity(
            title="a:T",
            entity_kind="typedef",
            name="T",
            source_path="a.c",
            line=1,
            col0=0,
            preprocessor_dependent=False,
            preprocessor_reasons=(),
            preprocessor_branches=(),
        )
    ]
    r3 = match_type_declarations(
        clang_decls=clang, tree_sitter=ts_ok, in_scope_paths={"a.c"}
    )
    assert r3["counts"]["matched"] == 1
    assert r3["buckets"]["matched"][0]["line_column_confirmed"] is True


def test_duplicate_graph_candidates_ambiguous():
    clang = [
        ClangTypeDeclaration(
            entity_kind="typedef",
            name="T",
            source_path="a.c",
            line=1,
            col0=0,
            clang_col1=1,
            location_origin="direct",
            is_package_local=True,
            entry_indices=[0],
            observation_variants=[],
        )
    ]
    ts = [
        TreeSitterTypeEntity(
            title="a:T",
            entity_kind="typedef",
            name="T",
            source_path="a.c",
            line=1,
            col0=0,
            preprocessor_dependent=False,
            preprocessor_reasons=(),
            preprocessor_branches=(),
        ),
        TreeSitterTypeEntity(
            title="a:T2",
            entity_kind="typedef",
            name="T",
            source_path="a.c",
            line=1,
            col0=0,
            preprocessor_dependent=False,
            preprocessor_reasons=(),
            preprocessor_branches=(),
        ),
    ]
    r = match_type_declarations(
        clang_decls=clang, tree_sitter=ts, in_scope_paths={"a.c"}
    )
    assert r["counts"]["ambiguous"] == 1
    assert r["counts"]["matched"] == 0
    assert "multiple tree-sitter" in r["buckets"]["ambiguous"][0]["reason"]


# ---------------------------------------------------------------------------
# Capture purity / compatibility
# ---------------------------------------------------------------------------


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_builder_zero_compiler_and_db_reloads(tmp_path: Path, monkeypatch):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.c").write_text(
        "struct { int x; } global_box;\n"
        "struct named { int y; };\n"
        "enum color { R, G };\n"
        "typedef int myint;\n",
        encoding="utf-8",
    )
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
    cli_output = tmp_path / "type-audit.json"
    assert type_audit_main(
        [
            "--package",
            str(pkg),
            "--output",
            str(cli_output),
            "--fail-on-mismatch",
        ]
    ) == 0
    assert json.loads(cli_output.read_text(encoding="utf-8"))["mode"] == MODE
    # Capture first with real dump.
    capture = capture_clang_ast_package(pkg)
    # Patch bound symbols that capture would call — builders must not touch them.
    dump_calls = {"n": 0}
    load_calls = {"n": 0}

    def boom_dump(*_a, **_k):
        dump_calls["n"] += 1
        raise AssertionError("builder must not dump AST")

    def boom_load(*_a, **_k):
        load_calls["n"] += 1
        raise AssertionError("builder must not reload compile_commands")

    monkeypatch.setattr(cap_mod, "run_ast_dump_for_entry", boom_dump)
    monkeypatch.setattr(cap_mod, "load_compile_entries", boom_load)

    report = build_type_declaration_audit_from_capture(capture)
    assert dump_calls["n"] == 0
    assert load_calls["n"] == 0
    assert report["mode"] == MODE
    assert report["counts"]["matched"] + report["counts"]["clang_only"] >= 1


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_shared_capture_function_call_type_no_mutation():
    pkg = ROOT / "examples" / "inih"
    capture = capture_clang_ast_package(pkg)
    # Snapshot AST root object ids and the complete canonical AST payload.
    roots = [id(e.ast_root) for e in capture.entries]
    fingerprints = [
        hashlib.sha256(
            json.dumps(
                e.ast_root, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        for e in capture.entries
    ]
    fn = build_function_audit_from_capture(capture)
    call = build_call_audit_from_capture(capture)
    typ = build_type_declaration_audit_from_capture(capture)
    # Re-run type builder for determinism
    typ2 = build_type_declaration_audit_from_capture(capture)
    assert audit_to_json(typ) == audit_to_json(typ2)
    assert [id(e.ast_root) for e in capture.entries] == roots
    assert [
        hashlib.sha256(
            json.dumps(
                e.ast_root, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        for e in capture.entries
    ] == fingerprints
    # Order-independent: build type first then function
    capture2 = capture_clang_ast_package(pkg)
    typ_a = build_type_declaration_audit_from_capture(capture2)
    fn_a = build_function_audit_from_capture(capture2)
    capture3 = capture_clang_ast_package(pkg)
    fn_b = build_function_audit_from_capture(capture3)
    typ_b = build_type_declaration_audit_from_capture(capture3)
    assert audit_to_json(typ_a) == audit_to_json(typ_b)
    assert function_audit_to_json(fn_a) == function_audit_to_json(fn_b)
    assert fn["counts"]["matched"] == fn_a["counts"]["matched"]
    assert call["counts"]["matched_internal"] >= 0
    assert typ["package"] == "inih"


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_standalone_equals_capture_built():
    for name in ("inih", "cjson"):
        pkg = ROOT / "examples" / name
        standalone = run_clang_type_audit(pkg)
        capture = capture_clang_ast_package(pkg)
        from_cap = build_type_declaration_audit_from_capture(capture)
        assert audit_to_json(standalone) == audit_to_json(from_cap)


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_existing_function_call_reports_still_compatible():
    """Regression: function/call reports unchanged vs standalone path."""
    for name in ("inih", "cjson"):
        pkg = ROOT / "examples" / name
        # Standalone vs capture-built equality (post-6fe6bf8 invariant).
        cap = capture_clang_ast_package(pkg)
        assert function_audit_to_json(run_clang_ast_audit(pkg)) == function_audit_to_json(
            build_function_audit_from_capture(cap)
        )
        assert call_audit_to_json(run_clang_call_audit(pkg)) == call_audit_to_json(
            build_call_audit_from_capture(cap)
        )


def test_invalid_timeout_and_missing_db(tmp_path: Path):
    for timeout in (0, -1, True, 1.5, "1"):
        with pytest.raises(ClangTypeAuditError, match="timeout"):
            run_clang_type_audit(
                ROOT / "examples" / "inih", timeout=timeout  # type: ignore[arg-type]
            )
    pkg = tmp_path / "empty"
    pkg.mkdir()
    (pkg / "a.c").write_text("int f(void){return 0;}\n")
    with pytest.raises(ClangTypeAuditError):
        run_clang_type_audit(pkg)
    assert type_audit_main(
        ["--package", str(pkg), "--output", str(tmp_path / "missing.json")]
    ) == 2


def test_malformed_capture_rejected():
    with pytest.raises(ClangTypeAuditError, match="ClangAstPackageCapture"):
        build_type_declaration_audit_from_capture(object())


# ---------------------------------------------------------------------------
# Live packages (measured counts)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_inih_type_audit_counts(tmp_path: Path):
    pkg = ROOT / "examples" / "inih"
    before = {p.relative_to(pkg).as_posix() for p in pkg.rglob("*") if p.is_file()}
    report = run_clang_type_audit(pkg)
    after = {p.relative_to(pkg).as_posix() for p in pkg.rglob("*") if p.is_file()}
    assert before == after
    assert not any(pkg.rglob("*.o"))
    assert not any(pkg.rglob("*.ast"))
    assert not any(pkg.rglob("*.d"))
    assert not any(pkg.rglob("*.i"))
    c = report["counts"]
    assert c["matched"] == 1
    assert c["tree_sitter_only"] == 0
    assert c["out_of_compile_db_scope"] == 0
    assert c["clang_only"] == 2
    assert c["ambiguous"] == 0
    assert c["macro_location_unsupported"] == 0
    assert c["anonymous_declarations"] == 1
    assert c["unsupported_declarations"] == 0
    assert c["outside_package_declarations"] == 109
    assert c["matched"] == len(report["matched"])
    assert c["clang_only"] == len(report["clang_only"])
    assert report["matched"][0]["name"] == "ini_parse_string_ctx"
    assert report["matched"][0]["entity_kind"] == "typedef"
    assert report["matched"][0]["line_column_confirmed"] is True
    # The measured clang_only residual makes strict CLI mode exit 1.
    assert type_audit_main(
        [
            "--package",
            str(pkg),
            "--output",
            str(tmp_path / "inih-types.json"),
            "--fail-on-mismatch",
        ]
    ) == 1


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_cjson_type_audit_counts():
    pkg = ROOT / "examples" / "cjson"
    before = {p.relative_to(pkg).as_posix() for p in pkg.rglob("*") if p.is_file()}
    report = run_clang_type_audit(pkg)
    after = {p.relative_to(pkg).as_posix() for p in pkg.rglob("*") if p.is_file()}
    assert before == after
    assert not any(pkg.rglob("*.o"))
    assert not any(pkg.rglob("*.ast"))
    c = report["counts"]
    assert c["matched"] == 7
    assert c["tree_sitter_only"] == 0
    assert c["out_of_compile_db_scope"] == 0
    assert c["clang_only"] == 3  # named complete structs without TS struct entity
    assert c["ambiguous"] == 0
    assert c["macro_location_unsupported"] == 0
    assert c["anonymous_declarations"] == 3
    assert c["unsupported_declarations"] == 0
    assert c["outside_package_declarations"] == 212
    matched_names = {m["name"] for m in report["matched"]}
    assert "cJSON" in matched_names
    assert "error" in matched_names
    clang_only_kinds = {r["entity_kind"] for r in report["clang_only"]}
    assert clang_only_kinds == {"struct"}


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_default_index_c_unchanged_by_type_audit(tmp_path: Path):
    """Type audit is diagnostic-only: index_c default manifests stay off-only."""
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
        allow_toolchain_drift=False,
    )
    snap = (graph / "current").read_text(encoding="utf-8").strip()
    manifest = json.loads(
        (graph / "snapshots" / snap / "manifest.json").read_text(encoding="utf-8")
    )
    assert "clang_types" not in manifest
    assert "clang_type" not in manifest
    assert manifest["clang_signatures"]["mode"] == "off"
    assert manifest["clang_calls"]["mode"] == "off"
    assert manifest["counts"]["entities"] == len(baseline["entities"])
    assert manifest["counts"]["relationships"] == len(baseline["relationships"])
    import pandas as pd

    ents = pd.read_parquet(graph / "snapshots" / snap / "entities.parquet")
    assert not any("clang_type" in str(c) for c in ents.columns)


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_no_artifacts_after_audit():
    pkg = ROOT / "examples" / "inih"
    run_clang_type_audit(pkg)
    assert not any(pkg.rglob("*.o"))
    assert not any(pkg.rglob("*.ast"))
    assert not any(pkg.rglob("*.d"))
    assert not any(pkg.rglob("*.i"))
