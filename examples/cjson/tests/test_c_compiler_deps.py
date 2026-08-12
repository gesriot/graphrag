"""Optional compiler-backed C translation-unit dependency overlay.

Locks:
  * flattened depends_on for direct + transitive local headers
  * exclusion of system headers, outside-package paths, and self-edges
  * exact provenance / non-direct wording
  * default build_c_byog / index path unchanged when the option is off
  * duplicate-dependency dedup and compile_commands directory/file resolution

Pure parsing/configuration tests always run. The integration test that invokes
the compiler skips with an explicit reason when none is available.

Run: uv run python -m pytest examples/cjson/tests/test_c_compiler_deps.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
from c_compiler_facts import (  # type: ignore
    FACT_KIND,
    CompilerDependencyError,
    append_compiler_dependencies,
    collect_translation_unit_dependencies,
    compiler_from_entry,
    compile_commands_digest,
    dependency_command_from_entry,
    file_entity_title,
    filter_package_dependencies,
    indexed_package_files,
    make_depends_on_relationship,
    parse_makefile_dependencies,
)
from c_preprocessor import (  # type: ignore
    find_c_compiler,
    resolve_compile_entry_cwd,
    resolve_compile_entry_source,
    split_compile_entry_args,
    strip_compile_output_flags,
)
from extract_c import build_c_byog  # type: ignore
from index_c import main as index_c_main  # type: ignore


def _cc():
    return find_c_compiler()


def _write_fixture_package(root: Path) -> Path:
    """One .c TU, direct local header, transitive local header, system include."""
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
    # Outside-package path is referenced only via a fake dep token in unit tests;
    # the real package does not need it on disk for compiler -M.
    return root


def _write_compile_commands(
    root: Path,
    *,
    directory: str | None = None,
    file_field: str | None = None,
    command: str | None = None,
    arguments: list[str] | None = None,
    compiler: str | None = None,
) -> None:
    cc = compiler or "cc"
    entry: dict = {
        "directory": directory if directory is not None else str(root),
        "file": file_field if file_field is not None else "main.c",
    }
    if arguments is not None:
        entry["arguments"] = arguments
    else:
        # Deliberately treat local headers as compiler-system headers. The
        # overlay uses -M then filters by package path; -MM would lose them.
        entry["command"] = command or f"{cc} -c -isystem . main.c -o main.o"
    (root / "compile_commands.json").write_text(
        json.dumps([entry]), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Pure parsing / configuration (always run)
# ---------------------------------------------------------------------------


def test_parse_makefile_dependencies_joins_continuations():
    text = "tu: main.c \\\n  direct.h \\\n  trans.h\n"
    deps = parse_makefile_dependencies(text)
    assert deps == ["main.c", "direct.h", "trans.h"]


def test_parse_makefile_dependencies_unescapes_make_paths():
    text = r"tu: main.c direct\ header.h hash\#name.h cash$$name.h" + "\n"
    assert parse_makefile_dependencies(text) == [
        "main.c",
        "direct header.h",
        "hash#name.h",
        "cash$name.h",
    ]


def test_filter_package_dependencies_drops_system_outside_self(tmp_path: Path):
    pkg = _write_fixture_package(tmp_path / "pkg")
    outside = tmp_path / "outside.h"
    outside.write_text("int x;\n", encoding="utf-8")
    indexed = indexed_package_files(pkg)
    raw = [
        str(pkg / "main.c"),
        str(pkg / "direct.h"),
        str(pkg / "trans.h"),
        "/usr/include/stdio.h",
        str(outside),
        str(pkg / "missing.h"),
    ]
    kept = filter_package_dependencies(
        raw,
        cwd=pkg,
        package_dir=pkg,
        tu_path=pkg / "main.c",
        indexed=indexed,
    )
    names = {p.name for p in kept}
    assert names == {"direct.h", "trans.h"}
    assert (pkg / "main.c").resolve() not in {p.resolve() for p in kept}


def test_file_entity_title_matches_extract_c():
    assert file_entity_title(Path("main.c")) == "main:main.c"
    assert file_entity_title(Path("direct.h")) == "direct:direct.h"


def test_depends_on_relationship_provenance_and_wording():
    rel = make_depends_on_relationship(
        source_title="main:main.c",
        target_title="direct:direct.h",
        human_readable_id=1,
        compiler_path="/usr/bin/clang",
        compiler_id="Apple clang version 17.0.0",
        compile_commands_digest="abc123",
        source_file="/tmp/main.c",
    )
    assert rel["type"] == "depends_on"
    assert rel["type"] != "includes"
    assert rel["source"] == "main:main.c"
    assert rel["target"] == "direct:direct.h"
    assert rel["fact_kind"] == FACT_KIND == "translation_unit_dependency"
    assert rel["confidence"] == 1.0
    assert rel["is_deterministic"] is True
    assert rel["compile_commands_digest"] == "abc123"
    assert rel["compiler_id"] == "Apple clang version 17.0.0"
    assert rel["compiler_path"] == "/usr/bin/clang"
    assert rel["preprocessor_dependent"] is True
    assert rel["preprocessor_reasons"] == ["compiler_configuration_dependency"]
    desc = rel["description"]
    assert "compiler/configuration-derived" in desc
    assert "transitive" in desc or "may be transitive" in desc
    assert "not a direct textual" in desc
    assert "#include" in desc


def test_strip_compile_output_flags_removes_object_writers():
    cleaned = strip_compile_output_flags(
        [
            "-c",
            "-I.",
            "main.c",
            "-o",
            "main.o",
            "-fsyntax-only",
            "-MMD",
            "-MF",
            "x.d",
            "-Wp,-MMD,hidden.d",
            "--dependency-file=hidden2.d",
            "--output=hidden.o",
        ]
    )
    assert "-c" not in cleaned
    assert "-o" not in cleaned
    assert "main.o" not in cleaned
    assert "-fsyntax-only" not in cleaned
    assert "-MMD" not in cleaned
    assert "-MF" not in cleaned
    assert "x.d" not in cleaned
    assert not any("hidden" in arg for arg in cleaned)
    assert "-I." in cleaned
    assert "main.c" in cleaned


def test_compile_commands_directory_and_file_resolution(tmp_path: Path):
    """Directory/file resolution: absolute paths, missing-dir fallback, arguments form."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "main.c").write_text("int x;\n", encoding="utf-8")

    # Absolute directory + relative file
    entry_abs = {
        "directory": str(pkg),
        "command": "cc -c main.c -o main.o",
        "file": "main.c",
    }
    cwd = resolve_compile_entry_cwd(entry_abs, pkg)
    assert cwd == pkg.resolve()
    src = resolve_compile_entry_source(entry_abs, cwd=cwd, package_dir=pkg)
    assert src == (pkg / "main.c").resolve()

    # Missing relative directory falls back to package_dir
    entry_missing_dir = {
        "directory": "examples/not-this-on-disk",
        "command": "cc -c main.c",
        "file": "main.c",
    }
    cwd2 = resolve_compile_entry_cwd(entry_missing_dir, pkg)
    assert cwd2 == pkg.resolve()
    src2 = resolve_compile_entry_source(
        entry_missing_dir, cwd=cwd2, package_dir=pkg
    )
    assert src2 == (pkg / "main.c").resolve()

    # Structured arguments win even if a producer redundantly includes command.
    fake_compiler = pkg / "clang"
    fake_compiler.write_text("", encoding="utf-8")
    entry_args = {
        "directory": str(pkg),
        "arguments": [str(fake_compiler), "-c", "-I.", "main.c", "-o", "main.o"],
        "command": "cc -c wrong.c -o wrong.o",
        "file": str(pkg / "main.c"),
    }
    args = split_compile_entry_args(entry_args)
    assert args == ["-c", "-I.", "main.c", "-o", "main.o"]
    assert compiler_from_entry(entry_args, cwd=pkg) == str(fake_compiler.resolve())
    # shlex path for the same flags
    entry_cmd = {
        "directory": str(pkg),
        "command": "cc -c -I. main.c -o main.o",
        "file": "main.c",
    }
    assert split_compile_entry_args(entry_cmd) == args


