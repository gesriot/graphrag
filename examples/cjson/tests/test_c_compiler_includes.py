"""Optional compiler-backed C direct include hierarchy overlay.

Locks:
  * pure -H parsers (clang + GCC shapes, spaces, footers, depth jumps);
  * direct edges only (no flattened TU→transitive);
  * configuration sensitivity (-DENABLE_ALT);
  * package-local filtering after full stack reconstruction;
  * independent CLI vs depends_on overlay;
  * no package-tree compiler artifacts.

Run: uv run python -m pytest examples/cjson/tests/test_c_compiler_includes.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
from c_compiler_includes import (  # type: ignore
    FACT_KIND,
    CompilerIncludeError,
    append_compiler_includes,
    collect_configured_direct_includes,
    collect_includes_for_entry,
    filter_package_include_edges,
    include_trace_command_from_entry,
    make_includes_relationship,
    parse_include_trace_lines,
    reconstruct_direct_include_edges,
)
from c_compiler_facts import (  # type: ignore
    append_compiler_dependencies,
    collect_translation_unit_dependencies,
)
from c_identities import (  # type: ignore
    build_module_key_map,
    file_title_map,
    list_indexed_c_files,
)
from c_preprocessor import find_c_compiler  # type: ignore
from extract_c import build_c_byog  # type: ignore
from index_c import main as index_c_main  # type: ignore


def _cc():
    return find_c_compiler()


# ---------------------------------------------------------------------------
# Pure parser fixtures (no compiler)
# ---------------------------------------------------------------------------


CLANG_STYLE_TRACE = """\
. ./direct.h
.. ./transitive.h
. /usr/include/stdio.h
.. /usr/include/_stdio.h
... /usr/include/sys/cdefs.h
"""

GCC_STYLE_WITH_FOOTER = """\
. ./direct.h
.. ./transitive.h
. ./sibling.h
Multiple include guards may be useful for:
/usr/include/stdio.h
/usr/include/stdlib.h
"""

SPACE_PATH_TRACE = """\
. with space/hdr file.h
.. with space/nested file.h
"""


def test_parse_one_level_and_nested_clang_shape():
    rows = parse_include_trace_lines(CLANG_STYLE_TRACE)
    assert rows[0] == (1, "./direct.h")
    assert rows[1] == (2, "./transitive.h")
    assert rows[2] == (1, "/usr/include/stdio.h")
    assert rows[3] == (2, "/usr/include/_stdio.h")
    assert rows[4] == (3, "/usr/include/sys/cdefs.h")


def test_parse_gcc_footer_ignored():
    rows = parse_include_trace_lines(GCC_STYLE_WITH_FOOTER)
    assert rows == [
        (1, "./direct.h"),
        (2, "./transitive.h"),
        (1, "./sibling.h"),
    ]


def test_parse_paths_with_spaces():
    rows = parse_include_trace_lines(SPACE_PATH_TRACE)
    assert rows == [
        (1, "with space/hdr file.h"),
        (2, "with space/nested file.h"),
    ]
    assert parse_include_trace_lines(
        ". /tmp/Foo.framework/Headers/Foo.h (framework directory)\n"
    ) == [(1, "/tmp/Foo.framework/Headers/Foo.h")]


def test_parse_duplicate_includes_and_sibling_depth_changes():
    text = ". ./a.h\n.. ./c.h\n. ./b.h\n.. ./c.h\n"
    rows = parse_include_trace_lines(text)
    assert rows == [
        (1, "./a.h"),
        (2, "./c.h"),
        (1, "./b.h"),
        (2, "./c.h"),
    ]


def test_reconstruct_hierarchy_keeps_outside_on_stack(tmp_path: Path):
    """Outside parents stay on the stack so their package children attach correctly."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    main = pkg / "main.c"
    main.write_text("int x;\n", encoding="utf-8")
    # Synthetic stack: TU -> system -> package child would be depth 2 under system.
    # Real graph is usually TU -> package; this locks stack behavior.
    outside = Path("/usr/include/stdio.h")
    child = pkg / "local.h"
    child.write_text("int y;\n", encoding="utf-8")
    grandchild = pkg / "grandchild.h"
    grandchild.write_text("int z;\n", encoding="utf-8")
    # Build edges from a hand-crafted resolved-style trace by calling reconstruct
    # with absolute paths in the path field.
    edges = reconstruct_direct_include_edges(
        main,
        [
            (1, str(outside)),
            (2, str(child)),
            (3, str(grandchild)),
            (1, str(pkg / "other.h")),
        ],
        cwd=pkg,
    )
    assert edges[0][0] == main.resolve()
    assert edges[0][1] == outside.resolve()
    assert edges[1][0] == outside.resolve()
    assert edges[1][1] == child.resolve()
    # Keeping the outside frame means a package-local grandchild is still
    # attached to its real package-local parent.
    assert edges[2][0] == child.resolve()
    assert edges[2][1] == grandchild.resolve()
    # sibling depth reset under TU
    assert edges[3][0] == main.resolve()
    files = list_indexed_c_files(pkg)
    indexed = file_title_map(pkg, build_module_key_map(pkg, files))
    assert filter_package_include_edges(
        edges, package_dir=pkg, indexed=indexed
    ) == [(child.resolve(), grandchild.resolve())]


