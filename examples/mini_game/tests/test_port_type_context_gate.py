"""Fail-closed context-pack gate + optional C type_context configuration.

Does not rewrite published byog_* roots. Live Clang smokes skip when no Clang.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

from byog_graph import publish_byog_snapshot  # type: ignore
from port_eval import (  # type: ignore
    build_eval_report,
    gen_context_packs,
    load_gate_manifest,
    run_declared_gate,
    validate_type_context,
    validate_type_context_requirement,
    _index_gate,
    _tool_probe,
)


MANIFEST = ROOT / "scripts" / "port_gates.json"


def _entity(title: str, etype: str = "function") -> dict:
    return {
        "id": f"ent:{etype}:{title}",
        "title": title,
        "type": etype,
        "description": f"{etype} {title}",
        "source_file": "a.c",
        "span": "1:0-2:0",
        "extractor": "tree-sitter-c",
        "confidence": 1.0,
        "is_deterministic": True,
        "text_unit_ids": [f"tu:{title}"],
        "document_ids": ["doc:a"],
    }


def _uses(source: str, target: str, *, rid: str | None = None) -> dict:
    return {
        "id": rid or f"rel:uses_type:{source}->{target}",
        "source": source,
        "target": target,
        "type": "uses_type",
        "description": f"{source} uses_type {target}",
        "weight": 1.0,
        "text_unit_ids": [],
        "human_readable_id": 1,
        "source_file": "a.c",
        "span": "",
        "extractor": "clang-ast-json",
        "confidence": 1.0,
        "is_deterministic": True,
        "document_ids": [],
        "covariate_ids": [],
        "fact_kind": "configured_type_use",
        "clang_type_use_status": "matched",
        "clang_type_use_fact_kind": "configured_type_use",
        "clang_type_use_extractor": "clang-ast-json",
        "clang_type_use_confidence": 1.0,
        "clang_type_use_is_deterministic": True,
        "clang_type_use_observation_count": 1,
        "clang_type_use_use_kinds": ["parameter"],
        "clang_type_use_entry_indices": [0],
        "clang_type_use_compiler_path": "/usr/bin/clang",
        "clang_type_use_compiler_id": "clang",
        "clang_type_use_compilers": "[]",
        "clang_type_use_compile_commands_digest": "abc",
        "clang_type_use_observations_json": "[]",
        "clang_type_use_source_entity_id": f"ent:function:{source}",
        "clang_type_use_target_entity_id": f"ent:typedef:{target}",
        "clang_type_use_description": f"{source}->{target}",
    }


def _publish(tmp_path: Path, entities: list, relationships: list) -> Path:
    graph = tmp_path / "byog_gate"
    texts = [
        {
            "id": f"tu:{e['title']}",
            "text": f"// body of {e['title']}\n",
            "n_tokens": 2,
            "document_ids": ["doc:a"],
            "entity_ids": [e["id"]],
            "relationship_ids": [],
        }
        for e in entities
    ]
    publish_byog_snapshot(
        pd.DataFrame(entities),
        pd.DataFrame(relationships),
        pd.DataFrame(texts),
        graph,
        keep_last=2,
    )
    return graph


def _minimal_pack(symbol: str) -> dict:
    return {
        "symbol": symbol,
        "purpose": "port-to-rust",
        "entity": {"title": symbol},
        "neighbors": [],
    }


# ---------------------------------------------------------------------------
# Fail-closed packs
# ---------------------------------------------------------------------------


def _calls_edge(source: str, target: str) -> dict:
    return {
        "id": f"rel:calls:{source}->{target}",
        "source": source,
        "target": target,
        "type": "calls",
        "description": "calls",
        "weight": 0.9,
        "text_unit_ids": [],
        "human_readable_id": 1,
        "source_file": "a.c",
        "span": "1:0",
        "extractor": "tree-sitter-c",
        "confidence": 0.9,
        "is_deterministic": True,
        "document_ids": [],
        "covariate_ids": [],
    }


def test_failed_context_pack_makes_overall_pass_false(tmp_path: Path):
    entities = [_entity("m:f"), _entity("m:g")]
    graph = _publish(tmp_path, entities, [_calls_edge("m:f", "m:g")])
    # Fake a port directory with Cargo.toml so rust isn't the only skip path.
    port = tmp_path / "port"
    port.mkdir()
    (port / "Cargo.toml").write_text('[package]\nname="p"\nversion="0.1.0"\nedition="2021"\n')
    source = tmp_path / "source"
    source.mkdir()
    (source / "tests").mkdir()

    # Request a symbol that does not resolve → context_pack fails.
    report = build_eval_report(
        source=source,
        port_dir=port,
        graph=graph,
        target="t",
        symbols=["missing:symbol"],
        reindex=False,
        use_advanced=False,
        manual_fixes=0,
        skip_rust=True,
    )
    packs = report["context_packs"]
    assert packs["complete"] is False
    assert "missing:symbol" in packs["failed"]
    assert packs["failures"]
    assert report["overall_pass"] is False


def test_absent_type_context_preserves_legacy_invocation(tmp_path: Path):
    entities = [_entity("m:f"), _entity("m:g")]
    graph = _publish(tmp_path, entities, [_calls_edge("m:f", "m:g")])
    out = tmp_path / "packs"
    result = gen_context_packs(["m:f"], graph, out, type_context=None)
    assert result["complete"] is True
    assert result["extra_context_pack_args"] == []
    pack_path = Path(result["pack_paths"]["m:f"])
    pack = json.loads(pack_path.read_text())
    # No type_*_closure keys at default depth.
    assert "type_dependency_closure" not in pack
    assert "type_user_closure" not in pack
    # Re-run and compare bytes for stability.
    out2 = tmp_path / "packs2"
    result2 = gen_context_packs(["m:f"], graph, out2, type_context=None)
    assert Path(result2["pack_paths"]["m:f"]).read_bytes() == pack_path.read_bytes()


def test_stale_pack_cannot_satisfy_current_run(tmp_path: Path, monkeypatch):
    out = tmp_path / "packs"
    out.mkdir()
    stale = out / "context_pack_m_f.json"
    stale.write_text('{"stale": true}')

    def fake_run(cmd, cwd, timeout=600, env=None):
        # Simulate an erroneous zero exit that produces no current output.
        return {"status": "ok", "returncode": 0, "output_tail": []}

    monkeypatch.setattr("port_eval._run", fake_run)
    result = gen_context_packs(["m:f"], tmp_path / "graph", out)
    assert result["complete"] is False
    assert result["generated"] == []
    assert result["failures"][0]["stage"] == "generate"
    assert stale.exists() is False


def test_pack_outputs_are_contained_and_collision_safe(tmp_path: Path, monkeypatch):
    out = tmp_path / "packs"

    def fake_run(cmd, cwd, timeout=600, env=None):
        output = Path(cmd[cmd.index("--output") + 1])
        output.write_text(json.dumps(_minimal_pack(cmd[2])))
        return {"status": "ok", "returncode": 0, "output_tail": []}

    monkeypatch.setattr("port_eval._run", fake_run)
    symbols = ["../escape", "a:b_c", "a_b:c"]
    result = gen_context_packs(symbols, tmp_path / "graph", out)
    assert result["complete"] is True
    paths = [Path(result["pack_paths"][symbol]).resolve() for symbol in symbols]
    assert len(set(paths)) == len(symbols)
    assert all(path.parent == out.resolve() for path in paths)
    assert not (tmp_path / "escape.json").exists()


def test_success_exit_with_invalid_json_is_incomplete(tmp_path: Path, monkeypatch):
    def fake_run(cmd, cwd, timeout=600, env=None):
        output = Path(cmd[cmd.index("--output") + 1])
        output.write_text("not-json")
        return {"status": "ok", "returncode": 0, "output_tail": []}

    monkeypatch.setattr("port_eval._run", fake_run)
    result = gen_context_packs(["m:f"], tmp_path / "graph", tmp_path / "packs")
    assert result["complete"] is False
    assert result["generated"] == []
    assert result["failures"][0]["stage"] == "validate"
    assert "unreadable pack JSON" in result["failures"][0]["reason"]

    def fake_object_run(cmd, cwd, timeout=600, env=None):
        output = Path(cmd[cmd.index("--output") + 1])
        output.write_text("{}")
        return {"status": "ok", "returncode": 0, "output_tail": []}

    monkeypatch.setattr("port_eval._run", fake_object_run)
    result2 = gen_context_packs(
        ["m:f"], tmp_path / "graph", tmp_path / "packs-object"
    )
    assert result2["complete"] is False
    assert result2["failures"][0]["reason"] == "invalid context-pack schema"


# ---------------------------------------------------------------------------
# Manifest validation
# ---------------------------------------------------------------------------


def test_shipped_manifest_type_context_loads():
    gates = load_gate_manifest(MANIFEST)
    assert "type_context" in gates["inih"]
    assert gates["inih"]["type_context"]["clang_type_uses"] is True
    assert gates["inih"]["type_context"]["depth"] == 2
    assert "type_context" in gates["cjson"]
    assert any(
        r["symbol"] == "cJSON:cJSON_Delete"
        for r in gates["cjson"]["type_context"]["requirements"]
    )
    # Python profiles must not gain type_context.
    assert "type_context" not in gates["mini_game"]


def test_strict_manifest_validation_rejects_bad_type_context():
    with pytest.raises(ValueError, match="unknown key"):
        validate_type_context(
            {
                "clang_type_uses": True,
                "depth": 2,
                "extra": 1,
            },
            indexer="c",
            symbols=["a:b"],
        )
    with pytest.raises(ValueError, match="only valid for C"):
        validate_type_context(
            {"clang_type_uses": True, "depth": 2},
            indexer="python",
            symbols=["a:b"],
        )
    with pytest.raises(ValueError, match="positive integer"):
        validate_type_context(
            {"clang_type_uses": True, "depth": 0},
            indexer="c",
            symbols=["a:b"],
        )
    with pytest.raises(ValueError, match="non-negative"):
        validate_type_context(
            {
                "clang_type_uses": True,
                "depth": 2,
                "max_type_edges": -1,
            },
            indexer="c",
            symbols=["a:b"],
        )
    with pytest.raises(ValueError, match="not in the profile"):
        validate_type_context(
            {
                "clang_type_uses": True,
                "depth": 2,
                "requirements": [
                    {
                        "symbol": "other:x",
                        "direction": "dependencies",
                        "require_nonempty": True,
                        "allow_truncation": False,
                    }
                ],
            },
            indexer="c",
            symbols=["a:b"],
        )
    with pytest.raises(ValueError, match="must not contain duplicates"):
        validate_type_context(
            {"clang_type_uses": True, "depth": 2},
            indexer="c",
            symbols=["a:b", "a:b"],
        )
    with pytest.raises(ValueError, match="duplicates"):
        validate_type_context(
            {
                "clang_type_uses": True,
                "depth": 2,
                "requirements": [
                    {"symbol": "a:b", "direction": "dependencies"},
                    {"symbol": "a:b", "direction": "dependencies"},
                ],
            },
            indexer="c",
            symbols=["a:b"],
        )


def test_load_gate_manifest_rejects_type_context_on_python(tmp_path: Path):
    data = json.loads(MANIFEST.read_text())
    for entry in data["ports"]:
        if entry.get("id") == "mini_game":
            entry["type_context"] = {
                "clang_type_uses": True,
                "depth": 2,
                "requirements": [],
            }
    path = tmp_path / "port_gates.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="only valid for C"):
        load_gate_manifest(path)

    valid = json.loads(MANIFEST.read_text())
    charset = next(entry for entry in valid["ports"] if entry["id"] == "charset_normalizer")
    full_examples = next(check for check in charset["checks"] if check["name"] == "full examples pytest")
    assert full_examples["timeout_seconds"] == 1200
    assert load_gate_manifest(MANIFEST)["charset_normalizer"]["checks"][2]["timeout_seconds"] == 1200
    for invalid_timeout in (True, 0, 3601, 1.5, "1200"):
        invalid = json.loads(MANIFEST.read_text())
        invalid_charset = next(
            entry for entry in invalid["ports"] if entry["id"] == "charset_normalizer"
        )
        invalid_charset["checks"][2]["timeout_seconds"] = invalid_timeout
        path.write_text(json.dumps(invalid))
        with pytest.raises(ValueError, match="timeout_seconds"):
            load_gate_manifest(path)


# ---------------------------------------------------------------------------
# Index flag wiring + Clang probe
# ---------------------------------------------------------------------------


def test_index_gate_wires_clang_type_uses_flag(tmp_path: Path, monkeypatch):
    captured: dict[str, Any] = {}

    def fake_run(cmd, cwd, timeout=600, env=None):
        captured["cmd"] = list(cmd)
        return {
            "status": "ok",
            "returncode": 0,
            "cmd": " ".join(cmd),
            "output_tail": [],
            "elapsed_seconds": 0.01,
        }

    monkeypatch.setattr("port_eval._run", fake_run)
    monkeypatch.setattr(
        "port_eval._tool_probe",
        lambda tool: {"status": "ok", "tool": "clang"},
    )
    gate = {
        "id": "inih",
        "indexer": "c",
        "source": "examples/inih",
        "type_context": {
            "clang_type_uses": True,
            "depth": 2,
            "max_type_edges": 50,
            "max_type_observations": 5,
            "requirements": [],
        },
    }
    graph = tmp_path / "g"
    result = _index_gate(gate, graph)
    assert result["status"] == "ok"
    assert "--clang-type-uses" in captured["cmd"]
    assert result["clang_type_uses"] is True
    # Disposable path only.
    assert "byog_inih" not in " ".join(captured["cmd"])

    # Without type_context, flag absent.
    captured.clear()
    gate2 = {"id": "jsmn", "indexer": "c", "source": "examples/jsmn"}
    result2 = _index_gate(gate2, graph)
    assert result2["status"] == "ok"
    assert "--clang-type-uses" not in captured["cmd"]


def test_missing_clang_is_skip_broken_is_fail(monkeypatch):
    # No clang/cc on PATH → skip
    monkeypatch.setattr("port_eval.shutil.which", lambda name: None)
    probe = _tool_probe("clang")
    assert probe["status"] == "skipped"

    # clang present but --version fails → fail
    def which(name):
        return "/usr/bin/clang" if name == "clang" else None

    monkeypatch.setattr("port_eval.shutil.which", which)

    def fake_run(cmd, cwd, timeout=600, env=None):
        return {
            "status": "fail",
            "returncode": 1,
            "cmd": " ".join(cmd),
            "output_tail": ["boom"],
            "elapsed_seconds": 0.0,
        }

    monkeypatch.setattr("port_eval._run", fake_run)
    probe2 = _tool_probe("clang")
    assert probe2["status"] == "fail"

    # A binary installed as clang but proving a non-Clang identity is broken,
    # not equivalent to an absent optional tool.
    def fake_ok_run(cmd, cwd, timeout=600, env=None):
        return {
            "status": "ok",
            "returncode": 0,
            "cmd": " ".join(cmd),
            "output_tail": ["gcc"],
            "elapsed_seconds": 0.0,
        }

    def reject_identity(executable):
        from c_compiler_common import CompilerOverlayError  # type: ignore

        raise CompilerOverlayError("not clang")

    monkeypatch.setattr("port_eval._run", fake_ok_run)
    monkeypatch.setattr("c_compiler_common.require_clang_identity", reject_identity)
    probe3 = _tool_probe("clang")
    assert probe3["status"] == "fail"


# ---------------------------------------------------------------------------
# Requirement validation
# ---------------------------------------------------------------------------


def test_requirement_success_and_failure_modes():
    good = {
        "type_dependency_closure": {
            "root": "A",
            "direction": "dependencies",
            "max_depth": 2,
            "n_nodes_total": 2,
            "n_edges_total": 1,
            "n_nodes_returned": 2,
            "n_edges_returned": 1,
            "nodes_truncated": False,
            "edges_truncated": False,
            "nodes": [
                {"title": "A", "depth": 0},
                {"title": "B", "depth": 1},
            ],
            "edges": [{"id": "e1", "source": "A", "target": "B", "depth": 0}],
        }
    }
    req = {
        "symbol": "A",
        "direction": "dependencies",
        "require_nonempty": True,
        "allow_truncation": False,
    }
    ok = validate_type_context_requirement(good, req)
    assert ok["ok"] is True

    empty = {"type_dependency_closure": {
        "root": "A",
        "direction": "dependencies",
        "max_depth": 2,
        "n_nodes_total": 1,
        "n_edges_total": 0,
        "n_nodes_returned": 1,
        "n_edges_returned": 0,
        "nodes_truncated": False,
        "edges_truncated": False,
        "nodes": [{"title": "A", "depth": 0}],
        "edges": [],
    }}
    assert validate_type_context_requirement(empty, req)["ok"] is False

    trunc = json.loads(json.dumps(good))
    trunc["type_dependency_closure"]["nodes_truncated"] = True
    trunc["type_dependency_closure"]["n_nodes_returned"] = 1
    trunc["type_dependency_closure"]["nodes"] = [{"title": "A", "depth": 0}]
    bad_trunc = validate_type_context_requirement(trunc, req)
    assert bad_trunc["ok"] is False
    assert "truncated_forbidden" in bad_trunc["errors"]

    missing_entity = json.loads(json.dumps(good))
    missing_entity["type_dependency_closure"]["nodes"][1]["entity_status"] = "missing"
    bad_ent = validate_type_context_requirement(missing_entity, req)
    assert bad_ent["ok"] is False
    assert any(e.startswith("entity_status=missing") for e in bad_ent["errors"])

    missing_rel = json.loads(json.dumps(good))
    missing_rel["type_dependency_closure"]["edges"][0]["relationship_status"] = "missing"
    bad_rel = validate_type_context_requirement(missing_rel, req)
    assert bad_rel["ok"] is False
    assert any("relationship_status=missing" in e for e in bad_rel["errors"])

    assert validate_type_context_requirement({}, req)["ok"] is False

    malformed = json.loads(json.dumps(good))
    malformed["type_dependency_closure"]["n_nodes_total"] = "2"
    malformed["type_dependency_closure"]["nodes"] = {"A": 0}
    bad_shape = validate_type_context_requirement(malformed, req)
    assert bad_shape["ok"] is False
    assert "invalid_n_nodes_total" in bad_shape["errors"]
    assert "invalid_nodes" in bad_shape["errors"]

    wrong_root = json.loads(json.dumps(good))
    wrong_root["type_dependency_closure"]["root"] = "stale:A"
    assert "root_mismatch" in validate_type_context_requirement(wrong_root, req)["errors"]


def test_gen_context_packs_validates_type_requirements(tmp_path: Path):
    entities = [
        _entity("A", "function"),
        _entity("B", "typedef"),
        _entity("C", "typedef"),
    ]
    rels = [_uses("A", "B"), _uses("B", "C", rid="rel:uses_type:B->C")]
    graph = _publish(tmp_path, entities, rels)
    type_ctx = {
        "clang_type_uses": True,
        "depth": 2,
        "max_type_edges": 50,
        "max_type_observations": 5,
        "requirements": [
            {
                "symbol": "A",
                "direction": "dependencies",
                "require_nonempty": True,
                "allow_truncation": False,
            }
        ],
    }
    out = tmp_path / "packs"
    result = gen_context_packs(["A"], graph, out, type_context=type_ctx)
    assert result["complete"] is True
    assert result["type_requirements_ok"] is True
    assert result["extra_context_pack_args"] == [
        "--type-depth",
        "2",
        "--max-type-edges",
        "50",
        "--max-type-observations",
        "5",
    ]
    pack = json.loads(Path(result["pack_paths"]["A"]).read_text())
    assert "type_dependency_closure" in pack
    assert pack["type_dependency_closure"]["n_edges_total"] >= 1

    # Force truncation failure with tiny caps.
    type_ctx_trunc = dict(type_ctx)
    type_ctx_trunc["max_type_edges"] = 1
    result2 = gen_context_packs(
        ["A"], graph, tmp_path / "packs2", type_context=type_ctx_trunc
    )
    # depth 2 with 3 nodes may truncate at max_nodes=1
    assert result2["complete"] is False
    assert result2["type_requirements_ok"] is False


# ---------------------------------------------------------------------------
# Declared gate failure on incomplete packs
# ---------------------------------------------------------------------------


def test_declared_gate_fails_when_packs_incomplete(monkeypatch):
    observed: dict[str, int] = {}
    gate = {
        "id": "toy",
        "kind": "port",
        "source": "examples/inih",
        "port": "examples/inih_rust",
        "indexer": "c",
        "symbols": ["ini:ini_parse"],
        "checks": [
            {
                "name": "bounded long check",
                "command": ["unused"],
                "timeout_seconds": 123,
            }
        ],
        "manual_fixes": 0,
        "run_binary": False,
    }

    monkeypatch.setattr(
        "port_eval._index_gate",
        lambda g, graph: {
            "name": "c graph index",
            "status": "ok",
            "elapsed_seconds": 0.0,
        },
    )
    monkeypatch.setattr(
        "port_eval._tool_probe",
        lambda tool: {"status": "ok", "tool": tool},
    )

    def fake_run(cmd, cwd, timeout=600, env=None):
        observed["timeout"] = timeout
        return {
            "status": "ok",
            "returncode": 0,
            "cmd": " ".join(cmd),
            "output_tail": [],
            "elapsed_seconds": 0.0,
        }

    monkeypatch.setattr("port_eval._run", fake_run)

    def fake_build_eval_report(**kwargs):
        return {
            "target": "toy",
            "graph": {
                "structural_pass_rate": 1.0,
                "total_calls": 0,
                "clean": True,
                "structural_anomalies": 0,
                "dangling_targets": 0,
                "semantic_suspicions": 0,
                "observations": 0,
            },
            "context_packs": {
                "requested": ["ini:ini_parse"],
                "generated": [],
                "failed": ["ini:ini_parse"],
                "count": 0,
                "complete": False,
                "failures": [
                    {
                        "symbol": "ini:ini_parse",
                        "stage": "generate",
                        "reason": "forced",
                        "detail": {},
                    }
                ],
                "type_requirements_ok": True,
            },
            "rust": {"all_ok": True, "status": "ok"},
            "golden_scenarios": {
                "count": 0,
                "passed": True,
                "contract_coverage": {"complete": True, "missing": []},
            },
            "manual_fix_count": 0,
            "overall_pass": False,
        }

    monkeypatch.setattr("port_eval.build_eval_report", fake_build_eval_report)
    result = run_declared_gate(gate, full=False, scale=False, differential_full=False)
    assert result["status"] == "fail"
    assert result["port_eval"]["context_packs"]["complete"] is False
    assert observed["timeout"] == 123


# ---------------------------------------------------------------------------
# Live disposable smokes
# ---------------------------------------------------------------------------


def _clang_ok() -> bool:
    probe = _tool_probe("clang")
    return probe.get("status") == "ok"


@pytest.mark.skipif(not _clang_ok(), reason="no verified Clang for type_context smoke")
def test_live_inih_disposable_type_context_gate(tmp_path: Path):
    """Run inih gate against disposable graph under tmp; never rewrite byog_inih."""
    gates = load_gate_manifest(MANIFEST)
    gate = dict(gates["inih"])
    # Redirect disposable graph root via monkeypatch of path construction.
    published = ROOT / "byog_inih"
    published_before = None
    if published.exists():
        current = published / "current"
        published_before = current.read_text() if current.is_file() else None

    # Point output/port_gates/inih/graph under tmp by monkeypatching ROOT path
    # used inside run_declared_gate — instead call pieces directly.
    graph = tmp_path / "port_gates" / "inih" / "graph"
    index = _index_gate(gate, graph)
    assert index["status"] == "ok", index
    assert index.get("clang_type_uses") is True
    assert "--clang-type-uses" in index.get("command", [])

    report = build_eval_report(
        source=ROOT / gate["source"],
        port_dir=ROOT / gate["port"],
        graph=graph,
        target="inih_type_ctx_test",
        symbols=list(gate["symbols"]),
        reindex=False,
        use_advanced=False,
        manual_fixes=0,
        skip_rust=True,
        type_context=gate["type_context"],
    )
    packs = report["context_packs"]
    assert packs["complete"] is True, packs.get("failures")
    assert packs["type_requirements_ok"] is True
    # overall_pass is None when skip_rust and packs complete
    assert report["overall_pass"] is None

    if published_before is not None:
        assert (published / "current").read_text() == published_before
    # No package artifacts.
    pkg = ROOT / "examples" / "inih"
    for pattern in ("*.o", "*.ast", "*.d", "*.i"):
        assert not list(pkg.glob(pattern))
        assert not list(pkg.glob(f"**/{pattern}"))


@pytest.mark.skipif(not _clang_ok(), reason="no verified Clang for type_context smoke")
def test_live_cjson_disposable_type_context_gate(tmp_path: Path):
    gates = load_gate_manifest(MANIFEST)
    gate = dict(gates["cjson"])
    published = ROOT / "byog_cjson"
    published_before = None
    if published.exists():
        current = published / "current"
        published_before = current.read_text() if current.is_file() else None

    graph = tmp_path / "port_gates" / "cjson" / "graph"
    index = _index_gate(gate, graph)
    assert index["status"] == "ok", index
    assert "--clang-type-uses" in index.get("command", [])

    report = build_eval_report(
        source=ROOT / gate["source"],
        port_dir=ROOT / gate["port"],
        graph=graph,
        target="cjson_type_ctx_test",
        symbols=list(gate["symbols"]),
        reindex=False,
        use_advanced=False,
        manual_fixes=0,
        skip_rust=True,
        type_context=gate["type_context"],
    )
    packs = report["context_packs"]
    assert packs["complete"] is True, packs.get("failures")
    assert packs["type_requirements_ok"] is True
    # Ensure the required symbol pack has a dependency closure.
    path = Path(packs["pack_paths"]["cJSON:cJSON_Delete"])
    pack = json.loads(path.read_text())
    cl = pack["type_dependency_closure"]
    assert cl["n_edges_total"] >= 1
    assert cl["nodes_truncated"] is False
    assert cl["edges_truncated"] is False

    if published_before is not None:
        assert (published / "current").read_text() == published_before
    pkg = ROOT / "examples" / "cjson"
    for pattern in ("*.o", "*.ast", "*.d", "*.i"):
        assert not list(pkg.glob(pattern))
