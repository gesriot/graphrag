#!/usr/bin/env python
"""C preprocessor-dependence diagnostics for tree-sitter-only extraction (Phase 6).

tree-sitter-c does not evaluate the preprocessor. This module flags extracted
facts that *depend* on preprocessor structure we cannot resolve without clang:
conditional regions, function-like macros, and compile-database -D defines.

Flags are **provenance labels**, not demotions: they do not change
``is_deterministic`` or remove edges, so ``audit_call_edges`` pass rates stay
unchanged. Consumers (context packs, humans) can see which material is
syntax-only relative to a real build configuration.

No clang dependency — only ``compile_commands.json`` + source text.
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

# Match leading preprocessor directive (after optional whitespace).
_DIR_RE = re.compile(
    r"^\s*#\s*(if|ifdef|ifndef|elif|else|endif|define|include)\b(.*)$"
)
# Function-like macros require '#' define NAME( with no space before '('.
# A space before '(' is an object-like macro whose replacement starts with '('.
_DEFINE_FUNC_RE = re.compile(r"^([A-Za-z_]\w*)\(")
_DEFINE_OBJ_RE = re.compile(r"^([A-Za-z_]\w*)\b")
_D_FLAG_RE = re.compile(r"(?:^|\s)-D([A-Za-z_]\w*)(?:=(\S+))?")


@dataclass
class ConditionalRegion:
    """A contiguous region controlled by #if / #ifdef / #ifndef / #elif / #else."""

    start_line: int
    end_line: int  # inclusive
    kind: str  # if / ifdef / ifndef / elif / else
    condition: str
    depth: int
    is_include_guard: bool = False
    file: str = ""


@dataclass
class MacroDef:
    name: str
    function_like: bool
    line: int
    file: str
    body_preview: str = ""


@dataclass
class FileAnalysis:
    path: Path
    regions: List[ConditionalRegion] = field(default_factory=list)
    macros: List[MacroDef] = field(default_factory=list)
    compile_defines: Dict[str, Optional[str]] = field(default_factory=dict)


@dataclass
class PackageAnalysis:
    package_dir: Path
    files: Dict[str, FileAnalysis] = field(default_factory=dict)  # resolved path str
    all_function_macros: Set[str] = field(default_factory=set)
    all_object_macros: Set[str] = field(default_factory=set)
    compile_defines: Dict[str, Optional[str]] = field(default_factory=dict)


def parse_compile_commands(package_dir: Path) -> Dict[str, Dict[str, Optional[str]]]:
    """Map source file basename -> {DEFINE: value_or_None} from compile_commands.json."""
    cc_path = package_dir / "compile_commands.json"
    per_file: Dict[str, Dict[str, Optional[str]]] = {}
    package_defines: Dict[str, Optional[str]] = {}
    if not cc_path.is_file():
        return per_file
    try:
        entries = json.loads(cc_path.read_text())
    except json.JSONDecodeError:
        return per_file
    if not isinstance(entries, list):
        return per_file
    for ent in entries:
        if not isinstance(ent, dict):
            continue
        cmd = ent.get("command") or ""
        if not cmd and ent.get("arguments"):
            cmd = " ".join(str(a) for a in ent["arguments"])
        defines: Dict[str, Optional[str]] = {}
        for m in _D_FLAG_RE.finditer(cmd):
            defines[m.group(1)] = m.group(2)
            package_defines[m.group(1)] = m.group(2)
        f = ent.get("file") or ""
        key = Path(str(f)).name
        if key:
            per_file[key] = defines
    # stash package-level on a reserved key
    per_file["*"] = package_defines
    return per_file


def _is_likely_include_guard(
    kind: str, condition: str, start_line: int, end_line: int, n_lines: int
) -> bool:
    """Heuristic: outer #ifndef/#if !defined wrapping most of a header."""
    if start_line > 40:
        return False
    if end_line < n_lines - 5:
        return False
    # Must cover a large fraction of the file
    if (end_line - start_line + 1) < 0.7 * max(n_lines, 1):
        return False
    cond = condition.strip()
    if kind == "ifndef":
        return True
    if kind == "if" and (
        cond.startswith("!defined") or cond.startswith("! defined")
    ):
        return True
    return False


