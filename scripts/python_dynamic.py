#!/usr/bin/env python
"""Python dynamic-dispatch diagnostics for syntax-only extraction.

The Python frontend (tree-sitter + stdlib ``ast``) does not follow runtime
dispatch. This module flags extracted facts that *depend* on constructs we
cannot resolve statically: registry/dict callable tables, ``getattr`` with a
non-literal name, calls through subscripts / ``dict.get``, polymorphic method
calls on receivers derived from those lookups, ``__getattr__`` hooks, and
``importlib`` dynamic imports.

Flags are **provenance labels**, not demotions of existing edges: they do not
flip ``is_deterministic`` on prior relationships. **Registry promotion** (see
``_registry_dispatch_edges``) may *add* non-deterministic ``calls`` edges for
statically named table members at labelled dispatch sites — never for
lambda/Call-valued or runtime-only discoveries. Consumers can tell unique
static callees from multi-target registry dispatch via confidence and
``is_deterministic=False``.

No type-checker dependency — source text + ``ast`` only.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
import textwrap
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
    # bare or Class.attr registry name -> ordered (key, value_name) entries
    # from the static dict literal (e.g. operations['add'] -> AddOperation).
    registry_tables: Dict[str, List[Tuple[str, str]]] = field(default_factory=dict)


@dataclass
class PackageAnalysis:
    package_dir: Path
    files: Dict[str, FileAnalysis] = field(default_factory=dict)
    n_sites: int = 0
    kinds: Dict[str, int] = field(default_factory=dict)
    # merged Class.attr / bare -> entries (last file wins; rare conflict)
    registry_tables: Dict[str, List[Tuple[str, str]]] = field(default_factory=dict)


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


def _ast_key_info(k: Optional[ast.AST]) -> Tuple[Optional[str], str]:
    """Return (concrete_key_or_None, key_shape).

    Only constant keys are concrete enough to compare to a runtime mapping.
    Name/attribute keys (e.g. ``KIND_SHORTEQ``) are unresolvable without eval.
    """
    if k is None:  # ``{**spread}`` / dict unpack — not a key
        return None, "unpack"
    if isinstance(k, ast.Constant):
        if isinstance(k.value, str):
            return k.value, "constant_str"
        if k.value is None:
            return None, "constant_none"  # concrete None key; encoded separately
        return str(k.value), "constant_other"
    if isinstance(k, ast.Name):
        return None, "name"
    if isinstance(k, ast.Attribute):
        return None, "attribute"
    return None, type(k).__name__


def _ast_value_shape(v: ast.AST) -> str:
    if isinstance(v, ast.Name):
        return "Name"
    if isinstance(v, ast.Attribute):
        return "Attribute"
    if isinstance(v, ast.Lambda):
        return "Lambda"
    if isinstance(v, ast.Call):
        return "Call"
    if isinstance(v, ast.Constant):
        return "Constant"
    return type(v).__name__


def _registry_table_entries(d: ast.Dict) -> List[Tuple[str, str]]:
    """Extract (key, callee_name) pairs from a callable-registry dict literal.

    Only **constant keys** with **Name/Attribute values** are emitted — the
    extractor cannot name lambdas or call results, and cannot resolve non-literal
    keys. Those gaps are what the runtime oracle measures.
    """
    entries: List[Tuple[str, str]] = []
    for k, v in zip(d.keys, d.values):
        key_s, key_shape = _ast_key_info(k)
        if key_shape == "constant_none":
            key_s = "__None__"  # stable encoding for the None key
        elif key_s is None:
            continue  # non-literal key: unresolvable statically
        if isinstance(v, ast.Name):
            entries.append((key_s, v.id))
        elif isinstance(v, ast.Attribute):
            entries.append((key_s, v.attr))
    return entries


def _registry_dict_inventory(d: ast.Dict) -> Dict[str, Any]:
    """Full key/value shape inventory for oracle reporting (includes unextracted)."""
    key_shapes: Dict[str, int] = defaultdict(int)
    value_shapes: Dict[str, int] = defaultdict(int)
    slots: List[Dict[str, Any]] = []
    for k, v in zip(d.keys, d.values):
        key_s, key_shape = _ast_key_info(k)
        if key_shape == "constant_none":
            key_s = "__None__"
        vshape = _ast_value_shape(v)
        key_shapes[key_shape] += 1
        value_shapes[vshape] += 1
        val_name: Optional[str] = None
        if isinstance(v, ast.Name):
            val_name = v.id
        elif isinstance(v, ast.Attribute):
            val_name = v.attr
        slots.append(
            {
                "key": key_s,
                "key_shape": key_shape,
                "value_shape": vshape,
                "value_name": val_name,  # only when Name/Attribute
            }
        )
    return {
        "key_shapes": dict(key_shapes),
        "value_shapes": dict(value_shapes),
        "slots": slots,
        "n_slots": len(slots),
        "n_extracted": sum(1 for s in slots if s["value_name"] is not None and s["key"] is not None),
        "source": "dict_literal",
    }


def _is_classmethod_decorator(decorators: List[ast.AST]) -> bool:
    for d in decorators:
        if isinstance(d, ast.Name) and d.id == "classmethod":
            return True
        if isinstance(d, ast.Attribute) and d.attr == "classmethod":
            return True
    return False


def _class_string_constants(class_node: ast.ClassDef) -> Dict[str, str]:
    """``SYNTAX = 'simple'`` style class-body string constants."""
    out: Dict[str, str] = {}
    for stmt in class_node.body:
        if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Constant):
            if isinstance(stmt.value.value, str):
                for t in stmt.targets:
                    if isinstance(t, ast.Name):
                        out[t.id] = stmt.value.value
        elif (
            isinstance(stmt, ast.AnnAssign)
            and isinstance(stmt.target, ast.Name)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        ):
            out[stmt.target.id] = stmt.value.value
    return out


def _register_method_spec(func: ast.AST, *, owner: str) -> Optional[Dict[str, Any]]:
    """If ``func`` is a classmethod that does ``cls.REG[key] = subclass``, describe it.

    Supports the common decorator-registration idiom::

        @classmethod
        def register_syntax(cls, subclass):
            syntax = subclass.SYNTAX
            cls.SYNTAXES[syntax] = subclass
            return subclass

    Returns ``{reg_attr, key_from_attr?, key_literal?}`` or None.
    """
    if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return None
    if not _is_classmethod_decorator(func.decorator_list):
        return None
    args = [a.arg for a in func.args.args]
    if len(args) < 2:
        return None
    cls_p, sub_p = args[0], args[1]

    # Locals bound from subclass attributes: syntax = subclass.SYNTAX
    local_from_sub: Dict[str, str] = {}
    for node in ast.walk(func):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            t0 = node.targets[0]
            if (
                isinstance(t0, ast.Name)
                and isinstance(node.value, ast.Attribute)
                and isinstance(node.value.value, ast.Name)
                and node.value.value.id == sub_p
            ):
                local_from_sub[t0.id] = node.value.attr
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == sub_p
        ):
            local_from_sub[node.target.id] = node.value.attr

    for node in ast.walk(func):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Subscript):
            continue
        base = target.value
        if not (
            isinstance(base, ast.Attribute)
            and isinstance(base.value, ast.Name)
            and base.value.id == cls_p
        ):
            continue
        # Value written must be the subclass parameter.
        if not (isinstance(node.value, ast.Name) and node.value.id == sub_p):
            continue
        reg_attr = base.attr
        key_node = target.slice
        # ast.Index wrapper only on very old Python; 3.9+ uses the inner node.
        if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
            return {
                "owner": owner,
                "method": func.name,
                "reg_attr": reg_attr,
                "key_literal": key_node.value,
                "key_from_attr": None,
            }
        if (
            isinstance(key_node, ast.Attribute)
            and isinstance(key_node.value, ast.Name)
            and key_node.value.id == sub_p
        ):
            return {
                "owner": owner,
                "method": func.name,
                "reg_attr": reg_attr,
                "key_literal": None,
                "key_from_attr": key_node.attr,
            }
        if isinstance(key_node, ast.Name) and key_node.id in local_from_sub:
            return {
                "owner": owner,
                "method": func.name,
                "reg_attr": reg_attr,
                "key_literal": None,
                "key_from_attr": local_from_sub[key_node.id],
            }
    return None


def collect_decorator_registry_entries(
    tree: ast.AST,
) -> List[Dict[str, Any]]:
    """Collect ``(owner, reg_attr, key, value_class)`` from class decorator registration.

    Pattern::

        class Base:
            REG = {}
            @classmethod
            def register(cls, subclass):
                cls.REG[subclass.KEY] = subclass
                return subclass

        @Base.register
        class Impl(Base):
            KEY = 'impl'
    """
    # owner.method -> register spec
    methods: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for node in getattr(tree, "body", []) or []:
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            spec = _register_method_spec(stmt, owner=node.name)
            if spec is not None:
                methods[(node.name, stmt.name)] = spec  # type: ignore[arg-type]

    entries: List[Dict[str, Any]] = []
    for node in getattr(tree, "body", []) or []:
        if not isinstance(node, ast.ClassDef):
            continue
        class_attrs = _class_string_constants(node)
        for dec in node.decorator_list:
            # @Owner.register_method  (not @Owner.register_method(args))
            if not (
                isinstance(dec, ast.Attribute) and isinstance(dec.value, ast.Name)
            ):
                continue
            owner, method = dec.value.id, dec.attr
            spec = methods.get((owner, method))
            if spec is None:
                continue
            key: Optional[str] = spec.get("key_literal")
            if key is None and spec.get("key_from_attr"):
                key = class_attrs.get(spec["key_from_attr"])
            if not key:
                # Visible decorator but no concrete key — skip rather than guess.
                continue
            entries.append(
                {
                    "owner": owner,
                    "reg_attr": spec["reg_attr"],
                    "key": key,
                    "value": node.name,
                    "method": method,
                    "line": int(getattr(node, "lineno", 0) or 0),
                }
            )
    return entries


def _decorator_inventory(entries: List[Tuple[str, str]]) -> Dict[str, Any]:
    """Inventory shape for registries filled only by decorator registration."""
    slots = [
        {
            "key": k,
            "key_shape": "constant_str",
            "value_shape": "Decorator",
            "value_name": v,
        }
        for k, v in entries
    ]
    return {
        "key_shapes": {"constant_str": len(entries)},
        "value_shapes": {"Decorator": len(entries)},
        "slots": slots,
        "n_slots": len(entries),
        "n_extracted": len(entries),
        "source": "decorator_registration",
    }


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

    # --- collect registry attribute names + static table members ---
    registries: Set[str] = set()
    registry_tables: Dict[str, List[Tuple[str, str]]] = {}

    def note_registry(
        name: str,
        dict_node: ast.Dict,
        class_name: Optional[str] = None,
    ) -> None:
        registries.add(name)
        entries = _registry_table_entries(dict_node)
        registry_tables[name] = entries
        if class_name:
            registries.add(f"{class_name}.{name}")
            registry_tables[f"{class_name}.{name}"] = entries

    for node in tree.body:
        dnode = _registry_dict_from_value(node.value) if isinstance(node, ast.Assign) else None
        if isinstance(node, ast.Assign) and dnode is not None:
            for n in _target_names(node.targets):
                note_registry(n, dnode)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            dnode = _registry_dict_from_value(node.value)
            if dnode is not None and isinstance(node.target, ast.Name):
                note_registry(node.target.id, dnode)
        elif isinstance(node, ast.ClassDef):
            for stmt in node.body:
                if isinstance(stmt, ast.Assign):
                    dnode = _registry_dict_from_value(stmt.value)
                    if dnode is not None:
                        for n in _target_names(stmt.targets):
                            note_registry(n, dnode, node.name)
                elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                    dnode = _registry_dict_from_value(stmt.value)
                    if dnode is not None and isinstance(stmt.target, ast.Name):
                        note_registry(stmt.target.id, dnode, node.name)

    # Decorator registration: @Owner.register_method on a class whose body sets
    # a string key, and register_method does cls.REG[key] = subclass.
    # SYNTAXES = {} is empty so the dict-literal path never names members;
    # this is how BaseSpec.SYNTAXES / SimpleSpec / NpmSpec become visible.
    decorator_entries = collect_decorator_registry_entries(tree)

    def note_decorator_entry(owner: str, reg_name: str, key: str, val_name: str) -> None:
        registries.add(reg_name)
        registries.add(f"{owner}.{reg_name}")
        for table_key in (reg_name, f"{owner}.{reg_name}"):
            bucket = registry_tables.setdefault(table_key, [])
            pair = (key, val_name)
            if pair not in bucket:
                bucket.append(pair)

    for dec_ent in decorator_entries:
        note_decorator_entry(
            dec_ent["owner"],
            dec_ent["reg_attr"],
            dec_ent["key"],
            dec_ent["value"],
        )

    fa.registries = set(registries)
    fa.registry_tables = registry_tables

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
    # Decorator registration sites (on the registered class body line).
    for dec_ent in decorator_entries:
        line = int(dec_ent.get("line") or 0)
        if line <= 0:
            continue
        sites.append(
            DynamicSite(
                file=str(path),
                line=line,
                end_line=line,
                col=0,
                kind="decorator_registration",
                detail=(
                    f"{dec_ent['owner']}.{dec_ent['method']}:"
                    f"{dec_ent['reg_attr']}[{dec_ent['key']!r}]->{dec_ent['value']}"
                ),
                enclosing=str(dec_ent["value"]),
            )
        )
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
        pa.registry_tables.update(fa.registry_tables)
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


# Confidence for *promoted* registry-dispatch call edges. Lower than a unique
# static Name call (~0.85–0.95), higher than a bare observation (0.35).
# is_deterministic is always False: which table member runs is not static.
REGISTRY_DISPATCH_CONFIDENCE = 0.75


def annotate_byog(
    data: Dict[str, List[Dict[str, Any]]], package_dir: Path
) -> Dict[str, Any]:
    """Stamp dynamic-dispatch provenance onto entities, relationships, observations.

    May **add** ``calls`` edges for statically named registry-dispatch targets
    (see ``_registry_dispatch_edges``). Does not flip ``is_deterministic`` on
    pre-existing edges and does not drop edges.
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

    # Drop prior registry-candidate observations *and* prior registry-dispatch
    # call edges so re-annotation is idempotent (observations may grow; other
    # call edges must not accumulate duplicates of our promotions).
    existing_obs = [
        o
        for o in (data.get("call_observations") or [])
        if not (
            str(o.get("reason", "")).startswith("registry_candidate:")
            or str(o.get("extractor", "")) == "python_dynamic"
        )
    ]
    data["call_observations"] = existing_obs
    data["relationships"] = [
        r
        for r in (data.get("relationships") or [])
        if not (
            str(r.get("type", "")) == "calls"
            and (
                str(r.get("extractor", "")) == "python_dynamic_registry"
                or str(r.get("description", "")).startswith("registry_dispatch:")
            )
        )
    ]

    for o in data.get("call_observations") or []:
        reasons = reasons_for_span(
            pa,
            str(o.get("source_file", "")),
            str(o.get("span", "")),
        )
        # Observations already marked unresolved/low-conf at a dynamic site keep the label.
        # Labels are always recomputed from source, never merged with what a previous
        # stamp wrote: re-annotating a published snapshot must not accumulate stale
        # reasons (and parquet round-trips reasons as arrays, not lists).
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

    # Registry dispatch: same static facts as candidates, but *promoted* to
    # calls edges under the rule in ``_registry_dispatch_edges``. Observations
    # remain for context packs (dispatch_candidates).
    candidate_obs = _registry_candidate_observations(data, pa)
    summary["registry_candidates_emitted"] = len(candidate_obs)
    data.setdefault("call_observations", []).extend(candidate_obs)
    for o in candidate_obs:
        summary["observations_flagged"] += 1
        sf = Path(str(o.get("source_file", ""))).name or "?"
        summary["by_file"][sf]["observations"] += 1
        if len(summary["samples"]) < 40:
            summary["samples"].append(
                {
                    "kind": "observation",
                    "source": o.get("source"),
                    "display_target": o.get("display_target"),
                    "file": sf,
                    "span": o.get("span"),
                    "reasons": o.get("dynamic_reasons"),
                }
            )

    dispatch_edges = _registry_dispatch_edges(data, pa)
    summary["registry_dispatch_edges_emitted"] = len(dispatch_edges)
    data.setdefault("relationships", []).extend(dispatch_edges)
    for r in dispatch_edges:
        summary["calls_flagged"] += 1
        # Not trusted/deterministic — promotion rule forbids that.
        sf = Path(str(r.get("source_file", ""))).name or "?"
        summary["by_file"][sf]["calls"] += 1

    summary["by_file"] = dict(summary["by_file"])
    summary["registry_tables"] = {k: list(v) for k, v in pa.registry_tables.items()}
    return summary


