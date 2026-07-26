#!/usr/bin/env python
"""Python dynamic-dispatch diagnostics for syntax-only extraction.

The Python frontend (tree-sitter + stdlib ``ast``) does not follow runtime
dispatch. This module flags extracted facts that *depend* on constructs we
cannot resolve statically: registry/dict callable tables, ``getattr`` with a
non-literal name, calls through subscripts / ``dict.get``, polymorphic method
calls on receivers derived from those lookups, ``__getattr__`` hooks, and
``importlib`` dynamic imports.

Flags are **provenance labels**, not demotions: they do not change
``is_deterministic`` or remove/add edges, so ``audit_call_edges`` pass rates
stay unchanged. Consumers (context packs, humans) can tell the difference
between "this function calls nothing else" and "callees are chosen at runtime".

No type-checker dependency — source text + ``ast`` only.
"""
from __future__ import annotations

import ast
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple


# ---------------------------------------------------------------------------
# Analysis model
# ---------------------------------------------------------------------------


@dataclass
class DynamicSite:
    """One source location the static extractor cannot follow."""

    file: str
    line: int
    end_line: int
    col: int
    kind: str  # stable reason tag (prefix before ':')
    detail: str
    enclosing: str  # best-effort Class.method or function name

    @property
    def reason(self) -> str:
        if self.detail:
            return f"{self.kind}:{self.detail}"
        return self.kind


@dataclass
class FileAnalysis:
    path: Path
    sites: List[DynamicSite] = field(default_factory=list)
    registries: Set[str] = field(default_factory=set)


@dataclass
class PackageAnalysis:
    package_dir: Path
    files: Dict[str, FileAnalysis] = field(default_factory=dict)
    n_sites: int = 0
    kinds: Dict[str, int] = field(default_factory=dict)


