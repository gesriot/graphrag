"""Branch liveness under compile_commands for the C preprocessor diagnostic.

Locks: live/dead/unknown decisions are derived from -D + header defaults, never
guessed for platform macros; parquet re-stamp overwrites safely; contract fails
if the define table is corrupted.

Run: uv run python -m pytest examples/cjson/tests/test_c_branch_liveness.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
from byog_graph import load_byog, publish_byog_snapshot  # type: ignore
from c_preprocessor import (  # type: ignore
    analyze_package,
    annotate_byog,
    branches_for_span,
    evaluate_region_liveness,
    ConditionalRegion,
)
from extract_c import build_c_byog  # type: ignore


def test_get_decimal_point_enable_locales_dead_else_live():
    """cJSON default build: ENABLE_LOCALES not -D'd → ifdef dead, else live."""
    pa_pkg = analyze_package(ROOT / "examples" / "cjson")
    assert "ENABLE_LOCALES" not in (pa_pkg.compile_defines or {})
    br = branches_for_span(pa_pkg, str(ROOT / "examples/cjson/cJSON.c"), "279:0-287:1")
    by_kind = {b["kind"]: b for b in br}
    assert by_kind["ifdef"]["condition"] == "ENABLE_LOCALES"
    assert by_kind["ifdef"]["liveness"] == "dead"
    assert by_kind["else"]["liveness"] == "live"


def test_platform_macro_stays_unknown():
    """Absent platform macros must not be folded into dead."""
    env: dict = {}  # no -D, no header default for _MSC_VER
    reg = ConditionalRegion(
        start_line=1, end_line=2, kind="ifdef", condition="_MSC_VER", depth=1
    )
    live, basis = evaluate_region_liveness(reg, env)
    assert live == "unknown", (live, basis)
    # else of unknown parent is also unknown
    els = ConditionalRegion(
        start_line=3,
        end_line=4,
        kind="else",
        condition="",
        depth=1,
        chain_kind="ifdef",
        chain_condition="_MSC_VER",
    )
    live2, _ = evaluate_region_liveness(els, env)
    assert live2 == "unknown"


def test_conditional_defines_are_not_harvested_as_build_defaults():
    """A `#define` guarded by a conditional is not an unconditional default.

    `cJSON.h` does `#define __WINDOWS__` inside
    `#if !defined(__WINDOWS__) && (defined(WIN32) || …)`. Treating that as a
    default inverts the condition guarding it and reports the Windows-only
    calling-convention block as live on a POSIX build.
    """
    pa_pkg = analyze_package(ROOT / "examples" / "cjson")
    assert "__WINDOWS__" not in pa_pkg.header_defaults
    assert "CJSON_EXPORT_SYMBOLS" not in pa_pkg.header_defaults
    br = branches_for_span(pa_pkg, str(ROOT / "examples/cjson/cJSON.h"), "35:0-36:0")
    windows = [b for b in br if b["condition"] == "__WINDOWS__" and b["kind"] == "ifdef"]
    assert windows and all(b["liveness"] == "unknown" for b in windows), br


def test_ifndef_default_region_is_live_not_falsified_by_its_own_define():
    """`#ifndef X` / `#define X v` is where the default comes from — it is live.

    Reading the default without its location makes the guard read as already
    satisfied, so every config-default region in `ini.h` reports dead.
    """
    pa_pkg = analyze_package(ROOT / "examples" / "inih")
    br = branches_for_span(pa_pkg, str(ROOT / "examples/inih/ini.h"), "107:0-109:0")
    guard = [b for b in br if b["kind"] == "ifndef" and "INI_ALLOW_MULTILINE" in b["condition"]]
    assert guard and all(b["liveness"] == "live" for b in guard), br
    # and the value is still available to the consumer of the default
    assert pa_pkg.header_defaults["INI_ALLOW_MULTILINE"][0] == "1"


def test_branch_inside_undecidable_parent_is_not_reported_live():
    """Nesting propagates: a decidable `#if` inside an unknown block is unknown."""
    pa_pkg = analyze_package(ROOT / "examples" / "cjson")
    br = branches_for_span(pa_pkg, str(ROOT / "examples/cjson/cJSON.h"), "59:0-61:0")
    inner = [b for b in br if b["start_line"] == 59]
    assert inner and all(b["liveness"] != "live" for b in inner), br