def _entity_class_and_method(title: str) -> Tuple[Optional[str], Optional[str]]:
    """``mod:JsonPatch.apply`` → (``JsonPatch``, ``apply``); ``mod:fn`` → (None, ``fn``)."""
    bare = str(title).split(":")[-1]
    if "." in bare:
        cls, meth = bare.split(".", 1)
        return cls, meth
    return None, bare


def _tables_for_class(pa: PackageAnalysis, class_name: str) -> List[Tuple[str, List[Tuple[str, str]]]]:
    """Return (registry_name, entries) for registries defined on class_name."""
    out: List[Tuple[str, List[Tuple[str, str]]]] = []
    prefix = f"{class_name}."
    for key, entries in pa.registry_tables.items():
        if key.startswith(prefix):
            out.append((key.split(".", 1)[1], entries))
    return out


def _iter_registry_dispatch_targets(
    data: Dict[str, List[Dict[str, Any]]], pa: PackageAnalysis
) -> List[Dict[str, Any]]:
    """Static registry-dispatch targets shared by observations and call edges.

    **Promotion rule (the only path that may mint a registry ``calls`` edge):**

    1. The registry table is extracted *statically* with a concrete key and a
       Name/Attribute value (dict-literal Name values or decorator registration).
       Lambda/Call-valued tables never qualify — they have no honest callee name.
    2. The dispatch site is an entity on the same class, statically labelled with
       ``registry_lookup`` / ``call_through_registry`` / ``call_through_dynamic_name:cls``
       and/or ``polymorphic_call`` / ``registry_derived_iter`` for that site.
    3. The target entity title already exists in the graph (no invented symbols).

    Runtime ``--vs-runtime`` confirmation of the table (e.g. jsonpatch 6/6)
    *justifies* this policy; extract time uses only (1–3), so promotion cannot
    invent members the AST did not name. Unconfirmed guesses (runtime-only
    discoveries, ambiguous receivers) are never promoted.
    """
    entity_titles = {str(e.get("title", "")) for e in data.get("entities") or []}
    module_by_class: Dict[str, str] = {}
    for t in entity_titles:
        if ":" not in t:
            continue
        mod, bare = t.split(":", 1)
        if "." in bare:
            cls = bare.split(".", 1)[0]
            module_by_class.setdefault(cls, mod)

    emitted: List[Dict[str, Any]] = []
    seen: Set[Tuple[str, str, str]] = set()

    for e in data.get("entities") or []:
        if not e.get("dynamic_dependent"):
            continue
        reasons = [str(r) for r in (e.get("dynamic_reasons") or [])]
        title = str(e.get("title", ""))
        cls, _meth = _entity_class_and_method(title)
        if not cls:
            continue
        tables = _tables_for_class(pa, cls)
        if not tables:
            continue

        poly_methods: List[str] = []
        has_registry_lookup = any(
            r.startswith("registry_lookup:")
            or r.startswith("call_through_registry")
            or r.startswith("call_through_dynamic_name:cls")
            for r in reasons
        )
        has_poly = False
        for r in reasons:
            if r.startswith("polymorphic_call:"):
                has_poly = True
                body = r[len("polymorphic_call:") :]
                head = body.split("<", 1)[0]
                if "." in head:
                    poly_methods.append(head.split(".", 1)[1])
            if r.startswith("registry_derived_iter:"):
                has_poly = True

        if not has_poly and not has_registry_lookup:
            continue

        mod = module_by_class.get(cls, title.split(":")[0] if ":" in title else "")
        for reg_name, entries in tables:
            # entries are only Name/Attribute values with concrete keys
            # (see _registry_table_entries / decorator registration).
            for key, val_name in entries:
                if has_poly and poly_methods:
                    targets = [f"{val_name}.{pm}" for pm in poly_methods]
                elif has_poly and not poly_methods:
                    targets = [val_name]
                else:
                    targets = [val_name]

                for disp in targets:
                    fq = f"{mod}:{disp}" if mod else disp
                    display = fq if fq in entity_titles else (
                        f"{mod}:{val_name}"
                        if (mod and f"{mod}:{val_name}" in entity_titles and "." not in disp)
                        else disp
                    )
                    if "." in disp and mod and f"{mod}:{disp}" in entity_titles:
                        display = f"{mod}:{disp}"
                    elif (
                        mod
                        and f"{mod}:{val_name}" in entity_titles
                        and has_registry_lookup
                        and not has_poly
                    ):
                        display = f"{mod}:{val_name}"

                    # Rule (3): refuse targets that are not graph entities.
                    if display not in entity_titles:
                        continue

                    dedup = (title, display, reg_name)
                    if dedup in seen:
                        continue
                    seen.add(dedup)
                    emitted.append(
                        {
                            "source": title,
                            "target": display,
                            "reg_name": reg_name,
                            "key": key,
                            "cls": cls,
                            "source_file": e.get("source_file", ""),
                            "span": e.get("span", ""),
                        }
                    )
    return emitted


