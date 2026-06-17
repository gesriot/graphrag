#!/usr/bin/env python
"""
Repeatable precision audit for resolved CALLS edges in a BYOG graph (no external API).

Turns the manual review we did by hand into a deterministic, reproducible report so
that scaling to larger repos yields comparable numbers instead of eyeballing.

It reports, over all `calls` relationships:
- structural anomalies: a call's span must fall inside its caller entity's line range
  (this is exactly the class of bug that the same-named-`main` collision produced:
  edges attributed to a caller whose body does not contain the call site).
- dangling targets: a call target that is not a known entity title.
- structural pass rate: (calls - anomalies - dangling) / calls. This is an objective
  lower bound, NOT semantic precision -- a clean structural pass still needs the
  sampled edges below to be eyeballed for true precision.
- a deterministic sample (seed + size) with the actual source line at each call site,
  for the human/semantic precision judgement.

Example:
    uv run python scripts/audit_call_edges.py --graph byog_tool_eval --sample 30 --seed 42
    uv run python scripts/audit_call_edges.py --graph byog_tool_eval --json > audit.json
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import typer

# Support both `python -m scripts.xxx` and direct `python scripts/xxx.py`
sys.path.insert(0, str(Path(__file__).parent))
from byog_graph import ByogGraph  # noqa: E402


def _line_range(span: str) -> Optional[Tuple[int, int]]:
    """Parse an entity span "startline:col-endline:col" into (start_line, end_line)."""
    sp = str(span)
    if not sp or sp == "None":
        return None
    try:
        if "-" in sp:
            a, b = sp.split("-", 1)
            return int(a.split(":")[0]), int(b.split(":")[0])
        n = int(sp.split(":")[0])
        return n, n
    except ValueError:
        return None


def _call_line(span: str) -> Optional[int]:
    """Parse a call-site span "line:col" (or "line") into its line number."""
    sp = str(span)
    try:
        return int(sp.split(":")[0])
    except ValueError:
        return None


def _resolve_source_path(source_file: str, source_root: Optional[Path]) -> Optional[Path]:
    """Best-effort locate the source file (only used for sample evidence lines)."""
    p = Path(str(source_file))
    if p.exists():
        return p
    if source_root is not None:
        cand = source_root / p.name
        if cand.exists():
            return cand
    return None


def structural_audit(ents: pd.DataFrame, calls: pd.DataFrame) -> List[Dict[str, Any]]:
    """Return one record per call whose span is not inside its caller's line range."""
    ranges = {
        str(row["title"]): _line_range(row.get("span"))
        for _, row in ents.iterrows()
    }
    anomalies: List[Dict[str, Any]] = []
    for _, r in calls.iterrows():
        src = str(r["source"])
        rng = ranges.get(src)
        if rng is None:
            anomalies.append({
                "kind": "no_caller_entity" if src not in ranges else "caller_no_span",
                "source": src, "target": str(r["target"]), "span": str(r.get("span")),
                "caller_range": None,
            })
            continue
        line = _call_line(r.get("span"))
        if line is None:
            continue  # cannot place the call; not counted as a structural failure
        start, end = rng
        if not (start <= line <= end):
            anomalies.append({
                "kind": "span_outside_caller",
                "source": src, "target": str(r["target"]), "span": str(r.get("span")),
                "caller_range": f"{start}-{end}",
            })
    return anomalies


def dangling_targets(ents: pd.DataFrame, calls: pd.DataFrame) -> List[Dict[str, Any]]:
    titles = set(ents["title"].astype(str))
    out: List[Dict[str, Any]] = []
    for _, r in calls.iterrows():
        if str(r["target"]) not in titles:
            out.append({"source": str(r["source"]), "target": str(r["target"]), "span": str(r.get("span"))})
    return out


