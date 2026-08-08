"""Collision-safe C module identities (shared extract_c + compiler overlay).

Locks:
  * stem-only keys when a stem appears under one parent (sibling .c/.h);
  * package-relative path keys when the same stem appears under multiple parents;
  * unique entity / relationship / text-unit identities under collisions;
  * non-dangling relationship endpoints;
  * compiler depends_on edges stay per-TU (no basename collapse);
  * deterministic, idempotent collection without package-tree build artifacts.

Run: uv run python -m pytest examples/cjson/tests/test_c_identities.py -q
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
from c_compiler_facts import (  # type: ignore
    append_compiler_dependencies,
    collect_translation_unit_dependencies,
    indexed_package_files,
)
from c_clang_ast_audit import collect_tree_sitter_functions  # type: ignore
from c_clang_call_audit import collect_tree_sitter_calls  # type: ignore
from c_identities import (  # type: ignore
    build_module_key_map,
    file_entity_title,
    list_indexed_c_files,
    symbol_entity_title,
)
import extract_c as extract_c_module  # type: ignore
from c_preprocessor import find_c_compiler  # type: ignore
from extract_c import build_c_byog  # type: ignore


def _cc():
    return find_c_compiler()


def _write_collision_package(root: Path) -> Path:
    """Two util.c TUs with the same function name and distinct local headers."""
    left = root / "src" / "left"
    right = root / "src" / "right"
    left.mkdir(parents=True)
    right.mkdir(parents=True)
    (left / "util.h").write_text(
        "#ifndef LEFT_UTIL_H\n"
        "#define LEFT_UTIL_H\n"
        "#if defined(LEFT_API) && defined(LEFT_FEATURE)\n"
        "int left_only(void);\n"
        "#else\n"
        "int left_only(void);\n"
        "#endif\n"
        "#endif\n",
        encoding="utf-8",
    )
    (right / "util.h").write_text(
        "#ifndef RIGHT_UTIL_H\n#define RIGHT_UTIL_H\nint right_only(void);\n#endif\n",
        encoding="utf-8",
    )
    (left / "util.c").write_text(
        '#include "util.h"\n'
        "static int helper(void) { return left_only(); }\n"
        "int work(void) { return helper(); }\n"
        "int left_only(void) { return 1; }\n",
        encoding="utf-8",
    )
    (right / "util.c").write_text(
        '#include "util.h"\n'
        "static int helper(void) { return right_only(); }\n"
        "int work(void) { return helper(); }\n"
        "int right_only(void) { return 2; }\n",
        encoding="utf-8",
    )
    # Distinct, non-colliding stems can still produce the same historical call
    # id when caller/callee names and coordinates match. The extractor must
    # preserve legacy IDs when unique and disambiguate only this collision.
    for stem, value in (("alpha", 3), ("beta", 4)):
        (root / f"{stem}.c").write_text(
            f"static int leaf(void) {{ return {value}; }}\n"
            "int same(void) { return leaf(); }\n",
            encoding="utf-8",
        )
    return root


def _write_sibling_package(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "foo.h").write_text(
        "#ifndef FOO_H\n#define FOO_H\nint foo_api(void);\n#endif\n",
        encoding="utf-8",
    )
    (root / "foo.c").write_text(
        '#include "foo.h"\nint foo_api(void) { return 0; }\n',
        encoding="utf-8",
    )
    return root


def test_sibling_c_and_h_keep_legacy_stem_module_key(tmp_path: Path):
    pkg = _write_sibling_package(tmp_path / "sib")
    keys = build_module_key_map(pkg)
    assert keys[(pkg / "foo.c").resolve()] == "foo"
    assert keys[(pkg / "foo.h").resolve()] == "foo"
    data = build_c_byog(pkg)
    titles = {e["title"] for e in data["entities"]}
    assert "foo:foo.c" in titles
    assert "foo:foo.h" in titles
    assert "foo:foo_api" in titles
    # No path-disambiguated prefixes for a single-parent stem.
    assert not any(t.startswith("src/") for t in titles)


def test_collision_module_keys_are_package_relative_paths(tmp_path: Path):
    pkg = _write_collision_package(tmp_path / "pkg")
    keys = build_module_key_map(pkg)
    assert keys[(pkg / "src/left/util.c").resolve()] == "src/left/util"
    assert keys[(pkg / "src/left/util.h").resolve()] == "src/left/util"
    assert keys[(pkg / "src/right/util.c").resolve()] == "src/right/util"
    assert keys[(pkg / "src/right/util.h").resolve()] == "src/right/util"


def test_collision_extract_unique_identities_and_endpoints(tmp_path: Path):
    pkg = _write_collision_package(tmp_path / "pkg")
    data = build_c_byog(pkg)

    titles = [e["title"] for e in data["entities"]]
    ids = [e["id"] for e in data["entities"]]
    assert len(titles) == len(set(titles)), titles
    assert len(ids) == len(set(ids)), ids

    # File + function entities for both sides.
    for key in (
        "src/left/util:util.c",
        "src/left/util:util.h",
        "src/right/util:util.c",
        "src/right/util:util.h",
        "src/left/util:work",
        "src/left/util:helper",
        "src/right/util:work",
        "src/right/util:helper",
        "alpha:same",
        "alpha:leaf",
        "beta:same",
        "beta:leaf",
    ):
        assert key in titles, key

    # No stem-only collapse.
    assert "util:util.c" not in titles
    assert "util:work" not in titles

    rel_ids = [r["id"] for r in data["relationships"]]
    assert len(rel_ids) == len(set(rel_ids)), rel_ids

    # contains / calls endpoints resolve to entity titles or file entity ids.
    title_set = set(titles)
    id_set = set(ids)
    for r in data["relationships"]:
        src, tgt = str(r["source"]), str(r["target"])
        if r["type"] == "contains":
            assert src in id_set, (r["type"], src)
            assert tgt in title_set, (r["type"], tgt)
        else:
            assert src in title_set, (r["type"], src)
            assert tgt in title_set, (r["type"], tgt)

    calls = {
        (r["source"], r["target"])
        for r in data["relationships"]
        if r["type"] == "calls"
    }
    assert ("src/left/util:work", "src/left/util:helper") in calls
    assert ("src/right/util:work", "src/right/util:helper") in calls
    # Same-file preference: work does not jump to the other util's helper.
    assert ("src/left/util:work", "src/right/util:helper") not in calls
    assert ("src/right/util:work", "src/left/util:helper") not in calls
    assert ("alpha:same", "alpha:leaf") in calls
    assert ("beta:same", "beta:leaf") in calls

    tu_ids = [t["id"] for t in data["text_units"]]
    assert len(tu_ids) == len(set(tu_ids))
    obs_sources = {o["source"] for o in data["call_observations"]}
    # Observations (if any) must reference real function titles.
    for src in obs_sources:
        assert src in title_set


def test_shared_mapping_matches_extract_and_compiler_index(tmp_path: Path):
    pkg = _write_collision_package(tmp_path / "pkg")
    keys = build_module_key_map(pkg)
    indexed = indexed_package_files(pkg)
    data = build_c_byog(pkg)
    file_ents = {
        e["title"]: e for e in data["entities"] if e["type"] == "file"
    }
    for path, title in indexed.items():
        assert title == file_entity_title(path, keys[path])
        assert title in file_ents


@pytest.mark.skipif(_cc() is None, reason="no C compiler (clang/cc/gcc) on PATH")
def test_collision_compiler_depends_on_per_tu(tmp_path: Path):
    pkg = _write_collision_package(tmp_path / "pkg")
    compiler = _cc()
    assert compiler is not None
    entries = [
        {
            "directory": str(pkg / "src" / "left"),
            "command": f"{compiler} -c -I. util.c -o util.o",
            "file": "util.c",
        },
        {
            "directory": str(pkg / "src" / "right"),
            "command": f"{compiler} -c -I. util.c -o util.o",
            "file": "util.c",
        },
    ]
    (pkg / "compile_commands.json").write_text(
        json.dumps(entries), encoding="utf-8"
    )

    before = {p.relative_to(pkg).as_posix() for p in pkg.rglob("*") if p.is_file()}
    collected = collect_translation_unit_dependencies(pkg, compiler=compiler)
    after = {p.relative_to(pkg).as_posix() for p in pkg.rglob("*") if p.is_file()}
    assert after == before
    assert not any(
        p.suffix in {".o", ".d"} or p.name.endswith(".d") for p in pkg.rglob("*")
    )

    pairs = {
        (e["source_title"], e["target_title"]) for e in collected["edges"]
    }
    assert ("src/left/util:util.c", "src/left/util:util.h") in pairs
    assert ("src/right/util:util.c", "src/right/util:util.h") in pairs
    # Must not cross-wire TUs via basename collapse.
    assert ("src/left/util:util.c", "src/right/util:util.h") not in pairs
    assert ("src/right/util:util.c", "src/left/util:util.h") not in pairs
    assert collected["n_translation_units"] == 2
    assert collected["n_facts"] == 2

    # Deterministic + idempotent append onto BYOG.
    data = build_c_byog(pkg)
    s1 = append_compiler_dependencies(data, pkg, compiler=compiler)
    n1 = len([r for r in data["relationships"] if r["type"] == "depends_on"])
    s2 = append_compiler_dependencies(data, pkg, compiler=compiler)
    n2 = len([r for r in data["relationships"] if r["type"] == "depends_on"])
    assert s1["n_facts"] == 2
    assert s2["n_facts_added"] == 0
    assert n1 == n2 == 2

    collected2 = collect_translation_unit_dependencies(pkg, compiler=compiler)
    assert collected2["edges"] == collected["edges"]
    assert collected2["translation_unit_titles"] == collected["translation_unit_titles"]


def test_identity_and_graph_output_deterministic(tmp_path: Path):
    pkg = _write_collision_package(tmp_path / "pkg")
    a = list_indexed_c_files(pkg)
    b = list_indexed_c_files(pkg)
    assert a == b
    # Matches historical Path sort (directory components before same-prefix files).
    assert a == sorted(a)

    # Preprocessor reason order must not depend on Python hash randomization.
    script = (
        "import json,sys; from pathlib import Path; "
        "from extract_c import build_c_byog; "
        "print(json.dumps(build_c_byog(Path(sys.argv[1])), "
        "sort_keys=True, separators=(',', ':')))"
    )
    outputs = []
    for seed in ("1", "2", "3"):
        env = dict(os.environ)
        env["PYTHONHASHSEED"] = seed
        env["PYTHONPATH"] = str(ROOT / "scripts")
        outputs.append(
            subprocess.check_output(
                [sys.executable, "-c", script, str(pkg)],
                env=env,
                text=True,
            )
        )
    assert len(set(outputs)) == 1

    # A package-local spelling that resolves outside the package cannot receive
    # an honest package-relative identity.
    outside = tmp_path / "outside.c"
    outside.write_text("int outside(void) { return 0; }\n", encoding="utf-8")
    escape = pkg / "escape.c"
    try:
        escape.symlink_to(outside)
    except OSError:
        return  # symlink creation may be unavailable on some test hosts
    with pytest.raises(ValueError, match="resolves outside package"):
        list_indexed_c_files(pkg)


# ---------------------------------------------------------------------------
# Cross-kind symbol identity (struct/enum/typedef/function collisions)
# ---------------------------------------------------------------------------


def test_typedef_struct_item_distinct_qualified_titles(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "item.c").write_text(
        "typedef struct Item { int x; } Item;\n",
        encoding="utf-8",
    )
    data = build_c_byog(pkg)
    by_type = {}
    for e in data["entities"]:
        if e["type"] in ("struct", "typedef"):
            by_type[e["type"]] = e
    assert set(by_type) == {"struct", "typedef"}
    assert by_type["struct"]["title"] == "item:struct:Item"
    assert by_type["typedef"]["title"] == "item:typedef:Item"
    assert by_type["struct"]["symbol_name"] == "Item"
    assert by_type["typedef"]["symbol_name"] == "Item"
    assert by_type["struct"]["id"] == "ent:struct:item:struct:Item"
    assert by_type["typedef"]["id"] == "ent:typedef:item:typedef:Item"
    assert by_type["struct"]["id"] != by_type["typedef"]["id"]
    # Unique IDs/titles/text units
    assert len({e["id"] for e in data["entities"]}) == len(data["entities"])
    assert len({e["title"] for e in data["entities"]}) == len(data["entities"])
    assert len({t["id"] for t in data["text_units"]}) == len(data["text_units"])
    contains = [r for r in data["relationships"] if r["type"] == "contains"]
    contains_ids = {r["id"] for r in contains}
    assert "rel:contains:item:struct:Item" in contains_ids
    assert "rel:contains:item:typedef:Item" in contains_ids
    assert len(contains_ids) == len(contains)
    # Exact types and non-empty spans
    for e in by_type.values():
        assert e["span"] and ":" in e["span"]
        assert e["extractor"] == "tree-sitter-c"


def test_typedef_enum_mode_distinct(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "mode.c").write_text(
        "typedef enum Mode { A, B } Mode;\n",
        encoding="utf-8",
    )
    data = build_c_byog(pkg)
    kinds = {
        e["type"]: e["title"]
        for e in data["entities"]
        if e["type"] in ("enum", "typedef")
    }
    assert kinds["enum"] == "mode:enum:Mode"
    assert kinds["typedef"] == "mode:typedef:Mode"


def test_struct_function_worker_call_resolves_to_function(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "worker.c").write_text(
        "struct Worker { int x; };\n"
        "int Worker(void) { return 0; }\n"
        "int run(void) { return Worker(); }\n",
        encoding="utf-8",
    )
    data = build_c_byog(pkg)
    titles = {e["title"] for e in data["entities"]}
    assert "worker:struct:Worker" in titles
    assert "worker:function:Worker" in titles
    assert "worker:run" in titles  # non-colliding function keeps legacy title
    calls = [
        (r["source"], r["target"])
        for r in data["relationships"]
        if r["type"] == "calls"
    ]
    assert ("worker:run", "worker:function:Worker") in calls
    # Call id still uses bare names
    call_ids = [r["id"] for r in data["relationships"] if r["type"] == "calls"]
    assert any(i.startswith("rel:call:run:Worker:") for i in call_ids)

    audited_functions = collect_tree_sitter_functions(pkg)
    worker = next(f for f in audited_functions if f.name == "Worker")
    assert worker.title == "worker:function:Worker"
    audited_calls, _, _, _ = collect_tree_sitter_calls(pkg)
    assert any(
        edge.caller_title == "worker:run"
        and edge.target_title == "worker:function:Worker"
        for edge in audited_calls
    )


def test_same_name_different_module_keys_separated(tmp_path: Path):
    pkg = tmp_path / "pkg"
    left = pkg / "src" / "left"
    right = pkg / "src" / "right"
    left.mkdir(parents=True)
    right.mkdir(parents=True)
    (left / "box.c").write_text(
        "typedef struct Box { int a; } Box;\n", encoding="utf-8"
    )
    (right / "box.c").write_text(
        "typedef struct Box { int b; } Box;\n", encoding="utf-8"
    )
    data = build_c_byog(pkg)
    titles = {e["title"] for e in data["entities"]}
    assert "src/left/box:struct:Box" in titles
    assert "src/left/box:typedef:Box" in titles
    assert "src/right/box:struct:Box" in titles
    assert "src/right/box:typedef:Box" in titles


def test_same_kind_redeclaration_no_duplicate(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.h").write_text("struct S { int x; };\n", encoding="utf-8")
    (pkg / "a.c").write_text(
        '#include "a.h"\nstruct S { int x; };\nint f(void){return 0;}\n',
        encoding="utf-8",
    )
    data = build_c_byog(pkg)
    structs = [
        e
        for e in data["entities"]
        if e["type"] == "struct" and e.get("symbol_name") == "S"
    ]
    # Same module key "a" for a.c and a.h → one struct S entity (same-kind dedup)
    assert len(structs) == 1


def test_non_colliding_fixture_preserves_legacy_titles(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "plain.c").write_text(
        "struct Alpha { int x; };\n"
        "typedef int Beta;\n"
        "enum Gamma { G0 };\n"
        "int delta(void) { return 0; }\n"
        "int eps(void) { return delta(); }\n",
        encoding="utf-8",
    )
    data = build_c_byog(pkg)
    titles = {e["title"] for e in data["entities"]}
    # No cross-kind collisions → all legacy titles.
    assert "plain:Alpha" in titles
    assert "plain:Beta" in titles
    assert "plain:Gamma" in titles
    assert "plain:delta" in titles
    assert "plain:eps" in titles
    assert not any(
        ":struct:" in t
        or ":typedef:" in t
        or ":function:" in t
        or ":enum:" in t
        for t in titles
    )
    symbols = {
        e["title"]: e["symbol_name"]
        for e in data["entities"]
        if e["type"] in {"function", "struct", "enum", "typedef"}
    }
    assert symbols == {
        "plain:Alpha": "Alpha",
        "plain:Beta": "Beta",
        "plain:Gamma": "Gamma",
        "plain:delta": "delta",
        "plain:eps": "eps",
    }
    calls = [r for r in data["relationships"] if r["type"] == "calls"]
    assert len(calls) == 1
    assert calls[0]["source"] == "plain:eps"
    assert calls[0]["target"] == "plain:delta"
    assert calls[0]["id"].startswith("rel:call:eps:delta:")


def test_kind_identity_determinism_hashseed_and_file_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "z.c").write_text(
        "typedef struct Item { int x; } Item;\nint use(void){return 0;}\n",
        encoding="utf-8",
    )
    (pkg / "a.c").write_text("int other(void){return 0;}\n", encoding="utf-8")
    nested = pkg / "tests" / "parse"
    nested.mkdir(parents=True)
    (nested / "runner.c").write_text(
        "int runner(void){return 0;}\n", encoding="utf-8"
    )
    (pkg / "tests.c").write_text("int tests(void){return 0;}\n", encoding="utf-8")
    d1 = build_c_byog(pkg)
    d2 = build_c_byog(pkg)
    assert [
        e["title"] for e in d1["entities"] if e["type"] == "file"
    ] == ["a:a.c", "runner:runner.c", "tests:tests.c", "z:z.c"]
    assert [e["title"] for e in d1["entities"]] == [e["title"] for e in d2["entities"]]
    assert [e["id"] for e in d1["entities"]] == [e["id"] for e in d2["entities"]]
    assert [r["id"] for r in d1["relationships"]] == [r["id"] for r in d2["relationships"]]

    discovered = list_indexed_c_files(pkg)
    monkeypatch.setattr(
        extract_c_module,
        "list_indexed_c_files",
        lambda _package_dir: list(reversed(discovered)),
    )
    reverse_discovery = build_c_byog(pkg)
    assert reverse_discovery == d1

    script = (
        "import json,sys\n"
        f"sys.path.insert(0, {str(ROOT / 'scripts')!r})\n"
        "from pathlib import Path\n"
        "from extract_c import build_c_byog\n"
        f"d=build_c_byog(Path({str(pkg)!r}))\n"
        "print(json.dumps({"
        "'titles':[e['title'] for e in d['entities']],"
        "'eids':[e['id'] for e in d['entities']],"
        "'rids':[r['id'] for r in d['relationships']]"
        "}, sort_keys=True))\n"
    )
    env = dict(os.environ)
    outs = []
    for seed in ("0", "1", "42"):
        env["PYTHONHASHSEED"] = seed
        proc = subprocess.run(
            [sys.executable, "-c", script],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        outs.append(proc.stdout.strip())
    assert outs[0] == outs[1] == outs[2]


def test_all_ids_unique_and_endpoints_resolve(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "x.c").write_text(
        "typedef struct Item { int x; } Item;\n"
        "struct Worker { int y; };\n"
        "int Worker(void){return 1;}\n"
        "int call(void){return Worker();}\n",
        encoding="utf-8",
    )
    data = build_c_byog(pkg)
    assert len({e["id"] for e in data["entities"]}) == len(data["entities"])
    assert len({e["title"] for e in data["entities"]}) == len(data["entities"])
    assert len({t["id"] for t in data["text_units"]}) == len(data["text_units"])
    assert len({r["id"] for r in data["relationships"]}) == len(data["relationships"])
    titles = {e["title"] for e in data["entities"]}
    ids = {e["id"] for e in data["entities"]}
    for r in data["relationships"]:
        if r["type"] == "contains":
            assert r["source"] in ids
            assert r["target"] in titles
        else:
            assert r["source"] in titles
            assert r["target"] in titles


@pytest.mark.skipif(_cc() is None, reason="no C compiler")
def test_live_inih_cjson_call_counts_stable():
    """Function call counts/IDs unchanged; type entities grow only for collisions."""
    for name, n_calls in (("inih", 38), ("cjson", 495), ("jsmn", 43)):
        d = build_c_byog(ROOT / "examples" / name)
        calls = [r for r in d["relationships"] if r["type"] == "calls"]
        assert len(calls) == n_calls
        assert len({r["id"] for r in calls}) == n_calls
        # No function title is kind-qualified on these packages.
        for e in d["entities"]:
            if e["type"] == "function":
                assert ":function:" not in e["title"]
