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
from c_identities import (  # type: ignore
    build_module_key_map,
    file_entity_title,
    list_indexed_c_files,
    symbol_entity_title,
)
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