def test_dependency_command_uses_temp_mf_and_no_package_output(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "main.c").write_text("int x;\n", encoding="utf-8")
    entry = {
        "directory": str(pkg),
        "command": "cc -c -I. main.c -o main.o",
        "file": "main.c",
    }
    depfile = tmp_path / "out" / "deps.d"
    depfile.parent.mkdir()
    cwd, argv, src = dependency_command_from_entry(
        entry, compiler="/usr/bin/cc", package_dir=pkg, depfile=depfile
    )
    assert cwd == pkg.resolve()
    assert src == (pkg / "main.c").resolve()
    assert argv[0] == "/usr/bin/cc"
    assert "-M" in argv
    assert "-MM" not in argv
    assert "-MF" in argv
    mf_idx = argv.index("-MF")
    assert argv[mf_idx + 1] == str(depfile)
    assert "-c" not in argv
    assert "-o" not in argv
    assert "main.o" not in argv

    response_entry = {
        "directory": str(pkg),
        "command": "cc @flags.rsp",
        "file": "main.c",
    }
    with pytest.raises(CompilerDependencyError, match="response-file"):
        dependency_command_from_entry(
            response_entry,
            compiler="/usr/bin/cc",
            package_dir=pkg,
            depfile=depfile,
        )

    module_entry = {
        **entry,
        "command": "cc -fmodules-cache-path=.cache -c main.c -o main.o",
    }
    with pytest.raises(CompilerDependencyError, match="module/cache"):
        dependency_command_from_entry(
            module_entry,
            compiler="/usr/bin/cc",
            package_dir=pkg,
            depfile=depfile,
        )


