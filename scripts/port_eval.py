#!/usr/bin/env python
"""
End-to-end Python->Rust port evaluation harness (no external API).

Where audit_call_edges.py measures the *graph* (the means), this measures the
*port* (the north-star: Python -> Rust working). It runs the full chain and
emits one repeatable report instead of a pass/crash loop:

    graph quality (audit_call_edges)  ->  context packs  ->  cargo fmt/check/test/run

Unlike agent_port_loop.py it never aborts on the first failure: every stage is
captured (ok / fail / skipped) so the report is comparable across runs and repos.

Example:
    uv run python scripts/port_eval.py \
        --source examples/mini_game --port examples/mini_game_rust --graph byog_mini_game
    uv run python scripts/port_eval.py --graph byog_mini_game --json > port_eval.json
    uv run python scripts/port_eval.py --graph byog_mini_game --markdown report.md
    uv run python scripts/port_eval.py --all-gates --full

The ``--gate``/``--all-gates`` mode reads ``scripts/port_gates.json``. It adds
fresh graph indexing, source-oracle checks, optional-tool skip semantics, and
non-zero exit status for a failed profile around the existing port-eval core.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import typer

sys.path.insert(0, str(Path(__file__).parent))
from audit_call_edges import build_report  # noqa: E402
from byog_graph import ByogGraph  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GATE_MANIFEST = ROOT / "scripts" / "port_gates.json"


def _run(
    cmd: List[str], cwd: Path, timeout: int = 600, env: Optional[Dict[str, str]] = None
) -> Dict[str, Any]:
    """Run a command, capturing status + output tail (never raises on non-zero)."""
    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL,  # so a CLI that reads stdin (cargo run) gets EOF, not a hang
            env=None if env is None else {**os.environ, **env},
        )
    except FileNotFoundError:
        return {
            "status": "skipped",
            "reason": f"{cmd[0]} not found",
            "cmd": " ".join(cmd),
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
    except subprocess.TimeoutExpired:
        return {
            "status": "fail",
            "reason": "timeout",
            "cmd": " ".join(cmd),
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
    tail = (proc.stdout + proc.stderr).strip().splitlines()[-20:]
    return {
        "status": "ok" if proc.returncode == 0 else "fail",
        "returncode": proc.returncode,
        "cmd": " ".join(cmd),
        "output_tail": tail,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def eval_graph(graph: Path, source: Path, reindex: bool, use_advanced: bool) -> Dict[str, Any]:
    reindex_result: Optional[Dict[str, Any]] = None
    if reindex:
        cmd = [
            sys.executable, str(ROOT / "scripts" / "index_python.py"),
            "--package", str(source), "--graph", str(graph),
        ]
        if use_advanced:
            cmd.append("--use-advanced")
        reindex_result = _run(cmd, cwd=ROOT)
        if reindex_result["status"] != "ok":
            detail = reindex_result.get("reason") or "\n".join(reindex_result.get("output_tail", []))
            raise RuntimeError(f"reindex failed; refusing to evaluate a stale graph: {detail}")
    report = build_report(graph, sample=0)
    g = ByogGraph(graph)
    s = report["structural"]
    return {
        "graph": report["graph"],
        "snapshot": report["snapshot"],
        "total_calls": report["total_calls"],
        "structural_pass_rate": s["pass_rate"],
        "structural_anomalies": s["anomaly_count"],
        "dangling_targets": report["dangling_count"],
        "semantic_suspicions": report.get("semantic_suspicion_count", 0),
        "observations": int(len(g.call_observations)),
        "clean": (
            s["anomaly_count"] == 0
            and report["dangling_count"] == 0
            and report.get("semantic_suspicion_count", 0) == 0
        ),
        "reindex": reindex_result,
    }


def default_key_symbols(graph: Path, n: int = 3) -> List[str]:
    """Pick the most-called symbols as 'key' symbols when none are given.

    Generic across projects: top targets by incoming call count that are real
    entities (functions/methods), so the harness is not mini_game-specific.
    """
    g = ByogGraph(graph)
    titles = set(g.ents["title"].astype(str))
    calls = g.rels[g.rels["type"].astype(str) == "calls"]
    counts = calls["target"].astype(str).value_counts()
    return [t for t in counts.index if t in titles][:n]


TYPE_CONTEXT_KEYS = frozenset(
    {
        "clang_type_uses",
        "depth",
        "max_type_edges",
        "max_type_observations",
        "requirements",
    }
)
TYPE_CONTEXT_REQUIREMENT_KEYS = frozenset(
    {
        "symbol",
        "direction",
        "require_nonempty",
        "allow_truncation",
    }
)
TYPE_CONTEXT_DIRECTIONS = frozenset({"dependencies", "users"})
_CLOSURE_KEY = {
    "dependencies": "type_dependency_closure",
    "users": "type_user_closure",
}


def validate_type_context(
    type_context: Any,
    *,
    indexer: str,
    symbols: List[str],
    profile_id: str = "profile",
) -> Dict[str, Any]:
    """Strict optional type_context schema for C port profiles.

    Rejects unknown keys, wrong types, non-positive depth, negative bounds,
    requirements for symbols not requested by the profile, and type_context
    on non-C profiles. Returns a normalized copy.
    """
    if type_context is None:
        raise ValueError(f"{profile_id}: type_context is null")
    if not isinstance(type_context, dict):
        raise ValueError(f"{profile_id}: type_context must be an object")
    unknown = sorted(set(type_context) - TYPE_CONTEXT_KEYS)
    if unknown:
        raise ValueError(
            f"{profile_id}: type_context has unknown key(s): {unknown}"
        )
    if indexer != "c":
        raise ValueError(
            f"{profile_id}: type_context is only valid for C profiles "
            f"(indexer={indexer!r})"
        )
    clang_type_uses = type_context.get("clang_type_uses")
    if clang_type_uses is not True:
        raise ValueError(
            f"{profile_id}: type_context.clang_type_uses must be true "
            f"(got {clang_type_uses!r})"
        )
    depth = type_context.get("depth")
    if isinstance(depth, bool) or not isinstance(depth, int) or depth < 1:
        raise ValueError(
            f"{profile_id}: type_context.depth must be a positive integer, "
            f"got {depth!r}"
        )
    max_type_edges = type_context.get("max_type_edges", 50)
    max_type_observations = type_context.get("max_type_observations", 5)
    for name, value in (
        ("max_type_edges", max_type_edges),
        ("max_type_observations", max_type_observations),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(
                f"{profile_id}: type_context.{name} must be a non-negative "
                f"integer, got {value!r}"
            )
    requirements = type_context.get("requirements", [])
    if not isinstance(requirements, list):
        raise ValueError(
            f"{profile_id}: type_context.requirements must be a list"
        )
    if not all(
        isinstance(symbol, str) and symbol and "\0" not in symbol
        for symbol in symbols
    ):
        raise ValueError(
            f"{profile_id}: symbols must be non-empty strings when type_context is declared"
        )
    if len(symbols) != len(set(symbols)):
        raise ValueError(
            f"{profile_id}: symbols must not contain duplicates when type_context is declared"
        )
    symbol_set = set(symbols)
    normalized_reqs: List[Dict[str, Any]] = []
    seen_requirements: set[tuple[str, str]] = set()
    for index, raw in enumerate(requirements):
        ctx = f"{profile_id}: type_context.requirements[{index}]"
        if not isinstance(raw, dict):
            raise ValueError(f"{ctx} must be an object")
        unknown_req = sorted(set(raw) - TYPE_CONTEXT_REQUIREMENT_KEYS)
        if unknown_req:
            raise ValueError(f"{ctx} has unknown key(s): {unknown_req}")
        symbol = raw.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            raise ValueError(f"{ctx}.symbol must be a non-empty string")
        if symbol not in symbol_set:
            raise ValueError(
                f"{ctx}.symbol {symbol!r} is not in the profile's symbols "
                f"{sorted(symbol_set)}"
            )
        direction = raw.get("direction")
        if direction not in TYPE_CONTEXT_DIRECTIONS:
            raise ValueError(
                f"{ctx}.direction must be one of "
                f"{sorted(TYPE_CONTEXT_DIRECTIONS)}, got {direction!r}"
            )
        require_nonempty = raw.get("require_nonempty", True)
        allow_truncation = raw.get("allow_truncation", False)
        if not isinstance(require_nonempty, bool):
            raise ValueError(f"{ctx}.require_nonempty must be a boolean")
        if not isinstance(allow_truncation, bool):
            raise ValueError(f"{ctx}.allow_truncation must be a boolean")
        requirement_key = (symbol, direction)
        if requirement_key in seen_requirements:
            raise ValueError(
                f"{ctx} duplicates the {symbol!r} / {direction!r} requirement"
            )
        seen_requirements.add(requirement_key)
        normalized_reqs.append(
            {
                "symbol": symbol,
                "direction": direction,
                "require_nonempty": require_nonempty,
                "allow_truncation": allow_truncation,
            }
        )
    return {
        "clang_type_uses": True,
        "depth": depth,
        "max_type_edges": max_type_edges,
        "max_type_observations": max_type_observations,
        "requirements": normalized_reqs,
    }


def validate_type_context_requirement(
    pack: Dict[str, Any], requirement: Dict[str, Any]
) -> Dict[str, Any]:
    """Validate one type_context requirement against a generated pack JSON."""
    symbol = str(requirement["symbol"])
    direction = str(requirement["direction"])
    section_key = _CLOSURE_KEY[direction]
    section = pack.get(section_key)
    errors: List[str] = []
    detail: Dict[str, Any] = {
        "section_key": section_key,
        "present": section is not None,
    }
    if not isinstance(section, dict):
        if requirement.get("require_nonempty", True):
            errors.append("missing_or_empty_closure")
        result = {
            "symbol": symbol,
            "direction": direction,
            "ok": not errors,
            "errors": errors,
            "detail": detail,
        }
        return result

    if section.get("root") != symbol:
        errors.append("root_mismatch")
    if section.get("direction") != direction:
        errors.append("direction_mismatch")

    def _nonnegative_int(key: str) -> Optional[int]:
        value = section.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            errors.append(f"invalid_{key}")
            return None
        detail[key] = value
        return value

    n_nodes_total = _nonnegative_int("n_nodes_total")
    n_edges_total = _nonnegative_int("n_edges_total")
    n_nodes_returned = _nonnegative_int("n_nodes_returned")
    n_edges_returned = _nonnegative_int("n_edges_returned")
    max_depth = section.get("max_depth")
    if isinstance(max_depth, bool) or not isinstance(max_depth, int) or max_depth < 1:
        errors.append("invalid_max_depth")
    else:
        detail["max_depth"] = max_depth

    raw_nodes = section.get("nodes")
    raw_edges = section.get("edges")
    if not isinstance(raw_nodes, list):
        errors.append("invalid_nodes")
        nodes: List[Dict[str, Any]] = []
    else:
        nodes = []
        for index, node in enumerate(raw_nodes):
            if not isinstance(node, dict):
                errors.append(f"invalid_node[{index}]")
            else:
                nodes.append(node)
    if not isinstance(raw_edges, list):
        errors.append("invalid_edges")
        edges: List[Dict[str, Any]] = []
    else:
        edges = []
        for index, edge in enumerate(raw_edges):
            if not isinstance(edge, dict):
                errors.append(f"invalid_edge[{index}]")
            else:
                edges.append(edge)

    truncation_flags: Dict[str, Optional[bool]] = {}
    for key in ("nodes_truncated", "edges_truncated"):
        value = section.get(key)
        if not isinstance(value, bool):
            errors.append(f"invalid_{key}")
            truncation_flags[key] = None
        else:
            truncation_flags[key] = value
            detail[key] = value

    for kind, total, returned, items in (
        ("node", n_nodes_total, n_nodes_returned, nodes),
        ("edge", n_edges_total, n_edges_returned, edges),
    ):
        if returned is not None and returned != len(items):
            errors.append(f"{kind}_returned_count_mismatch")
        if total is not None and returned is not None:
            if returned > total:
                errors.append(f"{kind}_returned_exceeds_total")
            flag = truncation_flags[f"{kind}s_truncated"]
            if flag is not None and flag != (total > returned):
                errors.append(f"{kind}_truncation_flag_mismatch")

    nonempty = bool(
        (n_edges_total is not None and n_edges_total > 0)
        or (n_nodes_total is not None and n_nodes_total > 1)
    )
    if requirement.get("require_nonempty", True) and not nonempty:
        errors.append("empty_closure")
    if not requirement.get("allow_truncation", False):
        if (
            truncation_flags["nodes_truncated"]
            or truncation_flags["edges_truncated"]
        ):
            errors.append("truncated_forbidden")
        if (
            n_nodes_returned is not None
            and n_nodes_total is not None
            and n_nodes_returned != n_nodes_total
        ):
            errors.append("node_total_mismatch")
        if (
            n_edges_returned is not None
            and n_edges_total is not None
            and n_edges_returned != n_edges_total
        ):
            errors.append("edge_total_mismatch")
    for node in nodes:
        status = node.get("entity_status")
        if status in {"missing", "ambiguous"}:
            errors.append(f"entity_status={status}:{node.get('title')}")
    for edge in edges:
        if edge.get("relationship_status") == "missing":
            errors.append(
                f"relationship_status=missing:{edge.get('id') or edge.get('source')}"
            )
    # Deterministic unique error list.
    uniq = sorted(set(errors))
    return {
        "symbol": symbol,
        "direction": direction,
        "ok": not uniq,
        "errors": uniq,
        "detail": detail,
    }


def _validate_context_pack_payload(pack: Dict[str, Any]) -> List[str]:
    """Validate the stable base shape shared by every context pack."""
    errors: List[str] = []
    symbol = pack.get("symbol")
    entity = pack.get("entity")
    if not isinstance(symbol, str) or not symbol:
        errors.append("invalid_symbol")
    if pack.get("purpose") != "port-to-rust":
        errors.append("invalid_purpose")
    if not isinstance(entity, dict):
        errors.append("invalid_entity")
    else:
        entity_title = entity.get("title")
        if not isinstance(entity_title, str) or not entity_title:
            errors.append("invalid_entity_title")
        elif isinstance(symbol, str) and entity_title != symbol:
            errors.append("symbol_entity_mismatch")
    if not isinstance(pack.get("neighbors"), list):
        errors.append("invalid_neighbors")
    return sorted(set(errors))


def _context_pack_output_paths(symbols: List[str], out_dir: Path) -> Dict[str, Path]:
    """Return deterministic collision-safe paths contained by ``out_dir``.

    Keep the historical readable filename for ordinary non-colliding symbols.
    Unsafe or colliding spellings receive a sanitized label plus a digest.
    """
    if not all(
        isinstance(symbol, str) and symbol and "\0" not in symbol
        for symbol in symbols
    ):
        raise ValueError("context-pack symbols must be non-empty strings")
    if len(symbols) != len(set(symbols)):
        raise ValueError("context-pack symbols must not contain duplicates")
    legacy_names = {
        symbol: f"context_pack_{symbol.replace(':', '_')}.json"
        for symbol in symbols
    }
    counts: Dict[str, int] = {}
    for name in legacy_names.values():
        counts[name] = counts.get(name, 0) + 1
    paths: Dict[str, Path] = {}
    resolved_root = out_dir.resolve()
    for symbol in symbols:
        legacy = legacy_names[symbol]
        unsafe = (
            "/" in legacy
            or "\\" in legacy
            or Path(legacy).name != legacy
            or counts[legacy] != 1
        )
        if unsafe:
            label = re.sub(r"[^A-Za-z0-9_.-]+", "_", symbol).strip("._-")
            label = (label[:80] or "symbol")
            digest = hashlib.sha256(symbol.encode("utf-8")).hexdigest()[:12]
            filename = f"context_pack_{label}_{digest}.json"
        else:
            filename = legacy
        path = out_dir / filename
        resolved = path.resolve()
        if resolved.parent != resolved_root:
            raise ValueError(
                f"context-pack output escaped its directory for symbol {symbol!r}"
            )
        paths[symbol] = path
    if len(set(paths.values())) != len(paths):
        raise ValueError("context-pack output filename collision")
    return paths


def gen_context_packs(
    symbols: List[str],
    graph: Path,
    out_dir: Path,
    *,
    type_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Generate context packs; fail-closed on any missing/invalid pack.

    When ``type_context`` is None, command lines and pack contents match the
    historical default (full text, no type-depth). When present, configured
    depth/edge/observation bounds are passed and requirements are validated
    against the generated JSON.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    output_paths = _context_pack_output_paths(symbols, out_dir)
    pack_script = ROOT / "scripts" / "context_pack.py"
    generated: List[str] = []
    failed_symbols: List[str] = []
    failures: List[Dict[str, Any]] = []
    pack_paths: Dict[str, str] = {}
    pack_payloads: Dict[str, Dict[str, Any]] = {}

    base_cmd = [
        sys.executable,
        str(pack_script),
        # symbol inserted per loop
        "--graph",
        str(graph),
        "--purpose",
        "port-to-rust",
        "--max-text-chars",
        "0",
    ]
    extra_cmd: List[str] = []
    if type_context is not None:
        extra_cmd = [
            "--type-depth",
            str(int(type_context["depth"])),
            "--max-type-edges",
            str(int(type_context["max_type_edges"])),
            "--max-type-observations",
            str(int(type_context["max_type_observations"])),
        ]

    for sym in symbols:
        out_file = output_paths[sym]
        try:
            # output/port_eval is intentionally reusable. Never let a prior
            # successful run satisfy this run's existence check.
            out_file.unlink(missing_ok=True)
        except OSError as error:
            failed_symbols.append(sym)
            failures.append(
                {
                    "symbol": sym,
                    "stage": "prepare",
                    "reason": f"cannot remove stale pack output: {error}",
                    "detail": {"path": str(out_file)},
                }
            )
            continue
        cmd = (
            [base_cmd[0], base_cmd[1], sym]
            + base_cmd[2:]
            + extra_cmd
            + ["--output", str(out_file)]
        )
        res = _run(cmd, cwd=ROOT)
        if res["status"] != "ok" or not out_file.is_file():
            failed_symbols.append(sym)
            failures.append(
                {
                    "symbol": sym,
                    "stage": "generate",
                    "reason": res.get("reason")
                    or f"context_pack exit status={res.get('status')}",
                    "detail": {
                        "returncode": res.get("returncode"),
                        "output_tail": res.get("output_tail", [])[-10:],
                        "path": str(out_file),
                        "exists": out_file.exists(),
                    },
                }
            )
            continue
        try:
            payload = json.loads(out_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            failed_symbols.append(sym)
            failures.append(
                {
                    "symbol": sym,
                    "stage": "validate",
                    "reason": f"unreadable pack JSON: {error}",
                    "detail": {"path": str(out_file)},
                }
            )
            continue
        if not isinstance(payload, dict):
            failed_symbols.append(sym)
            failures.append(
                {
                    "symbol": sym,
                    "stage": "validate",
                    "reason": "pack JSON is not an object",
                    "detail": {"path": str(out_file)},
                }
            )
            continue
        payload_errors = _validate_context_pack_payload(payload)
        if payload_errors:
            failed_symbols.append(sym)
            failures.append(
                {
                    "symbol": sym,
                    "stage": "validate",
                    "reason": "invalid context-pack schema",
                    "detail": {
                        "path": str(out_file),
                        "errors": payload_errors,
                    },
                }
            )
            continue
        generated.append(sym)
        pack_paths[sym] = str(out_file)
        pack_payloads[sym] = payload

    requirement_results: List[Dict[str, Any]] = []
    type_requirements_ok = True
    if type_context is not None and type_context.get("requirements"):
        by_symbol: Dict[str, List[Dict[str, Any]]] = {}
        for req in type_context["requirements"]:
            by_symbol.setdefault(str(req["symbol"]), []).append(req)
        for sym, reqs in by_symbol.items():
            if sym not in pack_paths:
                # Generation already failed; mark each requirement.
                for req in reqs:
                    requirement_results.append(
                        {
                            "symbol": sym,
                            "direction": req["direction"],
                            "ok": False,
                            "errors": ["pack_not_generated"],
                            "detail": {},
                        }
                    )
                type_requirements_ok = False
                continue
            pack = pack_payloads[sym]
            for req in reqs:
                outcome = validate_type_context_requirement(pack, req)
                requirement_results.append(outcome)
                if not outcome["ok"]:
                    type_requirements_ok = False
                    failures.append(
                        {
                            "symbol": sym,
                            "stage": "validate",
                            "reason": "type_context requirement failed",
                            "detail": outcome,
                        }
                    )
                    if sym not in failed_symbols:
                        failed_symbols.append(sym)

    complete = (
        len(failed_symbols) == 0
        and len(generated) == len(symbols)
        and type_requirements_ok
    )
    # Stable failure order by symbol then stage.
    failures.sort(
        key=lambda f: (
            str(f.get("symbol") or ""),
            str(f.get("stage") or ""),
            str(f.get("reason") or ""),
        )
    )
    requirement_results.sort(
        key=lambda r: (str(r.get("symbol") or ""), str(r.get("direction") or ""))
    )
    return {
        "requested": list(symbols),
        "generated": generated,
        "failed": failed_symbols,
        "count": len(generated),
        "complete": complete,
        "failures": failures,
        "type_context": type_context,
        "type_requirements": requirement_results,
        "type_requirements_ok": type_requirements_ok,
        "pack_paths": pack_paths,
        # Exact argv extras beyond the legacy default (empty when no type_context).
        "extra_context_pack_args": list(extra_cmd),
    }


def eval_rust(port_dir: Path, run_binary: bool = True) -> Dict[str, Any]:
    if not (port_dir / "Cargo.toml").exists():
        return {"status": "skipped", "reason": f"no Cargo.toml in {port_dir}"}
    stages: Dict[str, List[str]] = {
        "fmt": ["cargo", "fmt", "--check"],
        "check": ["cargo", "check"],
        # Run all integration tests so multi-contract ports (e.g. Version +
        # SimpleSpec) cannot pass port_eval by exercising only the first
        # golden_contract.rs file.
        "golden_test": ["cargo", "test", "--tests", "--", "--quiet"],
    }
    if run_binary:
        stages["run"] = ["cargo", "run", "--quiet"]
    results = {name: _run(cmd, cwd=port_dir) for name, cmd in stages.items()}
    results["all_ok"] = all(r.get("status") == "ok" for r in results.values())
    return results


def count_golden(source: Path) -> Dict[str, Any]:
    tests_dir = source / "tests"
    files = sorted(tests_dir.rglob("golden_*.json")) if tests_dir.exists() else []
    names = [str(p.relative_to(tests_dir)) for p in files]
    case_counts: Dict[str, int] = {}
    for p in files:
        rel_name = str(p.relative_to(tests_dir))
        data = json.loads(p.read_text())
        cases = data.get("cases")
        # mini_lang groups many golden cases per file; mini_game uses one trace
        # per file. Count the actual behavior cases when present, otherwise
        # count the file as a single golden scenario.
        case_counts[rel_name] = len(cases) if isinstance(cases, list) else 1
    return {
        "count": sum(case_counts.values()),
        "file_count": len(names),
        "names": names,
        "case_counts": case_counts,
    }


def golden_contract_coverage(
    source: Path,
    port_dir: Path,
    contract_tests: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Map source golden groups to the Rust integration tests expected to cover them."""
    tests_dir = source / "tests"
    files = sorted(tests_dir.rglob("golden_*.json")) if tests_dir.exists() else []
    groups = sorted({p.parent.relative_to(tests_dir) for p in files}, key=lambda p: str(p))
    expected: Dict[str, str] = {}
    missing: List[str] = []
    for group in groups:
        group_name = str(group)
        test_name = (contract_tests or {}).get(
            group_name,
            "golden_contract.rs" if group_name == "." else f"{'_'.join(group.parts)}_contract.rs",
        )
        expected[group_name] = test_name
        if not (port_dir / "tests" / test_name).exists():
            missing.append(test_name)
    return {
        "expected": expected,
        "missing": missing,
        "complete": not missing,
    }


