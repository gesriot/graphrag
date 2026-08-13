"""Read-only integrity audit for persisted C preprocessor-liveness stamps.

Pure synthetic/corruption tests always run. Live disposable-graph tests skip
when no C compiler is present. Nothing here writes a published byog_* root.

Run:
  uv run python -m pytest examples/cjson/tests/test_c_preprocessor_liveness_graph_audit.py -q
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
    build_disabled_provenance as call_off,
    validate_persisted_call_overlay,
)
from c_clang_signatures import (  # type: ignore
    build_disabled_provenance as signature_off,
    validate_persisted_signature_overlay,
)
from c_clang_type_shapes import (  # type: ignore
    build_disabled_provenance as shape_off,
    validate_persisted_type_shape_overlay,
)
from c_clang_type_uses import (  # type: ignore
    build_disabled_provenance as type_use_off,
    validate_persisted_type_use_overlay,
)
from c_clang_types import (  # type: ignore
    build_disabled_provenance as type_off,
    validate_persisted_type_overlay,
)
from c_compiler_facts import (  # type: ignore
    build_disabled_provenance as dep_off,
    make_depends_on_relationship,
    validate_persisted_compiler_dependency_overlay,
)
from c_compiler_includes import (  # type: ignore
    build_disabled_provenance as inc_off,
    make_includes_relationship,
    validate_persisted_compiler_include_overlay,
)
from c_preprocessor import (  # type: ignore
    find_c_compiler,
    validate_persisted_preprocessor_liveness,
)
from c_preprocessor_liveness_graph_audit import (  # type: ignore
    AUDIT_MODE,
    audit_graph_root,
    audit_rows,
    audit_to_json,
    format_report,
    main as audit_main,
    read_only_fingerprint,
)

DIGEST = "a" * 64
COMPILER_PATH = "/usr/bin/clang"

_STAMP_FIELDS = (
    "preprocessor_dependent",
    "preprocessor_reasons",
    "preprocessor_eval_mode",
    "preprocessor_macro_seed_digest",
    "preprocessor_branches",
)


def _cc():
    return find_c_compiler()


def _codes(report) -> set:
    return {a.get("code") for a in report.get("anomalies") or []}


def _no_compiler_block(**overrides) -> dict:
    block = {
        "eval_mode": "no_compiler",
        "compiler_path": None,
        "compiler_id": None,
        "compiler_version": None,
        "macro_seed_digest": DIGEST,
        "n_compiler_builtins": 0,
        "n_include_macros": 0,
        "host_independent": True,
    }
    block.update(overrides)
    return block


def _builtins_block(**overrides) -> dict:
    block = {
        "eval_mode": "compiler_builtins",
        "compiler_path": COMPILER_PATH,
        "compiler_id": "Apple clang version test",
        "compiler_version": "17.0.0",
        "macro_seed_digest": DIGEST,
        "n_compiler_builtins": 12,
        "n_include_macros": 3,
        "host_independent": False,
    }
    block.update(overrides)
    return block


def _branch(**overrides) -> dict:
    row = {
        "kind": "ifdef",
        "condition": "__GNUC__",
        "start_line": 10,
        "end_line": 20,
        "liveness": "unknown",
        "basis": "platform macro __GNUC__",
    }
    row.update(overrides)
    return row


def _stamp(
    item: dict,
    *,
    dependent: bool = False,
    reasons: list | None = None,
    branches: list | None = None,
    mode: str = "no_compiler",
    digest: str = DIGEST,
) -> dict:
    item["preprocessor_dependent"] = dependent
    item["preprocessor_reasons"] = list(reasons or [])
    item["preprocessor_eval_mode"] = mode
    item["preprocessor_macro_seed_digest"] = digest
    item["preprocessor_branches"] = list(branches or [])
    return item


def _entity(**kwargs) -> dict:
    return _stamp(
        {
            "id": "ent:file:main",
            "title": "main:main.c",
            "type": "file",
            "source_file": "/tmp/pkg/main.c",
        },
        **kwargs,
    )


def _rel(**kwargs) -> dict:
    return _stamp(
        {
            "id": "rel:contains:1",
            "source": "main:main.c",
            "target": "main:main",
            "type": "contains",
            "description": "file contains function",
            "extractor": "tree-sitter-c",
        },
        **kwargs,
    )


def _obs(**kwargs) -> dict:
    return _stamp(
        {
            "id": "obs:1",
            "source": "main:main",
            "display_target": "helper",
            "source_file": "/tmp/pkg/main.c",
            "span": "3:0",
        },
        **kwargs,
    )


def _dependent_kwargs(mode: str = "no_compiler") -> dict:
    return {
        "dependent": True,
        "reasons": [
            "inside_conditional:ifdef(__GNUC__)",
            "branch_unknown:ifdef(__GNUC__)",
        ],
        "branches": [_branch()],
        "mode": mode,
    }


def _valid_graph(*, mode: str = "no_compiler", extra_rel=None, extra_ent=None):
    kwargs = {} if mode == "no_compiler" else {"mode": mode}
    ents = [_entity(**kwargs)]
    if extra_ent is not None:
        ents.append(extra_ent)
    rels = [_rel(**kwargs)]
    if extra_rel is not None:
        rels.append(extra_rel)
    obs = [_obs(**kwargs)]
    block = _no_compiler_block() if mode == "no_compiler" else _builtins_block()
    manifest = {
        "preprocessor_liveness": block,
        "counts": {
            "entities": len(ents),
            "relationships": len(rels),
            "call_observations": len(obs),
        },
    }
    return ents, rels, obs, manifest


def test_legacy_absent_passes():
    ents = [{"id": "e1", "title": "main:main.c", "type": "file"}]
    rels = [{"id": "r1", "source": "a", "target": "b", "type": "contains"}]
    report = audit_rows(ents, rels, None, {})
    assert report["ok"] is True
    assert report["status"] == "legacy_absent"
    assert report["classification"] == "legacy_absent"
    assert audit_rows(ents, rels, None, None)["status"] == "legacy_absent"


def test_valid_no_compiler_manifest_and_stamps():
    ents, rels, obs, manifest = _valid_graph()
    report = audit_rows(ents, rels, obs, manifest)
    assert report["ok"] is True, report["anomalies"]
    assert report["status"] == "no_compiler"
    assert report["eval_mode"] == "no_compiler"
    assert report["audit_mode"] == AUDIT_MODE
    assert report["n_stamped_rows"] == 3
    assert report["observations_present"] is True


def test_valid_compiler_builtins_manifest_and_stamps():
    ents, rels, obs, manifest = _valid_graph(mode="compiler_builtins")
    dep = _entity(**_dependent_kwargs("compiler_builtins"))
    dep["id"] = "ent:fn:main"
    dep["title"] = "main:main"
    dep["type"] = "function"
    ents.append(dep)
    manifest["counts"]["entities"] = 2
    report = audit_rows(ents, rels, obs, manifest)
    assert report["ok"] is True, report["anomalies"]
    assert report["status"] == "compiler_builtins"


def test_malformed_null_partial_extra_manifest():
    ents, rels, obs, manifest = _valid_graph()
    for value in (None, [], "no_compiler", 0):
        report = audit_rows(ents, rels, obs, {"preprocessor_liveness": value})
        assert report["ok"] is False, value
        assert "invalid_liveness_block" in _codes(report)

    missing = copy.deepcopy(manifest)
    missing["preprocessor_liveness"].pop("macro_seed_digest")
    assert "missing_manifest_key" in _codes(audit_rows(ents, rels, obs, missing))

    extra = copy.deepcopy(manifest)
    extra["preprocessor_liveness"]["unexpected"] = True
    assert "extra_manifest_key" in _codes(audit_rows(ents, rels, obs, extra))


def test_mode_and_host_independent_contradictions():
    ents, rels, obs, manifest = _valid_graph()
    broken = copy.deepcopy(manifest)
    broken["preprocessor_liveness"]["host_independent"] = False
    assert "manifest_mode_mismatch" in _codes(audit_rows(ents, rels, obs, broken))

    builtins = copy.deepcopy(_valid_graph(mode="compiler_builtins")[3])
    builtins["preprocessor_liveness"]["host_independent"] = True
    ents_b, rels_b, obs_b, _ = _valid_graph(mode="compiler_builtins")
    assert "manifest_mode_mismatch" in _codes(
        audit_rows(ents_b, rels_b, obs_b, builtins)
    )


def test_nullable_compiler_field_rules_and_canonical_path():
    ents, rels, obs, manifest = _valid_graph()
    nonempty = copy.deepcopy(manifest)
    nonempty["preprocessor_liveness"]["compiler_path"] = COMPILER_PATH
    assert "compiler_mismatch" in _codes(audit_rows(ents, rels, obs, nonempty))

    ents_b, rels_b, obs_b, man_b = _valid_graph(mode="compiler_builtins")
    relative = copy.deepcopy(man_b)
    relative["preprocessor_liveness"]["compiler_path"] = "clang"
    assert "compiler_mismatch" in _codes(audit_rows(ents_b, rels_b, obs_b, relative))

    null_ok = copy.deepcopy(man_b)
    null_ok["preprocessor_liveness"]["compiler_id"] = None
    null_ok["preprocessor_liveness"]["compiler_version"] = None
    assert audit_rows(ents_b, rels_b, obs_b, null_ok)["ok"] is True

    space = copy.deepcopy(man_b)
    space["preprocessor_liveness"]["compiler_id"] = "  spaced  "
    assert "compiler_mismatch" in _codes(audit_rows(ents_b, rels_b, obs_b, space))


def test_strict_digest_and_count_types():
    ents, rels, obs, manifest = _valid_graph()
    for value in ("ABC" + "a" * 61, "a" * 63, "", None, 1):
        broken = copy.deepcopy(manifest)
        broken["preprocessor_liveness"]["macro_seed_digest"] = value
        assert "digest_mismatch" in _codes(audit_rows(ents, rels, obs, broken)), value

    for field, value in (
        ("n_compiler_builtins", False),
        ("n_include_macros", False),
        ("n_compiler_builtins", 0.0),
        ("n_compiler_builtins", 3),
    ):
        broken = copy.deepcopy(manifest)
        broken["preprocessor_liveness"][field] = value
        report = audit_rows(ents, rels, obs, broken)
        assert report["ok"] is False, (field, value)
        assert "manifest_count_mismatch" in _codes(report)

    ents_b, rels_b, obs_b, man_b = _valid_graph(mode="compiler_builtins")
    zero = copy.deepcopy(man_b)
    zero["preprocessor_liveness"]["n_compiler_builtins"] = 0
    assert "manifest_count_mismatch" in _codes(
        audit_rows(ents_b, rels_b, obs_b, zero)
    )


def test_manifest_absent_with_material_stamp_evidence_fails():
    ents, rels, obs, _manifest = _valid_graph()
    report = audit_rows(ents, rels, obs, {})
    assert report["ok"] is False
    assert "legacy_block_missing_with_fields" in _codes(report)

    for field, value in (
        ("preprocessor_dependent", "not-a-boolean"),
        ("preprocessor_reasons", "not-a-list"),
        ("preprocessor_eval_mode", 1),
        ("preprocessor_macro_seed_digest", ""),
        ("preprocessor_branches", {"not": "a list"}),
    ):
        corrupt = [{"id": "e1", field: value}]
        corrupt_report = audit_rows(corrupt, [], None, {})
        assert corrupt_report["ok"] is False, (field, value)
        assert "legacy_block_missing_with_fields" in _codes(corrupt_report)


def test_missing_one_of_every_required_row_field_fails():
    ents, rels, obs, manifest = _valid_graph()
    for field in _STAMP_FIELDS:
        rows = copy.deepcopy(ents)
        rows[0].pop(field)
        report = audit_rows(rows, rels, obs, manifest)
        assert report["ok"] is False, field
        assert "partial_liveness_payload" in _codes(report), field


def test_mixed_malformed_parquet_list_forms():
    ents, rels, obs, manifest = _valid_graph()
    import numpy as np

    rows = copy.deepcopy(ents)
    rows[0]["preprocessor_reasons"] = np.array([], dtype=object)
    rows[0]["preprocessor_branches"] = np.array([], dtype=object)
    assert audit_rows(rows, rels, obs, manifest)["ok"] is True

    for field, value in (
        ("preprocessor_reasons", "inside_conditional:ifdef(__GNUC__)"),
        ("preprocessor_reasons", None),
        ("preprocessor_branches", None),
    ):
        bad = copy.deepcopy(ents)
        bad[0][field] = value
        assert "liveness_field_type" in _codes(
            audit_rows(bad, rels, obs, manifest)
        )


def test_dependent_reasons_boolean_equivalence():
    ents, rels, obs, manifest = _valid_graph()
    rows = copy.deepcopy(ents)
    rows[0]["preprocessor_dependent"] = True
    assert "dependent_reasons_mismatch" in _codes(
        audit_rows(rows, rels, obs, manifest)
    )


def test_duplicate_empty_nonstring_unknown_reasons():
    ents, rels, obs, manifest = _valid_graph()
    for value in (
        [""],
        ["  spaced  "],
        [1],
        ["not_a_family"],
        [
            "inside_conditional:ifdef(__GNUC__)",
            "inside_conditional:ifdef(__GNUC__)",
        ],
    ):
        rows = copy.deepcopy(ents)
        rows[0]["preprocessor_dependent"] = True
        rows[0]["preprocessor_reasons"] = value
        report = audit_rows(rows, rels, obs, manifest)
        assert report["ok"] is False, value
        assert "reason_contract" in _codes(report), value


def test_branch_keys_kind_liveness_coordinates_and_duplicates():
    ents, rels, obs, manifest = _valid_graph(**{})
    base = copy.deepcopy(ents)
    stamped = _entity(**_dependent_kwargs())
    stamped["id"] = base[0]["id"]
    for mutation, _note in (
        ({"kind": "switch"}, "kind"),
        ({"liveness": "maybe"}, "liveness"),
        ({"start_line": 0}, "start"),
        ({"start_line": True}, "bool"),
        ({"start_line": 1.0}, "float"),
        ({"start_line": 30, "end_line": 20}, "order"),
        ({"condition": 1}, "condition type"),
        ({"basis": "  x  "}, "basis"),
    ):
        rows = [copy.deepcopy(stamped)]
        rows[0]["preprocessor_branches"][0].update(mutation)
        report = audit_rows(rows, rels, obs, manifest)
        assert report["ok"] is False, _note
        assert "branch_contract" in _codes(report), _note

    extra = copy.deepcopy(stamped)
    extra["preprocessor_branches"][0]["unexpected"] = True
    assert "branch_contract" in _codes(audit_rows([extra], rels, obs, manifest))

    dup = copy.deepcopy(stamped)
    dup["preprocessor_branches"] = [_branch(), _branch()]
    assert "branch_contract" in _codes(audit_rows([dup], rels, obs, manifest))


def test_branch_reason_disagreement_and_nonempty_without_reasons():
    ents, rels, obs, manifest = _valid_graph()
    rows = [_entity(**_dependent_kwargs())]
    rows[0]["preprocessor_reasons"] = ["inside_conditional:ifdef(__GNUC__)"]
    rows[0]["preprocessor_dependent"] = True
    assert "branch_contract" in _codes(audit_rows(rows, rels, obs, manifest))

    empty = [_entity()]
    empty[0]["preprocessor_branches"] = [_branch()]
    assert "branch_contract" in _codes(audit_rows(empty, rels, obs, manifest))

    for orphan_reason in (
        "branch_unknown:ifdef(__GNUC__)",
        "inside_conditional:ifdef(__GNUC__)",
    ):
        orphan = [_entity(dependent=True, reasons=[orphan_reason])]
        assert "branch_contract" in _codes(
            audit_rows(orphan, rels, obs, manifest)
        )

    no_inside = [_entity(**_dependent_kwargs())]
    no_inside[0]["preprocessor_reasons"] = [
        "branch_unknown:ifdef(__GNUC__)"
    ]
    assert "branch_contract" in _codes(
        audit_rows(no_inside, rels, obs, manifest)
    )


def test_manifest_mode_and_digest_disagreement_across_tables():
    ents, rels, obs, manifest = _valid_graph()
    for table, rows in (
        ("entities", ents),
        ("relationships", rels),
        ("call_observations", obs),
    ):
        copy_ents, copy_rels, copy_obs = (
            copy.deepcopy(ents),
            copy.deepcopy(rels),
            copy.deepcopy(obs),
        )
        target = {
            "entities": copy_ents,
            "relationships": copy_rels,
            "call_observations": copy_obs,
        }[table]
        target[0]["preprocessor_eval_mode"] = "compiler_builtins"
        report = audit_rows(copy_ents, copy_rels, copy_obs, manifest)
        assert report["ok"] is False, table
        assert "stamp_manifest_disagreement" in _codes(report), table
        target[0]["preprocessor_eval_mode"] = "no_compiler"
        target[0]["preprocessor_macro_seed_digest"] = "b" * 64
        report = audit_rows(copy_ents, copy_rels, copy_obs, manifest)
        assert "stamp_manifest_disagreement" in _codes(report), table


def test_optional_observation_file_and_explicit_null_counts():
    ents, rels, obs, manifest = _valid_graph()
    absent = copy.deepcopy(manifest)
    absent["counts"]["call_observations"] = 0
    assert audit_rows(ents, rels, None, absent)["ok"] is True

    missing_file = copy.deepcopy(manifest)
    missing_file["counts"]["call_observations"] = 4
    report = audit_rows(ents, rels, None, missing_file)
    assert report["ok"] is False
    assert "observation_file_mismatch" in _codes(report)

    null_counts = copy.deepcopy(manifest)
    null_counts["counts"]["entities"] = None
    assert "manifest_count_mismatch" in _codes(
        audit_rows(ents, rels, obs, null_counts)
    )

    present_corrupt = copy.deepcopy(manifest)
    present_corrupt["counts"]["call_observations"] = False
    report = audit_rows(ents, rels, obs, present_corrupt)
    assert report["ok"] is False
    assert "manifest_count_mismatch" in _codes(report)

    for missing_counts in (
        {"preprocessor_liveness": copy.deepcopy(manifest["preprocessor_liveness"])},
        {
            "preprocessor_liveness": copy.deepcopy(
                manifest["preprocessor_liveness"]
            ),
            "counts": {"entities": 1, "relationships": 1},
        },
    ):
        report = audit_rows(ents, rels, obs, missing_counts)
        assert report["ok"] is False
        assert "manifest_count_mismatch" in _codes(report)


def test_valid_post_stamp_exemptions_and_partial_markers_not_exempted():
    ents, rels, obs, manifest = _valid_graph()
    dep = make_depends_on_relationship(
        source_title="main:main.c",
        target_title="direct:direct.h",
        human_readable_id=9,
        compiler_path=COMPILER_PATH,
        compiler_id=None,
        compile_commands_digest=DIGEST,
        source_file="/tmp/pkg/main.c",
    )
    inc = make_includes_relationship(
        source_title="main:main.c",
        target_title="direct:direct.h",
        human_readable_id=10,
        compiler_path=COMPILER_PATH,
        compiler_id=None,
        compile_commands_digest=DIGEST,
        source_file="/tmp/pkg/main.c",
    )
    uses = {
        "id": "rel:uses_type:1",
        "source": "main:main",
        "target": "direct:item",
        "type": "uses_type",
        "fact_kind": "configured_type_use",
        "extractor": "clang-ast-json",
        "clang_type_use_status": "matched",
        "clang_type_use_fact_kind": "configured_type_use",
        "clang_type_use_extractor": "clang-ast-json",
    }
    mixed = copy.deepcopy(rels) + [dep, inc, uses]
    manifest["counts"]["relationships"] = 4
    report = audit_rows(ents, mixed, obs, manifest)
    assert report["ok"] is True, report["anomalies"]

    partial = copy.deepcopy(rels)
    partial.append(
        {
            "id": "rel:depends_on:partial",
            "source": "main:main.c",
            "target": "direct:direct.h",
            "type": "depends_on",
        }
    )
    manifest["counts"]["relationships"] = 2
    report_partial = audit_rows(ents, partial, obs, manifest)
    assert report_partial["ok"] is False
    assert "partial_liveness_payload" in _codes(report_partial)


def test_mixed_graph_keeps_all_seven_overlay_validators_valid():
    ents, rels, obs, manifest = _valid_graph()
    manifest.update(
        {
            "compiler_dependencies": dep_off(),
            "compiler_includes": inc_off(),
            "clang_calls": call_off(),
            "clang_signatures": signature_off(),
            "clang_types": type_off(),
            "clang_type_uses": type_use_off(),
            "clang_type_shapes": shape_off(),
        }
    )
    assert validate_persisted_preprocessor_liveness(
        ents, rels, obs, manifest
    )["ok"] is True
    assert validate_persisted_compiler_dependency_overlay(
        ents, rels, manifest
    )["ok"] is True
    assert validate_persisted_compiler_include_overlay(
        ents, rels, manifest
    )["ok"] is True
    assert validate_persisted_call_overlay(rels, manifest)["ok"] is True
    assert validate_persisted_signature_overlay(ents, manifest)["ok"] is True
    assert validate_persisted_type_overlay(ents, manifest)["ok"] is True
    assert validate_persisted_type_use_overlay(ents, rels, manifest)["ok"] is True
    assert validate_persisted_type_shape_overlay(ents, manifest)["ok"] is True


def test_deterministic_output_and_truncation():
    ents, rels, obs, manifest = _valid_graph()
    first = audit_to_json(audit_rows(ents, rels, obs, manifest))
    second = audit_to_json(
        audit_rows(
            copy.deepcopy(ents),
            copy.deepcopy(rels),
            copy.deepcopy(obs),
            copy.deepcopy(manifest),
        )
    )
    assert first == second
    parsed = json.loads(first)
    assert list(parsed.keys()) == sorted(parsed.keys())

    broken = copy.deepcopy(ents)
    for field in _STAMP_FIELDS:
        if field in broken[0]:
            broken[0][field] = 0
    truncated = audit_rows(broken, rels, obs, manifest, max_anomaly_samples=1)
    assert truncated["ok"] is False
    assert truncated["n_anomalies"] > 1
    assert truncated["n_anomaly_samples"] == 1
    assert truncated["anomalies_truncated"] is True


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


def test_flat_current_and_explicit_snapshot_resolution(tmp_path: Path):
    ents, rels, obs, manifest = _valid_graph()
    current = _publish(tmp_path, ents, rels, obs, manifest, name="cur")
    assert audit_graph_root(current)["ok"] is True
    assert audit_graph_root(current, snapshot="s1")["ok"] is True

    flat = tmp_path / "flat"
    _write_tables(flat, ents, rels, obs, manifest)
    report_flat = audit_graph_root(flat)
    assert report_flat["ok"] is True, report_flat["anomalies"]


def test_malformed_parquet_manifest_duplicate_keys_and_nan(tmp_path: Path, capsys):
    ents, rels, obs, manifest = _valid_graph()
    graph = _publish(tmp_path, ents, rels, obs, manifest)
    (graph / "snapshots" / "s1" / "manifest.json").write_text(
        '{"preprocessor_liveness": NaN}', encoding="utf-8"
    )
    assert audit_main(["--graph", str(graph)]) == 2
    capsys.readouterr()

    graph2 = _publish(tmp_path, ents, rels, obs, manifest, name="g2")
    (graph2 / "snapshots" / "s1" / "manifest.json").write_text(
        '{"eval_mode": "no_compiler", "eval_mode": "compiler_builtins"}',
        encoding="utf-8",
    )
    assert audit_main(["--graph", str(graph2)]) == 2
    capsys.readouterr()

    graph3 = _publish(tmp_path, ents, rels, obs, manifest, name="g3")
    (graph3 / "snapshots" / "s1" / "relationships.parquet").write_bytes(b"not parquet")
    assert audit_main(["--graph", str(graph3)]) == 2


def test_unsafe_snapshot_ids_and_output_containment(tmp_path: Path, capsys):
    ents, rels, obs, manifest = _valid_graph()
    graph = _publish(tmp_path, ents, rels, obs, manifest)
    assert audit_main(["--graph", str(graph), "--snapshot", "../s1"]) == 2
    capsys.readouterr()
    assert audit_main(["--graph", str(graph), "--snapshot", ""]) == 2
    capsys.readouterr()

    forbidden = graph / "snapshots" / "s1" / "liveness-audit.json"
    assert audit_main(["--graph", str(graph), "--output", str(forbidden)]) == 2
    assert "outside the audited graph root" in capsys.readouterr().err
    assert not forbidden.exists()

    alias = tmp_path / "alias"
    alias.symlink_to(graph)
    via_link = alias / "via-symlink.json"
    assert audit_main(["--graph", str(graph), "--output", str(via_link)]) == 2
    assert not via_link.exists()


def test_read_only_fingerprint_includes_call_observations(tmp_path: Path, capsys):
    ents, rels, obs, manifest = _valid_graph()
    graph = _publish(tmp_path, ents, rels, obs, manifest)
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
    out_path = tmp_path / "report.json"
    assert audit_main(["--graph", str(graph), "--output", str(out_path), "--json"]) == 0
    assert json.loads(out_path.read_text(encoding="utf-8"))["ok"] is True
    capsys.readouterr()
    text = format_report(report)
    assert "RESULT: PASS" in text
    assert "read-only" in text


def test_health_c_and_non_c_and_prior_keys():
    from published_graph_health import (  # type: ignore
        _call_integrity,
        _compiler_dependency_integrity,
        _compiler_include_integrity,
        _preprocessor_liveness_integrity,
        _signature_integrity,
        _type_integrity,
        _type_shape_integrity,
        _type_use_integrity,
    )

    ents, rels, obs, manifest = _valid_graph()
    published = {
        "entities": ents,
        "relationships": rels,
        "call_observations": obs,
    }
    enabled = _preprocessor_liveness_integrity(published, manifest, indexer="c")
    assert enabled is not None and enabled["ok"] is True
    assert enabled["status"] == "no_compiler"
    assert (
        _preprocessor_liveness_integrity(published, manifest, indexer="python")
        is None
    )
    assert _compiler_dependency_integrity(published, {}, indexer="c")["ok"] is True
    assert _compiler_include_integrity(published, {}, indexer="c")["ok"] is True
    assert _type_use_integrity(published, {}, indexer="c")["ok"] is True
    assert _type_shape_integrity({"entities": []}, {}, indexer="c")["ok"] is True
    assert _type_integrity({"entities": []}, {}, indexer="c")["ok"] is True
    assert _signature_integrity({"entities": []}, {}, indexer="c")["ok"] is True
    assert _call_integrity({"relationships": []}, {}, indexer="c")["ok"] is True


def test_validator_does_not_reanalyse_or_invoke_compiler(monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("read-only audit must not reanalyse or invoke a compiler")

    import c_preprocessor as pp  # type: ignore

    monkeypatch.setattr(pp, "analyze_package", boom)
    monkeypatch.setattr(pp, "annotate_byog", boom)
    monkeypatch.setattr(pp, "reasons_for_span", boom)
    monkeypatch.setattr(pp, "branches_for_span", boom)
    monkeypatch.setattr(pp, "check_liveness_stamp_freshness", boom)
    monkeypatch.setattr(pp, "macro_seed_digest", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    ents, rels, obs, manifest = _valid_graph()
    report = validate_persisted_preprocessor_liveness(ents, rels, obs, manifest)
    assert report["ok"] is True, report["anomalies"]


def _fail_if_analysis_used(monkeypatch):
    import c_preprocessor as pp  # type: ignore

    def boom(*_args, **_kwargs):
        raise AssertionError("read-only audit must not reanalyse or invoke a compiler")

    monkeypatch.setattr(pp, "analyze_package", boom)
    monkeypatch.setattr(pp, "annotate_byog", boom)
    monkeypatch.setattr(pp, "reasons_for_span", boom)
    monkeypatch.setattr(pp, "branches_for_span", boom)
    monkeypatch.setattr(pp, "check_liveness_stamp_freshness", boom)
    monkeypatch.setattr(pp, "macro_seed_digest", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "check_output", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)


def _index(package: Path, graph: Path, *, builtins: bool) -> None:
    from index_c import main as index_c_main  # type: ignore

    index_c_main(
        package=package,
        graph=graph,
        keep_snapshots=2,
        compiler_builtins=builtins,
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
def test_live_no_compiler_and_builtins_disposable(tmp_path: Path, monkeypatch):
    for name in ("cjson", "inih"):
        pkg = ROOT / "examples" / name
        before = {p.name for p in pkg.iterdir()}
        off_graph = tmp_path / f"byog_{name}_nc"
        _index(pkg, off_graph, builtins=False)
        snap = (off_graph / "current").read_text(encoding="utf-8").strip()
        block = json.loads(
            (off_graph / "snapshots" / snap / "manifest.json").read_text(
                encoding="utf-8"
            )
        )["preprocessor_liveness"]
        monkeypatch.undo()
        _fail_if_analysis_used(monkeypatch)
        report = audit_graph_root(off_graph)
        assert report["ok"] is True, (name, report["anomalies"])
        assert report["status"] == "no_compiler"
        assert block["eval_mode"] == "no_compiler"
        assert block["host_independent"] is True
        assert len(block["macro_seed_digest"]) == 64
        assert block["n_compiler_builtins"] == 0
        assert report["read_only_verification"]["verified"] is True
        assert {p.name for p in pkg.iterdir()} == before
        monkeypatch.undo()

        on_graph = tmp_path / f"byog_{name}_cb"
        _index(pkg, on_graph, builtins=True)
        snap = (on_graph / "current").read_text(encoding="utf-8").strip()
        block = json.loads(
            (on_graph / "snapshots" / snap / "manifest.json").read_text(
                encoding="utf-8"
            )
        )["preprocessor_liveness"]
        _fail_if_analysis_used(monkeypatch)
        report = audit_graph_root(on_graph)
        assert report["ok"] is True, (name, report["anomalies"])
        assert report["status"] == "compiler_builtins"
        assert block["eval_mode"] == "compiler_builtins"
        assert block["host_independent"] is False
        assert block["n_compiler_builtins"] > 0
        assert report["read_only_verification"]["verified"] is True
        assert {p.name for p in pkg.iterdir()} == before
        for pattern in ("*.o", "*.ast", "*.d", "*.i", "*.ii", "*.pch"):
            assert not list(pkg.rglob(pattern))
        monkeypatch.undo()


def test_published_byog_roots_are_no_compiler_and_read_only():
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
        assert report["status"] == "no_compiler", name
        assert report["read_only_verification"]["verified"] is True