def test_filter_after_stack_drops_outside_and_self(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    main = (pkg / "main.c").resolve()
    direct = (pkg / "direct.h").resolve()
    main.write_text("x\n", encoding="utf-8")
    direct.write_text("y\n", encoding="utf-8")
    outside = Path("/usr/include/stdio.h")
    files = list_indexed_c_files(pkg)
    indexed = file_title_map(pkg, build_module_key_map(pkg, files))
    raw = [
        (main, direct),
        (main, outside),
        (outside, direct),  # child package under outside parent
        (main, main),
    ]
    kept = filter_package_include_edges(raw, package_dir=pkg, indexed=indexed)
    assert kept == [(main, direct)]


def test_malformed_trace_and_missing_output_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    with pytest.raises(CompilerIncludeError, match="depth jump"):
        reconstruct_direct_include_edges(
            Path("/tmp/main.c"),
            [(1, "/tmp/a.h"), (3, "/tmp/b.h")],
            cwd=Path("/tmp"),
        )
    with pytest.raises(CompilerIncludeError, match="path delimiter"):
        parse_include_trace_lines("..\t/tmp/b.h\n")

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "main.c").write_text(
        "int main(void) { return 0; }\n", encoding="utf-8"
    )
    entry = {
        "directory": str(pkg),
        "command": "cc -c main.c -o main.o",
        "file": "main.c",
    }
    files = list_indexed_c_files(pkg)
    indexed = file_title_map(pkg, build_module_key_map(pkg, files))

    monkeypatch.setattr(
        "c_compiler_includes.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="", stderr=""
        ),
    )
    with pytest.raises(CompilerIncludeError, match="without creating"):
        collect_includes_for_entry(
            entry,
            compiler="/usr/bin/cc",
            package_dir=pkg,
            indexed=indexed,
        )

    module_entry = {
        **entry,
        "command": "cc -fmodules -c main.c -o main.o",
    }
    with pytest.raises(CompilerIncludeError, match="module/cache"):
        include_trace_command_from_entry(
            module_entry,
            compiler="/usr/bin/cc",
            package_dir=pkg,
            preprocessed_out=tmp_path / "module.i",
        )

    response_entry = {**entry, "command": "cc @flags.rsp"}
    with pytest.raises(CompilerIncludeError, match="response-file"):
        include_trace_command_from_entry(
            response_entry,
            compiler="/usr/bin/cc",
            package_dir=pkg,
            preprocessed_out=tmp_path / "response.i",
        )

    plugin_entry = {
        **entry,
        "command": "cc -Xclang -add-plugin -Xclang hidden -c main.c -o main.o",
    }
    with pytest.raises(CompilerIncludeError, match="plugin"):
        include_trace_command_from_entry(
            plugin_entry,
            compiler="/usr/bin/cc",
            package_dir=pkg,
            preprocessed_out=tmp_path / "plugin.i",
        )


