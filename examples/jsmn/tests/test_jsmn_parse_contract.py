"""Golden contract for the vendored jsmn parser (first C->Rust port target).

Ground truth is jsmn.h itself, captured via a dedicated C runner. Each case pins:
    (json, token_capacity) -> (result_code, [ {type,start,end,size}, ... ])
in jsmn default mode (non-strict, no JSMN_PARENT_LINKS). Token `type` is the jsmn
bit-flag enum: OBJECT=1, ARRAY=2, STRING=4, PRIMITIVE=8.

This test recompiles the C runner and re-derives the tokens to keep the golden in
sync; it is skipped if a C compiler is unavailable. The Rust port verifies against
the same golden.

Run: uv run python -m pytest examples/jsmn/tests/test_jsmn_parse_contract.py -q
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).parent
PARSE_DIR = HERE / "parse"
GOLDEN = PARSE_DIR / "golden_parse.json"


def _cc():
    return shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")


def test_golden_present_and_sized():
    cases = json.loads(GOLDEN.read_text())["cases"]
    assert len(cases) >= 15


@pytest.mark.skipif(_cc() is None, reason="no C compiler available")
def test_jsmn_golden_matches_reference():
    cc = _cc()
    cases = json.loads(GOLDEN.read_text())["cases"]
    with tempfile.TemporaryDirectory() as td:
        binary = Path(td) / "runner"
        subprocess.run(
            [cc, "-I", str(HERE.parent), "-o", str(binary), str(PARSE_DIR / "runner.c")],
            check=True,
            capture_output=True,
        )
        for c in cases:
            out = subprocess.run(
                [str(binary), str(c["cap"])],
                input=c["json"].encode(),
                capture_output=True,
                check=True,
            ).stdout.decode()
            got = json.loads(out)
            assert got["result"] == c["result"], f"result for {c['json']!r}"
            assert got["tokens"] == c["tokens"], f"tokens for {c['json']!r}"
