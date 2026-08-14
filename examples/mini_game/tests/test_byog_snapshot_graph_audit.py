"""Language-independent read-only audit of the persisted BYOG snapshot envelope.

Applies to every BYOG indexer. Disposable graphs only; published byog_* roots
are opened read-only and compared byte-for-byte before and after.

Run:
  uv run python -m pytest examples/mini_game/tests/test_byog_snapshot_graph_audit.py -q
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

from byog_graph import publish_byog_snapshot  # type: ignore
from byog_snapshot_graph_audit import (  # type: ignore
    AUDIT_MODE,
    SnapshotGraphAuditError,
    audit_graph_root,
    audit_rows,
    audit_to_json,
    format_report,
    main as audit_main,
    read_only_fingerprint,
)
from byog_snapshot_integrity import (  # type: ignore
    REQUIRED_CORE_KEYS,
    validate_persisted_byog_snapshot,
)

C_INTEGRITY_KEYS = (
    "clang_type_use_integrity",
    "clang_type_shape_integrity",
    "clang_type_integrity",
    "clang_signature_integrity",
    "clang_call_integrity",
    "compiler_dependency_integrity",
    "compiler_include_integrity",
    "preprocessor_liveness_integrity",
    "c_overlay_coherence_integrity",
)


def _codes(report) -> list[str]:
    return [a.get("code") for a in report.get("anomalies") or []]


def _tiny_tables(*, n_obs: int = 1):
    ents = pd.DataFrame(
        [
            {
                "id": "ent:fn:demo.main",
                "title": "demo:main",
                "type": "function",
                "source_file": "demo.py",
            }
        ]
    )
    rels = pd.DataFrame(
        [
            {
                "id": "rel:contains:1",
                "source": "demo:demo.py",
                "target": "demo:main",
                "type": "contains",
            }
        ]
    )
    tus = pd.DataFrame(
        [{"id": "tu:1", "title": "demo.py", "source_file": "demo.py", "entity_id": "ent:fn:demo.main"}]
    )
    obs = None
    if n_obs:
        obs = pd.DataFrame(
            [{"id": f"obs:{i}", "caller": "demo:main", "callee": "len"} for i in range(n_obs)]
        )
    return ents, rels, tus, obs


def _publish(
    tmp_path: Path,
    *,
    name: str = "byog_demo",
    n_obs: int = 1,
    settings_text: str | None = "settings: true\n",
    extra_manifest: dict | None = None,
    source_root: Path | None = None,
) -> Path:
    ents, rels, tus, obs = _tiny_tables(n_obs=n_obs)
    graph = tmp_path / name
    publish_byog_snapshot(
        ents,
        rels,
        tus,
        graph,
        settings_text=settings_text,
        keep_last=2,
        source_root=source_root or (tmp_path / "src"),
        call_observations_df=obs,
        extra_manifest=extra_manifest,
    )
    return graph


def _snap_dir(graph: Path) -> Path:
    snap_id = (graph / "current").read_text(encoding="utf-8").strip()
    return graph / "snapshots" / snap_id


def _read_manifest(snap: Path) -> dict:
    return json.loads((snap / "manifest.json").read_text(encoding="utf-8"))


def _write_manifest(snap: Path, manifest: dict) -> None:
    (snap / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )


def _inventory(snap: Path) -> tuple[list[str], dict[str, int]]:
    present = []
    sizes = {}
    for path in sorted(snap.iterdir(), key=lambda item: item.name):
        if path.is_symlink() or not path.is_file():
            continue
        present.append(path.name)
        sizes[path.name] = path.stat().st_size
    return present, sizes


def _load_tables(snap: Path):
    ents = pd.read_parquet(snap / "entities.parquet")
    rels = pd.read_parquet(snap / "relationships.parquet")
    tus = pd.read_parquet(snap / "text_units.parquet")
    obs_path = snap / "call_observations.parquet"
    obs = pd.read_parquet(obs_path) if obs_path.is_file() else None
    return ents, rels, tus, obs


def test_valid_snapshot_from_publisher(tmp_path: Path):
    graph = _publish(
        tmp_path,
        extra_manifest={"clang_calls": {"mode": "off", "enabled": False}},
    )
    report = audit_graph_root(graph)
    assert report["ok"] is True, report["anomalies"]
    assert report["status"] == "valid"
    assert report["directory_identity"] == "matched"
    assert report["audit_mode"] == AUDIT_MODE
    assert report["census"]["has_settings_yaml"] is True
    assert "clang_calls" in _read_manifest(_snap_dir(graph))


def test_required_core_keys_and_extra_overlay_keys_allowed():
    ents, rels, tus, obs = _tiny_tables()
    manifest = {
        "id": "20260813-010203-abcdef12",
        "created_at": "2026-08-13T01:02:03.000001",
        "schema_version": 1,
        "counts": {
            "entities": 1,
            "relationships": 1,
            "text_units": 1,
            "call_observations": 1,
        },
        "files": [
            "entities.parquet",
            "relationships.parquet",
            "text_units.parquet",
            "call_observations.parquet",
        ],
        "source_root": None,
        "git_commit": None,
        "total_size_bytes": 0,
        "corpus_hash": None,
        "preprocessor_liveness": {"eval_mode": "no_compiler"},
    }
    present = list(manifest["files"]) + ["manifest.json"]
    sizes = {name: 0 for name in manifest["files"]}
    report = validate_persisted_byog_snapshot(
        ents,
        rels,
        tus,
        obs,
        manifest,
        snapshot_id=manifest["id"],
        present_files=present,
        file_sizes=sizes,
    )
    assert report["ok"] is True, report["anomalies"]
    for key in REQUIRED_CORE_KEYS:
        stripped = {k: v for k, v in manifest.items() if k != key}
        missing = validate_persisted_byog_snapshot(
            ents, rels, tus, obs, stripped, snapshot_id=manifest["id"]
        )
        assert missing["ok"] is False
        assert "missing_core_key" in _codes(missing)


@pytest.mark.parametrize("manifest", [None, [], "nope", 1])
def test_malformed_or_null_or_list_manifest(manifest):
    ents, rels, tus, obs = _tiny_tables()
    report = validate_persisted_byog_snapshot(ents, rels, tus, obs, manifest)
    assert report["ok"] is False
    assert "malformed_core_field" in _codes(report)
    assert "missing_core_key" in _codes(report)


def test_strict_schema_version_and_count_types():
    ents, rels, tus, obs = _tiny_tables()
    base = {
        "id": "20260813-010203-abcdef12",
        "created_at": "2026-08-13T01:02:03",
        "schema_version": 1,
        "counts": {
            "entities": 1,
            "relationships": 1,
            "text_units": 1,
            "call_observations": 1,
        },
        "files": [
            "entities.parquet",
            "relationships.parquet",
            "text_units.parquet",
            "call_observations.parquet",
        ],
        "source_root": None,
        "git_commit": None,
        "total_size_bytes": 0,
        "corpus_hash": None,
    }
    present = list(base["files"]) + ["manifest.json"]
    sizes = {name: 0 for name in base["files"]}
    for bad in (True, 1.0, "1", None):
        manifest = dict(base)
        manifest["schema_version"] = bad
        report = validate_persisted_byog_snapshot(
            ents, rels, tus, obs, manifest, present_files=present, file_sizes=sizes
        )
        assert report["ok"] is False
        assert "invalid_schema_version" in _codes(report)
    for bad in (True, 1.0, "1", -1, None):
        manifest = dict(base)
        manifest["counts"] = dict(base["counts"])
        manifest["counts"]["entities"] = bad
        report = validate_persisted_byog_snapshot(
            ents, rels, tus, obs, manifest, present_files=present, file_sizes=sizes
        )
        assert report["ok"] is False
        assert "invalid_counts" in _codes(report)


def test_manifest_id_versus_directory_and_current(tmp_path: Path):
    graph = _publish(tmp_path)
    snap = _snap_dir(graph)
    manifest = _read_manifest(snap)
    manifest["id"] = "20260813-999999-deadbeef"
    _write_manifest(snap, manifest)
    report = audit_graph_root(graph)
    assert report["ok"] is False
    assert report["directory_identity"] == "mismatched"
    assert "snapshot_id_mismatch" in _codes(report)
    pointer = (graph / "current").read_text(encoding="utf-8").strip()
    assert pointer != manifest["id"]


def test_unsafe_and_escaping_snapshot_ids(tmp_path: Path, capsys):
    graph = _publish(tmp_path)
    assert audit_main(["--graph", str(graph), "--snapshot", "../x"]) == 2
    assert audit_main(["--graph", str(graph), "--snapshot", "foo/bar"]) == 2
    assert audit_main(["--graph", str(graph), "--snapshot", "foo\\bar"]) == 2
    assert audit_main(["--graph", str(graph), "--snapshot", ".."]) == 2
    assert audit_main(["--graph", str(graph), "--snapshot", "."]) == 2
    capsys.readouterr()
    ents, rels, tus, obs = _tiny_tables()
    manifest = {
        "id": "../escape",
        "created_at": "2026-08-13T01:02:03",
        "schema_version": 1,
        "counts": {
            "entities": 1,
            "relationships": 1,
            "text_units": 1,
            "call_observations": 1,
        },
        "files": [
            "entities.parquet",
            "relationships.parquet",
            "text_units.parquet",
            "call_observations.parquet",
        ],
        "source_root": None,
        "git_commit": None,
        "total_size_bytes": 0,
        "corpus_hash": None,
    }
    report = validate_persisted_byog_snapshot(ents, rels, tus, obs, manifest)
    assert "invalid_snapshot_id" in _codes(report)

    pointer = graph / "current"
    pointer_text = pointer.read_text(encoding="utf-8")
    external_pointer = tmp_path / "external-current"
    external_pointer.write_text(pointer_text, encoding="utf-8")
    pointer.unlink()
    pointer.symlink_to(external_pointer)
    assert audit_main(["--graph", str(graph)]) == 2
    assert "symlinked current pointer" in capsys.readouterr().err
    pointer.unlink()
    pointer.write_text(pointer_text, encoding="utf-8")


def test_files_missing_extra_reordered_duplicate():
    ents, rels, tus, obs = _tiny_tables()
    base_files = [
        "entities.parquet",
        "relationships.parquet",
        "text_units.parquet",
        "call_observations.parquet",
    ]
    manifest = {
        "id": "20260813-010203-abcdef12",
        "created_at": "2026-08-13T01:02:03",
        "schema_version": 1,
        "counts": {
            "entities": 1,
            "relationships": 1,
            "text_units": 1,
            "call_observations": 1,
        },
        "files": list(base_files),
        "source_root": None,
        "git_commit": None,
        "total_size_bytes": 0,
        "corpus_hash": None,
    }
    present = list(base_files) + ["manifest.json"]
    sizes = {name: 0 for name in base_files}
    ok = validate_persisted_byog_snapshot(
        ents, rels, tus, obs, manifest, present_files=present, file_sizes=sizes
    )
    assert ok["ok"] is True
    for files in (
        base_files[:-1],
        base_files + ["notes.parquet"],
        [base_files[1], base_files[0], *base_files[2:]],
        [*base_files, base_files[0]],
    ):
        broken = dict(manifest)
        broken["files"] = files
        report = validate_persisted_byog_snapshot(
            ents, rels, tus, obs, broken, present_files=present, file_sizes=sizes
        )
        assert report["ok"] is False
        assert "files_mismatch" in _codes(report)


def test_required_parquet_absence_and_observation_variants(tmp_path: Path):
    ents, rels, tus, obs = _tiny_tables()
    present = [
        "entities.parquet",
        "relationships.parquet",
        "text_units.parquet",
        "call_observations.parquet",
        "manifest.json",
    ]
    sizes = {name: 0 for name in present if name.endswith(".parquet")}
    manifest = {
        "id": "20260813-010203-abcdef12",
        "created_at": "2026-08-13T01:02:03",
        "schema_version": 1,
        "counts": {
            "entities": 1,
            "relationships": 1,
            "text_units": 1,
            "call_observations": 1,
        },
        "files": [
            "entities.parquet",
            "relationships.parquet",
            "text_units.parquet",
            "call_observations.parquet",
        ],
        "source_root": None,
        "git_commit": None,
        "total_size_bytes": 0,
        "corpus_hash": None,
    }
    missing = validate_persisted_byog_snapshot(
        ents,
        rels,
        tus,
        obs,
        manifest,
        present_files=["relationships.parquet", "text_units.parquet", "manifest.json"],
        file_sizes={"relationships.parquet": 0, "text_units.parquet": 0},
    )
    assert "missing_required_file" in _codes(missing)

    absent_obs = validate_persisted_byog_snapshot(
        ents,
        rels,
        tus,
        None,
        {
            **manifest,
            "counts": {**manifest["counts"], "call_observations": 0},
            "files": manifest["files"][:-1],
        },
        present_files=["entities.parquet", "relationships.parquet", "text_units.parquet", "manifest.json"],
        file_sizes={
            "entities.parquet": 0,
            "relationships.parquet": 0,
            "text_units.parquet": 0,
        },
    )
    assert absent_obs["ok"] is True, absent_obs["anomalies"]

    nonempty = validate_persisted_byog_snapshot(
        ents, rels, tus, obs, manifest, present_files=present, file_sizes=sizes
    )
    assert nonempty["ok"] is True

    zero = validate_persisted_byog_snapshot(
        ents,
        rels,
        tus,
        pd.DataFrame([]),
        {
            **manifest,
            "counts": {**manifest["counts"], "call_observations": 0},
            "files": manifest["files"][:-1],
        },
        present_files=present,
        file_sizes=sizes,
    )
    assert "zero_row_observation_file" in _codes(zero)

    # An empty in-memory table does not prove that a persisted file exists
    # when the caller deliberately omits filesystem inventory metadata.
    unknown_presence = validate_persisted_byog_snapshot(
        ents,
        rels,
        tus,
        pd.DataFrame([]),
        {
            **manifest,
            "counts": {**manifest["counts"], "call_observations": 0},
            "files": manifest["files"][:-1],
        },
    )
    assert unknown_presence["ok"] is True, unknown_presence["anomalies"]

    graph = _publish(tmp_path, n_obs=0, settings_text=None)
    snap = _snap_dir(graph)
    assert not (snap / "call_observations.parquet").exists()
    assert audit_graph_root(graph)["ok"] is True


def test_row_count_mismatches_for_all_tables():
    ents, rels, tus, obs = _tiny_tables()
    manifest = {
        "id": "20260813-010203-abcdef12",
        "created_at": "2026-08-13T01:02:03",
        "schema_version": 1,
        "counts": {
            "entities": 1,
            "relationships": 1,
            "text_units": 1,
            "call_observations": 1,
        },
        "files": [
            "entities.parquet",
            "relationships.parquet",
            "text_units.parquet",
            "call_observations.parquet",
        ],
        "source_root": None,
        "git_commit": None,
        "total_size_bytes": 0,
        "corpus_hash": None,
    }
    present = list(manifest["files"]) + ["manifest.json"]
    sizes = {name: 0 for name in manifest["files"]}
    for key in ("entities", "relationships", "text_units", "call_observations"):
        broken = dict(manifest)
        broken["counts"] = dict(manifest["counts"])
        broken["counts"][key] = 99
        report = validate_persisted_byog_snapshot(
            ents, rels, tus, obs, broken, present_files=present, file_sizes=sizes
        )
        assert "count_mismatch" in _codes(report)


def test_total_size_excludes_manifest_and_settings(tmp_path: Path):
    graph = _publish(tmp_path, settings_text="x" * 2048)
    snap = _snap_dir(graph)
    manifest = _read_manifest(snap)
    present, sizes = _inventory(snap)
    parquet_sum = sum(
        sizes[name] for name in manifest["files"] if name in sizes
    )
    assert manifest["total_size_bytes"] == parquet_sum
    assert sizes["manifest.json"] > 0
    assert sizes["settings.yaml"] > 0
    assert parquet_sum != parquet_sum + sizes["manifest.json"] + sizes["settings.yaml"]
    ents, rels, tus, obs = _load_tables(snap)
    report = validate_persisted_byog_snapshot(
        ents,
        rels,
        tus,
        obs,
        manifest,
        snapshot_id=manifest["id"],
        present_files=present,
        file_sizes=sizes,
    )
    assert report["ok"] is True, report["anomalies"]
    broken = dict(manifest)
    broken["total_size_bytes"] = parquet_sum + sizes["settings.yaml"]
    failed = validate_persisted_byog_snapshot(
        ents,
        rels,
        tus,
        obs,
        broken,
        snapshot_id=manifest["id"],
        present_files=present,
        file_sizes=sizes,
    )
    assert "total_size_mismatch" in _codes(failed)
    no_settings = dict(broken)
    no_settings["total_size_bytes"] = True
    assert "invalid_total_size_bytes" in _codes(
        validate_persisted_byog_snapshot(
            ents, rels, tus, obs, no_settings, present_files=present, file_sizes=sizes
        )
    )


def test_optional_settings_yaml(tmp_path: Path):
    with_settings = _publish(tmp_path, name="with_s", settings_text="a: 1\n")
    without = _publish(tmp_path, name="no_s", settings_text=None)
    assert (_snap_dir(with_settings) / "settings.yaml").is_file()
    assert not (_snap_dir(without) / "settings.yaml").exists()
    assert audit_graph_root(with_settings)["ok"] is True
    assert audit_graph_root(without)["ok"] is True
    assert "settings.yaml" not in _read_manifest(_snap_dir(with_settings))["files"]


def test_corpus_hash_git_commit_and_source_root():
    ents, rels, tus, obs = _tiny_tables()
    manifest = {
        "id": "20260813-010203-abcdef12",
        "created_at": "2026-08-13T01:02:03",
        "schema_version": 1,
        "counts": {
            "entities": 1,
            "relationships": 1,
            "text_units": 1,
            "call_observations": 1,
        },
        "files": [
            "entities.parquet",
            "relationships.parquet",
            "text_units.parquet",
            "call_observations.parquet",
        ],
        "source_root": "/does/not/exist/on/this/host/or/any/other",
        "git_commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "total_size_bytes": 0,
        "corpus_hash": None,
    }
    present = list(manifest["files"]) + ["manifest.json"]
    sizes = {name: 0 for name in manifest["files"]}
    ok = validate_persisted_byog_snapshot(
        ents, rels, tus, obs, manifest, present_files=present, file_sizes=sizes
    )
    assert ok["ok"] is True, ok["anomalies"]
    hashed = dict(manifest)
    hashed["corpus_hash"] = "abc"
    assert "invalid_corpus_hash" in _codes(
        validate_persisted_byog_snapshot(
            ents, rels, tus, obs, hashed, present_files=present, file_sizes=sizes
        )
    )
    for bad in ("ABC" * 13 + "ABCD", "deadbeef", 1, ""):
        broken = dict(manifest)
        broken["git_commit"] = bad
        assert "invalid_git_commit" in _codes(
            validate_persisted_byog_snapshot(
                ents, rels, tus, obs, broken, present_files=present, file_sizes=sizes
            )
        )
    null_git = dict(manifest)
    null_git["git_commit"] = None
    assert validate_persisted_byog_snapshot(
        ents, rels, tus, obs, null_git, present_files=present, file_sizes=sizes
    )["ok"]
    sha256 = dict(manifest)
    sha256["git_commit"] = "b" * 64
    assert validate_persisted_byog_snapshot(
        ents, rels, tus, obs, sha256, present_files=present, file_sizes=sizes
    )["ok"]
    bad_root = dict(manifest)
    bad_root["source_root"] = ["not", "a", "string"]
    assert "invalid_source_root" in _codes(
        validate_persisted_byog_snapshot(
            ents, rels, tus, obs, bad_root, present_files=present, file_sizes=sizes
        )
    )


def test_created_at_matches_datetime_now_isoformat_shape():
    ents, rels, tus, obs = _tiny_tables()
    manifest = {
        "id": "20260813-010203-abcdef12",
        "created_at": "2026-08-13T01:02:03.000001",
        "schema_version": 1,
        "counts": {
            "entities": 1,
            "relationships": 1,
            "text_units": 1,
            "call_observations": 1,
        },
        "files": [
            "entities.parquet",
            "relationships.parquet",
            "text_units.parquet",
            "call_observations.parquet",
        ],
        "source_root": None,
        "git_commit": None,
        "total_size_bytes": 0,
        "corpus_hash": None,
    }
    assert validate_persisted_byog_snapshot(
        ents, rels, tus, obs, manifest
    )["ok"]
    for bad in (
        "2026-08-13",
        "2026-08-13 01:02:03",
        "2026-08-13T01:02:03Z",
        "2026-08-13T01:02:03+00:00",
        "2026-08-13T01:02:03.1",
    ):
        broken = dict(manifest)
        broken["created_at"] = bad
        assert "invalid_created_at" in _codes(
            validate_persisted_byog_snapshot(ents, rels, tus, obs, broken)
        )


def test_undeclared_parquet_symlink_and_temp(tmp_path: Path):
    graph = _publish(tmp_path)
    snap = _snap_dir(graph)
    (snap / "extra.parquet").write_bytes(b"PAR1")
    leftover = snap / "entities.parquet.tmp"
    leftover.write_text("tmp", encoding="utf-8")
    report = audit_graph_root(graph)
    assert {"undeclared_parquet", "temp_remnant"} <= set(_codes(report))
    leftover.unlink()
    (snap / "extra.parquet").unlink()

    target = tmp_path / "outside.parquet"
    target.write_bytes((snap / "entities.parquet").read_bytes())
    linked = snap / "entities.parquet"
    real = linked.read_bytes()
    linked.unlink()
    linked.symlink_to(target)
    with pytest.raises(SnapshotGraphAuditError, match="symlinked core snapshot input"):
        audit_graph_root(graph)
    assert audit_main(["--graph", str(graph)]) == 2
    pure_report = validate_persisted_byog_snapshot(
        [],
        [],
        [],
        None,
        {},
        symlinked_files=["entities.parquet"],
    )
    assert "symlinked_core_input" in _codes(pure_report)
    linked.unlink()
    linked.write_bytes(real)

    notes_target = tmp_path / "notes-target"
    notes_target.write_text("notes", encoding="utf-8")
    notes_link = snap / "notes.txt"
    notes_link.symlink_to(notes_target)
    assert "symlinked_snapshot_entry" in _codes(audit_graph_root(graph))
    notes_link.unlink()

    unexpected_dir = snap / "unexpected-directory"
    unexpected_dir.mkdir()
    assert "unexpected_entry" in _codes(audit_graph_root(graph))
    unexpected_dir.rmdir()
    assert audit_graph_root(graph)["ok"] is True


def test_strict_duplicate_key_and_non_finite_json(tmp_path: Path, capsys):
    graph = _publish(tmp_path)
    snap = _snap_dir(graph)
    original = (snap / "manifest.json").read_text(encoding="utf-8")
    (snap / "manifest.json").write_text(
        '{"id": "a", "id": "b", "schema_version": 1}', encoding="utf-8"
    )
    assert audit_main(["--graph", str(graph)]) == 2
    (snap / "manifest.json").write_text(
        original.replace('"corpus_hash": null', '"corpus_hash": NaN'),
        encoding="utf-8",
    )
    assert audit_main(["--graph", str(graph)]) == 2
    (snap / "manifest.json").write_text(
        original.replace('"corpus_hash": null', '"corpus_hash": Infinity'),
        encoding="utf-8",
    )
    assert audit_main(["--graph", str(graph)]) == 2
    (snap / "manifest.json").write_text(original, encoding="utf-8")
    capsys.readouterr()


def test_flat_graph_directory_identity_unavailable(tmp_path: Path):
    ents, rels, tus, obs = _tiny_tables()
    flat = tmp_path / "flat"
    flat.mkdir()
    ents.to_parquet(flat / "entities.parquet")
    rels.to_parquet(flat / "relationships.parquet")
    tus.to_parquet(flat / "text_units.parquet")
    obs.to_parquet(flat / "call_observations.parquet")
    files = [
        "entities.parquet",
        "relationships.parquet",
        "text_units.parquet",
        "call_observations.parquet",
    ]
    total = sum((flat / name).stat().st_size for name in files)
    manifest = {
        "id": "flat-id",
        "created_at": "2026-08-13T01:02:03",
        "schema_version": 1,
        "counts": {
            "entities": 1,
            "relationships": 1,
            "text_units": 1,
            "call_observations": 1,
        },
        "files": files,
        "source_root": None,
        "git_commit": None,
        "total_size_bytes": total,
        "corpus_hash": None,
    }
    (flat / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    report = audit_graph_root(flat)
    assert report["ok"] is True, report["anomalies"]
    assert report["directory_identity"] == "unavailable"
    assert report.get("snapshot") is None
    assert report["mode"] == "flat"


def test_deterministic_anomaly_ordering_and_truncation():
    ents, rels, tus, obs = _tiny_tables()
    manifest = {
        "id": "../bad",
        "created_at": "not-a-date",
        "schema_version": True,
        "counts": {
            "entities": 7,
            "relationships": False,
            "text_units": 1,
            "call_observations": 1,
        },
        "files": ["text_units.parquet"],
        "source_root": 1,
        "git_commit": "XYZ",
        "total_size_bytes": -3,
        "corpus_hash": "nope",
    }
    first = validate_persisted_byog_snapshot(
        ents,
        rels,
        tus,
        obs,
        manifest,
        snapshot_id="other",
        present_files=["notes.txt", "extra.parquet", "foo.tmp"],
        file_sizes={},
        max_anomaly_samples=2,
    )
    second = validate_persisted_byog_snapshot(
        ents,
        rels,
        tus,
        obs,
        manifest,
        snapshot_id="other",
        present_files=["notes.txt", "extra.parquet", "foo.tmp"],
        file_sizes={},
        max_anomaly_samples=2,
    )
    assert first["anomalies"] == second["anomalies"]
    assert first["n_anomalies"] > 2
    assert first["n_anomaly_samples"] == 2
    assert first["anomalies_truncated"] is True
    full = validate_persisted_byog_snapshot(
        ents, rels, tus, obs, manifest, present_files=["notes.txt"]
    )
    codes = _codes(full)
    assert codes == sorted(codes)


def test_concurrent_fingerprint_change_respects_max_samples(
    tmp_path: Path, monkeypatch
):
    graph = _publish(tmp_path)
    import byog_snapshot_graph_audit as audit_module  # type: ignore

    original = audit_module.read_only_fingerprint

    def drift(graph_root, snap_dir):
        fingerprint = original(graph_root, snap_dir)
        if drift.calls == 1:
            fingerprint["manifest.json"] = "0" * 64
        drift.calls += 1
        return fingerprint

    drift.calls = 0
    monkeypatch.setattr(audit_module, "read_only_fingerprint", drift)
    zero = audit_module.audit_graph_root(graph, max_anomaly_samples=0)
    assert zero["ok"] is False
    assert zero["status"] == zero["state"] == zero["classification"] == "invalid"
    assert zero["n_anomalies"] == 1
    assert zero["n_anomaly_samples"] == 0
    assert zero["anomalies_truncated"] is True
    drift.calls = 0
    one = audit_module.audit_graph_root(graph, max_anomaly_samples=1)
    assert one["ok"] is False
    assert one["classification"] == "invalid"
    assert one["n_anomalies"] == 1
    assert one["n_anomaly_samples"] == 1
    assert one["anomalies"][0]["code"] == "read_only_violation"


def test_output_containment_through_symlinks(tmp_path: Path, capsys):
    graph = _publish(tmp_path)
    forbidden = _snap_dir(graph) / "audit.json"
    assert audit_main(["--graph", str(graph), "--output", str(forbidden)]) == 2
    assert "outside the audited graph root" in capsys.readouterr().err
    assert not forbidden.exists()
    alias = tmp_path / "alias"
    alias.symlink_to(graph)
    via = alias / "via.json"
    assert audit_main(["--graph", str(graph), "--output", str(via)]) == 2
    assert not via.exists()
    out = tmp_path / "ok.json"
    assert audit_main(["--graph", str(graph), "--output", str(out), "--json"]) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["ok"] is True


def test_non_mutating_dataframe_and_row_api():
    ents, rels, tus, obs = _tiny_tables()
    manifest = {
        "id": "20260813-010203-abcdef12",
        "created_at": "2026-08-13T01:02:03",
        "schema_version": 1,
        "counts": {
            "entities": 1,
            "relationships": 1,
            "text_units": 1,
            "call_observations": 1,
        },
        "files": [
            "entities.parquet",
            "relationships.parquet",
            "text_units.parquet",
            "call_observations.parquet",
        ],
        "source_root": None,
        "git_commit": None,
        "total_size_bytes": 0,
        "corpus_hash": None,
    }
    present = list(manifest["files"]) + ["manifest.json"]
    sizes = {name: 0 for name in manifest["files"]}
    ents_before = ents.copy(deep=True)
    rels_before = rels.copy(deep=True)
    tus_before = tus.copy(deep=True)
    obs_before = obs.copy(deep=True)
    manifest_before = copy.deepcopy(manifest)
    rows = ents.to_dict("records")
    report = audit_rows(
        ents,
        rels,
        tus,
        obs,
        manifest,
        snapshot_id=manifest["id"],
        present_files=present,
        file_sizes=sizes,
    )
    assert report["ok"] is True
    assert ents.equals(ents_before)
    assert rels.equals(rels_before)
    assert tus.equals(tus_before)
    assert obs.equals(obs_before)
    assert manifest == manifest_before
    again = audit_rows(
        rows,
        rels.to_dict("records"),
        tus.to_dict("records"),
        obs.to_dict("records"),
        manifest,
        snapshot_id=manifest["id"],
        present_files=present,
        file_sizes=sizes,
    )
    assert again["ok"] is True
    assert rows == ents.to_dict("records")


def test_gap_existing_component_audits_pass_on_corrupt_envelope(tmp_path: Path):
    """Existing overlay audits can pass while the core envelope is corrupt."""
    graph = _publish(tmp_path)
    snap = _snap_dir(graph)
    manifest = _read_manifest(snap)
    ents, rels, tus, obs = _load_tables(snap)
    from c_clang_calls import validate_persisted_call_overlay  # type: ignore
    from c_clang_signatures import validate_persisted_signature_overlay  # type: ignore
    from c_clang_type_shapes import validate_persisted_type_shape_overlay  # type: ignore
    from c_clang_type_uses import validate_persisted_type_use_overlay  # type: ignore
    from c_clang_types import validate_persisted_type_overlay  # type: ignore
    from c_compiler_facts import (  # type: ignore
        validate_persisted_compiler_dependency_overlay,
    )
    from c_compiler_includes import (  # type: ignore
        validate_persisted_compiler_include_overlay,
    )
    from c_overlay_coherence import validate_persisted_c_overlay_coherence  # type: ignore
    from c_preprocessor import validate_persisted_preprocessor_liveness  # type: ignore

    assert validate_persisted_call_overlay(rels, manifest)["ok"]
    assert validate_persisted_signature_overlay(ents, manifest)["ok"]
    assert validate_persisted_type_overlay(ents, manifest)["ok"]
    assert validate_persisted_type_use_overlay(ents, rels, manifest)["ok"]
    assert validate_persisted_type_shape_overlay(ents, manifest)["ok"]
    assert validate_persisted_compiler_dependency_overlay(ents, rels, manifest)["ok"]
    assert validate_persisted_compiler_include_overlay(ents, rels, manifest)["ok"]
    assert validate_persisted_preprocessor_liveness(ents, rels, obs, manifest)["ok"]
    assert validate_persisted_c_overlay_coherence(ents, rels, obs, manifest)["ok"]

    manifest["id"] = "not-the-directory"
    manifest["counts"] = dict(manifest["counts"])
    manifest["counts"]["text_units"] = 999
    manifest["files"] = ["entities.parquet", "invented.parquet"]
    manifest["total_size_bytes"] = 1
    _write_manifest(snap, manifest)
    report = audit_graph_root(graph)
    assert report["ok"] is False
    codes = set(_codes(report))
    assert "snapshot_id_mismatch" in codes
    assert "count_mismatch" in codes
    assert "files_mismatch" in codes
    assert "total_size_mismatch" in codes
    assert validate_persisted_call_overlay(rels, manifest)["ok"]
    assert validate_persisted_c_overlay_coherence(ents, rels, obs, manifest)["ok"]


def test_health_python_and_c_and_existing_c_keys(tmp_path: Path, monkeypatch):
    from published_graph_health import (  # type: ignore
        PublishedGraphSpec,
        _call_integrity,
        _compiler_dependency_integrity,
        _compiler_include_integrity,
        _fresh_data,
        _overlay_coherence_integrity,
        _preprocessor_liveness_integrity,
        _signature_integrity,
        _snapshot_integrity,
        _type_integrity,
        _type_shape_integrity,
        _type_use_integrity,
        check_spec,
        load_specs,
    )

    specs = load_specs()
    python_spec = next(spec for spec in specs if spec.ident == "mini_game")
    fresh = _fresh_data(python_spec, ROOT)
    graph = tmp_path / "byog_mini_game_health"
    publish_byog_snapshot(
        pd.DataFrame(fresh["entities"]),
        pd.DataFrame(fresh["relationships"]),
        pd.DataFrame(fresh["text_units"]),
        graph,
        settings_text="health: true\n",
        source_root=(ROOT / python_spec.source).resolve(),
        call_observations_df=pd.DataFrame(fresh.get("call_observations") or []),
    )
    result = check_spec(python_spec, root=ROOT, graph_root=graph)
    assert result["status"] == "pass", result
    assert result["snapshot_integrity"]["ok"] is True
    for key in C_INTEGRITY_KEYS:
        assert key not in result

    frozen = next(spec for spec in specs if spec.mode == "frozen")
    exempt = check_spec(frozen, root=ROOT, graph_root=ROOT / "does-not-exist")
    assert exempt["status"] == "exempt"
    assert "snapshot_integrity" not in exempt

    ents = fresh["entities"]
    rels = fresh["relationships"]
    published = {
        "entities": ents,
        "relationships": rels,
        "text_units": fresh["text_units"],
        "call_observations": fresh.get("call_observations"),
    }
    snap = _snap_dir(graph)
    manifest = _read_manifest(snap)
    present, sizes = _inventory(snap)
    envelope = _snapshot_integrity(
        published,
        manifest,
        snapshot_id=manifest["id"],
        present_files=present,
        file_sizes=sizes,
        symlinked_files=[],
    )
    assert envelope["ok"] is True
    c_published = {"entities": ents, "relationships": rels}
    assert _type_use_integrity(c_published, {}, indexer="c")["ok"]
    assert _type_shape_integrity({"entities": []}, {}, indexer="c")["ok"]
    assert _type_integrity({"entities": []}, {}, indexer="c")["ok"]
    assert _signature_integrity({"entities": []}, {}, indexer="c")["ok"]
    assert _call_integrity({"relationships": []}, {}, indexer="c")["ok"]
    assert _compiler_dependency_integrity(c_published, {}, indexer="c")["ok"]
    assert _compiler_include_integrity(c_published, {}, indexer="c")["ok"]
    assert _preprocessor_liveness_integrity(c_published, {}, indexer="c")["ok"]
    assert _overlay_coherence_integrity(c_published, {}, indexer="c")["ok"]
    assert _type_use_integrity(c_published, {}, indexer="python") is None

    c_spec = next(spec for spec in specs if spec.indexer == "c" and spec.mode == "mutable")
    c_result = check_spec(c_spec, root=ROOT)
    assert c_result["status"] in {"pass", "skipped"}, c_result
    if c_result["status"] == "pass":
        assert c_result["snapshot_integrity"]["ok"] is True
        for key in C_INTEGRITY_KEYS:
            assert key in c_result, key
            assert c_result[key]["ok"] is True

    broken = tmp_path / "byog_broken_envelope"
    publish_byog_snapshot(
        pd.DataFrame(fresh["entities"]),
        pd.DataFrame(fresh["relationships"]),
        pd.DataFrame(fresh["text_units"]),
        broken,
        settings_text="x\n",
        source_root=(ROOT / python_spec.source).resolve(),
        call_observations_df=pd.DataFrame(fresh.get("call_observations") or []),
    )
    bsnap = _snap_dir(broken)
    bman = _read_manifest(bsnap)
    bman["id"] = "not-the-dir"
    bman["counts"] = dict(bman["counts"])
    bman["counts"]["text_units"] = 0
    _write_manifest(bsnap, bman)
    import published_graph_health as health_module  # type: ignore

    def extractor_must_not_run(*_args, **_kwargs):
        raise AssertionError("invalid snapshot envelope must short-circuit extraction")

    monkeypatch.setattr(health_module, "_fresh_data", extractor_must_not_run)
    failed = check_spec(python_spec, root=ROOT, graph_root=broken)
    assert failed["status"] == "fail"
    assert failed["reason"] == "persisted snapshot envelope integrity anomalies"
    assert failed["snapshot_integrity"]["ok"] is False


def test_monkeypatch_proves_no_producer_or_extractor(tmp_path: Path, monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("snapshot audit must not invoke producers or extractors")

    import byog_graph
    import c_clang_ast_capture as cap
    import c_compiler_common as common
    import c_compiler_facts as deps
    import c_compiler_includes as incs
    import c_preprocessor as pp
    import extract_c
    import extract_python
    import index_c
    import index_python
    import mini_game_to_byog

    monkeypatch.setattr(byog_graph, "publish_byog_snapshot", boom)
    monkeypatch.setattr(mini_game_to_byog, "build_byog_for_package", boom)
    monkeypatch.setattr(extract_python, "extract_from_file", boom)
    monkeypatch.setattr(extract_python, "main", boom)
    monkeypatch.setattr(extract_c, "build_c_byog", boom)
    monkeypatch.setattr(index_c, "main", boom)
    monkeypatch.setattr(index_python, "main", boom)
    monkeypatch.setattr(pp, "analyze_package", boom)
    monkeypatch.setattr(pp, "annotate_byog", boom)
    monkeypatch.setattr(deps, "collect_translation_unit_dependencies", boom)
    monkeypatch.setattr(incs, "collect_configured_direct_includes", boom)
    monkeypatch.setattr(common, "load_compile_entries", boom)
    monkeypatch.setattr(cap, "capture_clang_ast_package", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "check_output", boom)

    ents, rels, tus, obs = _tiny_tables()
    manifest = {
        "id": "20260813-010203-abcdef12",
        "created_at": "2026-08-13T01:02:03",
        "schema_version": 1,
        "counts": {
            "entities": 1,
            "relationships": 1,
            "text_units": 1,
            "call_observations": 1,
        },
        "files": [
            "entities.parquet",
            "relationships.parquet",
            "text_units.parquet",
            "call_observations.parquet",
        ],
        "source_root": None,
        "git_commit": None,
        "total_size_bytes": 0,
        "corpus_hash": None,
    }
    present = list(manifest["files"]) + ["manifest.json"]
    sizes = {name: 0 for name in manifest["files"]}
    report = validate_persisted_byog_snapshot(
        ents, rels, tus, obs, manifest, present_files=present, file_sizes=sizes
    )
    assert report["ok"] is True
    disposable = tmp_path / "prebuilt"
    disposable.mkdir()
    snap = disposable / "snapshots" / manifest["id"]
    snap.mkdir(parents=True)
    ents.to_parquet(snap / "entities.parquet")
    rels.to_parquet(snap / "relationships.parquet")
    tus.to_parquet(snap / "text_units.parquet")
    obs.to_parquet(snap / "call_observations.parquet")
    (disposable / "current").write_text(manifest["id"], encoding="utf-8")
    files = list(manifest["files"])
    manifest["total_size_bytes"] = sum((snap / name).stat().st_size for name in files)
    (snap / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    assert audit_graph_root(disposable)["ok"] is True


def test_cli_and_format_on_valid_disposable(tmp_path: Path, capsys):
    graph = _publish(tmp_path)
    assert audit_main(["--graph", str(graph)]) == 0
    text = capsys.readouterr().out
    assert "RESULT: PASS" in text
    assert "BYOG snapshot envelope audit" in text
    report = audit_graph_root(graph)
    assert "RESULT: PASS" in format_report(report)
    parsed = json.loads(audit_to_json(report))
    assert parsed["ok"] is True


def test_published_mutable_roots_are_read_only():
    for name in ("byog_mini_game", "byog_sqlparse", "byog_cjson", "byog_inih"):
        graph = ROOT / name
        if not (graph / "current").is_file():
            pytest.skip(f"{name} is absent locally")
        before = {
            path.relative_to(graph).as_posix(): path.read_bytes()
            for path in sorted(graph.rglob("*"))
            if path.is_file() and not path.is_symlink()
        }
        report = audit_graph_root(graph)
        after = {
            path.relative_to(graph).as_posix(): path.read_bytes()
            for path in sorted(graph.rglob("*"))
            if path.is_file() and not path.is_symlink()
        }
        assert after == before, name
        assert report["ok"] is True, (name, report["anomalies"])
        assert report["read_only_verification"]["verified"] is True
        assert (
            "snapshot/settings.yaml"
            in report["read_only_verification"]["inputs"]
        )
        assert (
            read_only_fingerprint(graph, _snap_dir(graph))
            == report["read_only_verification"]["fingerprint"]
        )