def _is_callable_registry_dict(d: ast.Dict) -> bool:
    """True when most values look like callables/classes (not keyword tables).

    Keyword tables map strings to ``tokens.Keyword``-style Attributes or ints —
    those are data, not dispatch. Callable registries map to bare Names
    (classes/functions), Lambdas, or factory Calls.
    """
    if not d.values:
        return False
    score = 0
    for v in d.values:
        if isinstance(v, (ast.Name, ast.Lambda)):
            score += 1
        elif isinstance(v, ast.Call):
            # e.g. partial(...) — still a callable factory
            score += 1
        # Attribute / Constant values are data tables, not callable registries.
    return score >= max(1, (len(d.values) + 1) // 2)


def _registry_dict_from_value(node: ast.AST) -> Optional[ast.Dict]:
    """Return an inner dict if node is a callable-registry literal or MappingProxyType/dict wrap."""
    if isinstance(node, ast.Dict) and _is_callable_registry_dict(node):
        return node
    if isinstance(node, ast.Call):
        fname = None
        if isinstance(node.func, ast.Name):
            fname = node.func.id
        elif isinstance(node.func, ast.Attribute):
            fname = node.func.attr
        if fname in {"MappingProxyType", "dict", "OrderedDict"} and node.args:
            inner = node.args[0]
            if isinstance(inner, ast.Dict) and _is_callable_registry_dict(inner):
                return inner
    return None


def _target_names(targets: List[ast.AST]) -> List[str]:
    out: List[str] = []
    for t in targets:
        if isinstance(t, ast.Name):
            out.append(t.id)
        elif isinstance(t, ast.Attribute):
            out.append(t.attr)
            # also record dotted-ish form for matching self.operations
            out.append(t.attr)
    return out


def _attr_chain(node: ast.AST) -> Optional[str]:
    """``self.operations`` → ``self.operations``; ``operations`` → ``operations``."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _attr_chain(node.value)
        if base:
            return f"{base}.{node.attr}"
        return node.attr
    return None


def _parse_span_lines(span: str) -> Optional[Tuple[int, int]]:
    """Parse ``start:col-end:col`` or ``start:col`` or ``start`` into (start, end) lines."""
    if not span or span == "nan":
        return None
    s = str(span).strip()
    if not s or s == "None":
        return None
    try:
        if "-" in s:
            left, right = s.split("-", 1)
            start = int(left.split(":")[0])
            end = int(right.split(":")[0])
            return start, end
        start = int(s.split(":")[0])
        return start, start
    except (TypeError, ValueError):
        return None


def analyze_source_text(text: str, path: Path) -> FileAnalysis:
    """Detect dynamic-dispatch sites in one source file."""
    fa = FileAnalysis(path=path)
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return fa

    # --- collect registry attribute names (module + class body) ---
    registries: Set[str] = set()

    def note_registry(name: str, class_name: Optional[str] = None) -> None:
        registries.add(name)
        if class_name:
            registries.add(f"{class_name}.{name}")

    for node in tree.body:
        if isinstance(node, ast.Assign) and _registry_dict_from_value(node.value) is not None:
            for n in _target_names(node.targets):
                note_registry(n)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            if _registry_dict_from_value(node.value) is not None and isinstance(node.target, ast.Name):
                note_registry(node.target.id)
        elif isinstance(node, ast.ClassDef):
            for stmt in node.body:
                if isinstance(stmt, ast.Assign) and _registry_dict_from_value(stmt.value) is not None:
                    for n in _target_names(stmt.targets):
                        note_registry(n, node.name)
                elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                    if _registry_dict_from_value(stmt.value) is not None and isinstance(
                        stmt.target, ast.Name
                    ):
                        note_registry(stmt.target.id, node.name)

    fa.registries = set(registries)

    def is_registry_expr(expr: ast.AST) -> Optional[str]:
        """If expr loads a known registry, return a short name for the reason tag."""
        chain = _attr_chain(expr)
        if not chain:
            return None
        # self.operations / cls.operations / JsonPatch.operations / operations
        bare = chain.split(".")[-1]
        if bare in registries or chain in registries:
            return bare
        # class.attr form recorded as Class.attr
        parts = chain.split(".")
        if len(parts) >= 2:
            cand = f"{parts[-2]}.{parts[-1]}"
            if cand in registries:
                return parts[-1]
        return None

    sites: List[DynamicSite] = []

    def add_site(
        node: ast.AST,
        kind: str,
        detail: str,
        enclosing: str,
        end_node: Optional[ast.AST] = None,
    ) -> None:
        line = int(getattr(node, "lineno", 0) or 0)
        if line <= 0:
            return
        end_src = end_node or node
        end_line = int(getattr(end_src, "end_lineno", None) or getattr(end_src, "lineno", line) or line)
        col = int(getattr(node, "col_offset", 0) or 0)
        sites.append(
            DynamicSite(
                file=str(path),
                line=line,
                end_line=end_line,
                col=col,
                kind=kind,
                detail=detail,
                enclosing=enclosing,
            )
        )

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.class_stack: List[str] = []
            self.func_stack: List[str] = []
            # per-function: local name -> reason it is dynamically bound (callable path)
            self.dyn_stack: List[Dict[str, str]] = []
            # subscript/get results that become dynamic only if later *called*
            self.maybe_stack: List[Dict[str, Tuple[str, ast.AST]]] = []

        def _enclosing(self) -> str:
            if self.class_stack and self.func_stack:
                return f"{self.class_stack[-1]}.{self.func_stack[-1]}"
            if self.func_stack:
                return self.func_stack[-1]
            if self.class_stack:
                return self.class_stack[-1]
            return ""

        def _dyn(self) -> Dict[str, str]:
            if not self.dyn_stack:
                self.dyn_stack.append({})
            return self.dyn_stack[-1]

        def _maybe(self) -> Dict[str, Tuple[str, ast.AST]]:
            if not self.maybe_stack:
                self.maybe_stack.append({})
            return self.maybe_stack[-1]

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.class_stack.append(node.name)
            self.generic_visit(node)
            self.class_stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_func(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit_func(node)

        def _visit_func(self, node: ast.AST) -> None:
            assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            self.func_stack.append(node.name)
            self.dyn_stack.append({})
            self.maybe_stack.append({})
            enc = self._enclosing()
            if node.name in {"__getattr__", "__getattribute__"}:
                add_site(node, "def_getattr", node.name, enc, end_node=node)
            self.generic_visit(node)
            self.maybe_stack.pop()
            self.dyn_stack.pop()
            self.func_stack.pop()

        def visit_Assign(self, node: ast.Assign) -> None:
            self._handle_bind(node.targets, node.value, node)
            self.generic_visit(node)

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
            if node.value is not None:
                self._handle_bind([node.target], node.value, node)
            self.generic_visit(node)

        def _bind_dyn(self, targets: List[ast.AST], reason: str) -> None:
            dyn = self._dyn()
            for t in targets:
                if isinstance(t, ast.Name):
                    dyn[t.id] = reason

        def _bind_maybe(self, targets: List[ast.AST], reason: str, node: ast.AST) -> None:
            maybe = self._maybe()
            for t in targets:
                if isinstance(t, ast.Name):
                    maybe[t.id] = (reason, node)

        def _handle_bind(self, targets: List[ast.AST], value: ast.AST, node: ast.AST) -> None:
            enc = self._enclosing()

            # getattr(obj, non_literal, ...) — name may be used as a callable later
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "getattr":
                if len(value.args) >= 2 and not (
                    isinstance(value.args[1], ast.Constant) and isinstance(value.args[1].value, str)
                ):
                    add_site(value, "getattr_dynamic", "", enc)
                    self._bind_dyn(targets, "getattr_dynamic")
                    return

            # registry subscript: self.operations[op]
            if isinstance(value, ast.Subscript):
                reg = is_registry_expr(value.value)
                if reg:
                    add_site(value, "registry_lookup", reg, enc)
                    self._bind_dyn(targets, f"registry_lookup:{reg}")
                    return
                # Non-registry subscript (data dicts, keyword tables): only becomes a
                # blind spot if the loaded value is later *called* as a function.
                self._bind_maybe(
                    targets, f"table_lookup:{_attr_chain(value.value) or ''}", value
                )
                return

            # registry .get(key)
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute):
                if value.func.attr in {"get", "pop"}:
                    reg = is_registry_expr(value.func.value)
                    if reg:
                        add_site(value, "registry_get", reg, enc)
                        self._bind_dyn(targets, f"registry_get:{reg}")
                        return
                    self._bind_maybe(
                        targets,
                        f"dict_get:{_attr_chain(value.func.value) or ''}",
                        value,
                    )
                    return

            # functools.partial(...) — callee partially applied; static graph sees the
            # binding, not what partial closes over. Label the binding site only.
            if isinstance(value, ast.Call):
                f = value.func
                is_partial = (isinstance(f, ast.Name) and f.id == "partial") or (
                    isinstance(f, ast.Attribute) and f.attr == "partial"
                )
                if is_partial:
                    add_site(value, "functools_partial", "", enc)
                    self._bind_dyn(targets, "functools_partial")
                    return

            # importlib.import_module(...)
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute):
                if value.func.attr in {"import_module", "__import__"}:
                    add_site(value, "importlib_dynamic", value.func.attr, enc)
                    self._bind_dyn(targets, f"importlib_dynamic:{value.func.attr}")

        def visit_For(self, node: ast.For) -> None:
            enc = self._enclosing()
            dyn = self._dyn()
            # for operation in self._ops:  — property that maps registry factories
            it = node.iter
            dynamic_iter = False
            detail = ""
            if isinstance(it, ast.Attribute) and it.attr in {"_ops", "operations"}:
                dynamic_iter = True
                detail = it.attr
            elif isinstance(it, ast.Call) and isinstance(it.func, ast.Name) and it.func.id == "map":
                # map(self._get_operation, ...)
                if it.args:
                    a0 = it.args[0]
                    chain = _attr_chain(a0) or ""
                    if "_get_operation" in chain or any(
                        (r in chain) for r in registries
                    ):
                        dynamic_iter = True
                        detail = chain or "map"
            if dynamic_iter and isinstance(node.target, ast.Name):
                dyn[node.target.id] = f"registry_derived_iter:{detail}"
                add_site(node, "registry_derived_iter", detail, enc)
            self.generic_visit(node)

        def _promote_maybe_call(self, name: str, call_node: ast.AST, enc: str) -> Optional[str]:
            """If name was bound from a table get and is now called, promote to a site."""
            maybe = self._maybe()
            if name not in maybe:
                return None
            reason, bind_node = maybe[name]
            dyn = self._dyn()
            dyn[name] = reason
            # Site at the original load and at the call.
            kind = "call_through_subscript" if reason.startswith("table_lookup") else "call_through_dict_get"
            detail = reason.split(":", 1)[1] if ":" in reason else reason
            add_site(bind_node, kind, detail, enc)
            add_site(call_node, "call_through_dynamic_name", f"{name}<{reason}>", enc)
            return reason

        def visit_Call(self, node: ast.Call) -> None:
            enc = self._enclosing()
            dyn = self._dyn()
            func = node.func

            # ops[key](...)
            if isinstance(func, ast.Subscript):
                reg = is_registry_expr(func.value)
                if reg:
                    add_site(node, "call_through_registry", reg, enc)
                else:
                    add_site(
                        node,
                        "call_through_subscript",
                        _attr_chain(func.value) or "",
                        enc,
                    )

            # ops.get(key)(...)
            if isinstance(func, ast.Call) and isinstance(func.func, ast.Attribute):
                if func.func.attr in {"get", "pop"}:
                    reg = is_registry_expr(func.func.value)
                    if reg:
                        add_site(node, "call_through_registry_get", reg, enc)
                    else:
                        add_site(
                            node,
                            "call_through_dict_get",
                            _attr_chain(func.func.value) or "",
                            enc,
                        )

            # getattr(...)(...) — immediate call of dynamic attribute
            if isinstance(func, ast.Call) and isinstance(func.func, ast.Name) and func.func.id == "getattr":
                add_site(node, "call_through_getattr", "", enc)

            # getattr(obj, non_literal) used as a value (return/arg), not only assignment
            if isinstance(func, ast.Name) and func.id == "getattr":
                if len(node.args) >= 2 and not (
                    isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)
                ):
                    add_site(node, "getattr_dynamic", "", enc)

            # name(...) where name bound dynamically or promoted from table lookup
            if isinstance(func, ast.Name):
                if func.id in dyn:
                    add_site(
                        node,
                        "call_through_dynamic_name",
                        f"{func.id}<{dyn[func.id]}>",
                        enc,
                    )
                elif func.id in self._maybe():
                    self._promote_maybe_call(func.id, node, enc)

            # obj.method(...) where obj is registry/getattr-derived (polymorphic)
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                base = func.value.id
                if base in dyn and (
                    dyn[base].startswith("registry")
                    or dyn[base].startswith("getattr")
                    or dyn[base].startswith("importlib")
                ):
                    add_site(
                        node,
                        "polymorphic_call",
                        f"{base}.{func.attr}<{dyn[base]}>",
                        enc,
                    )

            # setattr/hasattr/delattr with dynamic name (getattr handled above on bind)
            if isinstance(func, ast.Name) and func.id in {"setattr", "hasattr", "delattr"}:
                if len(node.args) >= 2 and not (
                    isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)
                ):
                    add_site(node, f"{func.id}_dynamic", "", enc)

            # importlib.import_module(...) as a bare call (not only assignment)
            if isinstance(func, ast.Attribute) and func.attr in {"import_module", "__import__"}:
                add_site(node, "importlib_dynamic", func.attr, enc)
            if isinstance(func, ast.Name) and func.id == "__import__":
                add_site(node, "importlib_dynamic", "__import__", enc)

            self.generic_visit(node)

    Visitor().visit(tree)
    fa.sites = sites
    return fa


def analyze_package(package_dir: Path) -> PackageAnalysis:
    package_dir = package_dir.resolve()
    pa = PackageAnalysis(package_dir=package_dir)
    kinds: Dict[str, int] = defaultdict(int)
    n_sites = 0
    for path in sorted(package_dir.rglob("*.py")):
        if any(part in {"__pycache__", ".venv", "venv", "target"} for part in path.parts):
            continue
        # Match the Python indexer: skip tests and __init__.py so site↔entity
        # alignment matches published graphs. (Tests can still call analyze_source_text.)
        if "tests" in path.parts or path.name == "__init__.py":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        fa = analyze_source_text(text, path)
        key = str(path.resolve())
        pa.files[key] = fa
        # also index by name for span matching when absolute paths differ
        pa.files[path.name] = fa
        n_sites += len(fa.sites)
        for s in fa.sites:
            kinds[s.kind] += 1
    pa.n_sites = n_sites
    pa.kinds = dict(kinds)
    return pa


def _file_analysis_for(pa: PackageAnalysis, source_file: str) -> Optional[FileAnalysis]:
    if not source_file:
        return None
    p = Path(str(source_file))
    # try resolved, as-is, name
    for key in (str(p), str(p.resolve()) if p.exists() else "", p.name):
        if key and key in pa.files:
            return pa.files[key]
    # suffix match on stored paths
    name = p.name
    for k, fa in pa.files.items():
        if k.endswith(name) or Path(k).name == name:
            return fa
    return None


def reasons_for_span(
    pa: PackageAnalysis,
    source_file: str,
    span: str,
    *,
    entity_title: Optional[str] = None,
    entity_type: Optional[str] = None,
) -> List[str]:
    """Return reason tags for a fact at source_file:span."""
    fa = _file_analysis_for(pa, source_file)
    if not fa:
        return []
    lines = _parse_span_lines(span)
    reasons: List[str] = []
    seen: Set[str] = set()

    def add(r: str) -> None:
        if r and r not in seen:
            seen.add(r)
            reasons.append(r)

    # Match sites by line overlap with the fact's span.
    if lines:
        start, end = lines
        for site in fa.sites:
            if site.end_line < start or site.line > end:
                continue
            add(site.reason)

    # Entity title match: Class.method ↔ enclosing
    if entity_title and entity_type in {"method", "fn", "function", "class"}:
        bare = str(entity_title).split(":")[-1]
        for site in fa.sites:
            if not site.enclosing:
                continue
            if site.enclosing == bare or site.enclosing.endswith("." + bare) or bare.endswith(
                "." + site.enclosing.split(".")[-1]
            ):
                # only if site is roughly inside entity, or entity has no usable span
                if not lines or (site.line >= lines[0] - 1 and site.line <= lines[1] + 1):
                    add(site.reason)
            # Class entity: any site enclosed by Class.*
            if entity_type == "class" and site.enclosing.startswith(bare + "."):
                add(site.reason)

    return reasons


def annotate_byog(
    data: Dict[str, List[Dict[str, Any]]], package_dir: Path
) -> Dict[str, Any]:
    """Stamp dynamic-dispatch provenance onto entities, relationships, observations.

    Does **not** flip ``is_deterministic`` or drop/add edges.
    """
    pa = analyze_package(package_dir)
    summary: Dict[str, Any] = {
        "package": str(package_dir),
        "n_sites": pa.n_sites,
        "kinds": dict(pa.kinds),
        "entities_flagged": 0,
        "calls_flagged": 0,
        "observations_flagged": 0,
        "trusted_calls_flagged": 0,
        "by_file": defaultdict(lambda: {"entities": 0, "calls": 0, "observations": 0}),
        "samples": [],
        "registries": sorted(
            {r for fa in pa.files.values() for r in fa.registries if "." not in r}
        ),
    }

    for e in data.get("entities") or []:
        reasons = reasons_for_span(
            pa,
            str(e.get("source_file", "")),
            str(e.get("span", "")),
            entity_title=str(e.get("title", "")),
            entity_type=str(e.get("type", "")),
        )
        e["dynamic_dependent"] = bool(reasons)
        e["dynamic_reasons"] = reasons
        if reasons:
            summary["entities_flagged"] += 1
            sf = Path(str(e.get("source_file", ""))).name or "?"
            summary["by_file"][sf]["entities"] += 1
            if len(summary["samples"]) < 16:
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
        reasons = reasons_for_span(
            pa,
            str(r.get("source_file", "")),
            str(r.get("span", "")),
        )
        r["dynamic_dependent"] = bool(reasons)
        r["dynamic_reasons"] = reasons
        if reasons and r.get("type") == "calls":
            summary["calls_flagged"] += 1
            if r.get("is_deterministic"):
                summary["trusted_calls_flagged"] += 1
            sf = Path(str(r.get("source_file", ""))).name or "?"
            summary["by_file"][sf]["calls"] += 1
            if len(summary["samples"]) < 28:
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
        )
        # Observations already marked unresolved/low-conf at a dynamic site keep the label.
        o["dynamic_dependent"] = bool(reasons)
        o["dynamic_reasons"] = reasons
        if reasons:
            summary["observations_flagged"] += 1
            sf = Path(str(o.get("source_file", ""))).name or "?"
            summary["by_file"][sf]["observations"] += 1
            if len(summary["samples"]) < 36:
                summary["samples"].append(
                    {
                        "kind": "observation",
                        "source": o.get("source"),
                        "display_target": o.get("display_target"),
                        "file": sf,
                        "span": o.get("span"),
                        "reasons": reasons,
                    }
                )

    summary["by_file"] = dict(summary["by_file"])
    return summary


def format_report(summary: Dict[str, Any], *, totals: Optional[Dict[str, int]] = None) -> str:
    lines = [
        f"Python dynamic-dispatch blind-spot report: {summary.get('package')}",
        f"  dynamic sites found       : {summary.get('n_sites')}",
        f"  registries detected       : {', '.join(summary.get('registries') or []) or '—'}",
        f"  kinds                     : {summary.get('kinds') or {}}",
        f"  entities flagged          : {summary.get('entities_flagged')}"
        + (f" / {totals['entities']}" if totals and "entities" in totals else ""),
        f"  call edges flagged        : {summary.get('calls_flagged')}"
        + (f" / {totals['calls']}" if totals and "calls" in totals else ""),
        f"  of which trusted (det)    : {summary.get('trusted_calls_flagged')}",
        f"  observations flagged      : {summary.get('observations_flagged')}"
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
        for s in summary["samples"][:18]:
            if s["kind"] == "entity":
                lines.append(
                    f"    entity {s.get('title')} @{s.get('file')}:{s.get('span')} "
                    f"reasons={s.get('reasons')}"
                )
            elif s["kind"] == "call":
                lines.append(
                    f"    call {s.get('source')} -> {s.get('target')} "
                    f"@{s.get('file')}:{s.get('span')} det={s.get('is_deterministic')} "
                    f"reasons={s.get('reasons')}"
                )
            else:
                lines.append(
                    f"    obs {s.get('source')} -> {s.get('display_target')} "
                    f"@{s.get('file')}:{s.get('span')} reasons={s.get('reasons')}"
                )
    lines.append(
        "  note: flags are provenance only; is_deterministic and audit pass rates "
        "are not changed by this diagnostic."
    )
    return "\n".join(lines)


def _load_published_byog(graph_dir: Path) -> Dict[str, List[Dict[str, Any]]]:
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
        help="analyse a published BYOG snapshot instead of a fresh extraction",
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from mini_game_to_byog import build_byog_for_package  # type: ignore

    pkg = args.package.resolve()
    if args.graph is not None:
        data = _load_published_byog(args.graph.resolve())
        source_desc = f"published graph {args.graph}"
    else:
        data = build_byog_for_package(package_dir=pkg)
        source_desc = "fresh extraction (not the published snapshot)"
    summary = annotate_byog(data, pkg)
    totals = {
        "entities": len(data["entities"]),
        "calls": sum(1 for r in data["relationships"] if r.get("type") == "calls"),
        "observations": len(data.get("call_observations") or []),
    }
    summary["totals"] = totals
    summary["analysed"] = source_desc
    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    else:
        print(f"analysed: {source_desc}")
        print(format_report(summary, totals=totals))


if __name__ == "__main__":
    main()
