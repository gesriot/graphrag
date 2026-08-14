"""Product-level read-only persisted-integrity doctor.

Disposable graphs only. Published byog_* roots are opened read-only.

Run:
  uv run python -m pytest examples/mini_game/tests/test_persisted_graph_doctor.py -q
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

from byog_graph import STAGING_NAME_PREFIX, publish_byog_snapshot  # type: ignore
from persisted_graph_doctor import (  # type: ignore
    AUDIT_MODE,
    audit_graph_root,
    audit_rows,
    audit_to_json,
    format_report,
    main as doctor_main,
)
from persisted_graph_integrity import (  # type: ignore
    AmbiguousIndexerError,
    C_COMPONENT_ORDER,
    resolve_persisted_indexer,
    validate_persisted_graph_integrity,
)

C_KEYS = C_COMPONENT_ORDER
GRAPH_CODE = ROOT / "scripts" / "graphrag_code.py"
DOCTOR = ROOT / "scripts" / "persisted_graph_doctor.py"


def _py_rows():
    ents = [
        {
            "id": "ent:fn:demo.main",
            "title": "demo:main",
            "type": "function",
            "source_file": "demo.py",
            "extractor": "tree-sitter-python",
        }
    ]
    rels = [
        {
            "id": "rel:contains:1",
            "source": "demo:demo.py",
            "target": "demo:main",
            "type": "contains",
            "extractor": "tree-sitter-python",
        }
    ]
    tus = [
        {
            "id": "tu:1",
            "title": "demo.py",
            "source_file": "demo.py",
            "entity_id": "ent:fn:demo.main",
        }
    ]
    obs = [{"id": "obs:1", "caller": "demo:main", "callee": "len"}]
    return ents, rels, tus, obs


def _c_rows():
    ents = [
        {
            "id": "ent:fn:mod.main",
            "title": "mod:main",
            "type": "function",
            "source_file": "main.c",
            "extractor": "tree-sitter-c",
        }
    ]
    rels = [
        {
            "id": "rel:contains:1",
            "source": "mod:main.c",
            "target": "mod:main",
            "type": "contains",
            "extractor": "tree-sitter-c",
        }
    ]
    tus = [
        {
            "id": "tu:1",
            "title": "main.c",
            "source_file": "main.c",
            "entity_id": "ent:fn:mod.main",
        }
    ]
    obs = [{"id": "obs:1", "caller": "mod:main", "callee": "printf"}]
    return ents, rels, tus, obs


def _publish(tmp_path: Path, ents, rels, tus, obs, *, name: str = "g", extra=None) -> Path:
    graph = tmp_path / name
    publish_byog_snapshot(
        pd.DataFrame(ents),
        pd.DataFrame(rels),
        pd.DataFrame(tus),
        graph,
        settings_text="doctor: true\n",
        keep_last=2,
        call_observations_df=pd.DataFrame(obs) if obs else None,
        extra_manifest=extra,
    )
    return graph


def _snap(graph: Path) -> Path:
    return graph / "snapshots" / (graph / "current").read_text(encoding="utf-8").strip()


def _load(graph: Path):
    snap = _snap(graph)
    ents = pd.read_parquet(snap / "entities.parquet")
    rels = pd.read_parquet(snap / "relationships.parquet")
    tus = pd.read_parquet(snap / "text_units.parquet")
    obs_path = snap / "call_observations.parquet"
    obs = pd.read_parquet(obs_path) if obs_path.is_file() else None
    manifest = json.loads((snap / "manifest.json").read_text(encoding="utf-8"))
    present = [p.name for p in snap.iterdir() if p.is_file() and not p.is_symlink()]
    sizes = {p.name: p.stat().st_size for p in snap.iterdir() if p.is_file() and not p.is_symlink()}
    return ents, rels, tus, obs, manifest, present, sizes, snap


def test_valid_python_snapshot(tmp_path: Path):
    graph = _publish(tmp_path, *_py_rows())
    report = audit_graph_root(graph, indexer="python")
    assert report["ok"] is True, report["anomalies"]
    assert report["audit_mode"] == AUDIT_MODE
    assert report["indexer"] == "python"
    assert report["components"] == {}
    assert report["failed_components"] == []
    for key in C_KEYS:
        assert key not in report["components"]


def test_valid_c_snapshot_has_all_nine_keys(tmp_path: Path):
    graph = _publish(tmp_path, *_c_rows())
    report = audit_graph_root(graph, indexer="c")
    assert report["ok"] is True, report["anomalies"]
    assert list(report["components"]) == list(C_KEYS)
    for key in C_KEYS:
        component = report["components"][key]
        assert component["ok"] is True, (key, component.get("anomalies"))
        assert "status" in component and "mode" in component
        assert "n_anomalies" in component


def test_legacy_off_c_graph_passes(tmp_path: Path):
    extra = {
        "compiler_dependencies": {
            "mode": "off",
            "enabled": False,
            "n_facts": 0,
            "n_translation_units": 0,
        },
        "compiler_includes": {
            "mode": "off",
            "enabled": False,
            "n_facts": 0,
            "n_translation_units": 0,
        },
    }
    graph = _publish(tmp_path, *_c_rows(), extra=extra)
    report = audit_graph_root(graph, indexer="c")
    assert report["ok"] is True, report["anomalies"]
    assert report["components"]["compiler_dependency_integrity"]["mode"] in {
        "off",
        "legacy_absent",
    }


def test_explicit_indexer_python_and_c(tmp_path: Path):
    py = _publish(tmp_path, *_py_rows(), name="py")
    cgraph = _publish(tmp_path, *_c_rows(), name="c")
    py_rep = audit_graph_root(py, indexer="python")
    c_rep = audit_graph_root(cgraph, indexer="c")
    assert py_rep["indexer_resolution"]["reason"] == "explicit"
    assert c_rep["indexer_resolution"]["reason"] == "explicit"
    assert py_rep["components"] == {}
    assert set(c_rep["components"]) == set(C_KEYS)


def test_auto_detection_python_and_c():
    py = _py_rows()
    c_rows = _c_rows()
    resolved, info = resolve_persisted_indexer("auto", *py[:4], {"schema_version": 1})
    assert resolved == "python"
    assert info["requested"] == "auto"
    resolved_c, info_c = resolve_persisted_indexer(
        "auto", *c_rows[:4], {"schema_version": 1}
    )
    assert resolved_c == "c"


def test_auto_empty_mixed_contradictory():
    empty = ([{"id": "e1", "title": "x"}], [{"id": "r1"}], [{"id": "t1"}], None)
    with pytest.raises(AmbiguousIndexerError):
        resolve_persisted_indexer("auto", *empty, {})
    mixed_ents = [
        {"id": "e1", "source_file": "a.py", "extractor": "tree-sitter-python"},
        {"id": "e2", "source_file": "a.c", "extractor": "tree-sitter-c"},
    ]
    with pytest.raises(AmbiguousIndexerError):
        resolve_persisted_indexer("auto", mixed_ents, [], [], None, {})
    contrad = [
        {"id": "e1", "source_file": "a.py", "extractor": "tree-sitter-c"},
    ]
    with pytest.raises(AmbiguousIndexerError):
        resolve_persisted_indexer("auto", contrad, [], [], None, {})
    unknown = [{"id": "e1", "source_file": "a.py", "extractor": "mystery"}]
    with pytest.raises(AmbiguousIndexerError):
        resolve_persisted_indexer("auto", unknown, [], [], None, {})
    nullable = [{"id": "e1", "source_file": "a.py", "extractor": float("nan")}]
    assert resolve_persisted_indexer("auto", nullable, [], [], None, {})[0] == "python"


def test_invalid_envelope_short_circuits_c_validators(monkeypatch):
    ents, rels, tus, obs = _c_rows()
    manifest = {
        "id": "20260814-000000-abcd1234",
        "created_at": "2026-08-14T00:00:00",
        "schema_version": 1,
        "counts": {
            "entities": 99,
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

    def boom(*_a, **_k):
        raise AssertionError("C validator must not run on an invalid envelope")

    import persisted_graph_integrity as agg

    monkeypatch.setattr(agg, "_run_c_component", boom)
    report = validate_persisted_graph_integrity(
        ents,
        rels,
        tus,
        obs,
        manifest,
        indexer="c",
        snapshot_id=manifest["id"],
        present_files=present,
        file_sizes=sizes,
    )
    assert report["ok"] is False
    assert report["components"] == {}
    assert report["failed_components"] == ["snapshot_integrity"]
    assert report["n_anomalies"] == report["snapshot_integrity"]["n_anomalies"]
    with pytest.raises(ValueError, match="unknown indexer"):
        validate_persisted_graph_integrity(
            ents,
            rels,
            tus,
            obs,
            manifest,
            indexer="rust",
            snapshot_id=manifest["id"],
            present_files=present,
            file_sizes=sizes,
        )


def _corrupt_type_use(rels):
    rels.append(
        {
            "id": "rel:uses:bad",
            "source": "mod:main",
            "target": "mod:T",
            "type": "uses_type",
            "fact_kind": "configured_type_use",
            "extractor": "clang-ast-json",
        }
    )


def _corrupt_type_shape(ents):
    ents[0]["clang_shape_members"] = ["x"]


def _corrupt_type(ents):
    ents[0]["clang_type_kind"] = "struct"


def _corrupt_signature(ents):
    ents[0]["clang_signature_return"] = "int"


def _corrupt_call(rels):
    rels[0]["clang_call_name"] = "printf"
    rels[0]["type"] = "calls"


def _corrupt_deps(rels):
    rels.append(
        {
            "id": "rel:dep:bad",
            "source": "mod:main.c",
            "target": "mod:util.c",
            "type": "depends_on",
            "fact_kind": "translation_unit_dependency",
            "extractor": "c-compiler-deps",
        }
    )


def _corrupt_includes(rels):
    rels.append(
        {
            "id": "rel:inc:bad",
            "source": "mod:main.c",
            "target": "mod:util.h",
            "type": "includes",
            "fact_kind": "configured_direct_include",
            "extractor": "c-compiler-includes",
        }
    )


def _corrupt_liveness(ents):
    ents[0]["preprocessor_dependent"] = True


def _corrupt_coherence(manifest):
    manifest["compiler_dependencies"] = {
        "mode": "compiler_m",
        "enabled": True,
        "compile_commands_digest": "a" * 64,
        "n_compile_entries": 1,
        "n_facts": 0,
        "n_translation_units": 0,
    }
    manifest["compiler_includes"] = {
        "mode": "compiler_eh",
        "enabled": True,
        "compile_commands_digest": "b" * 64,
        "n_compile_entries": 1,
        "n_facts": 0,
        "n_translation_units": 0,
    }


@pytest.mark.parametrize(
    "target,mutator",
    [
        ("clang_type_use_integrity", lambda e, r, m: _corrupt_type_use(r)),
        ("clang_type_shape_integrity", lambda e, r, m: _corrupt_type_shape(e)),
        ("clang_type_integrity", lambda e, r, m: _corrupt_type(e)),
        ("clang_signature_integrity", lambda e, r, m: _corrupt_signature(e)),
        ("clang_call_integrity", lambda e, r, m: _corrupt_call(r)),
        ("compiler_dependency_integrity", lambda e, r, m: _corrupt_deps(r)),
        ("compiler_include_integrity", lambda e, r, m: _corrupt_includes(r)),
        ("preprocessor_liveness_integrity", lambda e, r, m: _corrupt_liveness(e)),
        ("c_overlay_coherence_integrity", lambda e, r, m: _corrupt_coherence(m)),
    ],
)
def test_focused_c_component_corruption(target, mutator):
    ents, rels, tus, obs = _c_rows()
    manifest = {
        "id": "20260814-000000-abcd1234",
        "created_at": "2026-08-14T00:00:00",
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
    mutator(ents, rels, manifest)
    if target in {
        "clang_type_use_integrity",
        "compiler_dependency_integrity",
        "compiler_include_integrity",
    }:
        manifest["counts"]["relationships"] = len(rels)
        if len(rels) > 1:
            manifest["files"] = list(manifest["files"])
    present = list(manifest["files"]) + ["manifest.json"]
    sizes = {name: 0 for name in manifest["files"]}
    report = validate_persisted_graph_integrity(
        ents,
        rels,
        tus,
        obs,
        manifest,
        indexer="c",
        snapshot_id=manifest["id"],
        present_files=present,
        file_sizes=sizes,
    )
    assert report["ok"] is False
    assert target in report["failed_components"]
    assert report["components"][target]["ok"] is False
    assert report["components"][target]["n_anomalies"] >= 1
    assert "status" in report["components"][target]
    assert "mode" in report["components"][target]


def test_combined_anomaly_totals_without_alias_double_count():
    ents, rels, tus, obs = _c_rows()
    _corrupt_type_use(rels)
    _corrupt_liveness(ents)
    manifest = {
        "id": "20260814-000000-abcd1234",
        "created_at": "2026-08-14T00:00:00",
        "schema_version": 1,
        "counts": {
            "entities": 1,
            "relationships": 2,
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
    report = validate_persisted_graph_integrity(
        ents,
        rels,
        tus,
        obs,
        manifest,
        indexer="c",
        snapshot_id=manifest["id"],
        present_files=present,
        file_sizes=sizes,
        max_anomaly_samples=1,
    )
    expected = int(report["snapshot_integrity"]["n_anomalies"])
    for component in report["components"].values():
        expected += int(component["n_anomalies"])
    assert report["n_anomalies"] == expected
    assert "n_violations" not in report
    assert report["n_anomaly_samples"] == 1
    assert report["anomalies_truncated"] is True
    zero = validate_persisted_graph_integrity(
        ents,
        rels,
        tus,
        obs,
        manifest,
        indexer="c",
        snapshot_id=manifest["id"],
        present_files=present,
        file_sizes=sizes,
        max_anomaly_samples=0,
    )
    assert zero["n_anomaly_samples"] == 0
    assert zero["n_anomalies"] == expected
    many = validate_persisted_graph_integrity(
        ents,
        rels,
        tus,
        obs,
        manifest,
        indexer="c",
        snapshot_id=manifest["id"],
        present_files=present,
        file_sizes=sizes,
        max_anomaly_samples=100,
    )
    documented = ("snapshot_integrity", *C_COMPONENT_ORDER)
    ranks = [documented.index(a["component"]) for a in many["anomalies"]]
    assert ranks == sorted(ranks)
    coherence = [
        anomaly
        for anomaly in many["anomalies"]
        if anomaly["component"] == "c_overlay_coherence_integrity"
    ]
    assert coherence
    assert {anomaly.get("subcomponent") for anomaly in coherence} >= {
        "clang_type_uses",
        "preprocessor_liveness",
    }


def test_current_switch_before_fingerprint(tmp_path: Path, monkeypatch):
    graph = _publish(tmp_path, *_py_rows())
    first = (_snap(graph)).name
    _publish(tmp_path, *_py_rows(), name="g")
    second = (graph / "current").read_text(encoding="utf-8").strip()
    assert first != second
    (graph / "current").write_text(first, encoding="utf-8")
    import persisted_graph_doctor as doctor

    original = doctor.doctor_fingerprint

    def switch(graph_root, snap_dir):
        if switch.calls == 0:
            (Path(graph_root) / "current").write_text(second, encoding="utf-8")
        switch.calls += 1
        return original(graph_root, snap_dir)

    switch.calls = 0
    monkeypatch.setattr(doctor, "doctor_fingerprint", switch)
    report = doctor.audit_graph_root(graph, indexer="python", max_anomaly_samples=1)
    assert report["ok"] is False
    assert "graph/current_selection" in report["read_only_verification"]["changed_inputs"]


def test_mutation_during_last_c_component(tmp_path: Path, monkeypatch):
    graph = _publish(tmp_path, *_c_rows())
    import persisted_graph_integrity as agg

    original = agg._run_c_component

    def mutate(name, *args, **kwargs):
        result = original(name, *args, **kwargs)
        if name == "c_overlay_coherence_integrity":
            (_snap(graph) / "settings.yaml").write_text("mutated\n", encoding="utf-8")
        return result

    monkeypatch.setattr(agg, "_run_c_component", mutate)
    report = audit_graph_root(graph, indexer="c", max_anomaly_samples=2)
    assert report["ok"] is False
    assert report["classification"] == "invalid"
    assert report["read_only_verification"]["verified"] is False


def test_stable_staging_is_notice_not_corruption(tmp_path: Path):
    graph = _publish(tmp_path, *_py_rows())
    staging = graph / "snapshots" / f"{STAGING_NAME_PREFIX}leftover"
    staging.mkdir()
    (staging / "entities.parquet").write_text("partial", encoding="utf-8")
    symlink = graph / "snapshots" / f"{STAGING_NAME_PREFIX}symlink"
    symlink.symlink_to(tmp_path, target_is_directory=True)
    report = audit_graph_root(graph, indexer="python")
    assert report["ok"] is True, report["anomalies"]
    assert report["publication_notices"]
    assert report["publication_notices"][0]["code"] == "staging_present"
    assert report["publication_notices"][0]["names"] == [staging.name]
    assert staging.is_dir()


def test_staging_listing_change_is_concurrent_mutation(tmp_path: Path, monkeypatch):
    graph = _publish(tmp_path, *_py_rows())
    import persisted_graph_doctor as doctor

    original = doctor.audit_rows

    def add_staging(*args, **kwargs):
        (graph / "snapshots" / f"{STAGING_NAME_PREFIX}live").mkdir()
        return original(*args, **kwargs)

    monkeypatch.setattr(doctor, "audit_rows", add_staging)
    report = doctor.audit_graph_root(graph, indexer="python")
    assert report["ok"] is False
    assert "graph/snapshots_listing" in report["read_only_verification"]["changed_inputs"]


def test_publish_lock_absent_regular_and_symlink(tmp_path: Path, capsys, monkeypatch):
    graph = _publish(tmp_path, *_py_rows())
    lock = graph / ".publish.lock"
    if lock.exists() or lock.is_symlink():
        lock.unlink()
    report = audit_graph_root(graph, indexer="python")
    assert report["read_only_verification"]["fingerprint"]["graph/publish_lock"] == "absent"
    lock.write_text("lock", encoding="utf-8")
    report = audit_graph_root(graph, indexer="python")
    assert report["ok"] is True
    assert report["read_only_verification"]["fingerprint"]["graph/publish_lock"].startswith(
        "regular:"
    )
    lock.unlink()
    lock.symlink_to(tmp_path / "outside")
    assert doctor_main(["--graph", str(graph), "--indexer", "python"]) == 2
    assert "symlinked publication lock" in capsys.readouterr().err
    lock.unlink()
    lock.write_text("lock", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.write_text("outside", encoding="utf-8")
    import persisted_graph_doctor as doctor

    original_open = doctor.os.open

    def swap_before_open(path, flags):
        lock.unlink()
        lock.symlink_to(outside)
        return original_open(path, flags)

    monkeypatch.setattr(doctor.os, "open", swap_before_open)
    assert doctor_main(["--graph", str(graph), "--indexer", "python"]) == 2
    assert "symlinked publication lock" in capsys.readouterr().err


def test_symlinked_current_is_unsafe(tmp_path: Path, capsys):
    graph = _publish(tmp_path, *_py_rows())
    pointer = graph / "current"
    value = pointer.read_text(encoding="utf-8")
    pointer.unlink()
    target = tmp_path / "ptr"
    target.write_text(value, encoding="utf-8")
    pointer.symlink_to(target)
    assert doctor_main(["--graph", str(graph), "--indexer", "python"]) == 2
    assert "symlinked current pointer" in capsys.readouterr().err


def test_output_containment_through_symlink(tmp_path: Path, capsys):
    graph = _publish(tmp_path, *_py_rows())
    forbidden = _snap(graph) / "doctor.json"
    assert (
        doctor_main(
            ["--graph", str(graph), "--indexer", "python", "--output", str(forbidden)]
        )
        == 2
    )
    assert not forbidden.exists()
    alias = tmp_path / "alias"
    alias.symlink_to(graph)
    via = alias / "via.json"
    assert (
        doctor_main(["--graph", str(graph), "--indexer", "python", "--output", str(via)])
        == 2
    )
    assert not via.exists()


def test_flat_graph(tmp_path: Path):
    ents, rels, tus, obs = _py_rows()
    flat = tmp_path / "flat"
    flat.mkdir()
    pd.DataFrame(ents).to_parquet(flat / "entities.parquet")
    pd.DataFrame(rels).to_parquet(flat / "relationships.parquet")
    pd.DataFrame(tus).to_parquet(flat / "text_units.parquet")
    pd.DataFrame(obs).to_parquet(flat / "call_observations.parquet")
    files = [
        "entities.parquet",
        "relationships.parquet",
        "text_units.parquet",
        "call_observations.parquet",
    ]
    total = sum((flat / name).stat().st_size for name in files)
    (flat / "manifest.json").write_text(
        json.dumps(
            {
                "id": "flat-id",
                "created_at": "2026-08-14T00:00:00",
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
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    report = audit_graph_root(flat, indexer="python")
    assert report["ok"] is True, report["anomalies"]
    assert report.get("snapshot") is None


def test_malformed_json_and_missing_parquet(tmp_path: Path, capsys):
    graph = _publish(tmp_path, *_py_rows())
    snap = _snap(graph)
    original = (snap / "manifest.json").read_text(encoding="utf-8")
    (snap / "manifest.json").write_text('{"id": "a", "id": "b"}', encoding="utf-8")
    assert doctor_main(["--graph", str(graph), "--indexer", "python"]) == 2
    (snap / "manifest.json").write_text(
        original.replace('"corpus_hash": null', '"corpus_hash": NaN'),
        encoding="utf-8",
    )
    assert doctor_main(["--graph", str(graph), "--indexer", "python"]) == 2
    (snap / "manifest.json").write_text(original, encoding="utf-8")
    (snap / "entities.parquet").unlink()
    assert doctor_main(["--graph", str(graph), "--indexer", "python"]) == 2
    capsys.readouterr()


def test_non_mutating_dataframe_api():
    ents, rels, tus, obs = _py_rows()
    edf, rdf, tdf, odf = (
        pd.DataFrame(ents),
        pd.DataFrame(rels),
        pd.DataFrame(tus),
        pd.DataFrame(obs),
    )
    manifest = {
        "id": "20260814-000000-abcd1234",
        "created_at": "2026-08-14T00:00:00",
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
    before = (edf.copy(deep=True), rdf.copy(deep=True), tdf.copy(deep=True), odf.copy(deep=True))
    manifest_before = copy.deepcopy(manifest)
    report = audit_rows(
        edf,
        rdf,
        tdf,
        odf,
        manifest,
        indexer="python",
        snapshot_id=manifest["id"],
        present_files=present,
        file_sizes=sizes,
    )
    assert report["ok"] is True
    assert edf.equals(before[0])
    assert rdf.equals(before[1])
    assert tdf.equals(before[2])
    assert odf.equals(before[3])
    assert manifest == manifest_before


def test_graphrag_code_doctor_parity(tmp_path: Path):
    graph = _publish(tmp_path, *_py_rows())
    args = ["--graph", str(graph), "--indexer", "python", "--json"]
    standalone = subprocess.run(
        [sys.executable, str(DOCTOR), *args],
        capture_output=True,
        text=True,
    )
    wrapped = subprocess.run(
        [sys.executable, str(GRAPH_CODE), "doctor", *args],
        capture_output=True,
        text=True,
    )
    assert standalone.returncode == wrapped.returncode == 0
    assert json.loads(standalone.stdout) == json.loads(wrapped.stdout)
    broken = _publish(tmp_path, *_py_rows(), name="b")
    man = json.loads((_snap(broken) / "manifest.json").read_text(encoding="utf-8"))
    man["counts"]["entities"] = 0
    (_snap(broken) / "manifest.json").write_text(json.dumps(man, indent=2), encoding="utf-8")
    s2 = subprocess.run(
        [sys.executable, str(DOCTOR), "--graph", str(broken), "--indexer", "python"],
        capture_output=True,
        text=True,
    )
    w2 = subprocess.run(
        [sys.executable, str(GRAPH_CODE), "doctor", "--graph", str(broken), "--indexer", "python"],
        capture_output=True,
        text=True,
    )
    assert s2.returncode == w2.returncode == 1
    bad_standalone = subprocess.run(
        [sys.executable, str(DOCTOR), "--graph", str(graph), "--indexer", "rust"],
        capture_output=True,
        text=True,
    )
    bad_wrapped = subprocess.run(
        [sys.executable, str(GRAPH_CODE), "doctor", "--graph", str(graph), "--indexer", "rust"],
        capture_output=True,
        text=True,
    )
    assert bad_standalone.returncode == bad_wrapped.returncode == 2


def test_standalone_audit_clis_still_work(tmp_path: Path):
    graph = _publish(tmp_path, *_c_rows())
    snap = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "byog_snapshot_graph_audit.py"),
            "--graph",
            str(graph),
        ],
        capture_output=True,
        text=True,
    )
    coh = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "c_overlay_coherence_graph_audit.py"),
            "--graph",
            str(graph),
        ],
        capture_output=True,
        text=True,
    )
    assert snap.returncode == 0, snap.stderr
    assert coh.returncode == 0, coh.stderr


def test_health_python_c_and_frozen(tmp_path: Path):
    from published_graph_health import (  # type: ignore
        PublishedGraphSpec,
        _fresh_data,
        check_spec,
        load_specs,
    )

    specs = load_specs()
    python_spec = next(spec for spec in specs if spec.ident == "mini_game")
    fresh = _fresh_data(python_spec, ROOT)
    graph = tmp_path / "byog_mini_game_doc"
    publish_byog_snapshot(
        pd.DataFrame(fresh["entities"]),
        pd.DataFrame(fresh["relationships"]),
        pd.DataFrame(fresh["text_units"]),
        graph,
        settings_text="h: 1\n",
        source_root=(ROOT / python_spec.source).resolve(),
        call_observations_df=pd.DataFrame(fresh.get("call_observations") or []),
    )
    result = check_spec(python_spec, root=ROOT, graph_root=graph)
    assert result["status"] == "pass", result
    assert result["snapshot_integrity"]["ok"] is True
    for key in C_KEYS:
        assert key not in result
    frozen = next(spec for spec in specs if spec.mode == "frozen")
    exempt = check_spec(frozen, root=ROOT, graph_root=ROOT / "does-not-exist")
    assert exempt["status"] == "exempt"
    assert "snapshot_integrity" not in exempt
    c_spec = next(spec for spec in specs if spec.indexer == "c" and spec.mode == "mutable")
    c_result = check_spec(c_spec, root=ROOT)
    assert c_result["status"] in {"pass", "skipped"}, c_result
    if c_result["status"] == "pass":
        assert c_result["snapshot_integrity"]["ok"] is True
        for key in C_KEYS:
            assert key in c_result


def test_doctor_does_not_invoke_producers(tmp_path: Path, monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("doctor must not invoke producers or extractors")

    import byog_graph
    import c_clang_ast_capture as cap
    import c_compiler_common as common
    import c_compiler_facts as deps
    import c_compiler_includes as incs
    import c_preprocessor as pp
    import extract_c
    import extract_python
    import mini_game_to_byog

    monkeypatch.setattr(byog_graph, "publish_byog_snapshot", boom)
    monkeypatch.setattr(byog_graph, "cleanup_old_snapshots", boom)
    monkeypatch.setattr(byog_graph, "_publication_lock", boom)
    monkeypatch.setattr(mini_game_to_byog, "build_byog_for_package", boom)
    monkeypatch.setattr(extract_python, "extract_from_file", boom)
    monkeypatch.setattr(extract_c, "build_c_byog", boom)
    monkeypatch.setattr(pp, "analyze_package", boom)
    monkeypatch.setattr(pp, "annotate_byog", boom)
    monkeypatch.setattr(deps, "collect_translation_unit_dependencies", boom)
    monkeypatch.setattr(incs, "collect_configured_direct_includes", boom)
    monkeypatch.setattr(common, "load_compile_entries", boom)
    monkeypatch.setattr(cap, "capture_clang_ast_package", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "check_output", boom)
    ents, rels, tus, obs = _c_rows()
    manifest = {
        "id": "20260814-000000-abcd1234",
        "created_at": "2026-08-14T00:00:00",
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
    report = validate_persisted_graph_integrity(
        ents,
        rels,
        tus,
        obs,
        manifest,
        indexer="c",
        snapshot_id=manifest["id"],
        present_files=present,
        file_sizes=sizes,
    )
    assert report["ok"] is True, report["anomalies"]
    graph = tmp_path / "prebuilt"
    snap = graph / "snapshots" / manifest["id"]
    snap.mkdir(parents=True)
    pd.DataFrame(ents).to_parquet(snap / "entities.parquet")
    pd.DataFrame(rels).to_parquet(snap / "relationships.parquet")
    pd.DataFrame(tus).to_parquet(snap / "text_units.parquet")
    pd.DataFrame(obs).to_parquet(snap / "call_observations.parquet")
    files = list(manifest["files"])
    manifest["total_size_bytes"] = sum((snap / name).stat().st_size for name in files)
    (snap / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (graph / "current").write_text(manifest["id"], encoding="utf-8")
    assert audit_graph_root(graph, indexer="c")["ok"] is True


def test_published_mutable_roots_read_only():
    names = (
        "byog_mini_game",
        "byog_mini_lang",
        "byog_jsmn",
        "byog_inih",
        "byog_sqlparse",
        "byog_semver",
        "byog_dmp",
        "byog_charset_normalizer",
        "byog_cjson",
        "byog_jsonpatch",
        "byog_humanize",
    )
    for name in names:
        graph = ROOT / name
        if not (graph / "current").is_file():
            pytest.skip(f"{name} is absent locally")
        before = {
            path.relative_to(graph).as_posix(): path.read_bytes()
            for path in sorted(graph.rglob("*"))
            if path.is_file() and not path.is_symlink()
        }
        indexer = (
            "c" if name in {"byog_cjson", "byog_inih", "byog_jsmn"} else "python"
        )
        report = audit_graph_root(graph, indexer=indexer)
        after = {
            path.relative_to(graph).as_posix(): path.read_bytes()
            for path in sorted(graph.rglob("*"))
            if path.is_file() and not path.is_symlink()
        }
        assert after == before, name
        assert report["ok"] is True, (name, report["anomalies"])
        assert report["read_only_verification"]["verified"] is True
        if indexer == "c":
            assert set(report["components"]) == set(C_KEYS)
        else:
            assert report["components"] == {}


def test_format_and_json_roundtrip(tmp_path: Path):
    graph = _publish(tmp_path, *_py_rows())
    report = audit_graph_root(graph, indexer="python")
    text = format_report(report)
    assert "RESULT: PASS" in text
    assert "snapshot_envelope" in text
    parsed = json.loads(audit_to_json(report))
    assert parsed["ok"] is True