def analyze_source_text(
    text: str, *, path: Path, compile_defines: Optional[Dict[str, Optional[str]]] = None
) -> FileAnalysis:
    """Find conditional regions and macro definitions in one translation unit's text."""
    lines = text.splitlines()
    n_lines = len(lines)
    fa = FileAnalysis(path=path, compile_defines=dict(compile_defines or {}))
    # stack entries: (start_line, kind, condition, depth)
    stack: List[Tuple[int, str, str, int]] = []
    # pending open regions for elif/else chains share the same depth slot
    open_regions: List[ConditionalRegion] = []

    def close_top(end_line: int) -> None:
        if not open_regions:
            return
        # close the most recent open region that has end_line == 0 sentinel
        for reg in reversed(open_regions):
            if reg.end_line == 0:
                reg.end_line = end_line
                reg.is_include_guard = _is_likely_include_guard(
                    reg.kind, reg.condition, reg.start_line, reg.end_line, n_lines
                )
                break

    for i, line in enumerate(lines, 1):
        m = _DIR_RE.match(line)
        if not m:
            continue
        directive, rest = m.group(1), (m.group(2) or "").strip()
        if directive == "define":
            fm = _DEFINE_FUNC_RE.match(rest)
            if fm:
                fa.macros.append(
                    MacroDef(
                        name=fm.group(1),
                        function_like=True,
                        line=i,
                        file=str(path),
                        body_preview=rest[:80],
                    )
                )
            else:
                om = _DEFINE_OBJ_RE.match(rest)
                if om:
                    fa.macros.append(
                        MacroDef(
                            name=om.group(1),
                            function_like=False,
                            line=i,
                            file=str(path),
                            body_preview=rest[:80],
                        )
                    )
            continue
        if directive in ("if", "ifdef", "ifndef"):
            depth = len(stack) + 1
            stack.append((i, directive, rest, depth))
            open_regions.append(
                ConditionalRegion(
                    start_line=i,
                    end_line=0,
                    kind=directive,
                    condition=rest,
                    depth=depth,
                    file=str(path),
                )
            )
        elif directive == "elif":
            close_top(i - 1)
            depth = len(stack)  # same depth as the if it continues
            if depth == 0:
                depth = 1
            open_regions.append(
                ConditionalRegion(
                    start_line=i,
                    end_line=0,
                    kind="elif",
                    condition=rest,
                    depth=depth,
                    file=str(path),
                )
            )
        elif directive == "else":
            close_top(i - 1)
            depth = len(stack) if stack else 1
            open_regions.append(
                ConditionalRegion(
                    start_line=i,
                    end_line=0,
                    kind="else",
                    condition="",
                    depth=depth,
                    file=str(path),
                )
            )
        elif directive == "endif":
            close_top(i)
            if stack:
                stack.pop()
        # include ignored for region structure

    # unclosed regions: extend to EOF
    for reg in open_regions:
        if reg.end_line == 0:
            reg.end_line = n_lines
            reg.is_include_guard = _is_likely_include_guard(
                reg.kind, reg.condition, reg.start_line, reg.end_line, n_lines
            )
    fa.regions = open_regions
    return fa


def analyze_package(package_dir: Path) -> PackageAnalysis:
    package_dir = Path(package_dir).resolve()
    cc = parse_compile_commands(package_dir)
    package_defines = cc.get("*", {})
    pa = PackageAnalysis(package_dir=package_dir, compile_defines=dict(package_defines))
    files = sorted(
        p for p in package_dir.rglob("*") if p.suffix in (".c", ".h") and p.is_file()
    )
    for path in files:
        # skip obvious non-source trees
        if any(part in {"tests", "target", "__pycache__", ".git"} for part in path.parts):
            # still analyze tests if under package — runners can have #if too
            pass
        defs = dict(package_defines)
        defs.update(cc.get(path.name, {}))
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fa = analyze_source_text(text, path=path, compile_defines=defs)
        pa.files[str(path.resolve())] = fa
        # also key by name for looser matching
        pa.files[path.name] = fa
        for mac in fa.macros:
            if mac.function_like:
                pa.all_function_macros.add(mac.name)
            else:
                pa.all_object_macros.add(mac.name)
    return pa


def _parse_span_lines(span: str) -> Optional[Tuple[int, int]]:
    sp = str(span or "")
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


def _file_analysis_for(pa: PackageAnalysis, source_file: str) -> Optional[FileAnalysis]:
    if not source_file:
        return None
    p = Path(source_file)
    for key in (str(p.resolve()) if p.exists() else None, str(p), p.name):
        if key and key in pa.files:
            return pa.files[key]
    # fuzzy: endswith
    s = str(source_file)
    for k, fa in pa.files.items():
        if s.endswith(k) or k.endswith(p.name):
            return fa
    return None


