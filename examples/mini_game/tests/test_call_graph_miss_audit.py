"""Fail-closed exhaustive classification for the three call-oracle populations."""
from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

import call_graph_miss_audit as audit  # type: ignore


def test_every_live_miss_has_one_reviewed_construct_category():
    """A changed oracle population cannot silently inherit yesterday's labels."""
    report = audit.build_report()
    assert report["ok"], report["failures"]
    assert report["live_misses"] == 18
    assert report["cold_import_artifacts_excluded"] == 3
    assert report["super_constructor_miss_closed"] == 1
    assert report["categories"]["rich_tuple_compare_protocol"] == 8
    assert report["categories"]["implicit_contains_protocol"] == 2
    assert report["categories"]["implicit_str_protocol"] == 2


def test_removed_classification_fails_closed(monkeypatch):
    """A real live miss without its explicit label is not a clean audit."""
    key = (
        "jsonpatch",
        "jsonpatch:JsonPatch.apply",
        "jsonpatch:JsonPatch._ops",
    )
    monkeypatch.delitem(audit.CURRENT_MISS_CLASSIFICATIONS, key)

    report = audit.build_report()

    assert report["ok"] is False
    assert any("unclassified live misses" in failure for failure in report["failures"])
