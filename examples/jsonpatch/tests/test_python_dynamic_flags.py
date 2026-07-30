"""Dynamic-dispatch provenance for the Python syntax/AST frontend.

Locks the diagnostic that labels registry/getattr/polymorphic sites without
demoting is_deterministic or changing audit pass-rate semantics.

jsonpatch is the ground-truth case: JsonPatch.operations registry dispatch
under-captures callees (documented Phase 7 ablation boundary).

Run: uv run python -m pytest examples/jsonpatch/tests/test_python_dynamic_flags.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
from mini_game_to_byog import build_byog_for_package  # type: ignore
from python_dynamic import analyze_source_text, annotate_byog  # type: ignore


def test_jsonpatch_registry_dispatch_is_detected():
    """Ground truth: operations table + polymorphic apply must be labelled."""
    data = build_byog_for_package(package_dir=ROOT / "examples" / "jsonpatch")
    by_title = {e["title"]: e for e in data["entities"]}

    get_op = by_title["jsonpatch:JsonPatch._get_operation"]
    assert get_op.get("dynamic_dependent") is True
    reasons = get_op.get("dynamic_reasons") or []
    assert any("registry_lookup:operations" in str(r) for r in reasons), reasons
    assert any("call_through_dynamic_name:cls" in str(r) for r in reasons), reasons

    apply = by_title["jsonpatch:JsonPatch.apply"]
    assert apply.get("dynamic_dependent") is True
    apply_reasons = apply.get("dynamic_reasons") or []
    assert any("polymorphic_call:operation.apply" in str(r) for r in apply_reasons), apply_reasons
    assert any("registry_derived_iter:_ops" in str(r) for r in apply_reasons), apply_reasons

    # The unresolved observation is the consumer-visible hole in the call graph.
    obs = [
        o
        for o in data["call_observations"]
        if o.get("source") == "jsonpatch:JsonPatch.apply"
        and str(o.get("display_target")) == "operation.apply"
    ]
    assert obs, "expected unresolved observation for operation.apply"
    assert all(o.get("dynamic_dependent") for o in obs)

    # Registry members are named as observations *and* promoted to non-deterministic
    # calls edges (static Name table + labelled dispatch site). See promotion rule
    # in scripts/python_dynamic.py::_iter_registry_dispatch_targets.
    cands = [
        o
        for o in data["call_observations"]
        if o.get("source") == "jsonpatch:JsonPatch.apply"
        and str(o.get("reason", "")).startswith("registry_candidate:")
    ]
    targets = {str(o.get("display_target")) for o in cands}
    for need in (
        "jsonpatch:AddOperation.apply",
        "jsonpatch:RemoveOperation.apply",
        "jsonpatch:ReplaceOperation.apply",
        "jsonpatch:MoveOperation.apply",
        "jsonpatch:TestOperation.apply",
        "jsonpatch:CopyOperation.apply",
    ):
        assert need in targets, (need, targets)
    assert all(float(o.get("confidence", 1)) < 0.6 for o in cands)
    promoted = [
        r
        for r in data["relationships"]
        if r.get("type") == "calls"
        and r.get("source") == "jsonpatch:JsonPatch.apply"
        and "Operation.apply" in str(r.get("target"))
        and r.get("extractor") == "python_dynamic_registry"
    ]
    assert len(promoted) == 6, promoted
    assert all(r.get("is_deterministic") is False for r in promoted)
    assert all(float(r.get("confidence", 0)) == 0.75 for r in promoted)

    # Labels do not demote trusted edges elsewhere.
    trusted = [
        r
        for r in data["relationships"]
        if r.get("type") == "calls"
        and r.get("is_deterministic")
        and r.get("extractor") != "python_dynamic_registry"
    ]
    assert trusted, "sanity: graph still has trusted calls"
    for r in trusted:
        # dynamic flag may be true or false; is_deterministic must be untouched by annotation
        assert r.get("is_deterministic") is True


def test_registry_dict_requires_callable_values():
    """KEYWORD-style int tables are not callable registries."""
    src = (
        "KEYWORDS = {'SELECT': 1, 'FROM': 2}\n"
        "OPS = {'add': Add, 'remove': Remove}\n"
        "def run(op):\n"
        "    cls = OPS[op]\n"
        "    return cls()\n"
    )
    fa = analyze_source_text(src, Path("t.py"))
    assert "OPS" in fa.registries
    assert "KEYWORDS" not in fa.registries
    kinds = {s.kind for s in fa.sites}
    assert "registry_lookup" in kinds
    assert "call_through_dynamic_name" in kinds


def test_getattr_dynamic_and_def_getattr():
    src = (
        "class D:\n"
        "    def __getattr__(self, name):\n"
        "        return getattr(self._inner, name)\n"
        "    def dispatch(self, func_name):\n"
        "        func = getattr(self, func_name.lower(), self._default)\n"
        "        return func()\n"
    )
    fa = analyze_source_text(src, Path("d.py"))
    kinds = {s.kind for s in fa.sites}
    assert "def_getattr" in kinds
    assert "getattr_dynamic" in kinds
    assert "call_through_dynamic_name" in kinds


def test_annotation_does_not_change_counts_or_determinism(tmp_path: Path):
    (tmp_path / "m.py").write_text(
        "def helper():\n"
        "    return 1\n"
        "OPS = {'h': helper}\n"
        "def run(k):\n"
        "    return OPS[k]()\n",
        encoding="utf-8",
    )
    data = build_byog_for_package(package_dir=tmp_path)
    n_ent = len(data["entities"])
    n_rel = len(data["relationships"])
    n_calls = sum(1 for r in data["relationships"] if r.get("type") == "calls")
    n_obs = len(data["call_observations"])
    dets = [bool(r.get("is_deterministic")) for r in data["relationships"] if r.get("type") == "calls"]
    # Re-annotate is idempotent on entities/rels/calls; observations stay same count
    # (registry_candidate rows are replaced, not doubled).
    annotate_byog(data, tmp_path)
    annotate_byog(data, tmp_path)
    assert len(data["entities"]) == n_ent
    assert len(data["relationships"]) == n_rel
    assert sum(1 for r in data["relationships"] if r.get("type") == "calls") == n_calls
    assert len(data["call_observations"]) == n_obs
    assert [bool(r.get("is_deterministic")) for r in data["relationships"] if r.get("type") == "calls"] == dets
    run = next(e for e in data["entities"] if str(e["title"]).endswith(":run") or e["title"] == "m:run")
    assert run.get("dynamic_dependent") is True


def test_reannotating_a_published_snapshot_round_trips(tmp_path: Path):
    """Stamping a published graph twice must work: parquet returns list columns
    as numpy arrays, and the first stamp writes the very columns the second reads.
    """
    import pandas as pd

    data = build_byog_for_package(package_dir=ROOT / "examples" / "jsonpatch")
    annotate_byog(data, ROOT / "examples" / "jsonpatch")
    n_obs = len(data["call_observations"])

    # Round-trip exactly like scripts/python_dynamic.py --graph does.
    round_tripped = {}
    for key in ("entities", "relationships", "call_observations"):
        path = tmp_path / f"{key}.parquet"
        pd.DataFrame(data[key]).to_parquet(path)
        round_tripped[key] = pd.read_parquet(path).to_dict("records")

    annotate_byog(round_tripped, ROOT / "examples" / "jsonpatch")
    assert len(round_tripped["call_observations"]) == n_obs
    assert len(round_tripped["relationships"]) == len(data["relationships"])
    apply_obs = next(
        o
        for o in round_tripped["call_observations"]
        if o.get("source") == "jsonpatch:JsonPatch.apply"
        and str(o.get("display_target")) == "operation.apply"
    )
    assert sorted(apply_obs["dynamic_reasons"]) == sorted(
        next(
            o
            for o in data["call_observations"]
            if o.get("source") == "jsonpatch:JsonPatch.apply"
            and str(o.get("display_target")) == "operation.apply"
        )["dynamic_reasons"]
    )