def reasons_for_span(
    pa: PackageAnalysis,
    source_file: str,
    span: str,
    *,
    callee: Optional[str] = None,
    entity_type: Optional[str] = None,
    snippet: Optional[str] = None,
) -> List[str]:
    """Return human-readable reason tags for a fact at source_file:span."""
    reasons: List[str] = []
    fa = _file_analysis_for(pa, source_file)
    lines = _parse_span_lines(span)

    if callee and callee in pa.all_function_macros:
        reasons.append(f"function_like_macro:{callee}")

    if fa and lines:
        start, end = lines
        for reg in fa.regions:
            if reg.is_include_guard:
                continue
            # overlap of [start,end] with [reg.start_line, reg.end_line]
            if end < reg.start_line or start > reg.end_line:
                continue
            tag = f"inside_conditional:{reg.kind}"
            if reg.condition:
                tag += f"({reg.condition[:40]})"
            reasons.append(tag)
            # config-ish if condition mentions a known define
            cond_tokens = set(re.findall(r"[A-Za-z_]\w*", reg.condition))
            for name in cond_tokens:
                if name in pa.compile_defines or name in fa.compile_defines:
                    reasons.append(f"compile_define_condition:{name}")
                if name in pa.all_object_macros or name in pa.all_function_macros:
                    reasons.append(f"macro_condition:{name}")

    if snippet and entity_type in {"function", "fn", "method"}:
        if re.search(r"^\s*#\s*(if|ifdef|ifndef|elif|else|endif)\b", snippet, re.M):
            reasons.append("entity_body_has_preprocessor")

    # de-dupe preserving order
    seen: Set[str] = set()
    out: List[str] = []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def annotate_byog(
    data: Dict[str, List[Dict[str, Any]]], package_dir: Path
) -> Dict[str, Any]:
    """Stamp preprocessor provenance onto entities, relationships, observations.

    Does **not** flip ``is_deterministic`` or drop edges.
    """
    pa = analyze_package(package_dir)
    summary = {
        "package": str(package_dir),
        "function_like_macros": sorted(pa.all_function_macros),
        "compile_defines": dict(pa.compile_defines),
        "entities_flagged": 0,
        "calls_flagged": 0,
        "observations_flagged": 0,
        "trusted_calls_flagged": 0,  # is_deterministic call edges that are flagged
        "by_file": defaultdict(lambda: {"entities": 0, "calls": 0, "observations": 0}),
        "samples": [],
    }

    for e in data.get("entities") or []:
        reasons = reasons_for_span(
            pa,
            str(e.get("source_file", "")),
            str(e.get("span", "")),
            entity_type=str(e.get("type", "")),
            snippet=str(e.get("snippet") or e.get("text") or ""),
        )
        e["preprocessor_dependent"] = bool(reasons)
        e["preprocessor_reasons"] = reasons
        if reasons:
            summary["entities_flagged"] += 1
            sf = Path(str(e.get("source_file", ""))).name or "?"
            summary["by_file"][sf]["entities"] += 1
            if len(summary["samples"]) < 12:
                summary["samples"].append(
                    {
                        "kind": "entity",
                        "title": e.get("title"),
                        "file": sf,
                        "span": e.get("span"),
                        "reasons": reasons,
                    }
                )

    for r in data.get("relationships") or []:
        callee = None
        if r.get("type") == "calls":
            tgt = str(r.get("target", ""))
            callee = tgt.split(":")[-1] if tgt else None
            # also check display name in description
        reasons = reasons_for_span(
            pa,
            str(r.get("source_file", "")),
            str(r.get("span", "")),
            callee=callee,
        )
        # For contains edges with empty span, check target entity reasons later — skip
        r["preprocessor_dependent"] = bool(reasons)
        r["preprocessor_reasons"] = reasons
        if reasons and r.get("type") == "calls":
            summary["calls_flagged"] += 1
            if r.get("is_deterministic"):
                summary["trusted_calls_flagged"] += 1
            sf = Path(str(r.get("source_file", ""))).name or "?"
            summary["by_file"][sf]["calls"] += 1
            if len(summary["samples"]) < 24:
                summary["samples"].append(
                    {
                        "kind": "call",
                        "source": r.get("source"),
                        "target": r.get("target"),
                        "file": sf,
                        "span": r.get("span"),
                        "is_deterministic": r.get("is_deterministic"),
                        "reasons": reasons,
                    }
                )

    for o in data.get("call_observations") or []:
        reasons = reasons_for_span(
            pa,
            str(o.get("source_file", "")),
            str(o.get("span", "")),
            callee=str(o.get("display_target") or ""),
        )
        o["preprocessor_dependent"] = bool(reasons)
        o["preprocessor_reasons"] = reasons
        if reasons:
            summary["observations_flagged"] += 1
            sf = Path(str(o.get("source_file", ""))).name or "?"
            summary["by_file"][sf]["observations"] += 1

    summary["by_file"] = dict(summary["by_file"])
    summary["n_function_like_macros"] = len(pa.all_function_macros)
    summary["n_conditional_regions"] = sum(
        len([r for r in fa.regions if not r.is_include_guard])
        for fa in {id(f): f for f in pa.files.values()}.values()
    )
    # recount regions uniquely by file path
    seen_fa = set()
    n_reg = 0
    for fa in pa.files.values():
        if id(fa) in seen_fa:
            continue
        seen_fa.add(id(fa))
        n_reg += sum(1 for r in fa.regions if not r.is_include_guard)
    summary["n_conditional_regions"] = n_reg
    return summary


