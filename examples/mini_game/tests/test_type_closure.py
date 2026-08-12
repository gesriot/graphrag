"""Bounded cycle-safe transitive uses_type closure (consumer-only).

Does not modify extraction, overlays, or published byog_* roots.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from scripts.byog_graph import (  # type: ignore
    ByogGraph,
    compute_uses_type_closure,
    publish_byog_snapshot,
)
from scripts.graph_query import (  # type: ignore
    format_type_closure_human,
    type_closure as free_type_closure,
    types_used_by,
    type_users,
)


def _entity(title: str, etype: str, **extra) -> dict:
    e = {
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
    e.update(extra)
    return e


def _uses(
    source: str,
    target: str,
    *,
    rid: str | None = None,
    hid: int = 1,
) -> dict:
    return {
        "id": rid or f"rel:uses_type:{source}->{target}",
        "source": source,
        "target": target,
        "type": "uses_type",
        "description": f"{source} uses_type {target}",
        "weight": 1.0,
        "text_unit_ids": [],
        "human_readable_id": hid,
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
        "clang_type_use_compiler_id": "clang test",
        "clang_type_use_compilers": json.dumps(
            [
                {
                    "compiler_path": "/usr/bin/clang",
                    "compiler_id": "clang test",
                    "compile_commands_digest": "abc",
                }
            ],
            sort_keys=True,
            separators=(",", ":"),
        ),
        "clang_type_use_compile_commands_digest": "abc",
        "clang_type_use_observations_json": json.dumps(
            [
                {
                    "use_kind": "parameter",
                    "entry_indices": [0],
                    "qualType": "T",
                    "source_path": "a.c",
                    "location_precision": "declaration_bearing_node",
                    "location_origin": "direct",
                    "resolver": "unique_typedef_spelling",
                    "owner_resolver": "exact_declaration_site",
                    "desugaredQualType": None,
                }
            ],
            sort_keys=True,
            separators=(",", ":"),
        ),
        "clang_type_use_source_entity_id": f"ent:function:{source}",
        "clang_type_use_target_entity_id": f"ent:typedef:{target}",
        "clang_type_use_description": f"{source}->{target}",
    }


def _calls(source: str, target: str, hid: int = 1) -> dict:
    return {
        "id": f"rel:calls:{source}->{target}",
        "source": source,
        "target": target,
        "type": "calls",
        "description": "calls",
        "weight": 0.9,
        "text_unit_ids": [],
        "human_readable_id": hid,
        "source_file": "a.c",
        "span": "1:0",
        "extractor": "tree-sitter-c",
        "confidence": 0.9,
        "is_deterministic": True,
        "document_ids": [],
        "covariate_ids": [],
    }


def _contains(source: str, target: str, hid: int = 1) -> dict:
    return {
        "id": f"rel:contains:{source}->{target}",
        "source": source,
        "target": target,
        "type": "contains",
        "description": "contains",
        "weight": 1.0,
        "text_unit_ids": [],
        "human_readable_id": hid,
        "source_file": "",
        "span": "",
        "extractor": "tree-sitter-c",
        "confidence": 1.0,
        "is_deterministic": True,
        "document_ids": [],
        "covariate_ids": [],
    }


def _publish(tmp_path: Path, entities: list, relationships: list) -> Path:
    graph = tmp_path / "byog_closure"
    texts = [
        {
            "id": f"tu:{e['title']}",
            "text": f"// body of {e['title']}\nint x;\n",
            "n_tokens": 3,
            "document_ids": ["doc:a"],
            "entity_ids": [e["id"]],
            "relationship_ids": [],
        }
        for e in entities
        if e.get("type") != "file"
    ]
    publish_byog_snapshot(
        pd.DataFrame(entities),
        pd.DataFrame(relationships),
        pd.DataFrame(texts),
        graph,
        keep_last=2,
    )
    return graph


def _node_map(result: dict) -> dict[str, int]:
    return {n["title"]: n["depth"] for n in result["nodes"]}


def _edge_pairs(result: dict) -> list[tuple[str, str]]:
    return [(e["source"], e["target"]) for e in result["edges"]]


# ---------------------------------------------------------------------------
# Pure BFS / topology
# ---------------------------------------------------------------------------


def test_linear_chain(tmp_path: Path):
    entities = [
        _entity("A", "function"),
        _entity("B", "typedef"),
        _entity("C", "typedef"),
        _entity("D", "typedef"),
    ]
    rels = [
        _uses("A", "B", hid=1),
        _uses("B", "C", hid=2),
        _uses("C", "D", hid=3),
    ]
    graph = _publish(tmp_path, entities, rels)
    g = ByogGraph(graph)
    r = g.type_closure("A", direction="dependencies", max_depth=3)
    assert r["resolved"] is True
    assert r["root"] == "A"
    assert _node_map(r) == {"A": 0, "B": 1, "C": 2, "D": 3}
    assert r["n_nodes_total"] == 4
    assert r["n_edges_total"] == 3
    assert r["nodes_truncated"] is False
    assert _edge_pairs(r) == [("A", "B"), ("B", "C"), ("C", "D")]


def test_diamond_minimum_depth(tmp_path: Path):
    # A -> B, A -> C, B -> D, C -> D
    entities = [_entity(t, "typedef") for t in "ABCD"]
    entities[0] = _entity("A", "function")
    rels = [
        _uses("A", "B", hid=1),
        _uses("A", "C", hid=2),
        _uses("B", "D", hid=3),
        _uses("C", "D", hid=4, rid="rel:uses_type:C->D:alt"),
    ]
    graph = _publish(tmp_path, entities, rels)
    r = ByogGraph(graph).type_closure("A", max_depth=5)
    assert _node_map(r)["D"] == 2  # min depth via either branch
    assert r["n_nodes_total"] == 4
    assert r["n_edges_total"] == 4


def test_directed_cycle(tmp_path: Path):
    entities = [_entity(t, "typedef") for t in "ABC"]
    rels = [
        _uses("A", "B", hid=1),
        _uses("B", "C", hid=2),
        _uses("C", "A", hid=3),
    ]
    graph = _publish(tmp_path, entities, rels)
    r = ByogGraph(graph).type_closure("A", max_depth=10)
    assert r["n_nodes_total"] == 3
    assert set(_node_map(r)) == {"A", "B", "C"}
    assert _node_map(r)["A"] == 0
    assert r["n_edges_total"] == 3
    # Finite: no blow-up
    assert r["n_nodes_returned"] == 3


def test_self_edge(tmp_path: Path):
    entities = [_entity("T", "struct")]
    rels = [_uses("T", "T", hid=1, rid="rel:uses_type:T->T:self")]
    # Fix entity id fields for self edge (struct not function/typedef).
    rels[0]["clang_type_use_source_entity_id"] = "ent:struct:T"
    rels[0]["clang_type_use_target_entity_id"] = "ent:struct:T"
    graph = _publish(tmp_path, entities, rels)
    r = ByogGraph(graph).type_closure("T", max_depth=3)
    assert r["n_nodes_total"] == 1
    assert r["nodes"] == [{"title": "T", "depth": 0}]
    assert r["n_edges_total"] == 1
    assert r["edges"][0]["source"] == "T"
    assert r["edges"][0]["target"] == "T"
    assert r["edges"][0]["depth"] == 0


def test_incoming_users_traversal(tmp_path: Path):
    # f -> T, g -> T, h -> f (h uses type f? unusual but valid graph)
    entities = [
        _entity("f", "function"),
        _entity("g", "function"),
        _entity("T", "typedef"),
        _entity("h", "function"),
    ]
    rels = [
        _uses("f", "T", hid=1),
        _uses("g", "T", hid=2),
        _uses("h", "f", hid=3, rid="rel:uses_type:h->f"),
    ]
    graph = _publish(tmp_path, entities, rels)
    r = ByogGraph(graph).type_closure("T", direction="users", max_depth=2)
    depths = _node_map(r)
    assert depths["T"] == 0
    assert depths["f"] == 1
    assert depths["g"] == 1
    assert depths["h"] == 2
    assert r["n_nodes_total"] == 4


def test_both_direction_traversal(tmp_path: Path):
    # U -> Root -> D
    entities = [
        _entity("Root", "typedef"),
        _entity("U", "function"),
        _entity("D", "typedef"),
    ]
    rels = [
        _uses("U", "Root", hid=1),
        _uses("Root", "D", hid=2),
    ]
    graph = _publish(tmp_path, entities, rels)
    r = ByogGraph(graph).type_closure("Root", direction="both", max_depth=1)
    assert set(_node_map(r)) == {"Root", "U", "D"}
    assert r["n_edges_total"] == 2


def test_unrelated_relationship_types_ignored(tmp_path: Path):
    entities = [
        _entity("A", "function"),
        _entity("B", "function"),
        _entity("T", "typedef"),
    ]
    rels = [
        _calls("A", "B", hid=1),
        _contains("file", "A", hid=2),
        _uses("A", "T", hid=3),
        {
            "id": "rel:depends_on:A->B",
            "source": "A",
            "target": "B",
            "type": "depends_on",
            "description": "dep",
            "weight": 1.0,
            "text_unit_ids": [],
            "human_readable_id": 4,
            "source_file": "",
            "span": "",
            "extractor": "compiler",
            "confidence": 1.0,
            "is_deterministic": True,
            "document_ids": [],
            "covariate_ids": [],
        },
    ]
    graph = _publish(tmp_path, entities, rels)
    g = ByogGraph(graph)
    r = g.type_closure("A", max_depth=5)
    assert set(_node_map(r)) == {"A", "T"}
    assert _edge_pairs(r) == [("A", "T")]
    # Call graph unchanged.
    assert g.callees("A") == ["B"]
    assert g.callers("B") == ["A"]


def test_deterministic_ordering_under_shuffled_rows(tmp_path: Path):
    entities = [
        _entity("A", "function"),
        _entity("Z", "typedef"),
        _entity("M", "typedef"),
        _entity("B", "typedef"),
    ]
    rels = [
        _uses("A", "Z", hid=1, rid="rel:z"),
        _uses("A", "B", hid=2, rid="rel:b"),
        _uses("A", "M", hid=3, rid="rel:m"),
    ]
    # Shuffle rows before publish.
    rels = [rels[1], rels[2], rels[0]]
    graph = _publish(tmp_path, entities, rels)
    r1 = ByogGraph(graph).type_closure("A", max_depth=1)
    r2 = ByogGraph(graph).type_closure("A", max_depth=1)
    assert r1 == r2
    assert [n["title"] for n in r1["nodes"]] == ["A", "B", "M", "Z"]
    assert [e["id"] for e in r1["edges"]] == ["rel:b", "rel:m", "rel:z"]


def test_max_depth_0_1_2(tmp_path: Path):
    entities = [
        _entity("A", "function"),
        _entity("B", "typedef"),
        _entity("C", "typedef"),
    ]
    rels = [_uses("A", "B", hid=1), _uses("B", "C", hid=2)]
    graph = _publish(tmp_path, entities, rels)
    g = ByogGraph(graph)
    d0 = g.type_closure("A", max_depth=0)
    assert _node_map(d0) == {"A": 0}
    assert d0["n_edges_total"] == 0
    d1 = g.type_closure("A", max_depth=1)
    assert _node_map(d1) == {"A": 0, "B": 1}
    assert d1["n_edges_total"] == 1
    d2 = g.type_closure("A", max_depth=2)
    assert _node_map(d2) == {"A": 0, "B": 1, "C": 2}
    assert d2["n_edges_total"] == 2


def test_independent_node_and_edge_caps_with_exact_totals(tmp_path: Path):
    entities = [_entity("A", "function")] + [
        _entity(f"T{i}", "typedef") for i in range(5)
    ]
    rels = [_uses("A", f"T{i}", hid=i + 1, rid=f"rel:{i}") for i in range(5)]
    # Chain one hop further from T0 for extra edges at depth 1 expansion only
    # from A; add T0->T1 already covered. Add deeper edges A doesn't need.
    graph = _publish(tmp_path, entities, rels)
    r = ByogGraph(graph).type_closure(
        "A", max_depth=1, max_nodes=2, max_edges=2
    )
    assert r["n_nodes_total"] == 6  # A + 5 targets
    assert r["n_edges_total"] == 5
    assert r["n_nodes_returned"] == 2
    assert r["n_edges_returned"] == 2
    assert r["nodes_truncated"] is True
    assert r["edges_truncated"] is True
    # Returned still deterministic prefix of sorted material.
    assert r["nodes"][0] == {"title": "A", "depth": 0}


def test_negative_limits_rejected():
    rels = pd.DataFrame([_uses("A", "B")])
    with pytest.raises(ValueError, match="max_depth"):
        compute_uses_type_closure(rels, "A", max_depth=-1)
    with pytest.raises(ValueError, match="max_nodes"):
        compute_uses_type_closure(rels, "A", max_nodes=-1)
    with pytest.raises(ValueError, match="max_edges"):
        compute_uses_type_closure(rels, "A", max_edges=-1)
    with pytest.raises(ValueError, match="direction"):
        compute_uses_type_closure(rels, "A", direction="sideways")


@pytest.mark.parametrize(
    ("field", "value"),
    [("id", 7), ("source", None), ("target", "")],
)
def test_malformed_uses_type_values_fail_closed(field: str, value):
    rel = _uses("A", "B")
    rel[field] = value
    with pytest.raises(ValueError, match=rf"invalid {field}"):
        compute_uses_type_closure(pd.DataFrame([rel]), "A")


def test_missing_uses_type_column_and_duplicate_ids_fail_closed():
    missing_target = pd.DataFrame([_uses("A", "B")]).drop(columns=["target"])
    with pytest.raises(ValueError, match="missing required columns.*target"):
        compute_uses_type_closure(missing_target, "A")

    first = _uses("A", "B", rid="rel:duplicate")
    second = _uses("B", "C", rid="rel:duplicate")
    with pytest.raises(ValueError, match="duplicate uses_type relationship id"):
        compute_uses_type_closure(pd.DataFrame([first, second]), "A")


def test_unresolved_and_ambiguous_symbols(tmp_path: Path):
    entities = [
        _entity("m:f", "function"),
        _entity("m:g", "function"),
        _entity("other:f", "function"),
    ]
    rels = [_uses("m:f", "m:g", hid=1)]
    # Make m:g a typedef target properly
    entities[1] = _entity("m:g", "typedef")
    graph = _publish(tmp_path, entities, rels)
    g = ByogGraph(graph)
    missing = g.type_closure("does-not-exist")
    assert missing["resolved"] is False
    assert missing["root"] is None
    assert missing["n_nodes_total"] == 0
    # Ambiguous partial "f" matches m:f and other:f → resolve returns None
    amb = g.type_closure("f")
    assert amb["resolved"] is False
    # Free function matches ByogGraph.
    assert free_type_closure(g.ents, g.rels, "m:f", max_depth=1) == g.type_closure(
        "m:f", max_depth=1
    )


def test_direct_type_queries_unchanged(tmp_path: Path):
    entities = [
        _entity("m:f", "function"),
        _entity("m:T", "typedef"),
        _entity("m:g", "function"),
    ]
    rels = [
        _uses("m:f", "m:T", hid=1),
        _calls("m:f", "m:g", hid=2),
    ]
    graph = _publish(tmp_path, entities, rels)
    g = ByogGraph(graph)
    assert g.types_used_by("m:f") == ["m:T"]
    assert g.type_users("m:T") == ["m:f"]
    assert types_used_by(g.ents, g.rels, "m:f") == ["m:T"]
    assert type_users(g.ents, g.rels, "m:T") == ["m:f"]
    assert g.callees("m:f") == ["m:g"]
    assert g.callers("m:g") == ["m:f"]
    # impact only follows calls
    assert g.impact("m:g") == ["m:f"]


def test_no_uses_type_empty_closure(tmp_path: Path):
    entities = [_entity("m:f", "function"), _entity("m:g", "function")]
    rels = [_calls("m:f", "m:g")]
    graph = _publish(tmp_path, entities, rels)
    r = ByogGraph(graph).type_closure("m:f", max_depth=5)
    assert r["resolved"] is True
    assert r["n_nodes_total"] == 1
    assert r["n_edges_total"] == 0
    assert r["nodes"] == [{"title": "m:f", "depth": 0}]


# ---------------------------------------------------------------------------
# CLI parity
# ---------------------------------------------------------------------------


def test_cli_negative_limit_nonzero_exit(tmp_path: Path):
    entities = [_entity("A", "function"), _entity("B", "typedef")]
    graph = _publish(tmp_path, entities, [_uses("A", "B")])
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "graph_query.py"),
            "type-closure",
            "A",
            "--graph",
            str(graph),
            "--max-depth",
            "-1",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "max_depth" in (proc.stderr + proc.stdout)


def test_cli_malformed_uses_type_nonzero_exit(tmp_path: Path):
    entities = [_entity("A", "function"), _entity("B", "typedef")]
    malformed = _uses("A", "B")
    malformed["target"] = None
    graph = _publish(tmp_path, entities, [malformed])
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "graph_query.py"),
            "type-closure",
            "A",
            "--graph",
            str(graph),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
    assert "invalid target" in (proc.stderr + proc.stdout)


def test_graph_query_graphrag_code_human_and_json_parity(tmp_path: Path):
    entities = [
        _entity("A", "function"),
        _entity("B", "typedef"),
        _entity("C", "typedef"),
    ]
    rels = [_uses("A", "B", hid=1), _uses("B", "C", hid=2)]
    graph = _publish(tmp_path, entities, rels)
    args_common = [
        "type-closure",
        "A",
        "--graph",
        str(graph),
        "--direction",
        "dependencies",
        "--max-depth",
        "2",
        "--max-nodes",
        "10",
        "--max-edges",
        "10",
    ]
    gq_h = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "graph_query.py"), *args_common],
        capture_output=True,
        text=True,
        check=True,
    )
    gc_h = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "graphrag_code.py"), *args_common],
        capture_output=True,
        text=True,
        check=True,
    )
    assert gq_h.stdout == gc_h.stdout

    gq_j = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "graph_query.py"),
            *args_common,
            "--json",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    gc_j = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "graphrag_code.py"),
            *args_common,
            "--json",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert gq_j.stdout == gc_j.stdout
    payload = json.loads(gq_j.stdout)
    assert payload["n_nodes_total"] == 3
    assert payload["n_edges_total"] == 2
    # Human formatter matches CLI human output.
    assert format_type_closure_human(payload).strip() == gq_h.stdout.strip()


# ---------------------------------------------------------------------------
# Context pack
# ---------------------------------------------------------------------------


def test_context_pack_default_depth_byte_identity(tmp_path: Path):
    entities = [
        _entity("m:f", "function"),
        _entity("m:T", "typedef"),
        _entity("m:U", "typedef"),
    ]
    rels = [
        _uses("m:f", "m:T", hid=1),
        _uses("m:T", "m:U", hid=2),
    ]
    graph = _publish(tmp_path, entities, rels)
    base = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "context_pack.py"),
            "m:f",
            "--graph",
            str(graph),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    explicit = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "context_pack.py"),
            "m:f",
            "--graph",
            str(graph),
            "--type-depth",
            "1",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert base.stdout == explicit.stdout
    pack = json.loads(base.stdout)
    assert "type_dependency_closure" not in pack
    assert "type_user_closure" not in pack
    assert "type_dependency_edges" in pack
    # No multi-hop leak into direct fields.
    assert all(e.get("target") == "m:T" for e in pack["type_dependency_edges"])


def test_context_pack_transitive_depth_and_bounds(tmp_path: Path):
    entities = [
        _entity("m:f", "function"),
        _entity("m:T", "typedef"),
        _entity("m:U", "typedef"),
        _entity("m:V", "typedef"),
    ]
    # Build many observations on first edge for observation bound check.
    obs = [
        {
            "use_kind": "parameter",
            "entry_indices": [0],
            "qualType": "T",
            "source_path": "a.c",
            "location_precision": "declaration_bearing_node",
            "location_origin": "direct",
            "resolver": "unique_typedef_spelling",
            "owner_resolver": "exact_declaration_site",
            "desugaredQualType": None,
            "line": i,
        }
        for i in range(8)
    ]
    e1 = _uses("m:f", "m:T", hid=1)
    e1["clang_type_use_observations_json"] = json.dumps(
        obs, sort_keys=True, separators=(",", ":")
    )
    e1["clang_type_use_observation_count"] = 8
    rels = [e1, _uses("m:T", "m:U", hid=2), _uses("m:U", "m:V", hid=3)]
    graph = _publish(tmp_path, entities, rels)

    out = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "context_pack.py"),
            "m:f",
            "--graph",
            str(graph),
            "--type-depth",
            "2",
            "--max-type-edges",
            "10",
            "--max-type-observations",
            "2",
            "--max-text-chars",
            "12",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    pack = json.loads(out.stdout)
    # Direct fields still present.
    assert pack["type_dependency_total"] == 1
    # Transitive section present.
    cl = pack["type_dependency_closure"]
    assert cl["max_depth"] == 2
    assert cl["n_nodes_total"] == 3  # f, T, U (V is depth 3)
    assert cl["n_edges_total"] == 2
    depths = {n["title"]: n["depth"] for n in cl["nodes"]}
    assert depths["m:f"] == 0
    assert depths["m:T"] == 1
    assert depths["m:U"] == 2
    # Observation bound on compact edges.
    for edge in cl["edges"]:
        if edge.get("source") == "m:f":
            assert edge["observation_sample_count"] == 2
            assert edge["observation_truncated"] is True
            assert "clang_type_use_observations_json" not in edge
    # Text bound.
    for node in cl["nodes"]:
        if node["title"] != "m:f":
            assert node["truncated"] is True or len(node["text"]) <= 12

    # Wrapper option wiring.
    wrap = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "graphrag_code.py"),
            "context-pack",
            "m:f",
            "--graph",
            str(graph),
            "--type-depth",
            "2",
            "--json",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "type_dependency_closure" in json.loads(wrap.stdout)


@pytest.mark.parametrize("type_depth", [0, -3])
def test_context_pack_nonpositive_type_depth_rejected(
    tmp_path: Path, type_depth: int
):
    entities = [_entity("m:f", "function")]
    graph = _publish(tmp_path, entities, [])
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "context_pack.py"),
            "m:f",
            "--graph",
            str(graph),
            "--type-depth",
            str(type_depth),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "positive integer" in (proc.stderr + proc.stdout)


def test_context_pack_preserves_dangling_closure_node_accounting(tmp_path: Path):
    entities = [_entity("m:f", "function")]
    graph = _publish(tmp_path, entities, [_uses("m:f", "m:Missing")])
    out = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "context_pack.py"),
            "m:f",
            "--graph",
            str(graph),
            "--type-depth",
            "2",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    closure = json.loads(out.stdout)["type_dependency_closure"]
    assert closure["n_nodes_total"] == 2
    assert closure["n_nodes_returned"] == len(closure["nodes"]) == 2
    missing = next(n for n in closure["nodes"] if n["title"] == "m:Missing")
    assert missing["entity_status"] == "missing"
    assert missing["entity_match_count"] == 0
    assert missing["text"] == ""


@pytest.mark.parametrize("bad_id", ["", pd.NA])
def test_context_pack_malformed_uses_type_fails_closed(
    tmp_path: Path, bad_id
):
    entities = [_entity("m:f", "function"), _entity("m:T", "typedef")]
    malformed = _uses("m:f", "m:T")
    malformed["id"] = bad_id
    graph = _publish(tmp_path, entities, [malformed])
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "context_pack.py"),
            "m:f",
            "--graph",
            str(graph),
            "--type-depth",
            "2",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
    assert "invalid id" in (proc.stderr + proc.stdout)


def test_module_pack_honors_type_depth_bounds(tmp_path: Path):
    entities = [
        _entity("m:m", "module"),
        _entity("m:f", "function"),
        _entity("m:T", "typedef"),
        _entity("m:U", "typedef"),
    ]
    # Module entity itself has no uses_type; members do. Module pack still
    # must not invent unbounded observations on module_neighbors.
    rels = [
        _uses("m:f", "m:T", hid=1),
        _uses("m:T", "m:U", hid=2),
        _contains("m:m", "m:f", hid=3),
    ]
    # Attach uses_type also as neighbor of module via a synthetic edge for
    # module_neighbors coverage.
    rels.append(_uses("m:m", "m:T", hid=4, rid="rel:uses_type:mod"))
    rels[-1]["clang_type_use_source_entity_id"] = "ent:module:m:m"
    graph = _publish(tmp_path, entities, rels)
    out = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "context_pack.py"),
            "m:m",
            "--graph",
            str(graph),
            "--type-depth",
            "2",
            "--max-type-observations",
            "0",
            "--max-type-edges",
            "5",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    pack = json.loads(out.stdout)
    assert pack.get("is_module_pack") is True
    for edge in pack.get("module_neighbors") or []:
        if edge.get("type") == "uses_type":
            assert edge.get("observation_sample") == []
    cl = pack.get("type_dependency_closure")
    assert cl is not None
    assert cl["n_edges_total"] >= 1
    for edge in cl["edges"]:
        assert edge.get("observation_sample") == []


# ---------------------------------------------------------------------------
# Live overlay smoke (compiler-conditional)
# ---------------------------------------------------------------------------


def _cc():
    from c_preprocessor import find_c_compiler  # type: ignore

    return find_c_compiler()


@pytest.mark.skipif(_cc() is None, reason="no C compiler")
def test_live_inih_type_closure_smoke(tmp_path: Path):
    from index_c import main as index_c_main  # type: ignore

    inih = ROOT / "examples" / "inih"
    before = {p.name for p in inih.iterdir()}
    graph = tmp_path / "byog_inih_closure"
    index_c_main(
        package=inih,
        graph=graph,
        keep_snapshots=2,
        compiler_builtins=False,
        compiler_dependencies=False,
        compiler_includes=False,
        clang_signatures=False,
        clang_calls=False,
        clang_types=False,
        clang_type_uses=True,
        clang_type_shapes=False,
        allow_toolchain_drift=False,
    )
    g = ByogGraph(graph)
    direct = g.types_used_by("ini:ini_parse")
    assert "ini:ini_handler" in direct
    closure = g.type_closure(
        "ini:ini_parse", direction="dependencies", max_depth=2
    )
    assert closure["resolved"] is True
    assert closure["n_edges_total"] >= 1
    titles = {n["title"] for n in closure["nodes"]}
    assert "ini:ini_parse" in titles
    assert "ini:ini_handler" in titles
    # Pack depth 1 unchanged keys; depth 2 may add closure.
    p1 = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "context_pack.py"),
            "ini:ini_parse",
            "--graph",
            str(graph),
            "--type-depth",
            "1",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    pack1 = json.loads(p1.stdout)
    assert "type_dependency_closure" not in pack1
    p2 = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "context_pack.py"),
            "ini:ini_parse",
            "--graph",
            str(graph),
            "--type-depth",
            "2",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    pack2 = json.loads(p2.stdout)
    assert "type_dependency_edges" in pack2
    assert "type_dependency_closure" in pack2
    packed_titles = {
        node["title"] for node in pack2["type_dependency_closure"]["nodes"]
    }
    assert "ini:ini_handler" in packed_titles
    after = {p.name for p in inih.iterdir()}
    assert after == before
    for pattern in ("*.o", "*.ast", "*.d", "*.i"):
        assert not list(inih.glob(pattern))
        assert not list(inih.glob(f"**/{pattern}"))


@pytest.mark.skipif(_cc() is None, reason="no C compiler")
def test_live_cjson_type_closure_smoke(tmp_path: Path):
    from index_c import main as index_c_main  # type: ignore

    pkg = ROOT / "examples" / "cjson"
    before = {p.name for p in pkg.iterdir()}
    graph = tmp_path / "byog_cjson_closure"
    index_c_main(
        package=pkg,
        graph=graph,
        keep_snapshots=2,
        compiler_builtins=False,
        compiler_dependencies=False,
        compiler_includes=False,
        clang_signatures=False,
        clang_calls=False,
        clang_types=False,
        clang_type_uses=True,
        clang_type_shapes=False,
        allow_toolchain_drift=False,
    )
    g = ByogGraph(graph)
    # Self-edge on struct cJSON must not explode.
    root = "cJSON:struct:cJSON"
    r = g.type_closure(
        root, direction="both", max_depth=2, max_nodes=1000, max_edges=1000
    )
    assert r["resolved"] is True
    assert r["n_nodes_total"] >= 1
    assert r["n_nodes_total"] == r["n_nodes_returned"]
    assert r["n_edges_total"] == r["n_edges_returned"]
    assert any(
        edge["source"] == root and edge["target"] == root
        for edge in r["edges"]
    )
    after = {p.name for p in pkg.iterdir()}
    assert after == before
    for pattern in ("*.o", "*.ast", "*.d", "*.i"):
        assert not list(pkg.glob(pattern))
