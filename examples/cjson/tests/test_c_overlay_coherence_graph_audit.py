"""Read-only snapshot-wide coherence audit for persisted C overlays.

Pure synthetic/corruption tests always run. Live disposable-graph tests skip
when no C compiler is present. Nothing here writes a published byog_* root.

Run:
  uv run python -m pytest examples/cjson/tests/test_c_overlay_coherence_graph_audit.py -q
"""
from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

from c_clang_calls import (  # type: ignore
    CONFIDENCE_BOUNDARY as CALL_CB,
    EXTRACTOR as CALL_EXTRACTOR,
    FACT_KIND as CALL_FACT,
    MODE as CALL_MODE,
    build_disabled_provenance as call_off,
    validate_persisted_call_overlay,
)
from c_clang_signatures import (  # type: ignore
    CONFIDENCE_BOUNDARY as SIG_CB,
    EXTRACTOR as SIG_EXTRACTOR,
    FACT_KIND as SIG_FACT,
    MODE as SIG_MODE,
    build_disabled_provenance as signature_off,
    validate_persisted_signature_overlay,
)
from c_clang_type_shapes import (  # type: ignore
    CONFIDENCE_BOUNDARY as SHAPE_CB,
    EVIDENCE_ONLY,
    EXTRACTOR as SHAPE_EXTRACTOR,
    FACT_KIND as SHAPE_FACT,
    HARD_EQUALITY,
    LIMITATIONS as SHAPE_LIMITATIONS,
    MODE as SHAPE_MODE,
    build_disabled_provenance as shape_off,
    validate_persisted_type_shape_overlay,
)
from c_clang_type_uses import (  # type: ignore
    CONFIDENCE_BOUNDARY as USE_CB,
    EXTRACTOR as USE_EXTRACTOR,
    FACT_KIND as USE_FACT,
    MODE as USE_MODE,
    build_disabled_provenance as type_use_off,
    validate_persisted_type_use_overlay,
)
from c_clang_types import (  # type: ignore
    CONFIDENCE_BOUNDARY as TYPE_CB,
    EXTRACTOR as TYPE_EXTRACTOR,
    FACT_KIND as TYPE_FACT,
    MODE as TYPE_MODE,
    build_disabled_provenance as type_off,
    validate_persisted_type_overlay,
)
from c_compiler_common import CONFIDENCE_BOUNDARY as DEP_CB  # type: ignore
from c_compiler_facts import (  # type: ignore
    FACT_KIND as DEP_FACT,
    MODE as DEP_MODE,
    build_disabled_provenance as dep_off,
    validate_persisted_compiler_dependency_overlay,
)
from c_compiler_includes import (  # type: ignore
    CONFIDENCE_BOUNDARY as INC_CB,
    FACT_KIND as INC_FACT,
    MODE as INC_MODE,
    build_disabled_provenance as inc_off,
    validate_persisted_compiler_include_overlay,
)
from c_overlay_coherence import validate_persisted_c_overlay_coherence  # type: ignore
from c_overlay_coherence_graph_audit import (  # type: ignore
    AUDIT_MODE,
    audit_graph_root,
    audit_rows,
    audit_to_json,
    format_report,
    main as audit_main,
    read_only_fingerprint,
)
from c_preprocessor import (  # type: ignore
    find_c_compiler,
    validate_persisted_preprocessor_liveness,
)

DIGEST = "a" * 64
OTHER_DIGEST = "b" * 64
COMPILER_PATH = "/usr/bin/clang"
OTHER_PATH = "/usr/bin/gcc"
COMPILER_ID = "Apple clang version test"
COMPILER_VERSION = "17.0.0"

COMPILER = {
    "compiler_path": COMPILER_PATH,
    "compiler_id": COMPILER_ID,
    "compiler_version": COMPILER_VERSION,
}


def _cc():
    return find_c_compiler()


def _codes(report) -> set:
    return {a.get("code") for a in report.get("anomalies") or []}