def test_inih_multiline_live_under_header_default():
    """ini.h defaults INI_ALLOW_MULTILINE=1 without -D."""
    pa_pkg = analyze_package(ROOT / "examples" / "inih")
    value, def_file, _ = pa_pkg.header_defaults["INI_ALLOW_MULTILINE"]
    assert value == "1"
    assert Path(def_file).name == "ini.h"
    br = branches_for_span(pa_pkg, str(ROOT / "examples/inih/ini.c"), "192:0-200:0")
    assert any(
        b["liveness"] == "live" and "INI_ALLOW_MULTILINE" in b["condition"] for b in br
    ), br


def test_corrupted_define_table_breaks_contract(tmp_path: Path):
    """If header defaults are wiped, get_decimal_point still decidable (undefined),
    but INI_ALLOW_MULTILINE loses its live default — contract detects the wipe."""
    pa_pkg = analyze_package(ROOT / "examples" / "inih")
    assert pa_pkg.header_defaults["INI_ALLOW_MULTILINE"][0] == "1"
    # corrupt
    pa_pkg.header_defaults.clear()
    pa_pkg.compile_defines.clear()
    br = branches_for_span(pa_pkg, str(ROOT / "examples/inih/ini.c"), "192:0-200:0")
    # Without the default, INI_ALLOW_MULTILINE is undefined → #if X is dead (0)
    multi = [b for b in br if "INI_ALLOW_MULTILINE" in b.get("condition", "")]
    assert multi, br
    assert all(b["liveness"] == "dead" for b in multi), multi


def test_annotate_overwrites_not_merges_and_parquet_roundtrip(tmp_path: Path):
    """Both input shapes: fresh dicts and parquet-loaded records."""
    data = build_c_byog(ROOT / "examples" / "cjson")
    # fresh path
    s1 = annotate_byog(data, ROOT / "examples" / "cjson")
    ent = next(e for e in data["entities"] if e["title"] == "cJSON:get_decimal_point")
    assert ent["preprocessor_dependent"] is True
    branches = ent["preprocessor_branches"]
    assert any(b["liveness"] == "dead" and "ENABLE_LOCALES" in b["condition"] for b in branches)
    assert any(b["liveness"] == "live" and b["kind"] == "else" for b in branches)
    n_calls = sum(1 for r in data["relationships"] if r.get("type") == "calls")
    n_ent = len(data["entities"])

    # write parquet and reload (numpy arrays for list columns)
    ents_df = pd.DataFrame(data["entities"])
    rels_df = pd.DataFrame(data["relationships"])
    obs_df = pd.DataFrame(data.get("call_observations") or [])
    epath = tmp_path / "entities.parquet"
    rpath = tmp_path / "relationships.parquet"
    opath = tmp_path / "call_observations.parquet"
    pq.write_table(pa.Table.from_pandas(ents_df), epath)
    pq.write_table(pa.Table.from_pandas(rels_df), rpath)
    if len(obs_df):
        pq.write_table(pa.Table.from_pandas(obs_df), opath)

    loaded = {
        "entities": pd.read_parquet(epath).to_dict("records"),
        "relationships": pd.read_parquet(rpath).to_dict("records"),
        "call_observations": (
            pd.read_parquet(opath).to_dict("records") if opath.exists() else []
        ),
    }
    # reasons come back as ndarray — re-stamp must not raise
    s2 = annotate_byog(loaded, ROOT / "examples" / "cjson")
    assert sum(1 for r in loaded["relationships"] if r.get("type") == "calls") == n_calls
    assert len(loaded["entities"]) == n_ent
    ent2 = next(e for e in loaded["entities"] if e["title"] == "cJSON:get_decimal_point")
    # overwrite: reasons is a plain list again
    assert isinstance(ent2["preprocessor_reasons"], list)
    assert isinstance(ent2["preprocessor_branches"], list)
    assert any(
        b.get("liveness") == "dead" and "ENABLE_LOCALES" in str(b.get("condition"))
        for b in ent2["preprocessor_branches"]
    )
    # second stamp idempotent on structure
    annotate_byog(loaded, ROOT / "examples" / "cjson")
    assert len(loaded["entities"]) == n_ent


def test_is_deterministic_untouched_on_cjson():
    data = build_c_byog(ROOT / "examples" / "cjson")
    before = {
        (r.get("source"), r.get("target"), r.get("span")): bool(r.get("is_deterministic"))
        for r in data["relationships"]
        if r.get("type") == "calls"
    }
    annotate_byog(data, ROOT / "examples" / "cjson")
    after = {
        (r.get("source"), r.get("target"), r.get("span")): bool(r.get("is_deterministic"))
        for r in data["relationships"]
        if r.get("type") == "calls"
    }
    assert before == after