def test_includes_relationship_provenance_wording():
    rel = make_includes_relationship(
        source_title="main:main.c",
        target_title="direct:direct.h",
        human_readable_id=1,
        compiler_path="/usr/bin/clang",
        compiler_id="Apple clang",
        compile_commands_digest="abc",
        source_file="/tmp/main.c",
    )
    assert rel["type"] == "includes"
    assert rel["fact_kind"] == FACT_KIND == "configured_direct_include"
    assert rel["confidence"] == 1.0
    assert rel["is_deterministic"] is True
    assert rel["preprocessor_dependent"] is True
    assert "compiler_configuration_direct_include" in rel["preprocessor_reasons"]
    desc = rel["description"]
    assert "compiler/configuration-derived" in desc
    assert "direct include" in desc
    assert "hierarchy" in desc
    assert "compile database" in desc or "compile" in desc


# ---------------------------------------------------------------------------
# Compiler integration
# ---------------------------------------------------------------------------


def _write_include_fixture(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    include_dir = root / "include"
    include_dir.mkdir()
    (root / "main.c").write_text(
        '#include "direct.h"\n'
        '#include "with space.h"\n'
        "#include <stdio.h>\n"
        "#ifdef ENABLE_ALT\n"
        '#include "alt.h"\n'
        "#endif\n"
        "int main(void) { return 0; }\n",
        encoding="utf-8",
    )
    (include_dir / "direct.h").write_text(
        '#include "transitive.h"\n'
        "int direct_fn(void);\n",
        encoding="utf-8",
    )
    (include_dir / "transitive.h").write_text(
        "int trans_fn(void);\n",
        encoding="utf-8",
    )
    (include_dir / "alt.h").write_text(
        "int alt_fn(void);\n",
        encoding="utf-8",
    )
    (include_dir / "with space.h").write_text(
        "int spaced_fn(void);\n",
        encoding="utf-8",
    )
    return root


def _write_cc(
    root: Path,
    *,
    compiler: str,
    extra_flags: str = "",
) -> None:
    cmd = f"{compiler} -c -Iinclude {extra_flags} main.c -o main.o".replace("  ", " ")
    (root / "compile_commands.json").write_text(
        json.dumps(
            [
                {
                    "directory": str(root),
                    "command": cmd,
                    "file": "main.c",
                }
            ]
        ),
        encoding="utf-8",
    )


@pytest.mark.skipif(_cc() is None, reason="no C compiler (clang/cc/gcc) on PATH")
def test_live_direct_includes_not_flattened(tmp_path: Path):
    pkg = _write_include_fixture(tmp_path / "pkg")
    compiler = _cc()
    assert compiler is not None
    _write_cc(pkg, compiler=compiler)

    before = {p.relative_to(pkg).as_posix() for p in pkg.rglob("*") if p.is_file()}
    collected = collect_configured_direct_includes(pkg, compiler=compiler)
    after = {p.relative_to(pkg).as_posix() for p in pkg.rglob("*") if p.is_file()}
    assert after == before
    assert not any(p.suffix in {".o", ".d", ".i"} for p in pkg.rglob("*"))

    pairs = {
        (e["source_title"], e["target_title"]) for e in collected["edges"]
    }
    assert ("main:main.c", "direct:direct.h") in pairs
    assert ("main:main.c", "with space:with space.h") in pairs
    assert ("direct:direct.h", "transitive:transitive.h") in pairs
    assert ("main:main.c", "transitive:transitive.h") not in pairs
    assert not any("stdio" in t or "stdio" in s for s, t in pairs)
    assert not any(t.endswith("alt.h") or s.endswith("alt.h") for s, t in pairs)
    assert collected["fact_kind"] == FACT_KIND
    assert collected["n_facts"] >= 2


@pytest.mark.skipif(_cc() is None, reason="no C compiler (clang/cc/gcc) on PATH")
def test_conditional_include_respects_defines(tmp_path: Path):
    pkg = _write_include_fixture(tmp_path / "pkg")
    compiler = _cc()
    assert compiler is not None

    _write_cc(pkg, compiler=compiler)
    off = collect_configured_direct_includes(pkg, compiler=compiler)
    off_pairs = {
        (e["source_title"], e["target_title"]) for e in off["edges"]
    }
    assert ("main:main.c", "alt:alt.h") not in off_pairs

    _write_cc(pkg, compiler=compiler, extra_flags="-DENABLE_ALT")
    on = collect_configured_direct_includes(pkg, compiler=compiler)
    on_pairs = {(e["source_title"], e["target_title"]) for e in on["edges"]}
    assert ("main:main.c", "alt:alt.h") in on_pairs
    assert ("main:main.c", "direct:direct.h") in on_pairs
    assert ("direct:direct.h", "transitive:transitive.h") in on_pairs
    assert ("main:main.c", "transitive:transitive.h") not in on_pairs


@pytest.mark.skipif(_cc() is None, reason="no C compiler (clang/cc/gcc) on PATH")
def test_append_idempotent_and_endpoints_exist(tmp_path: Path):
    pkg = _write_include_fixture(tmp_path / "pkg")
    compiler = _cc()
    assert compiler is not None
    _write_cc(pkg, compiler=compiler)

    data = build_c_byog(pkg)
    titles = {e["title"] for e in data["entities"] if e["type"] == "file"}
    s1 = append_compiler_includes(data, pkg, compiler=compiler)
    inc = [r for r in data["relationships"] if r["type"] == "includes"]
    assert s1["n_facts"] == len(inc) >= 2
    for r in inc:
        assert r["source"] in titles
        assert r["target"] in titles
        assert r["fact_kind"] == FACT_KIND
    s2 = append_compiler_includes(data, pkg, compiler=compiler)
    assert s2["n_facts_added"] == 0
    assert len([r for r in data["relationships"] if r["type"] == "includes"]) == len(
        inc
    )


@pytest.mark.skipif(_cc() is None, reason="no C compiler (clang/cc/gcc) on PATH")
def test_includes_and_depends_on_are_distinct_layers(tmp_path: Path):
    pkg = _write_include_fixture(tmp_path / "pkg")
    compiler = _cc()
    assert compiler is not None
    _write_cc(pkg, compiler=compiler)

    deps = collect_translation_unit_dependencies(pkg, compiler=compiler)
    incs = collect_configured_direct_includes(pkg, compiler=compiler)
    dep_pairs = {
        (e["source_title"], e["target_title"]) for e in deps["edges"]
    }
    inc_pairs = {
        (e["source_title"], e["target_title"]) for e in incs["edges"]
    }
    # Flattened TU deps include transitive; direct layer does not.
    assert ("main:main.c", "transitive:transitive.h") in dep_pairs
    assert ("main:main.c", "transitive:transitive.h") not in inc_pairs
    assert ("direct:direct.h", "transitive:transitive.h") in inc_pairs
    assert ("direct:direct.h", "transitive:transitive.h") not in dep_pairs


# ---------------------------------------------------------------------------
# CLI publication
# ---------------------------------------------------------------------------


def test_cli_default_off_includes_manifest(tmp_path: Path):
    pkg = _write_include_fixture(tmp_path / "pkg")
    graph = tmp_path / "g-off"
    baseline = build_c_byog(pkg)
    index_c_main(
        package=pkg,
        graph=graph,
        keep_snapshots=2,
        compiler_builtins=False,
        compiler_dependencies=False,
        compiler_includes=False,
        allow_toolchain_drift=False,
    )
    snapshot = (graph / "current").read_text(encoding="utf-8").strip()
    manifest = json.loads(
        (graph / "snapshots" / snapshot / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["compiler_includes"] == {
        "mode": "off",
        "enabled": False,
        "n_facts": 0,
        "n_translation_units": 0,
    }
    assert manifest["compiler_dependencies"]["enabled"] is False
    assert manifest["counts"]["relationships"] == len(baseline["relationships"])


@pytest.mark.skipif(_cc() is None, reason="no C compiler (clang/cc/gcc) on PATH")
def test_cli_includes_only(tmp_path: Path):
    pkg = _write_include_fixture(tmp_path / "pkg")
    compiler = _cc()
    assert compiler is not None
    _write_cc(pkg, compiler=compiler)
    graph = tmp_path / "g-inc"
    index_c_main(
        package=pkg,
        graph=graph,
        keep_snapshots=2,
        compiler_builtins=False,
        compiler_dependencies=False,
        compiler_includes=True,
        allow_toolchain_drift=False,
    )
    snapshot = (graph / "current").read_text(encoding="utf-8").strip()
    snap = graph / "snapshots" / snapshot
    manifest = json.loads((snap / "manifest.json").read_text(encoding="utf-8"))
    prov = manifest["compiler_includes"]
    assert prov["enabled"] is True
    assert prov["mode"] == "compiler_eh"
    assert prov["fact_kind"] == FACT_KIND
    assert prov["n_facts"] >= 2
    assert prov["n_facts_added"] == prov["n_facts"]
    assert prov["compile_commands_digest"]
    assert manifest["compiler_dependencies"]["enabled"] is False

    import pandas as pd

    rels = pd.read_parquet(snap / "relationships.parquet")
    includes = rels[rels["type"].astype(str) == "includes"]
    depends = rels[rels["type"].astype(str) == "depends_on"]
    assert len(includes) == prov["n_facts"]
    assert len(depends) == 0
    assert not any(pkg.rglob("*.o"))
    assert not any(pkg.rglob("*.i"))


@pytest.mark.skipif(_cc() is None, reason="no C compiler (clang/cc/gcc) on PATH")
def test_cli_dependencies_only_unchanged(tmp_path: Path):
    pkg = _write_include_fixture(tmp_path / "pkg")
    compiler = _cc()
    assert compiler is not None
    _write_cc(pkg, compiler=compiler)
    graph = tmp_path / "g-dep"
    index_c_main(
        package=pkg,
        graph=graph,
        keep_snapshots=2,
        compiler_builtins=False,
        compiler_dependencies=True,
        compiler_includes=False,
        allow_toolchain_drift=False,
    )
    snapshot = (graph / "current").read_text(encoding="utf-8").strip()
    snap = graph / "snapshots" / snapshot
    manifest = json.loads((snap / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["compiler_dependencies"]["enabled"] is True
    assert manifest["compiler_includes"]["enabled"] is False
    import pandas as pd

    rels = pd.read_parquet(snap / "relationships.parquet")
    assert len(rels[rels["type"].astype(str) == "depends_on"]) >= 2
    assert len(rels[rels["type"].astype(str) == "includes"]) == 0


@pytest.mark.skipif(_cc() is None, reason="no C compiler (clang/cc/gcc) on PATH")
def test_cli_both_overlays(tmp_path: Path):
    pkg = _write_include_fixture(tmp_path / "pkg")
    compiler = _cc()
    assert compiler is not None
    _write_cc(pkg, compiler=compiler)
    graph = tmp_path / "g-both"
    index_c_main(
        package=pkg,
        graph=graph,
        keep_snapshots=2,
        compiler_builtins=False,
        compiler_dependencies=True,
        compiler_includes=True,
        allow_toolchain_drift=False,
    )
    snapshot = (graph / "current").read_text(encoding="utf-8").strip()
    snap = graph / "snapshots" / snapshot
    manifest = json.loads((snap / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["compiler_dependencies"]["enabled"] is True
    assert manifest["compiler_includes"]["enabled"] is True
    import pandas as pd

    rels = pd.read_parquet(snap / "relationships.parquet")
    depends = rels[rels["type"].astype(str) == "depends_on"]
    includes = rels[rels["type"].astype(str) == "includes"]
    assert len(depends) >= 2
    assert len(includes) >= 2
    # Non-overlapping fact_kind values
    assert set(depends["fact_kind"].dropna().astype(str)) == {
        "translation_unit_dependency"
    }
    assert set(includes["fact_kind"].dropna().astype(str)) == {
        "configured_direct_include"
    }
    dep_pairs = set(zip(depends["source"], depends["target"]))
    inc_pairs = set(zip(includes["source"], includes["target"]))
    assert ("main:main.c", "transitive:transitive.h") in dep_pairs
    assert ("main:main.c", "transitive:transitive.h") not in inc_pairs


@pytest.mark.skipif(_cc() is None, reason="no C compiler (clang/cc/gcc) on PATH")
def test_missing_compile_db_fails_when_enabled(tmp_path: Path):
    pkg = _write_include_fixture(tmp_path / "pkg")
    with pytest.raises(CompilerIncludeError, match="compile_commands"):
        collect_configured_direct_includes(pkg)