def _registry_candidate_observations(
    data: Dict[str, List[Dict[str, Any]]], pa: PackageAnalysis
) -> List[Dict[str, Any]]:
    """Emit weak observations naming static registry members as possible callees.

    Confidence stays low; reason is ``registry_candidate:…``. Call edges for the
    same pairs are emitted separately by ``_registry_dispatch_edges``.
    """
    emitted: List[Dict[str, Any]] = []
    for item in _iter_registry_dispatch_targets(data, pa):
        title = item["source"]
        display = item["target"]
        reg_name = item["reg_name"]
        key = item["key"]
        cls = item["cls"]
        emitted.append(
            {
                "source": title,
                "display_target": display,
                "confidence": 0.35,
                "reason": f"registry_candidate:{reg_name}[{key!r}]->{display}",
                "source_file": item["source_file"],
                "span": item["span"],
                "extractor": "python_dynamic",
                "description": (
                    f"{title} may dispatch via static registry "
                    f"{cls}.{reg_name}[{key!r}] to {display} "
                    f"(static table member; also promoted to a non-deterministic calls edge)"
                ),
                "dynamic_dependent": True,
                "dynamic_reasons": [
                    f"registry_candidate:{reg_name}",
                    f"registry_key:{key}",
                ],
            }
        )
    return emitted