def _entity() -> dict:
    return {
        "id": "ent:file:main",
        "title": "main:main.c",
        "type": "file",
        "source_file": "/tmp/pkg/main.c",
    }


def _rel() -> dict:
    return {
        "id": "rel:contains:1",
        "source": "main:main.c",
        "target": "main:main",
        "type": "contains",
        "extractor": "tree-sitter-c",
    }


def _shared(digest: str = DIGEST, *, compiler: dict | None = None, n_entries: int = 1):
    rec = dict(compiler or COMPILER)
    return {
        "compiler_path": rec["compiler_path"],
        "compiler_id": rec["compiler_id"],
        "compiler_version": rec["compiler_version"],
        "compilers": [rec],
        "compile_commands_digest": digest,
        "n_compile_entries": n_entries,
        "n_translation_units": n_entries,
    }


def _deps_on(digest: str = DIGEST, **over) -> dict:
    shared = _shared(digest)
    block = {
        "mode": DEP_MODE,
        "enabled": True,
        "fact_kind": DEP_FACT,
        "n_facts": 0,
        "n_facts_added": 0,
        "n_facts_collected": 0,
        "translation_unit_titles": [],
        "confidence_boundary": DEP_CB,
        **shared,
        "n_translation_units": 0,
    }
    block.update(over)
    return block


def _incs_on(digest: str = DIGEST, **over) -> dict:
    shared = _shared(digest)
    block = {
        "mode": INC_MODE,
        "enabled": True,
        "fact_kind": INC_FACT,
        "n_facts": 0,
        "n_facts_added": 0,
        "n_facts_collected": 0,
        "translation_unit_titles": [],
        "confidence_boundary": INC_CB,
        **shared,
        "n_translation_units": 0,
    }
    block.update(over)
    return block


def _sig_on(digest: str = DIGEST, **over) -> dict:
    shared = _shared(digest)
    block = {
        "mode": SIG_MODE,
        "enabled": True,
        "fact_kind": SIG_FACT,
        "extractor": SIG_EXTRACTOR,
        "n_facts": 0,
        "n_facts_changed": 0,
        "counts": {
            "matched": 0,
            "tree_sitter_only": 0,
            "out_of_compile_db_scope": 0,
            "clang_only": 0,
            "ambiguous": 0,
            "macro_location_unsupported": 0,
        },
        "confidence_boundary": SIG_CB,
        **shared,
    }
    block.update(over)
    return block


def _calls_on(digest: str = DIGEST, **over) -> dict:
    shared = _shared(digest)
    block = {
        "mode": CALL_MODE,
        "enabled": True,
        "fact_kind": CALL_FACT,
        "extractor": CALL_EXTRACTOR,
        "n_facts": 0,
        "n_facts_changed": 0,
        "counts": {
            "matched_internal": 0,
            "clang_only_internal": 0,
            "tree_sitter_only_internal": 0,
            "external_direct": 0,
            "indirect": 0,
            "ambiguous": 0,
            "macro_location_unsupported": 0,
            "out_of_compile_db_scope": 0,
        },
        "tree_sitter_accounting": {
            "total_calls": 0,
            "matched_internal": 0,
            "covered_by_noninternal_clang_observation": 0,
            "tree_sitter_only_internal": 0,
            "out_of_compile_db_scope": 0,
        },
        "confidence_boundary": CALL_CB,
        **shared,
    }
    block.update(over)
    return block


def _types_on(digest: str = DIGEST, **over) -> dict:
    shared = _shared(digest)
    block = {
        "mode": TYPE_MODE,
        "enabled": True,
        "fact_kind": TYPE_FACT,
        "extractor": TYPE_EXTRACTOR,
        "n_facts": 0,
        "n_facts_changed": 0,
        "counts": {
            "matched": 0,
            "tree_sitter_only": 0,
            "clang_only": 0,
            "ambiguous": 0,
            "macro_location_unsupported": 0,
            "out_of_compile_db_scope": 0,
            "anonymous_declarations": 0,
            "unsupported_declarations": 0,
            "outside_package_declarations": 0,
            "alternate_declaration_sites": 0,
        },
        "confidence_boundary": TYPE_CB,
        **shared,
    }
    block.update(over)
    return block


