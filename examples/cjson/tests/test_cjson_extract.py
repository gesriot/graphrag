"""Regression for the cJSON C frontend bootstrap (Phase 6, third C target).

cJSON is the first struct/pointer/ownership-heavy C target (~3.2k LOC). This
locks the bootstrap facts that matter for the planned ownership slice
(parse -> inspect -> print -> delete):
- the struct graph is captured (the `cJSON` node struct and the parse/print
  buffer structs are entities);
- the ownership-slice API functions are extracted;
- the parse entry chain and the recursive ownership functions (cJSON_Delete and
  friends) are deterministic CALLS edges;
- allocation primitives (malloc/free/realloc/memcpy) stay weak observations,
  never core edges -- so heap ownership is visible but not silently promoted.

Run: uv run python -m pytest examples/cjson/tests/test_cjson_extract.py -q
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
from extract_c import build_c_byog  # type: ignore


def _graph():
    return build_c_byog(ROOT / "examples" / "cjson")


def test_struct_graph_and_slice_functions():
    data = _graph()
    titles = {e["title"] for e in data["entities"]}
    # struct graph: the node struct and the internal parse/print buffers.
    for struct in ("cJSON:cJSON", "cJSON:parse_buffer", "cJSON:printbuffer"):
        assert struct in titles, f"missing struct entity {struct}"
    # ownership-slice API surface.
    for fn in (
        "cJSON:cJSON_Parse",
        "cJSON:cJSON_ParseWithOpts",
        "cJSON:cJSON_Print",
        "cJSON:cJSON_PrintUnformatted",
        "cJSON:cJSON_Delete",
        "cJSON:cJSON_GetObjectItem",
        "cJSON:cJSON_GetArrayItem",
        "cJSON:cJSON_GetArraySize",
    ):
        assert fn in titles, f"missing function entity {fn}"

    # The API audit is header-derived, not a hand-maintained list of the slice.
    # A copied header with one synthetic exported declaration must enumerate
    # that declaration and then fail closed until it is classified.
    sys.path.insert(0, str(ROOT / "examples" / "cjson" / "tools"))
    import api_surface_audit  # type: ignore

    assert api_surface_audit.main(["--check"]) == 0
    assert set(api_surface_audit.COMPILER_REJECTIONS) == set(
        api_surface_audit.OWNERSHIP_BLOCKED
    )
    rustc = shutil.which("rustc")
    assert rustc is not None, "cJSON Rust-port audit requires rustc"
    with tempfile.TemporaryDirectory() as td:
        output = Path(td) / "rejection.rlib"
        for operation, proof in api_surface_audit.COMPILER_REJECTIONS.items():
            snippet = ROOT / proof["snippet"]
            text = snippet.read_text()
            assert f"cJSON API: {operation}" in text
            assert f"expected-error: {proof['diagnostic']}" in text
            assert "fn c_oracle_mutation_trace()" in text
            result = subprocess.run(
                [
                    rustc,
                    "--edition=2021",
                    "--crate-type=lib",
                    "--error-format=json",
                    str(snippet),
                    "-o",
                    str(output),
                ],
                capture_output=True,
                text=True,
            )
            assert result.returncode != 0, (
                f"{operation} candidate unexpectedly compiled; it must either be ported "
                "or reclassified"
            )
            rendered = "".join(
                json.loads(line).get("rendered") or ""
                for line in result.stderr.splitlines()
                if line.startswith("{")
            )
            assert proof["diagnostic"] in rendered, (
                f"{operation} failed for a different reason:\n{rendered}"
            )
            # The rejection must land on the C-trace mutation itself. Without
            # this the proof passes on a snippet whose trace was deleted and
            # whose error comes from an unrelated helper — verified by planting
            # exactly that.
            code = proof["diagnostic"].split(":", 1)[0].removeprefix("error")
            lines = text.splitlines()
            trace_start = next(
                i for i, ln in enumerate(lines, 1) if "fn c_oracle_mutation_trace()" in ln
            )
            primary_lines = [
                span["line_start"]
                for line in result.stderr.splitlines()
                if line.startswith("{")
                for diag in [json.loads(line)]
                if (diag.get("code") or {}).get("code") == code.strip("[]")
                for span in diag.get("spans", [])
                if span.get("is_primary")
            ]
            assert primary_lines, f"{operation}: no primary span for {code}"
            assert any(ln > trace_start for ln in primary_lines), (
                f"{operation}: {code} is reported at line(s) {primary_lines}, outside "
                f"c_oracle_mutation_trace (starts at {trace_start}) — the proof must "
                "reject the traced mutation, not incidental code"
            )
    header = ROOT / "examples" / "cjson" / "cJSON.h"
    with tempfile.TemporaryDirectory() as td:
        altered = Path(td) / "cJSON.h"
        text = header.read_text()
        insert_at = text.rfind("#endif")
        altered.write_text(
            text[:insert_at]
            + "\nCJSON_PUBLIC(void) cJSON_AuditProbe(void);\n"
            + text[insert_at:]
        )
        functions, _ = api_surface_audit.parse_header(altered)
        assert "cJSON_AuditProbe" in functions
        try:
            api_surface_audit.render(altered)
        except ValueError as error:
            assert "unclassified" in str(error)
        else:
            raise AssertionError("header addition must require an audit classification")


def test_parse_chain_and_recursive_ownership_edges():
    data = _graph()
    calls = {
        (r["source"], r["target"])
        for r in data["relationships"]
        if r["type"] == "calls"
    }
    assert ("cJSON:cJSON_Parse", "cJSON:cJSON_ParseWithOpts") in calls
    # Recursive free/compare/duplicate are captured as self-edges.
    assert ("cJSON:cJSON_Delete", "cJSON:cJSON_Delete") in calls
    assert ("cJSON:cJSON_Compare", "cJSON:cJSON_Compare") in calls


def test_allocation_primitives_stay_observations():
    data = _graph()
    # Library purity: every call originating in the cJSON library resolves to a
    # library function -- malloc/free/etc. must never become core edges. (The
    # co-located golden runner is also package code; scope this to the library.)
    lib_targets = {
        r["target"]
        for r in data["relationships"]
        if r["type"] == "calls" and r["source"].startswith("cJSON:")
    }
    assert all(t.startswith("cJSON:") for t in lib_targets), (
        "non-library call leaked into cJSON core edges"
    )
    obs_targets = {o["display_target"] for o in data["call_observations"]}
    for prim in ("malloc", "free", "realloc"):
        assert prim in obs_targets, f"{prim} should be a weak observation"