def _registry_dispatch_edges(
    data: Dict[str, List[Dict[str, Any]]], pa: PackageAnalysis
) -> List[Dict[str, Any]]:
    """Promote statically named registry members to ``calls`` edges.

    See ``_iter_registry_dispatch_targets`` for the promotion rule. Edges are
    always ``is_deterministic=False`` and use extractor
    ``python_dynamic_registry`` so re-annotation can replace them cleanly.
    """
    edges: List[Dict[str, Any]] = []
    existing = {
        (str(r.get("source")), str(r.get("target")))
        for r in (data.get("relationships") or [])
        if str(r.get("type", "")) == "calls"
    }
    for item in _iter_registry_dispatch_targets(data, pa):
        src, tgt = item["source"], item["target"]
        if (src, tgt) in existing:
            continue
        existing.add((src, tgt))
        reg_name, key, cls = item["reg_name"], item["key"], item["cls"]
        edges.append(
            {
                "source": src,
                "target": tgt,
                "type": "calls",
                "description": (
                    f"registry_dispatch:{cls}.{reg_name}[{key!r}]->{tgt} "
                    f"(static Name table member at labelled dispatch site; "
                    f"is_deterministic=False — which member runs is runtime)"
                ),
                "weight": REGISTRY_DISPATCH_CONFIDENCE,
                "text_unit_ids": [],
                "human_readable_id": 0,
                "source_file": item["source_file"],
                "span": item["span"],
                "extractor": "python_dynamic_registry",
                "confidence": REGISTRY_DISPATCH_CONFIDENCE,
                "is_deterministic": False,
                "dynamic_dependent": True,
                "dynamic_reasons": [
                    f"registry_dispatch:{reg_name}",
                    f"registry_key:{key}",
                ],
                "resolved_target_hint": tgt,
            }
        )
    return edges


# ---------------------------------------------------------------------------
# Runtime oracle — import real registry objects and score the AST extractor
# ---------------------------------------------------------------------------


@dataclass
class RegistryDef:
    """One AST-detected callable-registry table with enough location to import it."""

    name: str
    class_name: Optional[str]
    file: Path
    module: str
    entries: List[Tuple[str, str]]  # concrete key -> Name/Attribute value
    inventory: Dict[str, Any]

    @property
    def qual_name(self) -> str:
        if self.class_name:
            return f"{self.class_name}.{self.name}"
        return self.name

    @property
    def attr_path(self) -> str:
        return self.qual_name


def _import_layout(package_dir: Path) -> Tuple[Path, str]:
    """Return (sys.path entry, package root name or \"\" for flat modules)."""
    package_dir = package_dir.resolve()
    if (package_dir / "__init__.py").is_file():
        return package_dir.parent, package_dir.name
    return package_dir, ""


def _module_for_source(package_dir: Path, file: Path) -> str:
    package_dir = package_dir.resolve()
    file = file.resolve()
    path_entry, root = _import_layout(package_dir)
    rel = file.relative_to(package_dir)
    if root:
        if rel.name == "__init__.py":
            return root
        return root + "." + ".".join(rel.with_suffix("").parts)
    return ".".join(rel.with_suffix("").parts)