def _uses_on(digest: str = DIGEST, **over) -> dict:
    shared = _shared(digest)
    block = {
        "mode": USE_MODE,
        "enabled": True,
        "fact_kind": USE_FACT,
        "extractor": USE_EXTRACTOR,
        "n_facts": 0,
        "n_facts_changed": 0,
        "n_facts_added": 0,
        "n_observations": 0,
        "counts": {
            "matched_internal": 0,
            "owner_unmatched": 0,
            "target_unresolved": 0,
            "ambiguous_target": 0,
            "macro_location_unsupported": 0,
            "external_or_system": 0,
            "unsupported_type_form": 0,
            "unowned_context": 0,
        },
        "confidence_boundary": USE_CB,
        **shared,
    }
    block.update(over)
    return block


def _shapes_on(digest: str = DIGEST, **over) -> dict:
    shared = _shared(digest)
    block = {
        "mode": SHAPE_MODE,
        "enabled": True,
        "fact_kind": SHAPE_FACT,
        "extractor": SHAPE_EXTRACTOR,
        "n_facts": 0,
        "n_facts_changed": 0,
        "n_decorated_entities": 0,
        "counts": {
            "matched_shape": 0,
            "tree_sitter_only_members": 0,
            "clang_only_members": 0,
            "member_order_mismatch": 0,
            "duplicate_or_ambiguous_members": 0,
            "macro_location_unsupported": 0,
            "owner_unmatched": 0,
            "unsupported_member_form": 0,
            "outside_package_declarations": 0,
            "type_declaration_matched_struct_enum": 0,
            "type_declaration_matched_total": 0,
            "shape_owners_classified": 0,
        },
        "hard_equality": HARD_EQUALITY,
        "evidence_only": list(EVIDENCE_ONLY),
        "limitations": list(SHAPE_LIMITATIONS),
        "observation_only_buckets": [
            "unsupported_member_form",
            "outside_package_declarations",
        ],
        "fail_closed_buckets": [
            "tree_sitter_only_members",
            "clang_only_members",
            "member_order_mismatch",
            "duplicate_or_ambiguous_members",
            "macro_location_unsupported",
            "owner_unmatched",
        ],
        "confidence_boundary": SHAPE_CB,
        **shared,
    }
    block.update(over)
    return block


def _all_off() -> dict:
    return {
        "compiler_dependencies": dep_off(),
        "compiler_includes": inc_off(),
        "clang_signatures": signature_off(),
        "clang_calls": call_off(),
        "clang_types": type_off(),
        "clang_type_uses": type_use_off(),
        "clang_type_shapes": shape_off(),
    }


def _all_enabled(digest: str = DIGEST) -> dict:
    return {
        "compiler_dependencies": _deps_on(digest),
        "compiler_includes": _incs_on(digest),
        "clang_signatures": _sig_on(digest),
        "clang_calls": _calls_on(digest),
        "clang_types": _types_on(digest),
        "clang_type_uses": _uses_on(digest),
        "clang_type_shapes": _shapes_on(digest),
    }


def _base_rows():
    return [_entity()], [_rel()], None


def _counts(ents, rels) -> dict:
    return {"entities": len(ents), "relationships": len(rels)}


def test_legacy_graph_is_legacy_absent():
    ents, rels, obs = _base_rows()
    report = audit_rows(ents, rels, obs, {})
    assert report["ok"] is True
    assert report["status"] == "legacy_absent"
    assert report["classification"] == "legacy_absent"
    assert report["census"]["enabled"] == []
    assert report["census"]["off"] == []
    assert report["census"]["absent"] == [
        "compiler_dependencies",
        "compiler_includes",
        "clang_signatures",
        "clang_calls",
        "clang_types",
        "clang_type_uses",
        "clang_type_shapes",
    ]


