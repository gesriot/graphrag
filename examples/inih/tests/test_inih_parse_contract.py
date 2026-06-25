"""Golden contract for vendored inih (first inih C->Rust port target).

Ground truth is inih (`ini.c`/`ini.h`) in default config, captured via a
dedicated C runner. Each case pins an INI input to:
    (result_code, [ {section, name, value}, ... ])
where `result` is inih's return (0, or the first-error line number) and the list
is the ordered sequence of handler callbacks.

This test recompiles the C runner and re-derives the contract to keep the golden
in sync, and asserts string<->file input parity (`ini_parse_string_length` vs
`ini_parse_file`). It is skipped if a C compiler is unavailable. The Rust port
verifies against the same golden.

Run: uv run python -m pytest examples/inih/tests/test_inih_parse_contract.py -q
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).parent
INIH = HERE.parent
PARSE_DIR = HERE / "parse"
GOLDEN = PARSE_DIR / "golden_parse.json"


def _cc():
    return shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")


def test_golden_present_and_sized():
    cases = json.loads(GOLDEN.read_text())["cases"]
    assert len(cases) >= 18


@pytest.mark.skipif(_cc() is None, reason="no C compiler available")
def test_inih_golden_matches_reference_and_input_parity():
    cc = _cc()
    cases = json.loads(GOLDEN.read_text())["cases"]
    with tempfile.TemporaryDirectory() as td:
        binary = Path(td) / "runner"
        subprocess.run(
            [cc, "-I", str(INIH), "-o", str(binary),
             str(PARSE_DIR / "runner.c"), str(INIH / "ini.c")],
            check=True,
            capture_output=True,
        )

        def run(ini: str, mode: str):
            out = subprocess.run(
                [str(binary), mode],
                input=ini.encode(),
                capture_output=True,
                check=True,
            ).stdout.decode()
            return json.loads(out)

        for c in cases:
            got = run(c["ini"], "string")
            assert got["result"] == c["result"], f"result for {c['desc']!r}"
            assert got["events"] == c["events"], f"events for {c['desc']!r}"
            # string and file input paths must agree byte-for-byte.
            assert run(c["ini"], "file") == got, f"string/file parity for {c['desc']!r}"