def sample_edges(
    calls: pd.DataFrame,
    ents: pd.DataFrame,
    size: int,
    seed: int,
    source_root: Optional[Path],
) -> List[Dict[str, Any]]:
    records = calls.to_dict("records")
    rng = random.Random(seed)
    picked = records if size >= len(records) else rng.sample(records, size)
    ranges = {str(row["title"]): _line_range(row.get("span")) for _, row in ents.iterrows()}
    file_cache: Dict[str, List[str]] = {}
    out: List[Dict[str, Any]] = []
    for r in sorted(picked, key=lambda x: (str(x["source"]), str(x.get("span")))):
        src = str(r["source"])
        caller_rng = ranges.get(src)
        line = _call_line(r.get("span"))
        structural_ok = bool(caller_rng and line and caller_rng[0] <= line <= caller_rng[1])
        source_line = None
        path = _resolve_source_path(r.get("source_file"), source_root)
        if path is not None and line is not None:
            key = str(path)
            if key not in file_cache:
                file_cache[key] = path.read_text().splitlines()
            lines = file_cache[key]
            if 1 <= line <= len(lines):
                source_line = lines[line - 1].strip()
        out.append({
            "source": src,
            "target": str(r["target"]),
            "confidence": r.get("confidence"),
            "is_deterministic": r.get("is_deterministic"),
            "span": str(r.get("span")),
            "caller_range": f"{caller_rng[0]}-{caller_rng[1]}" if caller_rng else None,
            "structural_ok": structural_ok,
            "source_line": source_line,
        })
    return out


def build_report(
    graph: Path,
    sample: int = 30,
    seed: int = 42,
    source_root: Optional[Path] = None,
) -> Dict[str, Any]:
    g = ByogGraph(graph)
    calls = g.rels[g.rels["type"].astype(str) == "calls"]
    total = len(calls)
    anomalies = structural_audit(g.ents, calls)
    dangling = dangling_targets(g.ents, calls)
    clean = total - len(anomalies) - len(dangling)
    return {
        "graph": str(graph),
        "snapshot": str(g._snap_base),
        "total_calls": total,
        "structural": {
            "anomaly_count": len(anomalies),
            "anomalies": anomalies,
            "pass_rate": round(clean / total, 4) if total else 1.0,
        },
        "dangling_count": len(dangling),
        "dangling_targets": dangling,
        "sample": {
            "seed": seed,
            "size": min(sample, total),
            "edges": sample_edges(calls, g.ents, sample, seed, source_root),
        },
    }


def main(
    graph: Path = typer.Option(..., "--graph", help="BYOG graph root (e.g. byog_tool_eval)"),
    source_root: Optional[Path] = typer.Option(None, "--source-root", help="Fallback dir to locate source files if their recorded absolute paths moved"),
    sample: int = typer.Option(30, "--sample", help="How many call edges to sample for the eyeball precision check"),
    seed: int = typer.Option(42, "--seed", help="Sampling seed (keep fixed for comparable reports)"),
    json_output: bool = typer.Option(False, "--json", help="Emit the full machine-readable report"),
) -> None:
    """Audit resolved CALLS edges: structural anomalies, dangling targets, sampled evidence."""
    report = build_report(graph, sample=sample, seed=seed, source_root=source_root)
    if json_output:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        return

    s = report["structural"]
    print(f"graph    : {report['graph']}")
    print(f"snapshot : {report['snapshot']}")
    print(f"calls    : {report['total_calls']}")
    print(f"structural pass rate : {s['pass_rate']}  "
          f"(anomalies={s['anomaly_count']}, dangling={report['dangling_count']})")

    if s["anomalies"]:
        print("\nSTRUCTURAL ANOMALIES (call span outside caller range):")
        for a in s["anomalies"]:
            print(f"  [{a['kind']}] {a['source']} -> {a['target']}  @{a['span']} caller={a['caller_range']}")
    if report["dangling_targets"]:
        print("\nDANGLING TARGETS (target not a known entity):")
        for d in report["dangling_targets"]:
            print(f"  {d['source']} -> {d['target']}  @{d['span']}")

    print(f"\nSAMPLE (seed={report['sample']['seed']}, n={report['sample']['size']}) "
          f"-- eyeball each for semantic precision:")
    for e in report["sample"]["edges"]:
        flag = "ok " if e["structural_ok"] else "!! "
        print(f"  {flag}[{e['confidence']}|det={e['is_deterministic']}] "
              f"{e['source']} -> {e['target']}  @{e['span']}")
        if e["source_line"] is not None:
            print(f"        {e['source_line'][:100]}")


if __name__ == "__main__":
    typer.run(main)