def test_default_build_c_byog_has_no_depends_on():
    """Default extractor path must not emit compiler overlay edges."""
    data = build_c_byog(ROOT / "examples" / "cjson")
    types = {r["type"] for r in data["relationships"]}
    assert "depends_on" not in types
    assert "includes" not in types
    assert types <= {"calls", "contains"}


def test_missing_compile_commands_fails_explicitly(tmp_path: Path):
    (tmp_path / "main.c").write_text("int x;\n", encoding="utf-8")
    with pytest.raises(CompilerDependencyError, match="compile_commands.json"):
        collect_translation_unit_dependencies(tmp_path)

    (tmp_path / "compile_commands.json").write_text("[42]", encoding="utf-8")
    with pytest.raises(CompilerDependencyError, match="entry 0 is not an object"):
        collect_translation_unit_dependencies(tmp_path)

    (tmp_path / "compile_commands.json").write_bytes(b"\xff")
    with pytest.raises(CompilerDependencyError, match="cannot read"):
        collect_translation_unit_dependencies(tmp_path)


def test_compile_commands_digest_stable(tmp_path: Path):
    _write_compile_commands(tmp_path)
    d1 = compile_commands_digest(tmp_path)
    d2 = compile_commands_digest(tmp_path)
    assert d1 == d2
    assert len(d1) == 64  # full sha256 hex