def collect_registry_defs(package_dir: Path) -> List[RegistryDef]:
    """Walk package sources and collect unique AST-detected registries.

    Includes both dict-literal tables and decorator-registration tables
    (``@Owner.register_*`` writing into ``cls.REG[key]``). Prefers
    ``Class.attr`` over bare ``attr`` when both are recorded so each physical
    table is scored once.
    """
    package_dir = package_dir.resolve()
    # key: (module, class_or_None, name) -> RegistryDef
    found: Dict[Tuple[str, Optional[str], str], RegistryDef] = {}

    for path in sorted(package_dir.rglob("*.py")):
        if any(part in {"__pycache__", ".venv", "venv", "target", "tests"} for part in path.parts):
            continue
        if path.name == "__init__.py" and path.parent == package_dir:
            # still analyse package root __init__ for rare module-level tables
            pass
        elif path.name == "__init__.py":
            pass
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        try:
            module = _module_for_source(package_dir, path)
        except ValueError:
            continue

        def note(
            name: str,
            *,
            class_name: Optional[str],
            entries: List[Tuple[str, str]],
            inventory: Dict[str, Any],
        ) -> None:
            key = (module, class_name, name)
            prev = found.get(key)
            if prev is None:
                found[key] = RegistryDef(
                    name=name,
                    class_name=class_name,
                    file=path,
                    module=module,
                    entries=list(entries),
                    inventory=inventory,
                )
                return
            # Merge: decorator fills can extend a prior table (or replace empty).
            merged = list(prev.entries)
            for pair in entries:
                if pair not in merged:
                    merged.append(pair)
            if not prev.entries and entries:
                inv = inventory
            elif inventory.get("source") == "decorator_registration" and entries:
                inv = _decorator_inventory(merged)
            else:
                inv = prev.inventory
            found[key] = RegistryDef(
                name=name,
                class_name=class_name,
                file=path,
                module=module,
                entries=merged,
                inventory=inv,
            )

        for node in tree.body:
            if isinstance(node, ast.Assign):
                dnode = _registry_dict_from_value(node.value)
                if dnode is not None:
                    for n in _target_names(node.targets):
                        note(
                            n,
                            class_name=None,
                            entries=_registry_table_entries(dnode),
                            inventory=_registry_dict_inventory(dnode),
                        )
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                dnode = _registry_dict_from_value(node.value)
                if dnode is not None and isinstance(node.target, ast.Name):
                    note(
                        node.target.id,
                        class_name=None,
                        entries=_registry_table_entries(dnode),
                        inventory=_registry_dict_inventory(dnode),
                    )
            elif isinstance(node, ast.ClassDef):
                for stmt in node.body:
                    if isinstance(stmt, ast.Assign):
                        dnode = _registry_dict_from_value(stmt.value)
                        if dnode is not None:
                            for n in _target_names(stmt.targets):
                                note(
                                    n,
                                    class_name=node.name,
                                    entries=_registry_table_entries(dnode),
                                    inventory=_registry_dict_inventory(dnode),
                                )
                    elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                        dnode = _registry_dict_from_value(stmt.value)
                        if dnode is not None and isinstance(stmt.target, ast.Name):
                            note(
                                stmt.target.id,
                                class_name=node.name,
                                entries=_registry_table_entries(dnode),
                                inventory=_registry_dict_inventory(dnode),
                            )

        # Decorator-filled tables (may be the only source of members).
        by_table: Dict[Tuple[str, str], List[Tuple[str, str]]] = defaultdict(list)
        for dec_ent in collect_decorator_registry_entries(tree):
            owner = str(dec_ent["owner"])
            reg = str(dec_ent["reg_attr"])
            pair = (str(dec_ent["key"]), str(dec_ent["value"]))
            if pair not in by_table[(owner, reg)]:
                by_table[(owner, reg)].append(pair)
        for (owner, reg), pairs in by_table.items():
            note(
                reg,
                class_name=owner,
                entries=pairs,
                inventory=_decorator_inventory(pairs),
            )

    # Prefer Class.attr over bare attr when both describe the same class table.
    bare_covered: Set[Tuple[str, str]] = set()
    for (mod, cls, name), _rd in found.items():
        if cls is not None:
            bare_covered.add((mod, name))
    out: List[RegistryDef] = []
    for (mod, cls, name), rd in sorted(
        found.items(), key=lambda kv: (kv[0][0], kv[0][1] or "", kv[0][2])
    ):
        if cls is None and (mod, name) in bare_covered:
            continue
        out.append(rd)
    return out


def _runtime_probe_script() -> str:
    """Python source run in a subprocess: import attr path, dump mapping entries."""
    return textwrap.dedent(
        """
        import importlib
        import json
        import sys
        import types

        path_entry, module, attr_path = sys.argv[1], sys.argv[2], sys.argv[3]
        sys.path.insert(0, path_entry)

        def encode_key(k):
            if k is None:
                return "__None__"
            if isinstance(k, str):
                return k
            if isinstance(k, bool):
                return "True" if k else "False"
            if isinstance(k, (int, float)):
                return str(k)
            return "__repr__:" + repr(k)

        def describe(v):
            if isinstance(v, type):
                return {
                    "name": v.__name__,
                    "kind": "type",
                    "qualname": getattr(v, "__qualname__", v.__name__),
                    "module": getattr(v, "__module__", ""),
                }
            if isinstance(v, types.FunctionType):
                return {
                    "name": v.__name__,
                    "kind": "function",
                    "qualname": getattr(v, "__qualname__", v.__name__),
                    "module": getattr(v, "__module__", ""),
                }
            if callable(v) and hasattr(v, "__name__"):
                return {
                    "name": v.__name__,
                    "kind": "callable",
                    "qualname": getattr(v, "__qualname__", v.__name__),
                    "module": getattr(v, "__module__", ""),
                }
            t = type(v)
            return {
                "name": t.__name__,
                "kind": "instance",
                "qualname": getattr(t, "__qualname__", t.__name__),
                "module": getattr(t, "__module__", ""),
            }

        try:
            mod = importlib.import_module(module)
            obj = mod
            for part in attr_path.split("."):
                if not part:
                    continue
                obj = getattr(obj, part)
            if not hasattr(obj, "items"):
                print(json.dumps({
                    "ok": False,
                    "error": f"not a mapping: {type(obj).__name__}",
                }))
                raise SystemExit(0)
            entries = {}
            for k, v in obj.items():
                entries[encode_key(k)] = describe(v)
            print(json.dumps({"ok": True, "entries": entries, "n": len(entries)}))
        except Exception as e:
            print(json.dumps({
                "ok": False,
                "error": f"{type(e).__name__}: {e}",
            }))
        """
    ).strip()


