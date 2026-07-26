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


# Compiler/platform macros the compile_commands -D list does not set. Presence
# depends on the toolchain, so liveness under those names is *unknown* unless
# the user -D'd them. Never treat "absent from -D" as dead for these.
_PLATFORM_MACROS = frozenset(
    {
        "_MSC_VER",
        "_MSC_FULL_VER",
        "__GNUC__",
        "__GNUC_MINOR__",
        "__clang__",
        "__clang_major__",
        "_WIN32",
        "_WIN64",
        "WIN32",
        "WIN64",
        "__WINDOWS__",
        "__CYGWIN__",
        "__cplusplus",
        "__APPLE__",
        "__linux__",
        "__unix__",
        "__SUNPRO_C",
        "__SUNPRO_CC",
        "__MINGW32__",
        "__MINGW64__",
        "__BYTE_ORDER__",
        "__ORDER_LITTLE_ENDIAN__",
        "__ORDER_BIG_ENDIAN__",
    }
)

# Liveness under a recorded build configuration.
Liveness = str  # "live" | "dead" | "unknown"


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
    # For else/elif chains: the opening if/ifdef condition (used to invert else).
    chain_condition: str = ""
    chain_kind: str = ""


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
    # Object-like #define NAME [value] with source line (for ordered env).
    object_defines_at: List[Tuple[int, str, str]] = field(default_factory=list)


