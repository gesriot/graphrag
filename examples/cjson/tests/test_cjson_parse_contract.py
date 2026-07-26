"""Golden contract for vendored cJSON (first C->Rust port target).

Ground truth is cJSON (`cJSON.c`/`cJSON.h`), captured via a dedicated C runner.
The bounded ownership slice is parse -> inspect -> print -> delete. Each case
pins, for a JSON input:
- `unformatted`: `cJSON_PrintUnformatted` output (or `__PARSE_ERROR__`),
- `inspect`: a canonical tree descriptor built from cJSON's public API/fields
  (numbers carry valueint + the IEEE-754 bits of valuedouble, so parse fidelity
  is checked exactly without depending on float *printing*),
- `formatted`: `cJSON_Print` output, for a few cases.

Corpora under `tests/parse/golden_*.json`:
- `golden_parse.json` — ownership-bearing shapes (objects/arrays/strings/integers/…).
- `golden_float_print.json` — non-integer float-printing fidelity (`%1.15g` /
  `%1.17g`, exponents, precision boundaries, -0.0, overflow→inf→null).
- `golden_mutation.json` — fixed builder/mutation scenarios. Each trace records
  the tree after every ownership transfer, including the caller-owned detached
  return before it is deleted.

This test recompiles the C runner and re-derives every golden file to keep the
contract in sync, and (when the compiler supports it) recompiles under
AddressSanitizer to verify the parse+print+delete and mutation ownership paths
are free of leaks/double-frees. Skipped if no C compiler is available.

Run: uv run python -m pytest examples/cjson/tests/test_cjson_parse_contract.py -q
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from api_oracle import compile_oracle, run_oracle  # type: ignore

HERE = Path(__file__).parent
CJSON = HERE.parent
PARSE_DIR = HERE / "parse"
GOLDEN_PARSE = PARSE_DIR / "golden_parse.json"
GOLDEN_FLOAT = PARSE_DIR / "golden_float_print.json"
GOLDEN_MUTATION = PARSE_DIR / "golden_mutation.json"
RUST_DIR = CJSON.parent / "cjson_rust"


def _trace_lines(output: bytes) -> dict[str, str]:
    lines = output.decode().splitlines()
    result: dict[str, str] = {}
    for line in lines:
        key, separator, value = line.partition("=")
        assert separator, f"malformed C API oracle trace line: {line!r}"
        result[key] = value
    return result


def _cc():
    return shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")


def _compile(cc: str, out: Path, extra: list[str]) -> bool:
    res = subprocess.run(
        [cc, "-I", str(CJSON), *extra, "-o", str(out),
         str(PARSE_DIR / "runner.c"), str(CJSON / "cJSON.c")],
        capture_output=True,
    )
    return res.returncode == 0


def _run(binary: Path, mode: str, input_text: str) -> str:
    return subprocess.run(
        [str(binary), mode],
        input=input_text.encode(),
        capture_output=True,
        check=True,
    ).stdout.decode().rstrip("\n")


def _all_golden_files() -> list[Path]:
    return sorted(PARSE_DIR.glob("golden_*.json"))


def _all_parse_cases() -> list[dict]:
    cases: list[dict] = []
    for path in _all_golden_files():
        cases.extend(
            case
            for case in json.loads(path.read_text())["cases"]
            if "json" in case
        )
    return cases


def _mutation_cases() -> list[dict]:
    return json.loads(GOLDEN_MUTATION.read_text())["cases"]


def test_golden_present_and_sized():
    parse_cases = json.loads(GOLDEN_PARSE.read_text())["cases"]
    assert len(parse_cases) >= 22
    float_cases = json.loads(GOLDEN_FLOAT.read_text())["cases"]
    assert len(float_cases) >= 20
    mutation_cases = _mutation_cases()
    assert len(mutation_cases) >= 7
    # Combined floor used by the Rust contract test as well.
    assert len(_all_parse_cases()) + len(mutation_cases) >= 59


@pytest.mark.skipif(_cc() is None, reason="no C compiler available")
def test_cjson_golden_matches_reference():
    cc = _cc()
    parse_cases = _all_parse_cases()
    mutation_cases = _mutation_cases()
    with tempfile.TemporaryDirectory() as td:
        binary = Path(td) / "runner"
        assert _compile(cc, binary, []), "plain runner must compile"
        for c in parse_cases:
            assert _run(binary, "unformatted", c["json"]) == c["unformatted"], (
                f"unformatted for {c['desc']!r}"
            )
            insp = _run(binary, "inspect", c["json"])
            got = None if insp == "__PARSE_ERROR__" else json.loads(insp)
            assert got == c["inspect"], f"inspect for {c['desc']!r}"
            if "formatted" in c:
                assert _run(binary, "formatted", c["json"]) == c["formatted"], (
                    f"formatted for {c['desc']!r}"
                )
        for c in mutation_cases:
            got = json.loads(_run(binary, "mutation", c["scenario"]))
            assert got == c["trace"], f"mutation trace for {c['desc']!r}"

        api_oracle = Path(td) / "api_oracle"
        assert compile_oracle(cc, CJSON, api_oracle, []), "API oracle must compile"
        c_trace = run_oracle(api_oracle, "safe")
        assert c_trace.returncode == 0, c_trace.stderr.decode()[:400]
        rust_trace = subprocess.run(
            ["cargo", "run", "--quiet", "--bin", "api_trace"],
            cwd=RUST_DIR,
            capture_output=True,
        )
        assert rust_trace.returncode == 0, rust_trace.stderr.decode()[:400]
        assert c_trace.stdout == rust_trace.stdout, "safe API trace must match C oracle"

        # Every exclusion is a reached C operation, not merely a name in the
        # audit. The audit test separately proves the manifest is exact against
        # the header, so a planted/unreachable refusal fails this loop.
        sys.path.insert(0, str(CJSON / "tools"))
        import api_surface_audit  # type: ignore

        refusals = {
            **api_surface_audit.OWNERSHIP_BLOCKED,
            **api_surface_audit.GLOBAL_STATE_EXCLUDED,
        }
        observed_expectations = {
            "string_reference": '"after"',
            "object_reference": '{"value":2}',
            "array_reference": "[2]",
            "object_const_key": '{"after":1}',
            "array_item_reference": "[[2]]",
            "object_item_reference": '{"value":[2]}',
            "detach_via_pointer": "2",
            "replace_via_pointer": "[1,9,3]",
        }
        for operation, (_, scenario) in refusals.items():
            trace = run_oracle(api_oracle, "refusal", scenario)
            assert trace.returncode == 0, f"C refusal scenario {scenario}: {trace.stderr.decode()[:400]}"
            fields = _trace_lines(trace.stdout)
            assert fields.get("reached") == "1", f"unreached refusal {operation}"
            assert operation in fields.get("operations", "").split(","), (
                f"C trace {scenario} did not reach {operation}: {fields}"
            )
            if scenario in observed_expectations:
                assert fields.get("observed") == observed_expectations[scenario]
            if scenario == "detach_via_pointer":
                assert fields.get("identity") == "1"
            if scenario == "replace_via_pointer":
                assert fields.get("replaced") == "1"
            if scenario == "error_pointer":
                assert int(fields.get("error_offset", "-1")) >= 0
            if scenario == "custom_hooks":
                assert int(fields.get("allocations", "0")) > 0
                assert int(fields.get("frees", "0")) > 0


@pytest.mark.skipif(_cc() is None, reason="no C compiler available")
def test_cjson_ownership_under_asan():
    """C ownership paths must be leak/double-free clean under AddressSanitizer.

    If ASan is unavailable on this toolchain the test is skipped (recorded), so a
    missing sanitizer never silently passes the ownership check.
    """
    cc = _cc()
    parse_cases = _all_parse_cases()
    mutation_cases = _mutation_cases()
    with tempfile.TemporaryDirectory() as td:
        binary = Path(td) / "runner_asan"
        if not _compile(cc, binary, ["-fsanitize=address", "-g"]):
            pytest.skip("AddressSanitizer not supported by this compiler")
        for c in parse_cases:
            for mode in ("unformatted", "inspect", "formatted"):
                res = subprocess.run(
                    [str(binary), mode],
                    input=c["json"].encode(),
                    capture_output=True,
                )
                assert res.returncode == 0, (
                    f"ASan failure for {c['desc']!r}/{mode}: "
                    f"{res.stderr.decode()[:400]}"
                )
        for c in mutation_cases:
            res = subprocess.run(
                [str(binary), "mutation"],
                input=c["scenario"].encode(),
                capture_output=True,
            )
            assert res.returncode == 0, (
                f"ASan failure for mutation {c['desc']!r}: "
                f"{res.stderr.decode()[:400]}"
            )

        api_oracle = Path(td) / "api_oracle_asan"
        if not compile_oracle(cc, CJSON, api_oracle, ["-fsanitize=address", "-g"]):
            pytest.skip("AddressSanitizer API oracle compilation failed")
        sys.path.insert(0, str(CJSON / "tools"))
        import api_surface_audit  # type: ignore

        # Derived from the manifest, so a reclassification cannot leave a stale
        # scenario name here (an unknown scenario exits 2 and would read as an
        # ASan failure).
        scenarios = sorted(
            {
                scenario
                for _, scenario in {
                    **api_surface_audit.OWNERSHIP_BLOCKED,
                    **api_surface_audit.GLOBAL_STATE_EXCLUDED,
                }.values()
            }
        )
        for args in [("safe",), *(("refusal", scenario) for scenario in scenarios)]:
            res = run_oracle(api_oracle, *args)
            assert res.returncode == 0, (
                f"ASan failure for API oracle {args}: {res.stderr.decode()[:400]}"
            )