def read_runtime_registry(
    package_dir: Path,
    module: str,
    attr_path: str,
    *,
    timeout: float = 15.0,
) -> Dict[str, Any]:
    """Import ``module.attr_path`` in a subprocess and return its mapping entries.

    Never imports in-process. On failure returns ``{ok: False, error: ...}``.
    """
    path_entry, _root = _import_layout(package_dir.resolve())
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                _runtime_probe_script(),
                str(path_entry),
                module,
                attr_path,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(path_entry),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"timeout after {timeout}s importing {module}.{attr_path}"}
    except OSError as e:
        return {"ok": False, "error": f"OSError: {e}"}

    stdout = (proc.stdout or "").strip()
    if not stdout:
        err = (proc.stderr or "").strip() or f"exit {proc.returncode}"
        return {"ok": False, "error": f"empty probe output: {err}"}
    # Probe prints one JSON object; tolerate trailing noise.
    line = stdout.splitlines()[-1]
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return {
            "ok": False,
            "error": f"probe returned non-JSON: {line[:200]!r}; stderr={(proc.stderr or '')[:200]!r}",
        }
    if not isinstance(data, dict):
        return {"ok": False, "error": "probe returned non-object JSON"}
    return data


def _names_match(extracted: str, runtime: Dict[str, Any]) -> bool:
    """True when the AST Name/Attribute matches the runtime object's name."""
    rname = str(runtime.get("name") or "")
    rqual = str(runtime.get("qualname") or "")
    if extracted == rname:
        return True
    if extracted == rqual:
        return True
    if rqual.endswith("." + extracted):
        return True
    return False


def _discovery_probe_script() -> str:
    """Walk the package at runtime and report every dict-of-callables it holds."""
    return textwrap.dedent(
        """
        import importlib, inspect, json, os, sys

        path_entry, pkg_dir, pkg_root = sys.argv[1], sys.argv[2], sys.argv[3]
        sys.path.insert(0, path_entry)

        def as_mapping(value):
            if isinstance(value, dict):
                return value
            if type(value).__name__ == "mappingproxy":
                try:
                    return dict(value)
                except Exception:
                    return None
            return None

        def is_registry(mapping):
            return bool(mapping) and all(callable(v) for v in mapping.values())

        modules = []
        for root, dirs, files in os.walk(pkg_dir):
            dirs[:] = [d for d in dirs if d not in {"tests", "__pycache__", ".git"}]
            for f in files:
                if f.endswith(".py") and f != "__init__.py":
                    rel = os.path.relpath(os.path.join(root, f), pkg_dir)
                    dotted = rel[:-3].replace(os.sep, ".")
                    modules.append(pkg_root + "." + dotted if pkg_root else dotted)

        found = {}
        for name in modules:
            try:
                mod = importlib.import_module(name)
            except Exception:
                continue
            for attr, value in vars(mod).items():
                if attr.startswith("__"):
                    continue
                mapping = as_mapping(value)
                if mapping is not None and is_registry(mapping):
                    found[name + ":" + attr] = len(mapping)
                if inspect.isclass(value):
                    for cattr, cvalue in vars(value).items():
                        if cattr.startswith("__"):
                            continue
                        cmap = as_mapping(cvalue)
                        if cmap is not None and is_registry(cmap):
                            found[name + ":" + attr + "." + cattr] = len(cmap)
        print(json.dumps({"ok": True, "registries": found}))
        """
    ).strip()


def discover_undetected_registries(
    package_dir: Path,
    defs: Sequence["RegistryDef"],
    *,
    timeout: float = 30.0,
) -> List[Dict[str, Any]]:
    """Runtime registries the AST extractor never detected at all.

    The oracle can otherwise only grade tables the extractor already found, so a
    registry it never sees is invisible rather than reported — the same
    one-directional blind spot the port-evidence manifest had. The common Python
    idiom this catches is decorator registration:
    ``semantic_version:BaseSpec.SYNTAXES`` starts as ``{}`` and is filled by
    ``@BaseSpec.register_syntax``, so no dict literal ever names its members,
    yet ``SYNTAXES[syntax](expression)`` is a real dispatch site.
    """
    package_dir = Path(package_dir).resolve()
    path_entry, _root = _import_layout(package_dir)
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                _discovery_probe_script(),
                str(path_entry),
                str(package_dir),
                _root,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(path_entry),
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    stdout = (proc.stdout or "").strip()
    if not stdout:
        return []
    try:
        data = json.loads(stdout.splitlines()[-1])
    except json.JSONDecodeError:
        return []
    known = {rd.qual_name for rd in defs}
    out: List[Dict[str, Any]] = []
    for qualified, count in sorted((data.get("registries") or {}).items()):
        module, _, attr = qualified.partition(":")
        if attr in known:
            continue
        out.append(
            {"registry": attr, "module": module, "runtime_entries": int(count)}
        )
    return out