def build_eval_report(
    source: Path,
    port_dir: Path,
    graph: Path,
    target: str,
    symbols: List[str],
    reindex: bool,
    use_advanced: bool,
    manual_fixes: int,
    skip_rust: bool,
    contract_tests: Optional[Dict[str, str]] = None,
    run_binary: bool = True,
    type_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    graph_res = eval_graph(graph, source, reindex, use_advanced)
    if not symbols:
        symbols = default_key_symbols(graph)
    packs = gen_context_packs(
        symbols,
        graph,
        ROOT / "output" / "port_eval" / target,
        type_context=type_context,
    )
    rust = (
        {"status": "skipped", "reason": "--skip-rust"}
        if skip_rust
        else eval_rust(port_dir, run_binary=run_binary)
    )
    golden = count_golden(source)
    contract_coverage = golden_contract_coverage(source, port_dir, contract_tests)

    rust_ok = rust.get("all_ok", False)
    golden_passed = (
        rust.get("golden_test", {}).get("status") == "ok" and contract_coverage["complete"]
        if not skip_rust
        else None
    )
    packs_ok = packs.get("complete") is True
    # Without the cargo stages we cannot assert the full north-star result, but
    # incomplete/failed context packs still force overall_pass=False so declared
    # gates cannot ignore missing pack evidence.
    if not packs_ok:
        overall = False
    elif skip_rust:
        overall = None
    else:
        overall = bool(graph_res["clean"] and rust_ok and golden_passed)

    return {
        "target": target,
        "graph": graph_res,
        "context_packs": packs,
        "rust": rust,
        "golden_scenarios": {**golden, "passed": golden_passed, "contract_coverage": contract_coverage},
        "manual_fix_count": manual_fixes,
        "overall_pass": overall,
    }


def to_markdown(r: Dict[str, Any]) -> str:
    g = r["graph"]
    lines = [
        f"# Port eval: {r['target']}",
        "",
        "## Graph (means)",
        f"- structural pass rate: **{g['structural_pass_rate']}** "
        f"(anomalies={g['structural_anomalies']}, dangling={g['dangling_targets']}, "
        f"semantic_suspicions={g.get('semantic_suspicions', 0)})",
        f"- calls: {g['total_calls']}  |  observations: {g['observations']}  |  clean: {g['clean']}",
        "",
        "## Context packs",
        f"- complete: **{r['context_packs'].get('complete')}** "
        f"({r['context_packs']['count']}/{len(r['context_packs']['requested'])} generated)",
        f"- generated: {', '.join(r['context_packs']['generated']) or '-'}",
        f"- failed: {', '.join(r['context_packs'].get('failed') or []) or '-'}",
        "",
        "## Rust (end-to-end / north-star)",
    ]
    rust = r["rust"]
    if rust.get("status") == "skipped":
        lines.append(f"- skipped ({rust.get('reason')})")
    else:
        for stage in ("fmt", "check", "golden_test", "run"):
            lines.append(f"- {stage}: **{rust.get(stage, {}).get('status', '?')}**")
    gs = r["golden_scenarios"]
    lines += [
        "",
        "## Golden cases",
        f"- {gs['count']} cases/scenarios across {gs.get('file_count', len(gs['names']))} files, passed: {gs['passed']}",
        f"- contract coverage complete: {gs['contract_coverage']['complete']}"
        f" (missing: {', '.join(gs['contract_coverage']['missing']) or '-'})",
        "",
        f"**manual_fix_count: {r['manual_fix_count']}**",
        f"**OVERALL PASS: {r['overall_pass']}**",
    ]
    return "\n".join(lines)


def load_gate_manifest(path: Path) -> Dict[str, Dict[str, Any]]:
    """Load and fail closed on the small declarative port-gate manifest."""
    data = json.loads(path.read_text())
    entries = data.get("ports")
    if not isinstance(entries, list):
        raise ValueError(f"{path}: ports must be a list")
    gates: Dict[str, Dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: each port entry must be an object")
        ident = entry.get("id")
        if not isinstance(ident, str) or not ident:
            raise ValueError(f"{path}: every port entry needs a non-empty id")
        if ident in gates:
            raise ValueError(f"{path}: duplicate port id {ident!r}")
        kind = entry.get("kind", "port")
        if kind not in {"port", "gap"}:
            raise ValueError(f"{path}: {ident}: kind must be port or gap")
        if kind == "port":
            for key in ("source", "port", "indexer"):
                if not isinstance(entry.get(key), str) or not entry[key]:
                    raise ValueError(f"{path}: {ident}: missing {key!r}")
            if entry["indexer"] not in {"python", "c"}:
                raise ValueError(f"{path}: {ident}: unknown indexer {entry['indexer']!r}")
        elif not isinstance(entry.get("gap"), str) or not entry["gap"]:
            raise ValueError(f"{path}: {ident}: gap entries need a named gap")
        gates[ident] = entry

    # Published graphs are not port-gate inputs: source profiles deliberately
    # rebuild disposable graphs.  They are nevertheless shipped local evidence,
    # so each declared source target must state whether its corresponding
    # published root is mutable (and health-checked) or frozen (and why).  This
    # keeps the health population in the same fail-closed manifest as the port
    # population; a new source target cannot quietly miss both.
    published_paths: Dict[str, str] = {}
    for ident, entry in gates.items():
        source = entry.get("source")
        indexer = entry.get("indexer")
        published = entry.get("published_graph")
        if not isinstance(source, str) or not source:
            raise ValueError(f"{path}: {ident}: missing source for published-graph declaration")
        if indexer not in {"python", "c"}:
            raise ValueError(f"{path}: {ident}: missing/invalid indexer for published-graph declaration")
        if not isinstance(published, dict):
            raise ValueError(f"{path}: {ident}: missing published_graph declaration")
        graph_path = published.get("path")
        mode = published.get("mode")
        if not isinstance(graph_path, str) or not graph_path.startswith("byog_"):
            raise ValueError(f"{path}: {ident}: published_graph.path must name a byog_* root")
        if mode not in {"mutable", "frozen"}:
            raise ValueError(f"{path}: {ident}: published_graph.mode must be mutable or frozen")
        if mode == "frozen" and not isinstance(published.get("reason"), str):
            raise ValueError(f"{path}: {ident}: frozen published graph needs a reason")
        prior = published_paths.get(graph_path)
        if prior is not None:
            raise ValueError(
                f"{path}: published graph {graph_path!r} is declared by both {prior!r} and {ident!r}"
            )
        published_paths[graph_path] = ident

        # Optional type_context (C only): strict schema, no arbitrary CLI escape.
        if "type_context" in entry:
            symbols = entry.get("symbols", [])
            if symbols is None:
                symbols = []
            if not isinstance(symbols, list) or not all(
                isinstance(s, str) and s for s in symbols
            ):
                raise ValueError(
                    f"{path}: {ident}: symbols must be a list of non-empty strings "
                    "when type_context is declared"
                )
            entry["type_context"] = validate_type_context(
                entry["type_context"],
                indexer=str(entry.get("indexer")),
                symbols=list(symbols),
                profile_id=ident,
            )

    cargo_ports = {
        str(cargo.parent.relative_to(ROOT))
        for cargo in (ROOT / "examples").glob("*_rust/Cargo.toml")
    }
    declared_ports = {
        str(entry["port"])
        for entry in gates.values()
        if entry.get("kind", "port") == "port"
    }
    missing_ports = sorted(cargo_ports - declared_ports)
    stale_ports = sorted(declared_ports - cargo_ports)
    if missing_ports or stale_ports:
        raise ValueError(
            f"{path}: Rust-port coverage mismatch; missing={missing_ports or '-'} "
            f"stale={stale_ports or '-'}"
        )
    required_gaps = data.get("required_source_gaps", [])
    if not isinstance(required_gaps, list) or not all(isinstance(item, str) for item in required_gaps):
        raise ValueError(f"{path}: required_source_gaps must be a list of port ids")
    undeclared_gaps = sorted(set(required_gaps) - set(gates))
    non_gap_entries = sorted(
        ident
        for ident in required_gaps
        if ident in gates and gates[ident].get("kind", "port") != "gap"
    )
    if undeclared_gaps or non_gap_entries:
        raise ValueError(
            f"{path}: source-only coverage mismatch; missing={undeclared_gaps or '-'} "
            f"not_gap={non_gap_entries or '-'}"
        )

    # Rust ports are discovered from disk and cross-checked both ways, so one
    # cannot be dropped silently. Source packages carrying a contract need the
    # same treatment: without it a target simply vanishes from the report
    # instead of being listed with its gap. `jsonpatch` — 25 golden cases and a
    # published graph, no Rust port — was omitted exactly this way.
    contract_sources = {
        pkg.name
        for pkg in (ROOT / "examples").iterdir()
        if pkg.is_dir()
        and not pkg.name.endswith("_rust")
        and not pkg.name.startswith(".")
        and any((pkg / "tests").rglob("golden*"))
    }
    covered_sources = {
        Path(entry["source"]).name if entry.get("source") else ident
        for ident, entry in gates.items()
    }
    unlisted_sources = sorted(contract_sources - covered_sources)
    if unlisted_sources:
        raise ValueError(
            f"{path}: source package(s) with a golden contract and no profile: "
            f"{unlisted_sources}. Declare each as a port or as a named gap; "
            "silently omitting one hides a target from the evidence report."
        )

    for ident, entry in gates.items():
        after = entry.get("after", [])
        if not isinstance(after, list) or not all(isinstance(dep, str) for dep in after):
            raise ValueError(f"{path}: profile {ident!r} has invalid 'after' dependencies")
        if len(after) != len(set(after)):
            raise ValueError(f"{path}: profile {ident!r} repeats an 'after' dependency")
        missing = sorted(set(after) - set(gates))
        if missing:
            raise ValueError(
                f"{path}: profile {ident!r} depends on unknown profile(s): {missing}"
            )
        if ident in after:
            raise ValueError(f"{path}: profile {ident!r} cannot depend on itself")
    return gates


def load_aggregate_checks(path: Path) -> List[Dict[str, Any]]:
    """Load the small declarative checks that apply to an aggregate gate only."""
    data = json.loads(path.read_text())
    checks = data.get("aggregate_checks", [])
    if not isinstance(checks, list):
        raise ValueError(f"{path}: aggregate_checks must be a list")
    loaded: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for check in checks:
        if not isinstance(check, dict):
            raise ValueError(f"{path}: each aggregate check must be an object")
        name = check.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"{path}: aggregate check needs a non-empty name")
        if name in seen:
            raise ValueError(f"{path}: duplicate aggregate check {name!r}")
        seen.add(name)
        command = check.get("command")
        if not isinstance(command, list) or not command or not all(
            isinstance(part, str) for part in command
        ):
            raise ValueError(f"{path}: aggregate check {name!r} needs a non-empty command list")
        tools = check.get("tools", [])
        if not isinstance(tools, list) or not all(isinstance(tool, str) for tool in tools):
            raise ValueError(f"{path}: aggregate check {name!r} has invalid tools")
        when = check.get("when", "always")
        if when not in {"always", "full", "scale", "differential-full"}:
            raise ValueError(f"{path}: aggregate check {name!r} has invalid when={when!r}")
        loaded.append(check)
    return loaded


def _order_gate_ids(selected: List[str], gates: Dict[str, Dict[str, Any]]) -> List[str]:
    """Topologically order selected profiles while preserving manifest order.

    Dependencies are used only for aggregate runs.  A named ``--gate`` remains
    a focused diagnostic command, so it does not unexpectedly run neighbouring
    profiles merely because one full-suite check consumes their fresh output.
    """
    wanted = set(selected)
    ordered: List[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(ident: str) -> None:
        if ident in visited:
            return
        if ident in visiting:
            raise ValueError(f"port-gate dependency cycle includes {ident!r}")
        visiting.add(ident)
        for dep in gates[ident].get("after", []):
            if dep in wanted:
                visit(dep)
        visiting.remove(ident)
        visited.add(ident)
        ordered.append(ident)

    for ident in selected:
        visit(ident)
    return ordered


def _tool_probe(tool: str) -> Dict[str, Any]:
    """Classify a missing tool as a skip and an installed-but-broken tool as fail."""
    if tool == "c_compiler":
        for candidate in ("cc", "gcc", "clang"):
            executable = shutil.which(candidate)
            if executable is not None:
                result = _run([executable, "--version"], ROOT)
                if result["status"] == "ok":
                    return {"status": "ok", "tool": candidate}
                return {
                    "status": "fail",
                    "reason": f"C compiler {candidate!r} is present but its probe failed",
                    "detail": result,
                }
        return {"status": "skipped", "reason": "no C compiler (cc/gcc/clang) on PATH"}

    if tool == "clang":
        # Clang/Apple Clang only (required by --clang-type-uses). Missing Clang
        # is an explicit skip; a present binary that cannot prove Clang identity
        # or fails --version is a failure.
        from c_compiler_common import (  # type: ignore
            CompilerOverlayError,
            require_clang_identity,
        )

        candidates: List[tuple[str, str]] = []
        for name in ("clang", "cc"):
            path = shutil.which(name)
            if path and all(
                path != candidate_path for _, candidate_path in candidates
            ):
                candidates.append((name, path))
        if not candidates:
            return {
                "status": "skipped",
                "reason": "no Clang/Apple Clang toolchain on PATH",
            }
        hard_failures: List[Dict[str, Any]] = []
        for candidate_name, executable in candidates:
            result = _run([executable, "--version"], ROOT)
            if result["status"] != "ok":
                if candidate_name == "clang":
                    hard_failures.append(
                        {
                            "candidate": executable,
                            "reason": "version probe failed",
                            "detail": result,
                        }
                    )
                continue
            try:
                require_clang_identity(executable)
            except CompilerOverlayError as error:
                # Not Clang (e.g. gcc-as-cc); try next candidate.
                if candidate_name == "clang":
                    hard_failures.append(
                        {"candidate": executable, "reason": str(error)}
                    )
                continue
            return {"status": "ok", "tool": executable}
        if hard_failures:
            return {
                "status": "fail",
                "reason": "installed clang is present but broken or has a non-Clang identity",
                "detail": hard_failures,
            }
        return {
            "status": "skipped",
            "reason": "no verified Clang/Apple Clang toolchain on PATH",
        }

    if tool == "miri":
        cargo = _tool_probe("cargo")
        if cargo["status"] != "ok":
            return cargo
        result = _run(["cargo", "+nightly", "miri", "--version"], ROOT)
        if result["status"] == "ok":
            return {"status": "ok", "tool": "cargo +nightly miri"}
        detail = "\n".join(result.get("output_tail", []))
        missing_markers = ("component 'miri'", "miri component", "not installed", "not available")
        if any(marker in detail.lower() for marker in missing_markers):
            return {"status": "skipped", "reason": "nightly Miri component is unavailable"}
        return {
            "status": "fail",
            "reason": "cargo +nightly miri is present but its probe failed",
            "detail": result,
        }

    executable = shutil.which(tool)
    if executable is None:
        return {"status": "skipped", "reason": f"{tool} not found on PATH"}
    result = _run([executable, "--version"], ROOT)
    if result["status"] == "ok":
        return {"status": "ok", "tool": tool}
    return {
        "status": "fail",
        "reason": f"{tool!r} is present but its probe failed",
        "detail": result,
    }


def _expand_gate_command(command: List[str]) -> List[str]:
    """Expand only the two stable manifest placeholders; do not invoke a shell."""
    replacements = {"{python}": sys.executable, "{root}": str(ROOT)}
    return [replacements.get(part, part) for part in command]


def _check_is_enabled(check: Dict[str, Any], full: bool, scale: bool, differential_full: bool) -> bool:
    when = check.get("when", "always")
    enabled = {
        "always": True,
        "full": full,
        "scale": scale,
        "differential-full": differential_full,
    }
    if when not in enabled:
        raise ValueError(f"unknown gate check selector {when!r}")
    return enabled[when]


def _run_declared_check(
    check: Dict[str, Any], *, full: bool, scale: bool, differential_full: bool
) -> Dict[str, Any]:
    """Execute one manifest check with explicit tool skip/fail semantics."""
    name = str(check.get("name", "unnamed check"))
    if not _check_is_enabled(check, full, scale, differential_full):
        return {"name": name, "status": "skipped", "reason": f"requires --{check['when']}"}

    for tool in check.get("tools", []):
        probe = _tool_probe(str(tool))
        if probe["status"] != "ok":
            return {"name": name, **probe}

    command = check.get("command")
    if not isinstance(command, list) or not all(isinstance(part, str) for part in command):
        raise ValueError(f"{name}: command must be a list of strings")
    declared_env = check.get("env", {})
    if not isinstance(declared_env, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in declared_env.items()
    ):
        raise ValueError(f"{name}: env must be a string-to-string object")
    declared_cwd = check.get("cwd", ".")
    if not isinstance(declared_cwd, str):
        raise ValueError(f"{name}: cwd must be a relative path string")
    cwd = (ROOT / declared_cwd).resolve()
    if ROOT not in cwd.parents and cwd != ROOT:
        raise ValueError(f"{name}: cwd must stay within the repository")
    result = _run(_expand_gate_command(command), cwd, env=declared_env)
    result["name"] = name
    successful_skip_marker = check.get("successful_skip_marker")
    if successful_skip_marker is not None:
        if not isinstance(successful_skip_marker, str) or not successful_skip_marker:
            raise ValueError(f"{name}: successful_skip_marker must be a non-empty string")
        output = "\n".join(result.get("output_tail", []))
        if result["status"] == "ok" and successful_skip_marker in output:
            result["status"] = "skipped"
            result["reason"] = str(
                check.get("skip_reason", "command passed with declared source skips")
            )
    # pytest reports a deliberately skipped optional tool with exit code zero.
    # Preserve that in the aggregate status instead of silently calling it full coverage.
    output = "\n".join(result.get("output_tail", []))
    result["reported_skip"] = bool(re.search(r"\b\d+ skipped\b", output))
    return result


def _index_gate(gate: Dict[str, Any], graph: Path) -> Dict[str, Any]:
    type_context = gate.get("type_context")
    wants_clang_type_uses = bool(
        isinstance(type_context, dict) and type_context.get("clang_type_uses") is True
    )
    if gate["indexer"] == "c":
        if wants_clang_type_uses:
            # Configured type-use index requires a verified Clang identity.
            clang = _tool_probe("clang")
            if clang["status"] != "ok":
                return {
                    "name": "c graph index",
                    **clang,
                    "elapsed_seconds": 0.0,
                    "clang_type_uses": True,
                }
        else:
            compiler = _tool_probe("c_compiler")
            if compiler["status"] != "ok":
                return {
                    "name": "c graph index",
                    **compiler,
                    "elapsed_seconds": 0.0,
                }
    source = ROOT / gate["source"]
    if gate["indexer"] == "python":
        command = [
            sys.executable,
            str(ROOT / "scripts" / "index_python.py"),
            "--package",
            str(source),
            "--graph",
            str(graph),
        ]
    else:
        command = [
            sys.executable,
            str(ROOT / "scripts" / "index_c.py"),
            "--package",
            str(source),
            "--graph",
            str(graph),
        ]
        if wants_clang_type_uses:
            # Disposable output/port_gates/<id>/graph only — never published byog_*.
            command.append("--clang-type-uses")
    result = _run(command, ROOT)
    result["name"] = f"{gate['indexer']} graph index"
    result["clang_type_uses"] = wants_clang_type_uses
    result["command"] = list(command)
    return result


def _gate_summary_line(result: Dict[str, Any]) -> str:
    graph = result.get("port_eval", {}).get("graph")
    if not graph:
        return "graph=n/a"
    return (
        f"graph={graph['structural_pass_rate']} "
        f"calls={graph['total_calls']} packs="
        f"{result['port_eval']['context_packs']['count']}/"
        f"{len(result['port_eval']['context_packs']['requested'])}"
    )


def run_declared_gate(
    gate: Dict[str, Any], *, full: bool, scale: bool, differential_full: bool
) -> Dict[str, Any]:
    """Run one complete port profile; never turn a broken present tool into a skip."""
    ident = str(gate["id"])
    if gate.get("kind", "port") == "gap":
        return {
            "id": ident,
            "status": "gap",
            "gap": gate["gap"],
            "timing": {"total_seconds": 0.0},
        }

    started = time.monotonic()
    result: Dict[str, Any] = {"id": ident, "status": "pass", "checks": []}
    for check in gate.get("checks", []):
        check_result = _run_declared_check(
            check, full=full, scale=scale, differential_full=differential_full
        )
        result["checks"].append(check_result)

    graph = ROOT / "output" / "port_gates" / ident / "graph"
    index_result = _index_gate(gate, graph)
    result["index"] = index_result

    cargo = _tool_probe("cargo")
    rustc = _tool_probe("rustc")
    result["tool_probes"] = {"cargo": cargo, "rustc": rustc}
    skip_rust = any(probe["status"] == "skipped" for probe in (cargo, rustc))
    if index_result["status"] == "ok":
        port_eval_started = time.monotonic()
        try:
            result["port_eval"] = build_eval_report(
                source=ROOT / gate["source"],
                port_dir=ROOT / gate["port"],
                graph=graph,
                target=ident,
                symbols=list(gate.get("symbols", [])),
                reindex=False,
                use_advanced=False,
                manual_fixes=int(gate.get("manual_fixes", 0)),
                skip_rust=skip_rust,
                contract_tests=dict(gate.get("contract_tests", {})),
                run_binary=bool(gate.get("run_binary", True)),
                type_context=gate.get("type_context"),
            )
        except (OSError, RuntimeError, ValueError) as error:
            result["port_eval_error"] = str(error)
        result["port_eval_seconds"] = round(time.monotonic() - port_eval_started, 3)

    failed_checks = [check for check in result["checks"] if check["status"] == "fail"]
    broken_tools = [
        probe for probe in result["tool_probes"].values() if probe["status"] == "fail"
    ]
    port_eval = result.get("port_eval") or {}
    packs = port_eval.get("context_packs") or {}
    packs_incomplete = "port_eval" in result and packs.get("complete") is not True
    if (
        index_result["status"] == "fail"
        or failed_checks
        or broken_tools
        or "port_eval_error" in result
    ):
        result["status"] = "fail"
    elif packs_incomplete:
        # Failed/missing context packs fail the declared gate, not only a count.
        result["status"] = "fail"
    elif (
        index_result["status"] != "skipped"
        and not skip_rust
        and port_eval.get("overall_pass") is not True
    ):
        # This includes a corrupt golden: cargo test fails inside port_eval and
        # the gate must exit non-zero instead of merely printing a false report.
        result["status"] = "fail"
    else:
        skipped = index_result["status"] == "skipped" or skip_rust or any(
            check["status"] == "skipped" or check.get("reported_skip", False)
            for check in result["checks"]
        )
        result["status"] = "pass_with_skips" if skipped else "pass"

    checks_seconds = sum(float(check.get("elapsed_seconds", 0.0)) for check in result["checks"])
    index_seconds = float(index_result.get("elapsed_seconds", 0.0))
    port_eval_seconds = float(result.get("port_eval_seconds", 0.0))
    total_seconds = round(time.monotonic() - started, 3)
    result["timing"] = {
        "checks_seconds": round(checks_seconds, 3),
        "index_seconds": round(index_seconds, 3),
        "port_eval_seconds": round(port_eval_seconds, 3),
        # Tool probes and report setup are deliberately included rather than
        # disappearing from the wall-clock number.
        "other_seconds": round(max(0.0, total_seconds - checks_seconds - index_seconds - port_eval_seconds), 3),
        "total_seconds": total_seconds,
    }
    return result


def print_declared_gate_result(result: Dict[str, Any]) -> None:
    """Keep the human output compact while retaining JSON-style evidence details."""
    ident = result["id"]
    if result["status"] == "gap":
        print(f"{ident}: GAP — {result['gap']}")
        return
    print(f"== {ident} ==")
    for check in result.get("checks", []):
        suffix = (
            f" — {check.get('reason', '')}"
            if check["status"] in {"skipped", "fail"} and check.get("reason")
            else ""
        )
        print(f"  {check['status'].upper()}: {check['name']}{suffix}")
        if check["status"] == "fail":
            print("    " + "\n    ".join(check.get("output_tail", [])[-5:]))
    index = result.get("index", {})
    print(f"  {index.get('status', 'n/a').upper()}: {index.get('name', 'graph index')}")
    for tool, probe in result.get("tool_probes", {}).items():
        if probe["status"] != "ok":
            print(f"  {probe['status'].upper()}: {tool} — {probe.get('reason', '')}")
    if "port_eval" in result:
        port = result["port_eval"]
        packs = port.get("context_packs") or {}
        print(
            f"  PORT_EVAL: { _gate_summary_line(result) }; "
            f"packs_complete={packs.get('complete')} "
            f"golden={port['golden_scenarios']['count']} "
            f"manual_fixes={port['manual_fix_count']} overall={port['overall_pass']}"
        )
        if packs.get("complete") is not True:
            for failure in (packs.get("failures") or [])[:5]:
                print(
                    f"    pack-fail: {failure.get('symbol')} "
                    f"[{failure.get('stage')}] {failure.get('reason')}"
                )
    if "port_eval_error" in result:
        print(f"  FAIL: port_eval setup — {result['port_eval_error']}")
    timing = result.get("timing")
    if timing:
        print(
            "  TIMING: "
            f"checks={timing.get('checks_seconds', 0.0):.3f}s "
            f"index={timing.get('index_seconds', 0.0):.3f}s "
            f"port_eval={timing.get('port_eval_seconds', 0.0):.3f}s "
            f"other={timing.get('other_seconds', 0.0):.3f}s "
            f"total={timing.get('total_seconds', 0.0):.3f}s"
        )
    print(f"  RESULT: {result['status'].upper()}")


def main(
    graph: Optional[Path] = typer.Option(None, "--graph", help="BYOG graph root (e.g. byog_mini_game)"),
    source: Path = typer.Option(Path("examples/mini_game"), "--source", help="Python source project"),
    port: Path = typer.Option(Path("examples/mini_game_rust"), "--port", help="Rust port (Cargo project)"),
    target: Optional[str] = typer.Option(None, "--target", help="Logical target name (default: source dir name)"),
    symbol: List[str] = typer.Option(
        [], "--symbol",
        help="Key symbol(s)/module(s) to context-pack (repeatable). If omitted, the most-called symbols are auto-selected.",
    ),
    reindex: bool = typer.Option(False, "--reindex", help="Regenerate the graph from --source via index_python first"),
    use_advanced: bool = typer.Option(False, "--use-advanced", help="Advanced resolver when reindexing"),
    manual_fixes: int = typer.Option(0, "--manual-fixes", help="Manual interventions needed (recorded, set by hand for now)"),
    skip_rust: bool = typer.Option(False, "--skip-rust", help="Skip cargo stages (graph + packs only)"),
    json_output: bool = typer.Option(False, "--json", help="Emit full machine-readable report"),
    markdown: Optional[Path] = typer.Option(None, "--markdown", help="Also write a Markdown report to this path"),
    gate: List[str] = typer.Option(
        [], "--gate", help="Run a named manifest-backed evidence gate (repeatable)."
    ),
    all_gates: bool = typer.Option(
        False, "--all-gates", help="Run every manifest-backed port or declared gap."
    ),
    gate_manifest: Path = typer.Option(
        DEFAULT_GATE_MANIFEST,
        "--gate-manifest",
        help="Declarative port-evidence manifest used by --gate/--all-gates.",
    ),
    full: bool = typer.Option(
        False, "--full", help="Enable manifest checks marked full (for example Miri/full pytest)."
    ),
    scale: bool = typer.Option(
        False, "--scale", help="Enable manifest checks marked scale (charset compatibility mode)."
    ),
    differential_full: bool = typer.Option(
        False,
        "--differential-full",
        help="Enable manifest checks marked differential-full (charset compatibility mode).",
    ),
) -> None:
    """Run the Python->Rust port eval and emit a repeatable report."""
    if gate or all_gates:
        if graph is not None:
            raise typer.BadParameter("--graph cannot be combined with --gate/--all-gates")
        manifest_path = gate_manifest if gate_manifest.is_absolute() else ROOT / gate_manifest
        try:
            gates = load_gate_manifest(manifest_path)
            aggregate_checks = load_aggregate_checks(manifest_path) if all_gates else []
        except (OSError, ValueError, json.JSONDecodeError) as error:
            typer.secho(f"port-gate manifest error: {error}", fg=typer.colors.RED, err=True)
            raise typer.Exit(2) from error
        selected = list(gates) if all_gates else gate
        unknown = [ident for ident in selected if ident not in gates]
        if unknown:
            typer.secho(f"unknown port gate(s): {', '.join(unknown)}", fg=typer.colors.RED, err=True)
            raise typer.Exit(2)
        if not selected:
            typer.secho("no port gates selected", fg=typer.colors.RED, err=True)
            raise typer.Exit(2)
        if all_gates:
            try:
                selected = _order_gate_ids(selected, gates)
            except ValueError as error:
                typer.secho(f"port-gate manifest error: {error}", fg=typer.colors.RED, err=True)
                raise typer.Exit(2) from error
        aggregate_started = time.monotonic()
        reports = [
            run_declared_gate(
                gates[ident], full=full, scale=scale, differential_full=differential_full
            )
            for ident in selected
        ]
        aggregate_results = [
            _run_declared_check(
                check, full=full, scale=scale, differential_full=differential_full
            )
            for check in aggregate_checks
        ]
        for report in reports:
            print_declared_gate_result(report)
        if aggregate_results:
            print("== aggregate evidence ==")
            for check in aggregate_results:
                suffix = f" — {check['reason']}" if check.get("reason") else ""
                print(f"  {check['status'].upper()}: {check['name']}{suffix}")
                if check["status"] == "fail":
                    print("    " + "\n    ".join(check.get("output_tail", [])[-5:]))
        aggregate_seconds = time.monotonic() - aggregate_started
        timed_profiles = [
            f"{report['id']}={report['timing']['total_seconds']:.3f}s"
            for report in reports
            if report["status"] != "gap" and "timing" in report
        ]
        if timed_profiles:
            print(
                "PORT EVIDENCE TIMING: "
                f"wall={aggregate_seconds:.3f}s; profiles=" + ", ".join(timed_profiles)
            )
        failed = [report["id"] for report in reports if report["status"] == "fail"]
        failed.extend(
            f"aggregate:{check['name']}"
            for check in aggregate_results
            if check["status"] == "fail"
        )
        skipped = [report["id"] for report in reports if report["status"] == "pass_with_skips"]
        skipped.extend(
            f"aggregate:{check['name']}"
            for check in aggregate_results
            if check["status"] == "skipped" or check.get("reported_skip", False)
        )
        gaps = [report["id"] for report in reports if report["status"] == "gap"]
        if failed:
            print(f"PORT EVIDENCE: FAIL ({', '.join(failed)})")
            raise typer.Exit(1)
        if skipped:
            suffix = f"; DECLARED GAPS ({', '.join(gaps)})" if gaps else ""
            print(f"PORT EVIDENCE: PASS WITH SKIPS ({', '.join(skipped)}){suffix}")
        elif gaps:
            print(f"PORT EVIDENCE: PASS WITH DECLARED GAPS ({', '.join(gaps)})")
        else:
            print("PORT EVIDENCE: PASS")
        return

    if graph is None:
        raise typer.BadParameter("--graph is required unless --gate or --all-gates is used")
    report = build_eval_report(
        source=source if source.is_absolute() else ROOT / source,
        port_dir=port if port.is_absolute() else ROOT / port,
        graph=graph if graph.is_absolute() else ROOT / graph,
        target=target or source.name,
        symbols=symbol,
        reindex=reindex,
        use_advanced=use_advanced,
        manual_fixes=manual_fixes,
        skip_rust=skip_rust,
    )
    if markdown is not None:
        markdown.write_text(to_markdown(report))
    if json_output:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        return

    g = report["graph"]
    print(f"target            : {report['target']}")
    print(f"graph pass rate   : {g['structural_pass_rate']} "
          f"(anomalies={g['structural_anomalies']}, dangling={g['dangling_targets']}, "
          f"semantic_suspicions={g.get('semantic_suspicions', 0)}, "
          f"calls={g['total_calls']}, obs={g['observations']})")
    print(f"context packs     : {report['context_packs']['count']}/{len(report['context_packs']['requested'])} "
          f"{report['context_packs']['generated']}")
    rust = report["rust"]
    if rust.get("status") == "skipped":
        print(f"rust              : skipped ({rust.get('reason')})")
    else:
        print("rust              : " + "  ".join(
            f"{s}={rust.get(s, {}).get('status', '?')}" for s in ("fmt", "check", "golden_test", "run")))
    gs = report["golden_scenarios"]
    print(
        f"golden cases      : {gs['count']} across {gs.get('file_count', len(gs['names']))} files "
        f"(passed={gs['passed']})"
    )
    coverage = gs["contract_coverage"]
    if not coverage["complete"]:
        print(f"golden coverage   : missing {coverage['missing']}")
    print(f"manual fixes      : {report['manual_fix_count']}")
    print(f"OVERALL PASS      : {report['overall_pass']}")
    if markdown is not None:
        print(f"(markdown written : {markdown})")


if __name__ == "__main__":
    typer.run(main)