def test_all_exact_off_blocks_are_off():
    ents, rels, obs = _base_rows()
    report = audit_rows(ents, rels, obs, _all_off())
    assert report["ok"] is True, report["anomalies"]
    assert report["status"] == "off"
    assert report["census"]["enabled"] == []
    assert set(report["census"]["off"]) == {
        "compiler_dependencies",
        "compiler_includes",
        "clang_signatures",
        "clang_calls",
        "clang_types",
        "clang_type_uses",
        "clang_type_shapes",
    }


def test_one_enabled_block_is_trivially_coherent():
    ents, rels, obs = _base_rows()
    manifest = {**_all_off(), "compiler_dependencies": _deps_on()}
    report = audit_rows(ents, rels, obs, manifest)
    assert report["ok"] is True, report["anomalies"]
    assert report["status"] == "coherent"
    assert report["census"]["enabled"] == ["compiler_dependencies"]
    assert report["shared"]["compile_commands_digest"] == DIGEST
    assert report["shared"]["n_compile_entries"] == 1


def test_hybrid_valid_blocks_with_mismatched_digest_fail_coherence():
    ents, rels, obs = _base_rows()
    manifest = {
        **_all_off(),
        "compiler_dependencies": _deps_on(DIGEST),
        "compiler_includes": _incs_on(OTHER_DIGEST),
    }
    dep = validate_persisted_compiler_dependency_overlay(ents, rels, manifest)
    inc = validate_persisted_compiler_include_overlay(ents, rels, manifest)
    assert dep["ok"] is True and dep["status"] == "enabled"
    assert inc["ok"] is True and inc["status"] == "enabled"
    report = audit_rows(ents, rels, obs, manifest)
    assert report["ok"] is False
    assert report["status"] == "invalid"
    assert "digest_mismatch" in _codes(report)
    assert report["components"]["compiler_dependencies"]["ok"] is True
    assert report["components"]["compiler_includes"]["ok"] is True

    # An unrelated component failure must not suppress the independently
    # provable mismatch between the two valid enabled blocks.
    with_failure = copy.deepcopy(manifest)
    with_failure["clang_signatures"] = None
    combined = audit_rows(ents, rels, obs, with_failure)
    assert {"component_integrity", "digest_mismatch"} <= _codes(combined)


def test_all_seven_enabled_with_identical_provenance():
    ents, rels, obs = _base_rows()
    manifest = {**_all_enabled(), "counts": _counts(ents, rels)}
    report = audit_rows(ents, rels, obs, manifest)
    assert report["ok"] is True, report["anomalies"]
    assert report["status"] == "coherent"
    assert report["census"]["enabled"] == [
        "compiler_dependencies",
        "compiler_includes",
        "clang_signatures",
        "clang_calls",
        "clang_types",
        "clang_type_uses",
        "clang_type_shapes",
    ]
    for name, result in report["components"].items():
        if name == "preprocessor_liveness":
            assert result["ok"] is True
            continue
        assert result["ok"] is True, (name, result["anomalies"])
        assert result["status"] == "enabled"


def test_enabled_pairs_compare_shared_fields():
    ents, rels, obs = _base_rows()
    pairs = (
        ("compiler_dependencies", _deps_on, "compiler_includes", _incs_on),
        ("clang_signatures", _sig_on, "clang_calls", _calls_on),
        ("clang_types", _types_on, "clang_type_uses", _uses_on),
        ("clang_type_shapes", _shapes_on, "compiler_dependencies", _deps_on),
    )
    for left, left_fn, right, right_fn in pairs:
        matching = {**_all_off(), left: left_fn(DIGEST), right: right_fn(DIGEST)}
        matching["counts"] = _counts(ents, rels)
        ok = audit_rows(ents, rels, obs, matching)
        assert ok["ok"] is True, (left, right, ok["anomalies"])
        assert ok["status"] == "coherent"
        mismatched = {**_all_off(), left: left_fn(DIGEST), right: right_fn(OTHER_DIGEST)}
        mismatched["counts"] = _counts(ents, rels)
        bad = audit_rows(ents, rels, obs, mismatched)
        assert bad["ok"] is False, (left, right)
        assert "digest_mismatch" in _codes(bad)