@dataclass
class PackageAnalysis:
    package_dir: Path
    files: Dict[str, FileAnalysis] = field(default_factory=dict)  # resolved path str
    all_function_macros: Set[str] = field(default_factory=set)
    all_object_macros: Set[str] = field(default_factory=set)
    compile_defines: Dict[str, Optional[str]] = field(default_factory=dict)
    # Header build defaults: name -> (value, defining file, defining line).
    header_defaults: Dict[str, Tuple[str, str, int]] = field(default_factory=dict)


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
                    name = om.group(1)
                    # remainder after name is the replacement list (may be empty)
                    repl = rest[len(name) :].strip()
                    fa.macros.append(
                        MacroDef(
                            name=name,
                            function_like=False,
                            line=i,
                            file=str(path),
                            body_preview=rest[:80],
                        )
                    )
                    fa.object_defines_at.append((i, name, repl))
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
                    chain_kind=directive,
                    chain_condition=rest,
                )
            )
        elif directive == "elif":
            close_top(i - 1)
            depth = len(stack)  # same depth as the if it continues
            if depth == 0:
                depth = 1
            chain_kind = stack[-1][1] if stack else "if"
            chain_cond = stack[-1][2] if stack else rest
            open_regions.append(
                ConditionalRegion(
                    start_line=i,
                    end_line=0,
                    kind="elif",
                    condition=rest,
                    depth=depth,
                    file=str(path),
                    chain_kind=chain_kind,
                    chain_condition=chain_cond,
                )
            )
        elif directive == "else":
            close_top(i - 1)
            depth = len(stack) if stack else 1
            chain_kind = stack[-1][1] if stack else "if"
            chain_cond = stack[-1][2] if stack else ""
            open_regions.append(
                ConditionalRegion(
                    start_line=i,
                    end_line=0,
                    kind="else",
                    condition="",
                    depth=depth,
                    file=str(path),
                    chain_kind=chain_kind,
                    chain_condition=chain_cond,
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


def _collect_header_defaults(
    file_analyses: Dict[str, FileAnalysis],
) -> Dict[str, Tuple[str, str, int]]:
    """Collect header ``#define`` values that act as build defaults.

    The compile database usually lists only ``.c`` files; the real build still
    includes headers that set INI_*/CJSON_* defaults. Two — and only two —
    shapes are treated as defaults, because only these hold regardless of the
    toolchain:

    * an object-like ``#define`` outside every conditional (include guards
      excepted), e.g. ``CJSON_VERSION_MAJOR``;
    * the ``#ifndef X`` / ``#define X val`` idiom, whose whole purpose is "this
      is the value unless the build overrides it".

    A ``#define`` nested in any other conditional is **not** a default: it is
    conditional on something we may not be able to decide. Harvesting those
    inverts the very condition that guards them — ``#define __WINDOWS__`` inside
    ``#if !defined(__WINDOWS__) && defined(WIN32)…`` would otherwise make a
    POSIX build look like Windows.

    Each default carries (value, file, line) so a region can be evaluated in the
    environment that existed *at* the directive: the ``#ifndef X`` that
    establishes a default must still read as live.

    Compile-command ``-D`` always wins over these.
    """
    defaults: Dict[str, Tuple[str, str, int]] = {}
    for key, fa in file_analyses.items():
        if not key.endswith(".h"):
            continue
        guarding: Dict[int, List[ConditionalRegion]] = {}
        for line, name, value in fa.object_defines_at:
            if name in defaults:
                continue
            containing = guarding.get(line)
            if containing is None:
                containing = [
                    reg
                    for reg in fa.regions
                    if not reg.is_include_guard and reg.start_line <= line <= reg.end_line
                ]
                guarding[line] = containing
            if containing:
                # Only the `#ifndef NAME` default idiom survives nesting.
                if len(containing) != 1:
                    continue
                reg = containing[0]
                if reg.kind != "ifndef" or (reg.condition or "").split()[:1] != [name]:
                    continue
            defaults[name] = (value, key, line)
    return defaults


def analyze_package(package_dir: Path) -> PackageAnalysis:
    package_dir = Path(package_dir).resolve()
    cc = parse_compile_commands(package_dir)
    package_defines = cc.get("*", {})
    pa = PackageAnalysis(
        package_dir=package_dir,
        compile_defines=dict(package_defines),
    )
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
    # Defaults need the parsed regions, so collect them after the files are read.
    pa.header_defaults = _collect_header_defaults(pa.files)
    return pa


def _defined_env(
    pa: PackageAnalysis,
    fa: Optional[FileAnalysis],
    *,
    before_line: Optional[int] = None,
) -> Dict[str, Optional[str]]:
    """Merge header defaults, compile -D, and file-local object defines.

    Precedence (highest last): header_defaults < package -D < file -D <
    in-file ``#define`` that appear *before* ``before_line`` (if given).
    Using only prior in-file defines avoids treating a later ``#define true``
    as already visible to an earlier ``#ifdef true``.

    Header defaults obey the same ordering within their own file, so the
    ``#ifndef X`` that establishes a default still evaluates as live rather than
    being falsified by the ``#define X`` it guards.
    """
    env: Dict[str, Optional[str]] = {}
    same_file = {str(getattr(fa, "path", "")), Path(str(getattr(fa, "path", ""))).name}
    for k, (value, def_file, def_line) in (pa.header_defaults or {}).items():
        if (
            before_line is not None
            and def_line >= before_line
            and (def_file in same_file or Path(def_file).name in same_file)
        ):
            continue
        env[k] = value
    for k, v in (pa.compile_defines or {}).items():
        env[k] = v
    if fa is not None:
        for k, v in (fa.compile_defines or {}).items():
            env[k] = v
        for line, name, val in fa.object_defines_at or []:
            if before_line is not None and line >= before_line:
                continue
            # A `#define` nested in a conditional only holds when that
            # conditional does; `#define __WINDOWS__` inside
            # `#if !defined(__WINDOWS__) && defined(WIN32)…` must not make a
            # POSIX build look like Windows.
            if _enclosing_regions(fa, line):
                continue
            env[name] = val
    return env


def _enclosing_regions(fa: FileAnalysis, line: int) -> List[ConditionalRegion]:
    """Non-guard conditional regions containing ``line``."""
    return [
        reg
        for reg in fa.regions
        if not reg.is_include_guard and reg.start_line <= line <= reg.end_line
    ]


def _is_defined(name: str, env: Dict[str, Optional[str]]) -> Optional[bool]:
    """True/False if decidable; None if platform-unknown and not -D'd."""
    if name in env:
        return True
    if name in _PLATFORM_MACROS:
        return None  # unknown — do not treat as dead
    return False


def _eval_primary(expr: str, env: Dict[str, Optional[str]]) -> Tuple[Optional[bool], str]:
    """Evaluate a stripped primary condition fragment. None = unknown."""
    e = expr.strip()
    if not e:
        return None, "empty condition"
    # defined(X) / !defined(X)
    m = re.fullmatch(r"defined\s*\(\s*([A-Za-z_]\w*)\s*\)", e)
    if m:
        d = _is_defined(m.group(1), env)
        if d is None:
            return None, f"platform macro {m.group(1)} not in compile -D"
        return d, f"defined({m.group(1)})={'yes' if d else 'no'}"
    m = re.fullmatch(r"!\s*defined\s*\(\s*([A-Za-z_]\w*)\s*\)", e)
    if m:
        d = _is_defined(m.group(1), env)
        if d is None:
            return None, f"platform macro {m.group(1)} not in compile -D"
        return (not d), f"!defined({m.group(1)})={'yes' if not d else 'no'}"
    # bare identifier (common for INI_USE_STACK style #if X)
    m = re.fullmatch(r"([A-Za-z_]\w*)", e)
    if m:
        name = m.group(1)
        if name in env:
            val = env[name]
            # defined with empty or non-zero token → true; 0 → false
            if val is None or str(val).strip() == "":
                return True, f"{name} defined (empty/flag)"
            tok = str(val).strip().split()[0]
            if re.fullmatch(r"0[xX]?[0-9a-fA-F]*", tok) or tok == "0":
                return False, f"{name}={tok}"
            if re.fullmatch(r"[0-9]+", tok) or re.fullmatch(r"0[xX][0-9a-fA-F]+", tok):
                return (int(tok, 0) != 0), f"{name}={tok}"
            # non-numeric replacement (string, expression) → unknown
            return None, f"{name} has non-numeric value {tok!r}"
        if name in _PLATFORM_MACROS:
            return None, f"platform macro {name} not in compile -D"
        # not defined → 0 in #if
        return False, f"{name} undefined → 0"
    # unary !
    if e.startswith("!"):
        inner, basis = _eval_primary(e[1:].strip(), env)
        if inner is None:
            return None, basis
        return (not inner), f"!({basis})"
    # parentheses
    if e.startswith("(") and e.endswith(")"):
        return _eval_expr(e[1:-1], env)
    return None, f"unevaluable expression: {e[:60]}"


def _split_top(expr: str, op: str) -> Optional[List[str]]:
    """Split on op at paren depth 0; op is '&&' or '||'."""
    parts: List[str] = []
    depth = 0
    i = 0
    start = 0
    while i < len(expr):
        c = expr[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif depth == 0 and expr.startswith(op, i):
            parts.append(expr[start:i].strip())
            i += len(op)
            start = i
            continue
        i += 1
    if not parts:
        return None
    parts.append(expr[start:].strip())
    return parts


def _eval_expr(expr: str, env: Dict[str, Optional[str]]) -> Tuple[Optional[bool], str]:
    e = expr.strip()
    if not e:
        return None, "empty"
    # comparison forms we can do when both sides numeric/known: A != B, A == B
    m = re.fullmatch(
        r"(.+?)\s*(==|!=|<=|>=|<|>)\s*(.+)", e
    )
    # only if no &&/|| at top level
    if m and _split_top(e, "&&") is None and _split_top(e, "||") is None:
        left, op, right = m.group(1).strip(), m.group(2), m.group(3).strip()
        def _num(side: str) -> Optional[int]:
            side = side.strip()
            if re.fullmatch(r"[0-9]+|0[xX][0-9a-fA-F]+", side):
                return int(side, 0)
            if re.fullmatch(r"[A-Za-z_]\w*", side) and side in env:
                v = env[side]
                if v is None or str(v).strip() == "":
                    return None
                tok = str(v).strip().split()[0]
                if re.fullmatch(r"[0-9]+|0[xX][0-9a-fA-F]+", tok):
                    return int(tok, 0)
            return None
        lv, rv = _num(left), _num(right)
        if lv is not None and rv is not None:
            ops = {
                "==": lv == rv,
                "!=": lv != rv,
                "<": lv < rv,
                ">": lv > rv,
                "<=": lv <= rv,
                ">=": lv >= rv,
            }
            return ops[op], f"{left}{op}{right} ({lv}{op}{rv})"
        # fall through to unknown if identifiers missing

    parts = _split_top(e, "||")
    if parts and len(parts) > 1:
        bases = []
        any_true = False
        any_unknown = False
        for p in parts:
            v, b = _eval_expr(p, env)
            bases.append(b)
            if v is True:
                any_true = True
            elif v is None:
                any_unknown = True
        if any_true:
            return True, " || ".join(bases)
        if any_unknown:
            return None, " || ".join(bases)
        return False, " || ".join(bases)

    parts = _split_top(e, "&&")
    if parts and len(parts) > 1:
        bases = []
        any_false = False
        any_unknown = False
        for p in parts:
            v, b = _eval_expr(p, env)
            bases.append(b)
            if v is False:
                any_false = True
            elif v is None:
                any_unknown = True
        if any_false:
            return False, " && ".join(bases)
        if any_unknown:
            return None, " && ".join(bases)
        return True, " && ".join(bases)

    return _eval_primary(e, env)


def evaluate_region_liveness(
    reg: ConditionalRegion, env: Dict[str, Optional[str]]
) -> Tuple[Liveness, str]:
    """Return (live|dead|unknown, basis) for a region under define env."""
    kind = reg.kind
    cond = (reg.condition or "").strip()

    if kind == "ifdef":
        name = cond.split()[0] if cond else ""
        d = _is_defined(name, env)
        if d is None:
            return "unknown", f"platform macro {name} not in compile -D"
        return ("live" if d else "dead"), f"ifdef({name}) → {'defined' if d else 'undefined'}"

    if kind == "ifndef":
        name = cond.split()[0] if cond else ""
        d = _is_defined(name, env)
        if d is None:
            return "unknown", f"platform macro {name} not in compile -D"
        return ("live" if not d else "dead"), f"ifndef({name}) → {'undefined' if not d else 'defined'}"

    if kind in ("if", "elif"):
        # strip trailing backslash continuations already joined? keep simple
        val, basis = _eval_expr(cond, env)
        if val is None:
            return "unknown", basis
        return ("live" if val else "dead"), basis

    if kind == "else":
        # opposite of opening if/ifdef when decidable
        ck, cc = reg.chain_kind or "if", reg.chain_condition or ""
        pseudo = ConditionalRegion(
            start_line=reg.start_line,
            end_line=reg.end_line,
            kind=ck if ck in ("if", "ifdef", "ifndef") else "if",
            condition=cc,
            depth=reg.depth,
            file=reg.file,
        )
        parent_live, basis = evaluate_region_liveness(pseudo, env)
        if parent_live == "unknown":
            return "unknown", f"else of unknown parent ({basis})"
        if parent_live == "live":
            return "dead", f"else of live parent ({basis})"
        return "live", f"else of dead parent ({basis})"

    return "unknown", f"unhandled directive {kind}"


def region_liveness(
    pa: PackageAnalysis, fa: FileAnalysis, reg: ConditionalRegion
) -> Tuple[Liveness, str]:
    """Liveness of ``reg`` including the regions that enclose it.

    A branch inside a dead conditional is dead however decidable its own
    condition is, and one inside an undecidable conditional is undecidable —
    otherwise a Windows-only block reads as live because its inner `#if` happens
    to be evaluable.
    """
    env = _defined_env(pa, fa, before_line=reg.start_line)
    own, basis = evaluate_region_liveness(reg, env)
    for parent in _enclosing_regions(fa, reg.start_line):
        if parent.start_line >= reg.start_line and parent.end_line <= reg.end_line:
            continue  # itself, or a sibling arm of the same chain
        parent_env = _defined_env(pa, fa, before_line=parent.start_line)
        parent_live, parent_basis = evaluate_region_liveness(parent, parent_env)
        if parent_live == "dead":
            return "dead", f"inside dead {parent.kind} @{parent.start_line} ({parent_basis})"
        if parent_live == "unknown" and own != "dead":
            return (
                "unknown",
                f"inside undecidable {parent.kind} @{parent.start_line} ({parent_basis})",
            )
    return own, basis


def branches_for_span(
    pa: PackageAnalysis,
    source_file: str,
    span: str,
) -> List[Dict[str, Any]]:
    """Structured live/dead/unknown decisions for non-guard regions overlapping span."""
    fa = _file_analysis_for(pa, source_file)
    lines = _parse_span_lines(span)
    if not fa or not lines:
        return []
    start, end = lines
    out: List[Dict[str, Any]] = []
    seen: Set[Tuple[int, int, str]] = set()
    for reg in fa.regions:
        if reg.is_include_guard:
            continue
        if end < reg.start_line or start > reg.end_line:
            continue
        key = (reg.start_line, reg.end_line, reg.kind)
        if key in seen:
            continue
        seen.add(key)
        # Env as of the directive line (defines that appear later do not count).
        live, basis = region_liveness(pa, fa, reg)
        cond_disp = reg.condition if reg.kind != "else" else (
            f"else of {reg.chain_kind}({reg.chain_condition[:40]})"
            if reg.chain_condition
            else "else"
        )
        out.append(
            {
                "kind": reg.kind,
                "condition": cond_disp,
                "start_line": reg.start_line,
                "end_line": reg.end_line,
                "liveness": live,
                "basis": basis,
            }
        )
    return out


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


def _json_safe_list(val: Any) -> List[Any]:
    """Normalize parquet/numpy list-ish values to a plain Python list (overwrite path)."""
    if val is None:
        return []
    if hasattr(val, "tolist") and not isinstance(val, (str, bytes, dict)):
        try:
            val = val.tolist()
        except Exception:
            pass
    if isinstance(val, list):
        return list(val)
    if isinstance(val, tuple):
        return list(val)
    return [val]


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
            live, basis = region_liveness(pa, fa, reg)
            reasons.append(f"branch_{live}:{reg.kind}({(reg.condition or reg.chain_condition or '')[:40]})")
            # config-ish if condition mentions a known define
            cond_tokens = set(re.findall(r"[A-Za-z_]\w*", reg.condition or reg.chain_condition or ""))
            for name in cond_tokens:
                if name in pa.compile_defines or name in fa.compile_defines:
                    reasons.append(f"compile_define_condition:{name}")
                if name in pa.header_defaults:
                    reasons.append(f"header_default:{name}")
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
    Overwrites ``preprocessor_*`` fields (never merges with prior parquet values).
    """
    pa = analyze_package(package_dir)
    summary: Dict[str, Any] = {
        "package": str(package_dir),
        "function_like_macros": sorted(pa.all_function_macros),
        "compile_defines": dict(pa.compile_defines),
        "header_defaults": dict(pa.header_defaults),
        "entities_flagged": 0,
        "calls_flagged": 0,
        "observations_flagged": 0,
        "trusted_calls_flagged": 0,  # is_deterministic call edges that are flagged
        "branches_live": 0,
        "branches_dead": 0,
        "branches_unknown": 0,
        "by_file": defaultdict(lambda: {"entities": 0, "calls": 0, "observations": 0}),
        "samples": [],
    }

    def _stamp_item(item: Dict[str, Any], reasons: List[str], branches: List[Dict[str, Any]]) -> None:
        # Overwrite — never merge with prior ndarray reasons from parquet.
        item["preprocessor_dependent"] = bool(reasons)
        item["preprocessor_reasons"] = list(reasons)
        # JSON-serializable list of plain dicts (parquet round-trip safe as objects/JSON).
        item["preprocessor_branches"] = [
            {
                "kind": str(b.get("kind", "")),
                "condition": str(b.get("condition", "")),
                "start_line": int(b.get("start_line") or 0),
                "end_line": int(b.get("end_line") or 0),
                "liveness": str(b.get("liveness", "unknown")),
                "basis": str(b.get("basis", "")),
            }
            for b in branches
        ]

    for e in data.get("entities") or []:
        reasons = reasons_for_span(
            pa,
            str(e.get("source_file", "")),
            str(e.get("span", "")),
            entity_type=str(e.get("type", "")),
            snippet=str(e.get("snippet") or e.get("text") or ""),
        )
        branches = branches_for_span(pa, str(e.get("source_file", "")), str(e.get("span", "")))
        _stamp_item(e, reasons, branches)
        for b in branches:
            live = b.get("liveness")
            if live == "live":
                summary["branches_live"] += 1
            elif live == "dead":
                summary["branches_dead"] += 1
            else:
                summary["branches_unknown"] += 1
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
                        "branches": branches,
                    }
                )

    for r in data.get("relationships") or []:
        callee = None
        if r.get("type") == "calls":
            tgt = str(r.get("target", ""))
            callee = tgt.split(":")[-1] if tgt else None
        reasons = reasons_for_span(
            pa,
            str(r.get("source_file", "")),
            str(r.get("span", "")),
            callee=callee,
        )
        branches = branches_for_span(pa, str(r.get("source_file", "")), str(r.get("span", "")))
        _stamp_item(r, reasons, branches)
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
                        "branches": branches,
                    }
                )

    for o in data.get("call_observations") or []:
        reasons = reasons_for_span(
            pa,
            str(o.get("source_file", "")),
            str(o.get("span", "")),
            callee=str(o.get("display_target") or ""),
        )
        branches = branches_for_span(pa, str(o.get("source_file", "")), str(o.get("span", "")))
        _stamp_item(o, reasons, branches)
        if reasons:
            summary["observations_flagged"] += 1
            sf = Path(str(o.get("source_file", ""))).name or "?"
            summary["by_file"][sf]["observations"] += 1

    summary["by_file"] = dict(summary["by_file"])
    summary["n_function_like_macros"] = len(pa.all_function_macros)
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
        f"  header defaults            : {summary.get('header_defaults') or '{}'}",
        f"  non-guard #if regions      : {summary.get('n_conditional_regions')}",
        f"  branch liveness (entity spans, non-unique): "
        f"live={summary.get('branches_live', 0)} "
        f"dead={summary.get('branches_dead', 0)} "
        f"unknown={summary.get('branches_unknown', 0)}",
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


# ---------------------------------------------------------------------------
# Compiler oracle: compare liveness labels to real `clang -E` / `cc -E` survival
# ---------------------------------------------------------------------------


def find_c_compiler() -> Optional[str]:
    """Return a C compiler path, or None if none is available."""
    import shutil

    return shutil.which("clang") or shutil.which("cc") or shutil.which("gcc")


def _load_compile_command_entries(package_dir: Path) -> List[Dict[str, Any]]:
    cc_path = package_dir / "compile_commands.json"
    if not cc_path.is_file():
        return []
    try:
        entries = json.loads(cc_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return entries if isinstance(entries, list) else []


def preprocess_command_from_entry(
    entry: Dict[str, Any], *, compiler: str, package_dir: Path
) -> Tuple[Path, List[str], Path]:
    """Build a ``compiler -E …`` argv from one compile_commands entry.

    Returns (cwd, argv, primary_source).
    """
    directory = entry.get("directory") or str(package_dir)
    cwd_raw = Path(directory)
    if cwd_raw.is_absolute() and cwd_raw.exists():
        cwd = cwd_raw.resolve()
    elif cwd_raw.exists():
        cwd = cwd_raw.resolve()
    else:
        # compile_commands in this repo use repo-relative dirs; callers pass
        # package_dir as the package root — fall back to it.
        cwd = package_dir.resolve()

    raw_cmd = entry.get("command") or ""
    if not raw_cmd and entry.get("arguments"):
        args = [str(a) for a in entry["arguments"]]
    else:
        import shlex

        args = shlex.split(raw_cmd)

    # Drop the original compiler token; rebuild with -E.
    if args and not args[0].startswith("-"):
        args = args[1:]

    cleaned: List[str] = []
    drop_flags = {"-c", "-fsyntax-only"}
    i = 0
    while i < len(args):
        a = args[i]
        if a in drop_flags:
            i += 1
            continue
        if a == "-o":
            i += 2  # skip -o and its argument
            continue
        if a.startswith("-o") and a != "-o":
            i += 1
            continue
        cleaned.append(a)
        i += 1

    src = entry.get("file") or ""
    src_path = Path(src)
    if not src_path.is_absolute():
        cand = (cwd / src_path).resolve()
        if not cand.exists():
            cand = (package_dir / Path(src).name).resolve()
        src_path = cand

    argv = [compiler, "-E", *cleaned]
    # Ensure the source path is present (some entries only list it as "file").
    joined = " ".join(cleaned)
    if src_path.name not in joined and str(src_path) not in cleaned:
        if (cwd / src_path.name).exists():
            argv.append(src_path.name)
        else:
            argv.append(str(src_path))

    return cwd, argv, src_path


def final_macro_state(
    *, compiler: str, package_dir: Path, entry: Dict[str, Any]
) -> Dict[str, str]:
    """Return the compiler's final macro table for one translation unit.

    ``-E -dM`` dumps every macro that survives preprocessing. This is what makes
    directive-only regions scoreable: a region whose body is just
    ``#define NAME value`` leaves no output line, so line survival cannot judge
    it, but the macro table says directly whether that ``#define`` ran.
    """
    import subprocess

    cwd, argv, _primary = preprocess_command_from_entry(
        entry, compiler=compiler, package_dir=package_dir
    )
    argv = [*argv[:2], "-dM", *argv[2:]]
    proc = subprocess.run(
        argv, cwd=str(cwd), capture_output=True, text=True, errors="replace"
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"preprocessor -dM failed ({proc.returncode}): {' '.join(argv)}\n"
            f"{proc.stderr[:800]}"
        )
    macros: Dict[str, str] = {}
    for line in proc.stdout.splitlines():
        m = re.match(r"^#define\s+([A-Za-z_]\w*)(?:\([^)]*\))?\s*(.*)$", line)
        if m:
            macros[m.group(1)] = m.group(2).strip()
    return macros


def _normalize_macro_body(text: str) -> str:
    """Collapse whitespace so source text compares to ``-dM`` output."""
    return " ".join(str(text).split())


def _region_defined_macros(
    src_lines: List[str], reg: ConditionalRegion
) -> List[Tuple[str, str]]:
    """``(name, replacement)`` for object-like macros defined directly in ``reg``.

    Nested conditionals are excluded: a ``#define`` one level down runs only if
    *that* region is live, which is a different question from this region's.
    """
    macros: List[Tuple[str, str]] = []
    depth = 0
    for lineno in range(reg.start_line + 1, reg.end_line + 1):
        if lineno > len(src_lines):
            break
        m = _DIR_RE.match(src_lines[lineno - 1])
        if not m:
            continue
        directive, rest = m.group(1), (m.group(2) or "").strip()
        if directive in ("if", "ifdef", "ifndef"):
            depth += 1
        elif directive == "endif":
            depth -= 1
        elif directive == "define" and depth == 0:
            om = _DEFINE_OBJ_RE.match(rest)
            if om and not _DEFINE_FUNC_RE.match(rest):
                name = om.group(1)
                macros.append((name, _normalize_macro_body(rest[len(name) :])))
    return macros


def surviving_source_lines(
    *,
    compiler: str,
    package_dir: Path,
    entry: Dict[str, Any],
    package_files: Optional[Set[str]] = None,
) -> Dict[str, Set[int]]:
    """Return basename -> set of original source line numbers that survive -E.

    Uses the preprocessor's ``# linenum "file"`` markers — not a second
    implementation of our liveness rules. Only files under ``package_dir``
    (or listed in ``package_files`` basenames) are retained.
    """
    import subprocess

    cwd, argv, _primary = preprocess_command_from_entry(
        entry, compiler=compiler, package_dir=package_dir
    )
    proc = subprocess.run(
        argv,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"preprocessor failed ({proc.returncode}): {' '.join(argv)}\n"
            f"{proc.stderr[:800]}"
        )

    pkg_resolved = package_dir.resolve()
    if package_files is None:
        package_files = {
            p.name
            for p in pkg_resolved.rglob("*")
            if p.suffix in {".c", ".h"} and p.is_file()
        }

    survived: Dict[str, Set[int]] = defaultdict(set)
    cur_file: Optional[str] = None
    cur_line = 0
    in_package = False

    for line in proc.stdout.splitlines():
        m = re.match(r'^#\s+(\d+)\s+"([^"]*)"', line)
        if m:
            cur_line = int(m.group(1))
            cur_file = m.group(2)
            # package file?
            name = Path(cur_file).name
            # absolute path under package, or basename match for local includes
            try:
                cpath = Path(cur_file)
                if cpath.is_absolute():
                    in_package = (
                        pkg_resolved in cpath.resolve().parents
                        or cpath.resolve() == pkg_resolved
                        or name in package_files
                    )
                else:
                    in_package = name in package_files
            except Exception:
                in_package = name in package_files
            continue

        if not in_package or cur_file is None:
            continue

        name = Path(cur_file).name
        # Every retained output line (including blanks) advances the line
        # counter; only non-empty lines count as "surviving content".
        if line.strip():
            survived[name].add(cur_line)
        cur_line += 1

    return dict(survived)


def _region_body_content_lines(source_lines: List[str], reg: ConditionalRegion) -> List[int]:
    """Non-directive, non-empty source lines inside a region's closed span."""
    body: List[int] = []
    # start_line is the opening directive; body begins on the next line.
    for ln in range(reg.start_line + 1, reg.end_line + 1):
        if ln < 1 or ln > len(source_lines):
            continue
        text = source_lines[ln - 1]
        if _DIR_RE.match(text):
            continue
        if not text.strip():
            continue
        body.append(ln)
    return body


def _unknown_macro_families(reg: ConditionalRegion) -> List[str]:
    """Token families mentioned in a region condition (for unknown-rate reporting)."""
    text = f"{reg.condition} {reg.chain_condition}"
    tokens = re.findall(r"[A-Za-z_]\w*", text)
    families: List[str] = []
    for t in tokens:
        if t in _PLATFORM_MACROS or t.startswith("__") or t.startswith("_"):
            families.append(f"platform:{t}")
        elif t.startswith("INI_"):
            families.append("INI_*")
        elif t.startswith("CJSON_") or t.startswith("cJSON_"):
            families.append("CJSON_*")
        elif t.startswith("JSMN_"):
            families.append("JSMN_*")
        elif t in {"defined", "if", "ifdef", "ifndef", "else", "elif", "endif"}:
            continue
        else:
            families.append(t)
    # unique preserve order
    seen: Set[str] = set()
    out: List[str] = []
    for f in families:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def compare_liveness_to_compiler(
    package_dir: Path,
    *,
    compiler: Optional[str] = None,
) -> Dict[str, Any]:
    """Compare diagnostic live/dead labels to real preprocessor line survival.

    * ``live`` regions with non-directive body lines must have ≥1 survivor.
    * ``dead`` regions must have 0 survivors among body lines.
    * ``unknown`` is **not** scored as agreement or error — only reported.

    Returns a machine-readable report. Raises RuntimeError if the compiler
    invocation fails.
    """
    package_dir = package_dir.resolve()
    compiler = compiler or find_c_compiler()
    if not compiler:
        raise FileNotFoundError("no C compiler (clang/cc/gcc) on PATH")

    entries = _load_compile_command_entries(package_dir)
    if not entries:
        raise FileNotFoundError(f"no compile_commands.json under {package_dir}")

    pa = analyze_package(package_dir)

    # Union survival maps across all compile_commands entries.
    package_names = {
        p.name
        for p in package_dir.rglob("*")
        if p.suffix in {".c", ".h"} and p.is_file()
    }
    survived: Dict[str, Set[int]] = defaultdict(set)
    macro_state: Dict[str, str] = {}
    preprocess_cmds: List[str] = []
    files_in_tu: Set[str] = set()
    for ent in entries:
        if not isinstance(ent, dict):
            continue
        primary = Path(str(ent.get("file") or "")).name
        if primary:
            files_in_tu.add(primary)
        part = surviving_source_lines(
            compiler=compiler,
            package_dir=package_dir,
            entry=ent,
            package_files=package_names,
        )
        for name, lines in part.items():
            survived[name].update(lines)
            files_in_tu.add(name)
        macro_state.update(
            final_macro_state(compiler=compiler, package_dir=package_dir, entry=ent)
        )
        _cwd, argv, _ = preprocess_command_from_entry(
            ent, compiler=compiler, package_dir=package_dir
        )
        preprocess_cmds.append(" ".join(argv))

    # Source text cache
    source_cache: Dict[str, List[str]] = {}

    def source_lines_for(path: Path) -> List[str]:
        key = str(path.resolve())
        if key not in source_cache:
            source_cache[key] = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return source_cache[key]

    agreements: List[Dict[str, Any]] = []
    disagreements: List[Dict[str, Any]] = []
    unknowns: List[Dict[str, Any]] = []
    vacuous: List[Dict[str, Any]] = []
    skipped_not_in_tu = 0
    empty_bodies = 0
    family_counter: Dict[str, int] = defaultdict(int)

    seen_fa: Set[int] = set()
    for fa in pa.files.values():
        if id(fa) in seen_fa:
            continue
        seen_fa.add(id(fa))
        path = Path(fa.path)
        if not path.exists():
            continue
        basename = path.name
        # Only score files that participate in a compile_commands translation
        # unit (primary source or #include'd package headers). Otherwise a
        # live label on tests.c would "fail" because we never preprocessed it.
        if basename not in files_in_tu and basename not in survived:
            skipped_not_in_tu += sum(1 for r in fa.regions if not r.is_include_guard)
            continue
        src_lines = source_lines_for(path)
        file_surv = survived.get(basename, set())
        # Every replacement text each object-like macro gets in this file, so a
        # region can tell whether its own value identifies it uniquely.
        file_macro_values: Dict[str, List[str]] = defaultdict(list)
        for other in fa.regions:
            if other.is_include_guard:
                continue
            for name, value in _region_defined_macros(src_lines, other):
                file_macro_values[name].append(value)

        for reg in fa.regions:
            if reg.is_include_guard:
                continue
            live, basis = region_liveness(pa, fa, reg)
            body = _region_body_content_lines(src_lines, reg)
            survivors = [ln for ln in body if ln in file_surv]
            rec = {
                "file": basename,
                "kind": reg.kind,
                "condition": (reg.condition or reg.chain_condition or "")[:80],
                "start_line": reg.start_line,
                "end_line": reg.end_line,
                "label": live,
                "basis": basis,
                "body_lines": body,
                "surviving_body_lines": survivors,
            }
            if live == "unknown":
                fams = _unknown_macro_families(reg)
                rec["macro_families"] = fams
                for f in fams:
                    family_counter[f] += 1
                unknowns.append(rec)
                continue

            if not body:
                # Line survival cannot judge a directive-only region: a
                # `#define` emits nothing either way. Ask the macro table
                # instead — that scores the `#ifndef X`/`#define X v` idiom,
                # which is most of the config surface in these packages.
                defined_here = _region_defined_macros(src_lines, reg)
                # A name defined by several mutually exclusive branches (INI_API,
                # JSMN_API) cannot be attributed by presence — only the *value*
                # says which branch ran, and only when this region's value is
                # unique among its siblings.
                usable = [
                    (name, value)
                    for name, value in defined_here
                    if sum(
                        1
                        for other_value in file_macro_values.get(name, ())
                        if other_value == value
                    )
                    == 1
                ]
                if usable and macro_state:
                    rec["macro_evidence"] = {
                        name: {"expected": value, "final": macro_state.get(name)}
                        for name, value in usable
                    }
                    matched = [
                        name in macro_state
                        and _normalize_macro_body(macro_state[name]) == value
                        for name, value in usable
                    ]
                    ok = all(matched) if live == "live" else not any(matched)
                    rec["note"] = "macro_state"
                    (agreements if ok else disagreements).append(rec)
                    continue
                # Genuinely unscoreable: nothing observable either way. Counted
                # separately — folding these into agreements would have made
                # cJSON read 15/15 when only 5 regions were really checked.
                empty_bodies += 1
                vacuous.append({**rec, "note": "empty_body_unscoreable"})
                continue

            if live == "live":
                ok = len(survivors) >= 1
            else:  # dead
                ok = len(survivors) == 0

            if ok:
                agreements.append(rec)
            else:
                disagreements.append(rec)

    n_scored = len(agreements) + len(disagreements)
    n_unknown = len(unknowns)
    n_vacuous = len(vacuous)
    n_total = n_scored + n_unknown + n_vacuous
    by_evidence: Dict[str, int] = defaultdict(int)
    for rec in agreements:
        by_evidence[str(rec.get("note") or "line_survival")] += 1
    report = {
        "package": str(package_dir),
        "compiler": compiler,
        "preprocess_commands": preprocess_cmds,
        "files_in_translation_units": sorted(files_in_tu),
        "regions_total": n_total,
        "regions_scored": n_scored,
        "regions_unknown": n_unknown,
        "regions_skipped_not_in_tu": skipped_not_in_tu,
        "unknown_rate": (n_unknown / n_total) if n_total else 0.0,
        "agreements": len(agreements),
        "disagreements": len(disagreements),
        # Regions nothing can judge: directive-only bodies that define no macro.
        # Kept out of `regions_scored` so the agreement rate means what it says.
        "regions_vacuous": n_vacuous,
        "empty_body_regions": empty_bodies,
        "agreement_evidence": dict(by_evidence),
        "agreement_rate_scored": (len(agreements) / n_scored) if n_scored else 1.0,
        "unknown_macro_families": dict(
            sorted(family_counter.items(), key=lambda kv: (-kv[1], kv[0]))
        ),
        "disagreement_details": disagreements[:40],
        "unknown_samples": unknowns[:20],
        "vacuous_samples": vacuous[:20],
        "ok": len(disagreements) == 0,
    }
    return report


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
    ap.add_argument(
        "--vs-compiler",
        action="store_true",
        help="compare liveness labels to real clang/cc -E line survival "
        "(requires a C compiler; does not modify graphs)",
    )
    args = ap.parse_args(argv)

    pkg = args.package.resolve()

    if args.vs_compiler:
        report = compare_liveness_to_compiler(pkg)
        if args.json:
            print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        else:
            print(f"compiler oracle: {report['package']}")
            print(f"  compiler              : {report['compiler']}")
            print(f"  regions total         : {report['regions_total']}")
            print(f"  scored (live/dead)    : {report['regions_scored']}")
            print(f"  unknown (unscored)    : {report['regions_unknown']} "
                  f"({100.0 * report['unknown_rate']:.1f}%)")
            print(f"  vacuous (unscoreable) : {report['regions_vacuous']}")
            print(f"  agreements            : {report['agreements']} "
                  f"{report['agreement_evidence']}")
            print(f"  disagreements         : {report['disagreements']}")
            print(f"  agreement rate (scored): {100.0 * report['agreement_rate_scored']:.1f}%")
            print(f"  unknown macro families: {report['unknown_macro_families']}")
            if report["disagreement_details"]:
                print("  disagreements (sample):")
                for d in report["disagreement_details"][:15]:
                    print(
                        f"    {d['file']}:{d['start_line']}-{d['end_line']} "
                        f"{d['kind']} label={d['label']} "
                        f"survivors={d['surviving_body_lines']} "
                        f"cond={d['condition']!r}"
                    )
            print(f"  ok                    : {report['ok']}")
        raise SystemExit(0 if report["ok"] else 1)

    from extract_c import build_c_byog  # type: ignore

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
        print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    else:
        print(f"analysed: {source_desc}")
        print(format_report(summary, totals=totals))


if __name__ == "__main__":
    main()
