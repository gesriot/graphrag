"""Golden contract for vendored jsonpatch (Phase 7 ablation v2 target).

Ground truth is the vendored Python `jsonpatch` (which depends on `jsonpointer`).
Each case pins, for (doc, patch):
    apply_patch(doc, patch) -> Ok(result_json) | Err(error_class)
where error_class is one of TestFailed / Conflict / InvalidPointer / InvalidPatch.

This test re-derives the contract from the vendored library to keep the golden in
sync. Scope = the bounded `apply_patch` API only (add/remove/replace/move/copy/
test, pointer escaping, array index and '-', failed paths, failed test, bad
pointer, invalid op). The mutable in-place API, CLI, and diff/make_patch are out
of scope.

Run: uv run python -m pytest examples/jsonpatch/tests/test_jsonpatch_contract.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
PKG = HERE.parent
GOLDEN = HERE / "apply" / "golden_apply.json"
sys.path.insert(0, str(PKG))


def _classify(exc: Exception) -> str:
    return {
        "JsonPatchTestFailed": "TestFailed",
        "JsonPatchConflict": "Conflict",
        "JsonPointerException": "InvalidPointer",
        "JsonPatchException": "InvalidPatch",
        "InvalidJsonPatch": "InvalidPatch",
        "KeyError": "Conflict",
        "IndexError": "Conflict",
        "TypeError": "InvalidPatch",
    }.get(type(exc).__name__, type(exc).__name__)


def test_golden_present_and_sized():
    cases = json.loads(GOLDEN.read_text())["cases"]
    assert len(cases) >= 24


def test_jsonpatch_golden_matches_reference():
    import jsonpatch  # vendored; imports the vendored jsonpointer

    cases = json.loads(GOLDEN.read_text())["cases"]
    for c in cases:
        doc = json.loads(c["doc"])
        patch = json.loads(c["patch"])
        # Note: jsonpatch's JsonPatchTestFailed subclasses AssertionError, so we
        # classify inside the except and assert *outside* it (never let the
        # library's exception collide with our own assertions).
        res = None
        err = None
        try:
            res = jsonpatch.apply_patch(doc, patch)
        except Exception as e:  # noqa: BLE001 - classify like the generator
            err = _classify(e)
        if c["ok"]:
            assert err is None, f"{c['desc']}: unexpected error {err}"
            assert res == json.loads(json.dumps(c["result"])), f"result for {c['desc']}"
        else:
            assert err == c["error"], f"error class for {c['desc']} (got {err})"