def test_mismatched_compile_entry_count():
    ents, rels, obs = _base_rows()
    manifest = {
        **_all_off(),
        "compiler_dependencies": _deps_on(n_compile_entries=1, n_translation_units=0),
        "clang_signatures": _sig_on(n_compile_entries=2, n_translation_units=2),
        "counts": _counts(ents, rels),
    }
    assert validate_persisted_compiler_dependency_overlay(ents, rels, manifest)["ok"]
    assert validate_persisted_signature_overlay(ents, manifest)["ok"]
    report = audit_rows(ents, rels, obs, manifest)
    assert report["ok"] is False
    assert "compile_entry_count_mismatch" in _codes(report)


def test_changed_compiler_census_with_same_digest():
    ents, rels, obs = _base_rows()
    other = {
        "compiler_path": OTHER_PATH,
        "compiler_id": "gcc version test",
        "compiler_version": "14.0.0",
    }
    manifest = {
        **_all_off(),
        "compiler_dependencies": _deps_on(DIGEST),
        "compiler_includes": _incs_on(
            DIGEST,
            compiler_path=other["compiler_path"],
            compiler_id=other["compiler_id"],
            compiler_version=other["compiler_version"],
            compilers=[other],
        ),
    }
    assert validate_persisted_compiler_dependency_overlay(ents, rels, manifest)["ok"]
    assert validate_persisted_compiler_include_overlay(ents, rels, manifest)["ok"]
    report = audit_rows(ents, rels, obs, manifest)
    assert report["ok"] is False
    codes = _codes(report)
    assert "compiler_census_mismatch" in codes
    assert "compiler_shortcut_mismatch" in codes


def test_dependency_include_tu_disagreement():
    ents, rels, obs = _base_rows()
    extra = {
        "id": "ent:file:other",
        "title": "other:other.c",
        "type": "file",
        "source_file": "/tmp/pkg/other.c",
    }
    ents = ents + [extra]
    manifest = {
        **_all_off(),
        "compiler_dependencies": _deps_on(
            n_translation_units=1, translation_unit_titles=["main:main.c"]
        ),
        "compiler_includes": _incs_on(
            n_translation_units=1, translation_unit_titles=["other:other.c"]
        ),
        "counts": _counts(ents, rels),
    }
    assert validate_persisted_compiler_dependency_overlay(ents, rels, manifest)["ok"]
    assert validate_persisted_compiler_include_overlay(ents, rels, manifest)["ok"]
    report = audit_rows(ents, rels, obs, manifest)
    assert report["ok"] is False
    assert "translation_unit_census_mismatch" in _codes(report)


def test_off_blocks_are_ignored_during_comparison():
    ents, rels, obs = _base_rows()
    manifest = {
        **_all_off(),
        "compiler_dependencies": _deps_on(DIGEST),
        "compiler_includes": inc_off(),
    }
    report = audit_rows(ents, rels, obs, manifest)
    assert report["ok"] is True, report["anomalies"]
    assert report["census"]["enabled"] == ["compiler_dependencies"]
    assert "compiler_includes" in report["census"]["off"]


