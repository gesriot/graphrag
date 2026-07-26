"""Repo-wide documentation consistency checks (load-bearing numbers vs artifacts).

Claims live in ``scripts/doc_claims.json`` and are verified by
``scripts/check_doc_claims.py``. This test is the suite entry point so drift
fails like a broken golden.

Historical dated paragraphs are records, not current claims — see the manifest
``kind: historical`` rule and the checker docstring.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from check_doc_claims import run_all  # noqa: E402


def test_documented_numbers_match_artifacts():
    report = run_all()
    # Surface verified-live vs traced in assertion message on failure / for -s.
    summary = (
        f"live={report['verified_live']} traced={report['traced_only']} "
        f"historical={report['historical_record']}"
    )
    if not report["ok"]:
        detail = "\n".join(report["failures"])
        pytest.fail(f"doc claim mismatch ({summary}):\n{detail}")
    # Keep a soft breadcrumb when tests run with -s
    print(f"doc claims ok ({summary})")