def compare_registries_to_runtime(
    package_dir: Path,
    *,
    timeout: float = 15.0,
    # Optional override for tests: inject extra extracted entries per qual name.
    extra_extracted: Optional[Dict[str, List[Tuple[str, str]]]] = None,
) -> Dict[str, Any]:
    """Compare AST-inferred registry tables to real imported mapping objects.

    Units are **registry entries** (key → target name):

    * **agreement** — key in both; extracted Name/Attribute matches runtime
      class/function ``__name__``.
    * **disagreement** — extracted key missing at runtime, or wrong target name.
    * **missed** — runtime key the extractor did not emit (lambda/Call values,
      non-literal keys, or empty extraction). Counted and reported, **not**
      folded into the agreement numerator.
    * **unscored** — AST slots the extractor cannot decide (non-literal keys,
      non-Name values) when we still want a shape breakdown; these are the same
      population that drives *missed* once runtime keys are known.

    Import runs in a **subprocess**. An import failure skips the package with a
    named reason (not an empty registry agreement).

    ``ok`` is True iff there are zero disagreements (misses do not fail the check).
    """
    package_dir = Path(package_dir).resolve()
    defs = collect_registry_defs(package_dir)
    report: Dict[str, Any] = {
        "package": str(package_dir),
        "status": "ok",
        "skip_reason": None,
        "registries": [],
        "registries_total": len(defs),
        "registries_import_ok": 0,
        "registries_import_failed": 0,
        "entries_runtime": 0,
        "entries_extracted": 0,
        "entries_scored": 0,
        "agreements": 0,
        "disagreements": 0,
        "missed": 0,
        "invented": 0,
        "wrong_target": 0,
        "by_value_shape": {},
        # AST-detected tables whose runtime values are not callable at all, and
        # genuine runtime registries the AST never detected. Both are kept out
        # of the agreement numerator and out of `missed`.
        "false_positive_tables": 0,
        "false_positive_entries": 0,
        "undetected_registries": [],
        "undetected_entries": 0,
        "agreement_rate_scored": None,
        "coverage_of_runtime": None,
        "disagreement_details": [],
        "missed_samples": [],
        "import_failures": [],
        "ok": True,
    }
    # Even with zero AST defs we still run independent runtime discovery below —
    # that is how decorator-only registries surface when detection misses them.

    # Shape tallies across all importable registries.
    shape_stats: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {
            "ast_slots": 0,
            "extracted": 0,
            "runtime_entries": 0,
            "agreements": 0,
            "disagreements": 0,
            "missed": 0,
        }
    )

    agreements = 0
    disagreements = 0
    missed = 0
    invented = 0
    wrong_target = 0
    n_runtime = 0
    n_extracted = 0
    details: List[Dict[str, Any]] = []
    missed_samples: List[Dict[str, Any]] = []
    reg_rows: List[Dict[str, Any]] = []
    import_failures: List[Dict[str, Any]] = []
    false_positives: List[Dict[str, Any]] = []
    any_import_ok = False

    extra_extracted = extra_extracted or {}

    for rd in defs:
        runtime = read_runtime_registry(
            package_dir, rd.module, rd.attr_path, timeout=timeout
        )
        inv = rd.inventory
        extracted_map = {k: v for k, v in rd.entries}
        # Test/hook: inject or overwrite extracted pairs (e.g. a wrong candidate).
        for ek, ev in extra_extracted.get(rd.qual_name, []):
            extracted_map[ek] = ev

        row: Dict[str, Any] = {
            "registry": rd.qual_name,
            "module": rd.module,
            "file": str(rd.file),
            "ast_slots": inv.get("n_slots", 0),
            "extracted": len(extracted_map),
            "value_shapes": inv.get("value_shapes", {}),
            "key_shapes": inv.get("key_shapes", {}),
        }

        if not runtime.get("ok"):
            report["registries_import_failed"] += 1
            err = str(runtime.get("error") or "import failed")
            row["status"] = "import_failed"
            row["error"] = err
            import_failures.append(
                {"registry": rd.qual_name, "module": rd.module, "error": err}
            )
            reg_rows.append(row)
            continue

        any_import_ok = True
        report["registries_import_ok"] += 1
        rt_entries: Dict[str, Any] = runtime.get("entries") or {}

        # The AST criterion is syntactic, so it also matches tables whose values
        # are not callable at all — `charset:UNICODE_RANGES_COMBINED` is
        # str -> range, `humanize:_TRANSLATIONS` holds a NullTranslations
        # instance, `semantic_version:SpecItem.KIND_ALIASES` is str -> str.
        # Runtime settles it. Counting those as *missed dispatch targets*
        # inflated the headline: 350 of a reported 375 misses came from three
        # tables that are not dispatch registries.
        callable_kinds = {"type", "function", "callable"}
        non_callable = [
            key
            for key, rt in rt_entries.items()
            if str(rt.get("kind")) not in callable_kinds
        ]
        if rt_entries and len(non_callable) == len(rt_entries):
            row["status"] = "not_a_callable_registry"
            row["runtime_entries"] = len(rt_entries)
            row["runtime_value_kinds"] = sorted(
                {str(rt.get("kind")) for rt in rt_entries.values()}
            )
            report["false_positive_tables"] += 1
            report["false_positive_entries"] += len(rt_entries)
            false_positives.append(
                {
                    "registry": rd.qual_name,
                    "module": rd.module,
                    "runtime_entries": len(rt_entries),
                    "runtime_value_kinds": row["runtime_value_kinds"],
                }
            )
            reg_rows.append(row)
            continue

        row["status"] = "compared"
        row["runtime_entries"] = len(rt_entries)
        n_runtime += len(rt_entries)
        n_extracted += len(extracted_map)

        # Map runtime key -> dominant AST value shape (for breakdown).
        slot_shape_by_key: Dict[str, str] = {}
        for slot in inv.get("slots") or []:
            k = slot.get("key")
            if k is not None:
                slot_shape_by_key[str(k)] = str(slot.get("value_shape") or "?")

        for shape, n in (inv.get("value_shapes") or {}).items():
            shape_stats[shape]["ast_slots"] += int(n)
        for _k, _v in extracted_map.items():
            # Extracted values are always Name/Attribute.
            sh = slot_shape_by_key.get(_k, "Name")
            shape_stats[sh]["extracted"] += 1

        reg_agree = reg_disagree = reg_miss = 0

        # Score extracted keys.
        for key, val_name in extracted_map.items():
            if key not in rt_entries:
                reg_disagree += 1
                invented += 1
                disagreements += 1
                shape = slot_shape_by_key.get(key, "Name")
                shape_stats[shape]["disagreements"] += 1
                rec = {
                    "registry": rd.qual_name,
                    "key": key,
                    "kind": "invented",
                    "extracted": val_name,
                    "runtime": None,
                }
                details.append(rec)
                continue
            rt = rt_entries[key]
            if _names_match(val_name, rt):
                reg_agree += 1
                agreements += 1
                shape = slot_shape_by_key.get(key, "Name")
                shape_stats[shape]["agreements"] += 1
            else:
                reg_disagree += 1
                wrong_target += 1
                disagreements += 1
                shape = slot_shape_by_key.get(key, "Name")
                shape_stats[shape]["disagreements"] += 1
                details.append(
                    {
                        "registry": rd.qual_name,
                        "key": key,
                        "kind": "wrong_target",
                        "extracted": val_name,
                        "runtime": rt.get("name"),
                        "runtime_kind": rt.get("kind"),
                    }
                )

        # Missed runtime keys (not in extracted).
        for key, rt in rt_entries.items():
            shape_stats[slot_shape_by_key.get(key, rt.get("kind") or "?")][
                "runtime_entries"
            ] += 1
            if key in extracted_map:
                continue
            reg_miss += 1
            missed += 1
            shape = slot_shape_by_key.get(key, rt.get("kind") or "?")
            shape_stats[shape]["missed"] += 1
            if len(missed_samples) < 40:
                missed_samples.append(
                    {
                        "registry": rd.qual_name,
                        "key": key,
                        "runtime_name": rt.get("name"),
                        "runtime_kind": rt.get("kind"),
                        "ast_value_shape": slot_shape_by_key.get(key),
                    }
                )

        row["agreements"] = reg_agree
        row["disagreements"] = reg_disagree
        row["missed"] = reg_miss
        reg_rows.append(row)

    report["registries"] = reg_rows
    report["entries_runtime"] = n_runtime
    report["entries_extracted"] = n_extracted
    report["entries_scored"] = agreements + disagreements
    report["agreements"] = agreements
    report["disagreements"] = disagreements
    report["missed"] = missed
    report["invented"] = invented
    report["wrong_target"] = wrong_target
    report["by_value_shape"] = {k: dict(v) for k, v in sorted(shape_stats.items())}
    report["disagreement_details"] = details[:40]
    report["missed_samples"] = missed_samples
    report["import_failures"] = import_failures
    report["false_positives"] = false_positives
    undetected = discover_undetected_registries(package_dir, defs, timeout=timeout)
    report["undetected_registries"] = undetected
    report["undetected_entries"] = sum(int(u["runtime_entries"]) for u in undetected)
    scored = agreements + disagreements
    # A rate over an empty population is not 100% — it is undefined. Printing
    # 1.0 made packages where nothing was examined read as fully verified.
    report["agreement_rate_scored"] = (agreements / scored) if scored else None
    report["coverage_of_runtime"] = (agreements / n_runtime) if n_runtime else None
    report["ok"] = disagreements == 0

    if not any_import_ok and import_failures:
        # Entire package unimportable — not an agreement.
        report["status"] = "skipped"
        report["skip_reason"] = import_failures[0]["error"]
        report["ok"] = True  # skip is not a scoring failure
        report["message"] = (
            f"skipped: could not import registries ({len(import_failures)} failure(s)); "
            f"first: {import_failures[0]['error']}"
        )
    elif not any_import_ok:
        report["status"] = "skipped"
        report["skip_reason"] = "no registries compared"
        report["ok"] = True

    return report