def test_preprocessor_builtins_identity_is_ignored():
    ents, rels, obs = _base_rows()
    for row in (*ents, *rels):
        row["preprocessor_dependent"] = False
        row["preprocessor_reasons"] = []
        row["preprocessor_eval_mode"] = "compiler_builtins"
        row["preprocessor_macro_seed_digest"] = "c" * 64
        row["preprocessor_branches"] = []
    manifest = {
        **_all_enabled(DIGEST),
        "preprocessor_liveness": {
            "eval_mode": "compiler_builtins",
            "compiler_path": OTHER_PATH,
            "compiler_id": "gcc version test",
            "compiler_version": "14.0.0",
            "macro_seed_digest": "c" * 64,
            "n_compiler_builtins": 12,
            "n_include_macros": 0,
            "host_independent": False,
        },
        "counts": {**_counts(ents, rels), "call_observations": 0},
    }
    liveness = validate_persisted_preprocessor_liveness(ents, rels, [], manifest)
    assert liveness["ok"] is True, liveness["anomalies"]
    report = audit_rows(ents, rels, [], manifest)
    assert report["ok"] is True, report["anomalies"]
    assert report["status"] == "coherent"
    assert report["components"]["preprocessor_liveness"]["status"] == "compiler_builtins"
    assert report["shared"]["compiler_path"] == COMPILER_PATH


def test_malformed_block_is_delegated_to_component_validator():
    ents, rels, obs = _base_rows()
    manifest = {**_all_off(), "compiler_dependencies": None}
    report = audit_rows(ents, rels, obs, manifest)
    assert report["ok"] is False
    assert "component_integrity" in _codes(report)
    assert report["components"]["compiler_dependencies"]["ok"] is False

    # Liveness stays outside provenance equality, but remains an integrity
    # component of the aggregate audit and therefore cannot fail silently.
    bad_liveness = {**_all_off(), "preprocessor_liveness": None}
    liveness_report = audit_rows(ents, rels, obs, bad_liveness)
    assert liveness_report["ok"] is False
    assert liveness_report["status"] == "invalid"
    assert "preprocessor_liveness" in liveness_report["component_failures"]
    assert "component_integrity" in _codes(liveness_report)


def test_deterministic_ordering_and_truncation():
    ents, rels, obs = _base_rows()
    manifest = {**_all_enabled(DIGEST), "counts": _counts(ents, rels)}
    first = audit_to_json(audit_rows(ents, rels, obs, manifest))
    second = audit_to_json(
        audit_rows(
            copy.deepcopy(ents),
            copy.deepcopy(rels),
            None,
            copy.deepcopy(manifest),
        )
    )
    assert first == second
    parsed = json.loads(first)
    assert list(parsed.keys()) == sorted(parsed.keys())
    assert parsed["audit_mode"] == AUDIT_MODE

    hybrid = {
        **_all_off(),
        "compiler_dependencies": _deps_on(DIGEST),
        "compiler_includes": _incs_on(OTHER_DIGEST),
        "clang_signatures": _sig_on("c" * 64),
        "counts": _counts(ents, rels),
    }
    truncated = audit_rows(ents, rels, obs, hybrid, max_anomaly_samples=1)
    assert truncated["ok"] is False
    assert truncated["n_anomalies"] > 1
    assert truncated["n_anomaly_samples"] == 1
    assert truncated["anomalies_truncated"] is True


def test_dataframe_and_optional_observations(tmp_path: Path):
    ents, rels, _obs = _base_rows()
    manifest = {**_all_off(), "counts": _counts(ents, rels)}
    report = audit_rows(pd.DataFrame(ents), pd.DataFrame(rels), None, manifest)
    assert report["ok"] is True
    assert report["status"] == "off"


def _write_tables(snap: Path, ents, rels, obs, manifest) -> None:
    snap.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(ents).to_parquet(snap / "entities.parquet")
    pd.DataFrame(rels).to_parquet(snap / "relationships.parquet")
    pd.DataFrame([{"id": "t1", "title": "a"}]).to_parquet(snap / "text_units.parquet")
    if obs is not None:
        pd.DataFrame(obs).to_parquet(snap / "call_observations.parquet")
    (snap / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )


def _publish(tmp_path: Path, ents, rels, obs, manifest, *, name: str = "g") -> Path:
    graph = tmp_path / name
    _write_tables(graph / "snapshots" / "s1", ents, rels, obs, manifest)
    (graph / "current").write_text("s1", encoding="utf-8")
    return graph