# ---------------------------------------------------------------------------
# Compiler integration (skip only when no compiler)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(_cc() is None, reason="no C compiler (clang/cc/gcc) on PATH")
def test_flattened_depends_on_direct_and_transitive_local_headers(tmp_path: Path):
    pkg = _write_fixture_package(tmp_path / "pkg")
    compiler = _cc()
    assert compiler is not None
    _write_compile_commands(pkg, compiler=compiler)

    # Guard: no object files before/after under the package.
    before = {p.name for p in pkg.iterdir()}
    collected = collect_translation_unit_dependencies(pkg, compiler=compiler)
    after = {p.name for p in pkg.iterdir()}
    assert after == before
    assert not any(p.suffix == ".o" for p in pkg.iterdir())

    pairs = {
        (e["source_title"], e["target_title"]) for e in collected["edges"]
    }
    assert ("main:main.c", "direct:direct.h") in pairs
    assert ("main:main.c", "trans:trans.h") in pairs
    # Self-edge and system headers must not appear.
    assert ("main:main.c", "main:main.c") not in pairs
    for _s, t in pairs:
        assert "stdio" not in t
        assert not t.endswith("stdio.h")

    assert collected["fact_kind"] == FACT_KIND
    assert collected["compile_commands_digest"] == compile_commands_digest(pkg)
    assert collected["compiler_path"] == compiler
    assert collected["n_translation_units"] == 1
    assert collected["n_facts"] == 2
    assert collected["mode"] == "compiler_m"
    assert "confidence_boundary" in collected


@pytest.mark.skipif(_cc() is None, reason="no C compiler (clang/cc/gcc) on PATH")
def test_append_compiler_dependencies_onto_byog(tmp_path: Path):
    pkg = _write_fixture_package(tmp_path / "pkg")
    compiler = _cc()
    assert compiler is not None
    _write_compile_commands(pkg, compiler=compiler)

    data = build_c_byog(pkg)
    base_n = len(data["relationships"])
    base_types = {r["type"] for r in data["relationships"]}
    assert "depends_on" not in base_types

    summary = append_compiler_dependencies(data, pkg, compiler=compiler)
    assert summary["enabled"] is True
    assert summary["n_facts"] == 2
    assert summary["n_translation_units"] == 1
    assert summary["compile_commands_digest"]
    assert summary["compiler_id"] or summary["compiler_path"]

    dep_rels = [r for r in data["relationships"] if r["type"] == "depends_on"]
    assert len(dep_rels) == 2
    assert len(data["relationships"]) == base_n + 2
    pairs = {(r["source"], r["target"]) for r in dep_rels}
    assert pairs == {
        ("main:main.c", "direct:direct.h"),
        ("main:main.c", "trans:trans.h"),
    }
    for r in dep_rels:
        assert r["fact_kind"] == "translation_unit_dependency"
        assert r["confidence"] == 1.0
        assert r["is_deterministic"] is True
        assert r["preprocessor_dependent"] is True
        assert r["compile_commands_digest"] == summary["compile_commands_digest"]
        assert "compiler/configuration-derived" in r["description"]
        assert "transitive" in r["description"]
        assert "not a direct textual" in r["description"]
        assert r["type"] != "includes"

    # Idempotent: second append adds nothing.
    summary2 = append_compiler_dependencies(data, pkg, compiler=compiler)
    assert summary2["n_facts"] == 2
    assert summary2["n_facts_added"] == 0
    assert len([r for r in data["relationships"] if r["type"] == "depends_on"]) == 2


@pytest.mark.skipif(_cc() is None, reason="no C compiler (clang/cc/gcc) on PATH")
def test_duplicate_compile_entries_dedup(tmp_path: Path):
    pkg = _write_fixture_package(tmp_path / "pkg")
    compiler = _cc()
    assert compiler is not None
    entry = {
        "directory": str(pkg),
        "command": f"{compiler} -c -I. main.c -o main.o",
        "file": "main.c",
    }
    (pkg / "compile_commands.json").write_text(
        json.dumps([entry, entry]), encoding="utf-8"
    )
    collected = collect_translation_unit_dependencies(pkg, compiler=compiler)
    assert collected["n_compile_entries"] == 2
    assert collected["n_facts"] == 2  # deduped, not 4


