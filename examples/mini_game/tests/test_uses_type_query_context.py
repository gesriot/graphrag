"""Consumer-side uses_type queries and context-pack evidence.

Pure tests always run. Live inih --clang-type-uses smoke skips without a C compiler.

Does not modify C extraction or overlay generation.
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

from scripts.byog_graph import ByogGraph, publish_byog_snapshot  # type: ignore
from scripts.context_pack import (  # type: ignore
    compact_relationship,
    _decode_type_use_observations,
)
from scripts.graph_query import types_used_by, type_users  # type: ignore


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


def _uses_type_rel(
    *,
    source: str,
    target: str,
    src_id: str,
    tgt_id: str,
    hid: int = 1,
    observations: list | None = None,
    use_kinds: list | None = None,
    obs_count: int | None = None,
    **extra,
) -> dict:
    obs = observations
    if obs is None:
        obs = [
            {
                "source_path": "a.c",
                "line": 1,
                "col0": 0,
                "use_kind": "parameter",
                "qualType": "T",
                "resolver": "unique_typedef_spelling",
                "owner_resolver": "exact_declaration_site",
                "entry_indices": [0],
                "location_precision": "declaration_bearing_node",
            }
        ]
    kinds = use_kinds or sorted({o.get("use_kind", "parameter") for o in obs})
    n = obs_count if obs_count is not None else len(obs)
    rel = {
        "id": f"rel:uses_type:{source}->{target}:deadbeef0001",
        "source": source,
        "target": target,
        "type": "uses_type",
        "description": f"configured Clang type-use evidence: {source} uses_type {target}",
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
        "clang_type_use_observation_count": n,
        "clang_type_use_use_kinds": kinds,
        "clang_type_use_entry_indices": [0],
        "clang_type_use_compiler_path": "/usr/bin/clang",
        "clang_type_use_compiler_id": "Apple clang version test",
        "clang_type_use_compilers": json.dumps(
            [
                {
                    "compiler_path": "/usr/bin/clang",
                    "compiler_id": "Apple clang version test",
                    "compile_commands_digest": "abc123",
                }
            ],
            sort_keys=True,
            separators=(",", ":"),
        ),
        "clang_type_use_compile_commands_digest": "abc123",
        "clang_type_use_observations_json": json.dumps(
            obs, sort_keys=True, separators=(",", ":")
        ),
        "clang_type_use_source_entity_id": src_id,
        "clang_type_use_target_entity_id": tgt_id,
        "clang_type_use_description": f"configured type use {source}->{target}",
    }
    rel.update(extra)
    return rel


def _publish_graph(tmp_path: Path, entities: list, relationships: list, texts: list | None = None) -> Path:
    graph = tmp_path / "byog_test"
    texts = texts or [
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
        pd.DataFrame(texts) if texts else pd.DataFrame(
            columns=["id", "text", "n_tokens", "document_ids", "entity_ids", "relationship_ids"]
        ),
        graph,
        keep_last=2,
    )
    return graph


# ---------------------------------------------------------------------------
# Pure query
# ---------------------------------------------------------------------------


def test_types_used_by_and_type_users_outgoing_incoming(tmp_path: Path):
    entities = [
        _entity("m:f", "function"),
        _entity("m:T", "typedef"),
        _entity("m:U", "typedef"),
        _entity("m:g", "function"),
    ]
    rels = [
        {
            "id": "rel:contains:m:f",
            "source": "ent:file:m",
            "target": "m:f",
            "type": "contains",
            "weight": 1.0,
            "human_readable_id": 1,
            "description": "contains",
            "extractor": "tree-sitter-c",
            "confidence": 1.0,
            "is_deterministic": True,
            "text_unit_ids": [],
            "document_ids": [],
            "covariate_ids": [],
            "source_file": "",
            "span": "",
        },
        {
            "id": "rel:call:m:f:m:g:1:0",
            "source": "m:f",
            "target": "m:g",
            "type": "calls",
            "weight": 0.9,
            "human_readable_id": 2,
            "description": "calls",
            "extractor": "tree-sitter-c",
            "confidence": 0.9,
            "is_deterministic": True,
            "text_unit_ids": [],
            "document_ids": [],
            "covariate_ids": [],
            "source_file": "a.c",
            "span": "1:0",
        },
        _uses_type_rel(
            source="m:f",
            target="m:T",
            src_id="ent:function:m:f",
            tgt_id="ent:typedef:m:T",
            hid=3,
        ),
        _uses_type_rel(
            source="m:f",
            target="m:U",
            src_id="ent:function:m:f",
            tgt_id="ent:typedef:m:U",
            hid=4,
            id="rel:uses_type:m:f->m:U:deadbeef0002",
        ),
        _uses_type_rel(
            source="m:g",
            target="m:T",
            src_id="ent:function:m:g",
            tgt_id="ent:typedef:m:T",
            hid=5,
            id="rel:uses_type:m:g->m:T:deadbeef0003",
        ),
    ]
    graph = _publish_graph(tmp_path, entities, rels)
    g = ByogGraph(graph)
    assert g.types_used_by("m:f") == ["m:T", "m:U"]
    assert g.type_users("m:T") == ["m:f", "m:g"]
    # Unrelated calls ignored for type queries.
    assert "m:g" not in g.types_used_by("m:f")
    assert g.callees("m:f") == ["m:g"]
    assert g.callers("m:g") == ["m:f"]
    # Free functions in graph_query match ByogGraph.
    ents = g.ents
    relsdf = g.rels
    assert types_used_by(ents, relsdf, "m:f") == g.types_used_by("m:f")
    assert type_users(ents, relsdf, "m:T") == g.type_users("m:T")


def test_self_edge_in_both_directions(tmp_path: Path):
    entities = [_entity("m:struct:T", "struct")]
    rels = [
        _uses_type_rel(
            source="m:struct:T",
            target="m:struct:T",
            src_id="ent:struct:m:struct:T",
            tgt_id="ent:struct:m:struct:T",
            use_kinds=["field"],
            observations=[
                {
                    "source_path": "a.c",
                    "line": 10,
                    "use_kind": "field",
                    "qualType": "struct T *",
                    "resolver": "exact_tag_spelling",
                    "owner_resolver": "exact_declaration_site",
                    "entry_indices": [0],
                    "location_precision": "declaration_bearing_node",
                }
            ],
        )
    ]
    graph = _publish_graph(tmp_path, entities, rels)
    g = ByogGraph(graph)
    assert g.types_used_by("m:struct:T") == ["m:struct:T"]
    assert g.type_users("m:struct:T") == ["m:struct:T"]


def test_sorted_deduplication(tmp_path: Path):
    entities = [
        _entity("m:f", "function"),
        _entity("m:T", "typedef"),
    ]
    # Two identical endpoint edges should not happen in real overlay, but
    # unique() on titles still dedupes query results if present.
    rels = [
        _uses_type_rel(
            source="m:f",
            target="m:T",
            src_id="ent:function:m:f",
            tgt_id="ent:typedef:m:T",
            hid=1,
            id="rel:uses_type:a",
        ),
        _uses_type_rel(
            source="m:f",
            target="m:T",
            src_id="ent:function:m:f",
            tgt_id="ent:typedef:m:T",
            hid=2,
            id="rel:uses_type:b",
        ),
    ]
    graph = _publish_graph(tmp_path, entities, rels)
    g = ByogGraph(graph)
    assert g.types_used_by("m:f") == ["m:T"]
    assert g.type_users("m:T") == ["m:f"]


def test_no_uses_type_returns_empty(tmp_path: Path):
    entities = [
        _entity("m:f", "function"),
        _entity("m:g", "function"),
    ]
    rels = [
        {
            "id": "rel:call:m:f:m:g:1:0",
            "source": "m:f",
            "target": "m:g",
            "type": "calls",
            "weight": 0.9,
            "human_readable_id": 1,
            "description": "calls",
            "extractor": "tree-sitter-c",
            "confidence": 0.9,
            "is_deterministic": True,
            "text_unit_ids": [],
            "document_ids": [],
            "covariate_ids": [],
            "source_file": "a.c",
            "span": "1:0",
        }
    ]
    graph = _publish_graph(tmp_path, entities, rels)
    g = ByogGraph(graph)
    assert g.types_used_by("m:f") == []
    assert g.type_users("m:T") == []
    assert g.type_users("m:g") == []


def test_struct_and_typedef_same_spelling_distinct(tmp_path: Path):
    entities = [
        _entity("cJSON:f", "function"),
        _entity("cJSON:struct:cJSON", "struct"),
        _entity("cJSON:typedef:cJSON", "typedef"),
    ]
    rels = [
        _uses_type_rel(
            source="cJSON:f",
            target="cJSON:struct:cJSON",
            src_id="ent:function:cJSON:f",
            tgt_id="ent:struct:cJSON:struct:cJSON",
            hid=1,
            id="rel:uses_type:struct",
            observations=[
                {
                    "source_path": "cJSON.c",
                    "use_kind": "parameter",
                    "qualType": "struct cJSON *",
                    "resolver": "exact_tag_spelling",
                    "owner_resolver": "exact_declaration_site",
                    "entry_indices": [0],
                    "location_precision": "declaration_bearing_node",
                }
            ],
        ),
        _uses_type_rel(
            source="cJSON:f",
            target="cJSON:typedef:cJSON",
            src_id="ent:function:cJSON:f",
            tgt_id="ent:typedef:cJSON:typedef:cJSON",
            hid=2,
            id="rel:uses_type:typedef",
            observations=[
                {
                    "source_path": "cJSON.c",
                    "use_kind": "parameter",
                    "qualType": "cJSON *",
                    "resolver": "unique_typedef_spelling",
                    "owner_resolver": "exact_declaration_site",
                    "entry_indices": [0],
                    "location_precision": "declaration_bearing_node",
                }
            ],
        ),
    ]
    graph = _publish_graph(tmp_path, entities, rels)
    g = ByogGraph(graph)
    assert g.types_used_by("cJSON:f") == [
        "cJSON:struct:cJSON",
        "cJSON:typedef:cJSON",
    ]


# ---------------------------------------------------------------------------
# Compact evidence
# ---------------------------------------------------------------------------


def test_compact_relationship_bounded_observations():
    obs = [
        {
            "source_path": "a.c",
            "line": i,
            "use_kind": "parameter",
            "qualType": "T",
            "resolver": "unique_typedef_spelling",
            "owner_resolver": "exact_declaration_site",
            "entry_indices": [0],
            "location_precision": "declaration_bearing_node",
        }
        for i in range(10)
    ]
    rel = _uses_type_rel(
        source="m:f",
        target="m:T",
        src_id="ent:function:m:f",
        tgt_id="ent:typedef:m:T",
        observations=obs,
        obs_count=10,
        use_kinds=["parameter"],
    )
    # Simulate parquet ndarray for entry indices / use kinds.
    rel["clang_type_use_entry_indices"] = pd.Series([0, 1]).values
    rel["clang_type_use_use_kinds"] = pd.Series(["parameter", "local_variable"]).values
    compact = compact_relationship(rel, max_type_observations=3)
    assert compact["type"] == "uses_type"
    assert compact["fact_kind"] == "configured_type_use"
    assert compact["observation_count"] == 10
    assert compact["observation_sample_count"] == 3
    assert compact["observation_total_count"] == 10
    assert compact["observation_truncated"] is True
    assert len(compact["observation_sample"]) == 3
    assert "clang_type_use_observations_json" not in compact
    assert compact["entry_indices"] == [0, 1]
    assert compact["use_kinds"] == ["local_variable", "parameter"]
    assert compact["source_entity_id"] == "ent:function:m:f"
    assert compact["compile_commands_digest"] == "abc123"


def test_compact_malformed_observations_json():
    rel = _uses_type_rel(
        source="m:f",
        target="m:T",
        src_id="ent:function:m:f",
        tgt_id="ent:typedef:m:T",
    )
    rel["clang_type_use_observations_json"] = "{not-json"
    compact = compact_relationship(rel, max_type_observations=5)
    assert compact["observation_sample"] == []
    assert compact["observation_sample_count"] == 0
    assert "observation_decode_error" in compact
    assert compact["observation_decode_error"].startswith("decode_error:")


def test_decode_observations_list_and_nan():
    sample, total, err = _decode_type_use_observations(
        [{"use_kind": "parameter"}], max_observations=5
    )
    assert err is None and total == 1 and sample[0]["use_kind"] == "parameter"
    sample2, total2, err2 = _decode_type_use_observations(float("nan"), max_observations=5)
    assert sample2 == [] and total2 == 0 and err2 is None


def test_compact_sparse_nan_fields_stay_json_safe():
    rel = _uses_type_rel(
        source="m:f",
        target="m:T",
        src_id="ent:function:m:f",
        tgt_id="ent:typedef:m:T",
    )
    for field in (
        "clang_type_use_observation_count",
        "clang_type_use_use_kinds",
        "clang_type_use_entry_indices",
        "clang_type_use_compiler_path",
        "clang_type_use_compiler_id",
    ):
        rel[field] = float("nan")
    rel["clang_type_use_compiler_id"] = float("inf")
    rel["clang_type_use_observations_json"] = "[]"
    compact = compact_relationship(rel)
    assert "observation_count" not in compact
    assert "use_kinds" not in compact
    assert "entry_indices" not in compact
    assert "compiler_path" not in compact
    assert "compiler_id" not in compact
    json.dumps(compact, allow_nan=False)


def test_invalid_observation_item_fails_closed():
    sample, total, error = _decode_type_use_observations(
        [{"use_kind": "parameter"}, "not-an-object"],
        max_observations=5,
    )
    assert sample == []
    assert total == 0
    assert error == "decode_error:item_not_object"


def test_declared_and_decoded_observation_counts_are_distinct():
    rel = _uses_type_rel(
        source="m:f",
        target="m:T",
        src_id="ent:function:m:f",
        tgt_id="ent:typedef:m:T",
        obs_count=9,
    )
    compact = compact_relationship(rel)
    assert compact["observation_count"] == 9
    assert compact["observation_total_count"] == 1
    assert compact["observation_count_mismatch"] == {
        "declared": 9,
        "decoded": 1,
    }


# ---------------------------------------------------------------------------
# Context pack
# ---------------------------------------------------------------------------


def test_context_pack_type_sections_beyond_neighbor_cap(tmp_path: Path):
    """uses_type after >30 unrelated neighbors still appears in dedicated sections."""
    entities = [
        _entity("m:f", "function"),
        _entity("m:T", "typedef"),
    ]
    # 35 unrelated contains-like edges from fake sources.
    rels = []
    for i in range(35):
        title = f"m:x{i}"
        entities.append(_entity(title, "function"))
        rels.append(
            {
                "id": f"rel:call:m:f:{title}:{i}:0",
                "source": "m:f",
                "target": title,
                "type": "calls",
                "weight": 0.9,
                "human_readable_id": i + 1,
                "description": "calls",
                "extractor": "tree-sitter-c",
                "confidence": 0.9,
                "is_deterministic": True,
                "text_unit_ids": [],
                "document_ids": [],
                "covariate_ids": [],
                "source_file": "a.c",
                "span": f"{i}:0",
            }
        )
    rels.append(
        _uses_type_rel(
            source="m:f",
            target="m:T",
            src_id="ent:function:m:f",
            tgt_id="ent:typedef:m:T",
            hid=100,
        )
    )
    # Also an incoming user edge.
    entities.append(_entity("m:user", "function"))
    rels.append(
        _uses_type_rel(
            source="m:user",
            target="m:T",
            src_id="ent:function:m:user",
            tgt_id="ent:typedef:m:T",
            hid=101,
            id="rel:uses_type:user",
        )
    )
    graph = _publish_graph(tmp_path, entities, rels)

    out = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "context_pack.py"),
            "m:f",
            "--graph",
            str(graph),
            "--full-text",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    pack = json.loads(out.stdout)
    # Dedicated section must still carry the type edge.
    assert "type_dependency_edges" in pack
    assert any(
        e.get("target") == "m:T" for e in pack["type_dependency_edges"]
    )
    assert "type_dependencies" in pack
    deps = {d["title"]: d for d in pack["type_dependencies"]}
    assert "m:T" in deps
    assert "body of m:T" in deps["m:T"]["text"]
    assert deps["m:T"]["type"] == "typedef"

    # Type users for m:T
    out2 = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "context_pack.py"),
            "m:T",
            "--graph",
            str(graph),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    pack2 = json.loads(out2.stdout)
    assert "type_user_edges" in pack2
    users = sorted(e["source"] for e in pack2["type_user_edges"])
    assert users == ["m:f", "m:user"]


def test_context_pack_without_uses_type_unchanged_shape(tmp_path: Path):
    """Graphs without uses_type must not grow new type_* keys."""
    entities = [
        _entity("m:f", "function"),
        _entity("m:g", "function"),
    ]
    rels = [
        {
            "id": "rel:call:m:f:m:g:1:0",
            "source": "m:f",
            "target": "m:g",
            "type": "calls",
            "weight": 0.9,
            "human_readable_id": 1,
            "description": "calls",
            "extractor": "tree-sitter-c",
            "confidence": 0.9,
            "is_deterministic": True,
            "text_unit_ids": [],
            "document_ids": [],
            "covariate_ids": [],
            "source_file": "a.c",
            "span": "1:0",
        }
    ]
    graph = _publish_graph(tmp_path, entities, rels)
    out = subprocess.run(
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
    pack = json.loads(out.stdout)
    for key in (
        "type_dependencies",
        "type_dependency_edges",
        "type_user_edges",
        "type_dependency_total",
        "type_user_total",
    ):
        assert key not in pack

    explicit_defaults = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "context_pack.py"),
            "m:f",
            "--graph",
            str(graph),
            "--max-type-edges",
            "20",
            "--max-type-observations",
            "5",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert explicit_defaults.stdout == out.stdout


def test_context_pack_self_edge_both_roles(tmp_path: Path):
    entities = [_entity("m:struct:T", "struct")]
    rels = [
        _uses_type_rel(
            source="m:struct:T",
            target="m:struct:T",
            src_id="ent:struct:m:struct:T",
            tgt_id="ent:struct:m:struct:T",
            use_kinds=["field"],
        )
    ]
    graph = _publish_graph(tmp_path, entities, rels)
    out = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "context_pack.py"),
            "m:struct:T",
            "--graph",
            str(graph),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    pack = json.loads(out.stdout)
    assert pack.get("type_dependency_edges")
    assert pack.get("type_user_edges")
    assert pack["type_dependency_edges"][0]["source"] == "m:struct:T"
    assert pack["type_user_edges"][0]["target"] == "m:struct:T"


def test_context_pack_edge_caps_and_wrapper_options(tmp_path: Path):
    entities = [
        _entity("m:m", "module"),
        _entity("m:f", "function"),
    ]
    relationships = []
    for index in range(3):
        target = f"m:T{index}"
        entities.append(_entity(target, "typedef"))
        relationships.append(
            _uses_type_rel(
                source="m:f",
                target=target,
                src_id="ent:function:m:f",
                tgt_id=f"ent:typedef:{target}",
                hid=index + 1,
                id=f"rel:uses_type:dependency:{index}",
            )
        )
    for index in range(3):
        user = f"m:user{index}"
        entities.append(_entity(user, "function"))
        relationships.append(
            _uses_type_rel(
                source=user,
                target="m:T0",
                src_id=f"ent:function:{user}",
                tgt_id="ent:typedef:m:T0",
                hid=index + 10,
                id=f"rel:uses_type:user:{index}",
            )
        )
    graph = _publish_graph(tmp_path, entities, relationships)

    owner_result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "context_pack.py"),
            "m:f",
            "--graph",
            str(graph),
            "--max-type-edges",
            "2",
            "--max-type-observations",
            "0",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    owner_pack = json.loads(owner_result.stdout)
    assert owner_pack["type_dependency_total"] == 3
    assert owner_pack["type_dependency_truncated"] is True
    assert len(owner_pack["type_dependency_edges"]) == 2
    assert len(owner_pack["type_dependencies"]) == 2
    assert all(
        edge["observation_sample"] == []
        and edge["observation_truncated"] is True
        for edge in owner_pack["type_dependency_edges"]
    )

    type_result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "context_pack.py"),
            "m:T0",
            "--graph",
            str(graph),
            "--max-type-edges",
            "2",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    type_pack = json.loads(type_result.stdout)
    assert type_pack["type_user_total"] == 4
    assert type_pack["type_user_truncated"] is True
    assert len(type_pack["type_user_edges"]) == 2

    wrapper_result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "graphrag_code.py"),
            "context-pack",
            "m:f",
            "--graph",
            str(graph),
            "--max-type-edges",
            "1",
            "--max-type-observations",
            "0",
            "--json",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    wrapper_pack = json.loads(wrapper_result.stdout)
    assert wrapper_pack["type_dependency_total"] == 3
    assert len(wrapper_pack["type_dependency_edges"]) == 1
    assert wrapper_pack["type_dependency_edges"][0]["observation_sample"] == []

    module_result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "context_pack.py"),
            "m:m",
            "--graph",
            str(graph),
            "--max-type-observations",
            "0",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    module_pack = json.loads(module_result.stdout)
    module_type_edges = [
        edge
        for edge in module_pack["module_neighbors"]
        if edge.get("type") == "uses_type"
    ]
    assert module_type_edges
    assert all(edge["observation_sample"] == [] for edge in module_type_edges)


def test_graph_query_cli_and_graphrag_code_json_parity(tmp_path: Path):
    entities = [
        _entity("m:f", "function"),
        _entity("m:T", "typedef"),
    ]
    rels = [
        _uses_type_rel(
            source="m:f",
            target="m:T",
            src_id="ent:function:m:f",
            tgt_id="ent:typedef:m:T",
        )
    ]
    graph = _publish_graph(tmp_path, entities, rels)

    gq = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "graph_query.py"),
            "types-used-by",
            "m:f",
            "--graph",
            str(graph),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert gq.stdout.strip().splitlines() == ["m:T"]

    gq_json = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "graph_query.py"),
            "types-used-by",
            "m:f",
            "--graph",
            str(graph),
            "--json",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(gq_json.stdout) == ["m:T"]

    code_json = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "graphrag_code.py"),
            "types-used-by",
            "m:f",
            "--graph",
            str(graph),
            "--json",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert code_json.stdout == gq_json.stdout

    code_human = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "graphrag_code.py"),
            "type-users",
            "m:T",
            "--graph",
            str(graph),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    # Delegates to graph_query: one title per line.
    assert code_human.stdout.strip().splitlines() == ["m:f"]


# ---------------------------------------------------------------------------
# Live inih smoke
# ---------------------------------------------------------------------------


def _cc():
    from c_preprocessor import find_c_compiler  # type: ignore

    return find_c_compiler()


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_live_inih_type_use_query_and_pack(tmp_path: Path):
    pkg = ROOT / "examples" / "inih"
    graph = tmp_path / "byog_inih_type_uses"
    before = {p.relative_to(pkg).as_posix() for p in pkg.rglob("*") if p.is_file()}

    # Index via index_c with --clang-type-uses only.
    from index_c import main as index_c_main  # type: ignore

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
    after = {p.relative_to(pkg).as_posix() for p in pkg.rglob("*") if p.is_file()}
    assert before == after
    assert not any(pkg.rglob("*.o"))
    assert not any(pkg.rglob("*.ast"))
    assert not any(pkg.rglob("*.d"))
    assert not any(pkg.rglob("*.i"))

    g = ByogGraph(graph)
    uses = g.rels[g.rels["type"].astype(str) == "uses_type"]
    assert len(uses) == 8

    # Known owners from the type-use overlay (parameter uses of ini_handler).
    handlers = g.type_users("ini:ini_handler")
    assert "ini:ini_parse" in handlers
    assert g.types_used_by("ini:ini_parse") == ["ini:ini_handler"]

    pack_out = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "context_pack.py"),
            "ini:ini_parse",
            "--graph",
            str(graph),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    pack = json.loads(pack_out.stdout)
    assert pack.get("type_dependency_edges")
    assert any(
        e.get("target") == "ini:ini_handler" for e in pack["type_dependency_edges"]
    )
    assert any(
        d.get("title") == "ini:ini_handler" for d in pack.get("type_dependencies") or []
    )
    # Bounded evidence shape present.
    edge0 = pack["type_dependency_edges"][0]
    assert "observation_sample" in edge0
    assert "observation_sample_count" in edge0
    assert "clang_type_use_observations_json" not in edge0
