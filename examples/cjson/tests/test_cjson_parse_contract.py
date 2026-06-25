"""Golden contract for vendored cJSON (first cJSON C->Rust port target).

Ground truth is cJSON (`cJSON.c`/`cJSON.h`), captured via a dedicated C runner.
The bounded ownership slice is parse -> inspect -> print -> delete. Each case
pins, for a JSON input:
- `unformatted`: `cJSON_PrintUnformatted` output (or `__PARSE_ERROR__`),
- `inspect`: a canonical tree descriptor built from cJSON's public API/fields
  (numbers carry valueint + the IEEE-754 bits of valuedouble, so parse fidelity
  is checked exactly without depending on float *printing*),
- `formatted`: `cJSON_Print` output, for a few cases.

Scope: bounded corpus (objects/arrays/strings/escapes/integers/bool/null/
nesting/empty). Float-printing edge cases are deferred to a later sub-stage.

This test recompiles the C runner and re-derives the contract to keep the golden
in sync, and (when the compiler supports it) recompiles under AddressSanitizer to
verify the parse+print+delete path is free of leaks/double-frees. Skipped if no C
compiler is available.

Run: uv run python -m pytest examples/cjson/tests/test_cjson_parse_contract.py -q
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).parent
CJSON = HERE.parent
PARSE_DIR = HERE / "parse"
GOLDEN = PARSE_DIR / "golden_parse.json"


def _cc():
    return shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")


def _compile(cc: str, out: Path, extra: list[str]) -> bool:
    res = subprocess.run(
        [cc, "-I", str(CJSON), *extra, "-o", str(out),
         str(PARSE_DIR / "runner.c"), str(CJSON / "cJSON.c")],
        capture_output=True,
    )
    return res.returncode == 0


def _run(binary: Path, mode: str, js: str) -> str:
    return subprocess.run(
        [str(binary), mode],
        input=js.encode(),
        capture_output=True,
        check=True,
    ).stdout.decode().rstrip("\n")


def test_golden_present_and_sized():
    cases = json.loads(GOLDEN.read_text())["cases"]
    assert len(cases) >= 22


@pytest.mark.skipif(_cc() is None, reason="no C compiler available")
def test_cjson_golden_matches_reference():
    cc = _cc()
    cases = json.loads(GOLDEN.read_text())["cases"]
    with tempfile.TemporaryDirectory() as td:
        binary = Path(td) / "runner"
        assert _compile(cc, binary, []), "plain runner must compile"
        for c in cases:
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


@pytest.mark.skipif(_cc() is None, reason="no C compiler available")
def test_cjson_ownership_under_asan():
    """parse+print+delete must be leak/double-free clean under AddressSanitizer.

    If ASan is unavailable on this toolchain the test is skipped (recorded), so a
    missing sanitizer never silently passes the ownership check.
    """
    cc = _cc()
    cases = json.loads(GOLDEN.read_text())["cases"]
    with tempfile.TemporaryDirectory() as td:
        binary = Path(td) / "runner_asan"
        if not _compile(cc, binary, ["-fsanitize=address", "-g"]):
            pytest.skip("AddressSanitizer not supported by this compiler")
        for c in cases:
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