@pytest.mark.skipif(_cc() is None, reason="no C compiler (clang/cc/gcc) on PATH")
def test_repo_relative_directory_resolution(tmp_path: Path):
    """Missing relative compile_commands directory falls back to package_dir."""
    pkg = _write_fixture_package(tmp_path / "pkg")
    compiler = _cc()
    assert compiler is not None
    # Simulate repo-style relative directory that does not exist from CWD.
    entry = {
        "directory": "examples/does-not-exist-here",
        "command": f"{compiler} -c -I. main.c -o main.o",
        "file": "main.c",
    }
    (pkg / "compile_commands.json").write_text(
        json.dumps([entry]), encoding="utf-8"
    )
    collected = collect_translation_unit_dependencies(pkg, compiler=compiler)
    pairs = {
        (e["source_title"], e["target_title"]) for e in collected["edges"]
    }
    assert ("main:main.c", "direct:direct.h") in pairs
    assert ("main:main.c", "trans:trans.h") in pairs


def test_broken_compiler_fails_explicitly(tmp_path: Path):
    pkg = _write_fixture_package(tmp_path / "pkg")
    _write_compile_commands(pkg, compiler="/nonexistent/compiler-binary")
    with pytest.raises(CompilerDependencyError):
        collect_translation_unit_dependencies(
            pkg, compiler="/nonexistent/compiler-binary"
        )
def test_default_index_option_off_records_no_facts(tmp_path: Path):
    """The default CLI publishes the explicit off block and no overlay rows."""
    pkg = _write_fixture_package(tmp_path / "pkg")
    graph = tmp_path / "graph-off"
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
        clang_type_shapes=False,
        allow_toolchain_drift=False,
    )
    snapshot = (graph / "current").read_text(encoding="utf-8").strip()
    manifest = json.loads(
        (graph / "snapshots" / snapshot / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["compiler_dependencies"] == {
        "mode": "off",
        "enabled": False,
        "n_facts": 0,
        "n_translation_units": 0,
    }
    assert manifest["compiler_includes"] == {
        "mode": "off",
        "enabled": False,
        "n_facts": 0,
        "n_translation_units": 0,
    }
    assert manifest["clang_signatures"] == {
        "mode": "off",
        "enabled": False,
        "n_facts": 0,
        "n_translation_units": 0,
    }
    assert manifest["counts"]["relationships"] == len(baseline["relationships"])


@pytest.mark.skipif(_cc() is None, reason="no C compiler (clang/cc/gcc) on PATH")
def test_enabled_index_manifest_matches_published_overlay(tmp_path: Path):
    """The CLI manifest reports the facts actually written to relationships."""
    pkg = _write_fixture_package(tmp_path / "pkg")
    compiler = _cc()
    assert compiler is not None
    _write_compile_commands(pkg, compiler=compiler)
    graph = tmp_path / "graph-on"
    index_c_main(
        package=pkg,
        graph=graph,
        keep_snapshots=2,
        compiler_builtins=False,
        compiler_dependencies=True,
        compiler_includes=False,
        clang_signatures=False,
        clang_calls=False,
        clang_types=False,
        clang_type_uses=False,
        clang_type_shapes=False,
        allow_toolchain_drift=False,
    )
    snapshot = (graph / "current").read_text(encoding="utf-8").strip()
    snap_dir = graph / "snapshots" / snapshot
    manifest = json.loads(
        (snap_dir / "manifest.json").read_text(encoding="utf-8")
    )
    provenance = manifest["compiler_dependencies"]
    assert provenance["mode"] == "compiler_m"
    assert provenance["enabled"] is True
    assert provenance["n_facts"] == 2
    assert provenance["n_facts_added"] == 2
    assert provenance["n_translation_units"] == 1

    import pandas as pd

    rels = pd.read_parquet(snap_dir / "relationships.parquet")
    depends_on = rels[rels["type"].astype(str) == "depends_on"]
    assert len(depends_on) == provenance["n_facts"]
    assert not any(pkg.glob("*.o"))
