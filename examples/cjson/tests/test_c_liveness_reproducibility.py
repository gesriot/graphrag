"""Host-independent liveness stamps + toolchain drift detection.

Published default is ``no_compiler``: labels and ``macro_seed_digest`` depend
only on package-local inputs. ``compiler_builtins`` records toolchain identity
and the seeded macro table digest in the snapshot manifest; a re-stamp whose
digest does not match the recorded one refuses unless explicitly allowed.

Run: uv run python -m pytest examples/cjson/tests/test_c_liveness_reproducibility.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
from byog_graph import publish_byog_snapshot  # type: ignore
from c_preprocessor import (  # type: ignore
    ToolchainDriftError,
    analyze_package,
    annotate_byog,
    build_liveness_provenance,
    check_liveness_stamp_freshness,
    find_c_compiler,
    macro_seed_digest,
    read_graph_liveness_provenance,
)


def test_no_compiler_digest_is_package_local_and_stable():
    pa1 = analyze_package(ROOT / "examples" / "cjson", use_compiler_builtins=False)
    pa2 = analyze_package(ROOT / "examples" / "cjson", use_compiler_builtins=False)
    assert pa1.eval_mode == "no_compiler"
    assert pa1.macro_seed_digest
    assert pa1.macro_seed_digest == pa2.macro_seed_digest
    assert pa1.compiler_path is None
    assert pa1.compiler_id is None
    # Digest must not depend on host builtins even if we pass a fake table.
    d_local = macro_seed_digest(
        eval_mode="no_compiler",
        compile_defines=pa1.compile_defines,
        header_defaults=pa1.header_defaults,
        compiler_builtins={"__APPLE__": "1", "__GNUC__": "4"},
        include_macros={"NAN": "nan"},
    )
    assert d_local == pa1.macro_seed_digest
    prov = build_liveness_provenance(pa1)
    assert prov["host_independent"] is True
    assert prov["eval_mode"] == "no_compiler"
    assert prov["macro_seed_digest"] == pa1.macro_seed_digest


@pytest.mark.skipif(find_c_compiler() is None, reason="no C compiler available")
def test_compiler_builtins_digest_includes_toolchain_table():
    pa = analyze_package(ROOT / "examples" / "cjson", use_compiler_builtins=True)
    assert pa.eval_mode == "compiler_builtins"
    assert pa.compiler_builtins
    assert pa.macro_seed_digest
    d_with = macro_seed_digest(
        eval_mode="compiler_builtins",
        compile_defines=pa.compile_defines,
        header_defaults=pa.header_defaults,
        compiler_builtins=pa.compiler_builtins,
        include_macros=pa.include_macros,
    )
    assert d_with == pa.macro_seed_digest
    # Perturbing the seed table must change the digest.
    poisoned = dict(pa.compiler_builtins)
    poisoned["__REPRO_POISON__"] = "1"
    d_poison = macro_seed_digest(
        eval_mode="compiler_builtins",
        compile_defines=pa.compile_defines,
        header_defaults=pa.header_defaults,
        compiler_builtins=poisoned,
        include_macros=pa.include_macros,
    )
    assert d_poison != pa.macro_seed_digest
    # no_compiler digest for the same package is different from builtins.
    pa_nc = analyze_package(ROOT / "examples" / "cjson", use_compiler_builtins=False)
    assert pa_nc.macro_seed_digest != pa.macro_seed_digest
    prov = build_liveness_provenance(pa)
    assert prov["host_independent"] is False
    assert prov["compiler_id"] or prov["compiler_path"]
    assert prov["n_compiler_builtins"] == len(pa.compiler_builtins)


def test_drift_refuses_tampered_digest(tmp_path: Path):
    """Changing the recorded macro digest makes re-stamp refuse, not relabel."""
    pkg = ROOT / "examples" / "jsmn"
    pa = analyze_package(pkg, use_compiler_builtins=False)
    prov = build_liveness_provenance(pa)
    # Publish a minimal snapshot with a deliberately wrong digest.
    bad = dict(prov)
    bad["macro_seed_digest"] = "0" * 64
    ents = [{"id": "e1", "title": "x", "type": "function"}]
    rels = []
    tus = [{"id": "t1", "text": "x"}]
    import pandas as pd

    graph_dir = tmp_path / "byog_repro"
    publish_byog_snapshot(
        pd.DataFrame(ents),
        pd.DataFrame(rels),
        pd.DataFrame(tus),
        graph_dir,
        settings_text=None,
        keep_last=2,
        source_root=pkg,
        extra_manifest={"preprocessor_liveness": bad},
    )
    prior = read_graph_liveness_provenance(graph_dir)
    assert prior is not None
    assert prior["macro_seed_digest"] == "0" * 64

    freshness = check_liveness_stamp_freshness(
        graph_dir, pkg, use_compiler_builtins=False
    )
    assert freshness["status"] == "drift"
    assert freshness["ok"] is False
    assert "digest" in freshness["message"]

    data = {"entities": list(ents), "relationships": [], "call_observations": []}
    with pytest.raises(ToolchainDriftError, match="liveness stamp drift"):
        annotate_byog(
            data,
            pkg,
            use_compiler_builtins=False,
            graph_dir=graph_dir,
            allow_toolchain_drift=False,
        )

    # Explicit override is allowed and rewrites.
    summary = annotate_byog(
        data,
        pkg,
        use_compiler_builtins=False,
        graph_dir=graph_dir,
        allow_toolchain_drift=True,
    )
    assert summary["eval_mode"] == "no_compiler"
    assert summary["liveness_provenance"]["macro_seed_digest"] == pa.macro_seed_digest


def test_matching_digest_is_fresh(tmp_path: Path):
    pkg = ROOT / "examples" / "jsmn"
    pa = analyze_package(pkg, use_compiler_builtins=False)
    prov = build_liveness_provenance(pa)
    import pandas as pd

    graph_dir = tmp_path / "byog_fresh"
    publish_byog_snapshot(
        pd.DataFrame([{"id": "e1", "title": "x", "type": "function"}]),
        pd.DataFrame([]),
        pd.DataFrame([{"id": "t1", "text": "x"}]),
        graph_dir,
        keep_last=2,
        source_root=pkg,
        extra_manifest={"preprocessor_liveness": prov},
    )
    freshness = check_liveness_stamp_freshness(
        graph_dir, pkg, use_compiler_builtins=False
    )
    assert freshness["status"] == "match"
    assert freshness["ok"] is True

    data = {
        "entities": [{"id": "e1", "title": "x", "type": "function", "source_file": "", "span": ""}],
        "relationships": [],
        "call_observations": [],
    }
    # No raise on matching digest.
    summary = annotate_byog(
        data, pkg, use_compiler_builtins=False, graph_dir=graph_dir
    )
    assert summary["liveness_provenance"]["macro_seed_digest"] == pa.macro_seed_digest
    assert data["entities"][0]["preprocessor_eval_mode"] == "no_compiler"
    assert data["entities"][0]["preprocessor_macro_seed_digest"] == pa.macro_seed_digest


def test_annotate_default_is_no_compiler():
    """Published path default must not consult the host toolchain."""
    data = {
        "entities": [
            {
                "id": "e1",
                "title": "f",
                "type": "function",
                "source_file": "jsmn.c",
                "span": "1:1-2:1",
            }
        ],
        "relationships": [],
        "call_observations": [],
    }
    summary = annotate_byog(data, ROOT / "examples" / "jsmn")
    assert summary["eval_mode"] == "no_compiler"
    assert summary["liveness_provenance"]["host_independent"] is True
    assert data["entities"][0]["preprocessor_eval_mode"] == "no_compiler"
    assert data["entities"][0]["preprocessor_macro_seed_digest"]