def format_report(summary: Dict[str, Any], *, totals: Optional[Dict[str, int]] = None) -> str:
    lines = [
        f"C preprocessor blind-spot report: {summary.get('package')}",
        f"  function-like macros found : {summary.get('n_function_like_macros')} "
        f"({', '.join(summary.get('function_like_macros') or []) or '—'})",
        f"  compile -D defines         : {summary.get('compile_defines') or '{}'}",
        f"  non-guard #if regions      : {summary.get('n_conditional_regions')}",
        f"  entities flagged           : {summary.get('entities_flagged')}"
        + (f" / {totals['entities']}" if totals and "entities" in totals else ""),
        f"  call edges flagged         : {summary.get('calls_flagged')}"
        + (f" / {totals['calls']}" if totals and "calls" in totals else ""),
        f"  of which trusted (det)     : {summary.get('trusted_calls_flagged')}",
        f"  observations flagged       : {summary.get('observations_flagged')}"
        + (f" / {totals['observations']}" if totals and "observations" in totals else ""),
        "  by file:",
    ]
    for fn, counts in sorted((summary.get("by_file") or {}).items()):
        lines.append(
            f"    {fn}: entities={counts.get('entities', 0)} "
            f"calls={counts.get('calls', 0)} obs={counts.get('observations', 0)}"
        )
    if summary.get("samples"):
        lines.append("  samples:")
        for s in summary["samples"][:15]:
            if s["kind"] == "entity":
                lines.append(
                    f"    entity {s.get('title')} @{s.get('file')}:{s.get('span')} "
                    f"reasons={s.get('reasons')}"
                )
            else:
                lines.append(
                    f"    call {s.get('source')} -> {s.get('target')} "
                    f"@{s.get('file')}:{s.get('span')} det={s.get('is_deterministic')} "
                    f"reasons={s.get('reasons')}"
                )
    lines.append(
        "  note: flags are provenance only; is_deterministic and audit pass rates "
        "are not changed by this diagnostic."
    )
    return "\n".join(lines)


def _load_published_byog(graph_dir: Path) -> Dict[str, List[Dict[str, Any]]]:
    """Read a published BYOG snapshot into the shape `annotate_byog` expects."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from byog_graph import ByogGraph  # type: ignore

    g = ByogGraph(graph_dir)
    return {
        "entities": g.ents.to_dict("records"),
        "relationships": g.rels.to_dict("records"),
        "call_observations": g.call_observations.to_dict("records"),
    }


def main(argv: Optional[Sequence[str]] = None) -> None:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--package", "-p", required=True, type=Path)
    ap.add_argument(
        "--graph",
        type=Path,
        default=None,
        help="analyse a published BYOG snapshot instead of a fresh extraction; "
        "use this to make claims about the graph the ports were actually built on",
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    from extract_c import build_c_byog  # type: ignore

    pkg = args.package.resolve()
    # Prefer --graph when making claims about the ports: a published snapshot
    # and a fresh extraction can diverge when the package (e.g. golden runner)
    # grows. Name the source in the output so reports are not mis-attributed.
    if args.graph is not None:
        data = _load_published_byog(args.graph.resolve())
        source_desc = f"published graph {args.graph}"
    else:
        data = build_c_byog(pkg)
        source_desc = "fresh extraction (not the published snapshot)"
    summary = annotate_byog(data, pkg)
    totals = {
        "entities": len(data["entities"]),
        "calls": sum(1 for r in data["relationships"] if r["type"] == "calls"),
        "observations": len(data.get("call_observations") or []),
    }
    summary["totals"] = totals
    summary["analysed"] = source_desc
    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        print(f"analysed: {source_desc}")
        print(format_report(summary, totals=totals))


if __name__ == "__main__":
    main()