def test_unsafe_snapshot_and_output_containment(tmp_path: Path, capsys):
    ents, rels, obs = _base_rows()
    graph = _publish(tmp_path, ents, rels, None, _all_off())
    assert audit_main(["--graph", str(graph), "--snapshot", "../s1"]) == 2
    capsys.readouterr()
    forbidden = graph / "snapshots" / "s1" / "coherence.json"
    assert audit_main(["--graph", str(graph), "--output", str(forbidden)]) == 2
    assert "outside the audited graph root" in capsys.readouterr().err
    assert not forbidden.exists()
    alias = tmp_path / "alias"
    alias.symlink_to(graph)
    via = alias / "via.json"
    assert audit_main(["--graph", str(graph), "--output", str(via)]) == 2
    assert not via.exists()


def test_read_only_fingerprint_and_cli(tmp_path: Path, capsys, monkeypatch):
    ents, rels, obs = _base_rows()
    graph = _publish(tmp_path, ents, rels, [{"id": "obs:1"}], _all_off())
    before = {
        path.relative_to(graph).as_posix(): path.read_bytes()
        for path in sorted(graph.rglob("*"))
        if path.is_file()
    }
    report = audit_graph_root(graph)
    after = {
        path.relative_to(graph).as_posix(): path.read_bytes()
        for path in sorted(graph.rglob("*"))
        if path.is_file()
    }
    assert report["ok"] is True
    assert after == before
    assert report["read_only_verification"]["verified"] is True
    assert "call_observations.parquet" in report["read_only_verification"]["inputs"]
    assert (
        read_only_fingerprint(graph, graph / "snapshots" / "s1")
        == report["read_only_verification"]["fingerprint"]
    )
    out = tmp_path / "report.json"
    assert audit_main(["--graph", str(graph), "--output", str(out), "--json"]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["ok"] is True
    text = format_report(report)
    assert "RESULT: PASS" in text
    capsys.readouterr()

    import c_overlay_coherence_graph_audit as audit_module  # type: ignore

    original_fingerprint = audit_module.read_only_fingerprint
    calls = 0

    def drifting_fingerprint(graph_root, snap_dir):
        nonlocal calls
        calls += 1
        fingerprint = original_fingerprint(graph_root, snap_dir)
        if calls == 2:
            fingerprint["manifest.json"] = "0" * 64
        return fingerprint

    monkeypatch.setattr(
        audit_module, "read_only_fingerprint", drifting_fingerprint
    )
    drift = audit_module.audit_graph_root(graph, max_anomaly_samples=0)
    assert drift["ok"] is False
    assert drift["classification"] == "invalid"
    assert drift["n_anomalies"] == 1
    assert drift["n_anomaly_samples"] == 0
    assert drift["anomalies_truncated"] is True


def test_health_c_and_non_c_and_prior_keys():
    from published_graph_health import (  # type: ignore
        _call_integrity,
        _compiler_dependency_integrity,
        _compiler_include_integrity,
        _overlay_coherence_integrity,
        _preprocessor_liveness_integrity,
        _signature_integrity,
        _type_integrity,
        _type_shape_integrity,
        _type_use_integrity,
    )

    ents, rels, obs = _base_rows()
    published = {"entities": ents, "relationships": rels}
    manifest = _all_off()
    result = _overlay_coherence_integrity(published, manifest, indexer="c")
    assert result is not None and result["ok"] is True
    assert result["status"] == "off"
    assert _overlay_coherence_integrity(published, manifest, indexer="python") is None
    assert _compiler_dependency_integrity(published, manifest, indexer="c")["ok"]
    assert _compiler_include_integrity(published, manifest, indexer="c")["ok"]
    assert _type_use_integrity(published, manifest, indexer="c")["ok"]
    assert _type_shape_integrity({"entities": []}, {}, indexer="c")["ok"]
    assert _type_integrity({"entities": []}, {}, indexer="c")["ok"]
    assert _signature_integrity({"entities": []}, {}, indexer="c")["ok"]
    assert _call_integrity({"relationships": []}, {}, indexer="c")["ok"]
    assert _preprocessor_liveness_integrity(published, {}, indexer="c")["ok"]


def test_mixed_valid_fixture_keeps_component_validators_passing():
    ents, rels, obs = _base_rows()
    manifest = {**_all_enabled(), "counts": _counts(ents, rels)}
    report = validate_persisted_c_overlay_coherence(ents, rels, obs, manifest)
    assert report["ok"] is True, report["anomalies"]
    assert validate_persisted_compiler_dependency_overlay(ents, rels, manifest)["ok"]
    assert validate_persisted_compiler_include_overlay(ents, rels, manifest)["ok"]
    assert validate_persisted_call_overlay(rels, manifest)["ok"]
    assert validate_persisted_signature_overlay(ents, manifest)["ok"]
    assert validate_persisted_type_overlay(ents, manifest)["ok"]
    assert validate_persisted_type_use_overlay(ents, rels, manifest)["ok"]
    assert validate_persisted_type_shape_overlay(ents, manifest)["ok"]
    assert validate_persisted_preprocessor_liveness(ents, rels, obs, manifest)["ok"]


def test_validator_does_not_invoke_compiler_or_sources(monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("coherence audit must not invoke analysis or a compiler")

    import c_clang_ast_capture as cap  # type: ignore
    import c_compiler_common as common  # type: ignore
    import c_compiler_facts as deps  # type: ignore
    import c_compiler_includes as incs  # type: ignore
    import c_preprocessor as pp  # type: ignore

    monkeypatch.setattr(pp, "analyze_package", boom)
    monkeypatch.setattr(pp, "annotate_byog", boom)
    monkeypatch.setattr(deps, "collect_translation_unit_dependencies", boom)
    monkeypatch.setattr(incs, "collect_configured_direct_includes", boom)
    monkeypatch.setattr(common, "load_compile_entries", boom)
    monkeypatch.setattr(cap, "capture_clang_ast_package", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    ents, rels, obs = _base_rows()
    report = validate_persisted_c_overlay_coherence(ents, rels, obs, _all_off())
    assert report["ok"] is True, report["anomalies"]


def _index(package: Path, graph: Path) -> None:
    from index_c import main as index_c_main  # type: ignore

    index_c_main(
        package=package,
        graph=graph,
        keep_snapshots=2,
        compiler_builtins=False,
        compiler_dependencies=False,
        compiler_includes=False,
        clang_signatures=False,
        clang_calls=False,
        clang_types=False,
        clang_type_uses=False,
        clang_type_shapes=False,
        allow_toolchain_drift=False,
    )


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_default_off_disposable(tmp_path: Path, monkeypatch):
    pkg = ROOT / "examples" / "inih"
    graph = tmp_path / "byog_inih_off"
    _index(pkg, graph)

    def boom(*_args, **_kwargs):
        raise AssertionError("read-only audit must not invoke a compiler")

    import c_preprocessor as pp  # type: ignore

    monkeypatch.setattr(pp, "analyze_package", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    report = audit_graph_root(graph)
    assert report["ok"] is True, report["anomalies"]
    assert report["status"] == "off"
    assert report["read_only_verification"]["verified"] is True


def test_published_byog_roots_are_off_and_read_only():
    for name, pointer in (
        ("byog_cjson", "20260808-153310-5b68f044"),
        ("byog_inih", "20260808-162204-a897f1e3"),
    ):
        graph = ROOT / name
        assert (graph / "current").read_text(encoding="utf-8").strip() == pointer
        before = {
            path.relative_to(graph).as_posix(): path.read_bytes()
            for path in sorted(graph.rglob("*"))
            if path.is_file()
        }
        report = audit_graph_root(graph)
        after = {
            path.relative_to(graph).as_posix(): path.read_bytes()
            for path in sorted(graph.rglob("*"))
            if path.is_file()
        }
        assert after == before, name
        assert report["ok"] is True, (name, report["anomalies"])
        assert report["status"] == "off", name
        assert report["read_only_verification"]["verified"] is True