def format_runtime_oracle_report(report: Dict[str, Any]) -> str:
    lines = [
        f"Python registry runtime oracle: {report.get('package')}",
        f"  status                  : {report.get('status')}"
        + (f" ({report.get('skip_reason')})" if report.get("skip_reason") else ""),
        f"  registries (AST)        : {report.get('registries_total')} "
        f"(import_ok={report.get('registries_import_ok')}, "
        f"import_failed={report.get('registries_import_failed')})",
        f"  entries runtime         : {report.get('entries_runtime')}",
        f"  entries extracted       : {report.get('entries_extracted')}",
        f"  scored (agree+disagree) : {report.get('entries_scored')}",
        f"  agreements              : {report.get('agreements')}",
        f"  disagreements           : {report.get('disagreements')} "
        f"(invented={report.get('invented')}, wrong_target={report.get('wrong_target')})",
        f"  missed (runtime only)   : {report.get('missed')}",
        f"  false-positive tables   : {report.get('false_positive_tables')} "
        f"({report.get('false_positive_entries')} entries; values not callable at runtime)",
        f"  undetected registries   : {len(report.get('undetected_registries') or [])} "
        f"({report.get('undetected_entries')} entries the AST never saw)",
        "  agreement rate (scored) : "
        + (
            f"{100.0 * report['agreement_rate_scored']:.1f}%"
            if report.get("agreement_rate_scored") is not None
            else "n/a (nothing scored)"
        ),
        "  coverage of runtime     : "
        + (
            f"{100.0 * report['coverage_of_runtime']:.1f}%"
            if report.get("coverage_of_runtime") is not None
            else "n/a (no comparable runtime entries)"
        ),
        f"  ok (no disagreements)   : {report.get('ok')}",
    ]
    if report.get("by_value_shape"):
        lines.append("  by AST value shape:")
        for shape, st in (report["by_value_shape"] or {}).items():
            lines.append(
                f"    {shape}: ast_slots={st.get('ast_slots', 0)} "
                f"extracted={st.get('extracted', 0)} "
                f"agree={st.get('agreements', 0)} "
                f"disagree={st.get('disagreements', 0)} "
                f"missed={st.get('missed', 0)}"
            )
    if report.get("undetected_registries"):
        lines.append("  undetected by the AST extractor (runtime discovery):")
        for u in report["undetected_registries"]:
            lines.append(
                f"    {u.get('module')}:{u.get('registry')} runtime={u.get('runtime_entries')}"
            )
    if report.get("false_positives"):
        lines.append("  detected but not callable registries at runtime:")
        for f in report["false_positives"]:
            lines.append(
                f"    {f.get('registry')}: runtime={f.get('runtime_entries')} "
                f"value_kinds={f.get('runtime_value_kinds')}"
            )
    if report.get("registries"):
        lines.append("  per registry:")
        for row in report["registries"]:
            if row.get("status") == "import_failed":
                lines.append(
                    f"    {row.get('registry')}: IMPORT FAILED — {row.get('error')}"
                )
            else:
                lines.append(
                    f"    {row.get('registry')}: runtime={row.get('runtime_entries')} "
                    f"extracted={row.get('extracted')} "
                    f"agree={row.get('agreements')} "
                    f"disagree={row.get('disagreements')} "
                    f"missed={row.get('missed')} "
                    f"shapes={row.get('value_shapes')}"
                )
    if report.get("disagreement_details"):
        lines.append("  disagreements (sample):")
        for d in report["disagreement_details"][:12]:
            lines.append(
                f"    {d.get('registry')}[{d.get('key')!r}] "
                f"{d.get('kind')}: extracted={d.get('extracted')!r} "
                f"runtime={d.get('runtime')!r}"
            )
    if report.get("missed_samples"):
        lines.append("  missed (sample):")
        for m in report["missed_samples"][:12]:
            lines.append(
                f"    {m.get('registry')}[{m.get('key')!r}] "
                f"runtime={m.get('runtime_name')!r} kind={m.get('runtime_kind')} "
                f"ast_shape={m.get('ast_value_shape')}"
            )
    lines.append(
        "  note: missed entries stay out of the agreement numerator; "
        "import failure skips (does not count as agreement)."
    )
    return "\n".join(lines)


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
    ap.add_argument(
        "--vs-runtime",
        action="store_true",
        help=(
            "compare AST-inferred registry tables to real imported mapping "
            "objects (subprocess import; does not modify graphs)"
        ),
    )
    args = ap.parse_args(argv)

    pkg = args.package.resolve()

    if args.vs_runtime:
        report = compare_registries_to_runtime(pkg)
        if args.json:
            print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        else:
            print(format_runtime_oracle_report(report))
        # Skip is success; scoring failure is disagreements > 0.
        if report.get("status") == "skipped":
            raise SystemExit(0)
        raise SystemExit(0 if report.get("ok") else 1)

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from mini_game_to_byog import build_byog_for_package  # type: ignore

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
