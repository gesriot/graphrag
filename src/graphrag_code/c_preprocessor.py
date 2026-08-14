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
import math
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
    # Toolchain built-ins from ``compiler -E -dM`` (empty TU), when available.
    # Name -> replacement text. Seeded only when eval_mode is compiler_builtins.
    compiler_builtins: Dict[str, str] = field(default_factory=dict)
    # How liveness was evaluated for this analysis.
    # "compiler_builtins" | "no_compiler"
    eval_mode: str = "no_compiler"
    compiler_path: Optional[str] = None
    # Names the real translation unit ends up with that no package `#define`
    # can account for — i.e. they came from an `#include`. Empty-TU builtins
    # cannot see these, so without them `#ifndef NAN` reads "undefined" when
    # math.h has in fact defined it. Name -> replacement text.
    include_macros: Dict[str, str] = field(default_factory=dict)
    # Host/toolchain fingerprint for reproducibility (filled when eval runs).
    compiler_id: Optional[str] = None  # first line of `compiler --version`
    compiler_version: Optional[str] = None
    # SHA-256 (hex, truncated) of the macro seed table that drove liveness.
    macro_seed_digest: Optional[str] = None


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


def fetch_compiler_builtins(
    compiler: Optional[str] = None,
    *,
    extra_flags: Optional[Sequence[str]] = None,
) -> Dict[str, str]:
    """Return object-like macros from ``compiler -E -dM`` on an empty TU.

    This is the toolchain's built-in table (``__GNUC__``, ``__APPLE__``, …),
    not our own rules. Returns {} if no compiler is available or the probe fails.
    """
    import subprocess

    compiler = compiler or find_c_compiler()
    if not compiler:
        return {}
    # Empty translation unit: stdin as C source.
    argv = [compiler, "-E", "-dM", "-x", "c", *(extra_flags or ()), "-"]
    try:
        proc = subprocess.run(
            argv,
            input="",
            capture_output=True,
            text=True,
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if proc.returncode != 0:
        return {}
    out: Dict[str, str] = {}
    for line in proc.stdout.splitlines():
        # #define NAME replacement
        m = re.match(r"^\s*#\s*define\s+([A-Za-z_]\w*)(\([^\)]*\))?\s*(.*)$", line)
        if not m:
            continue
        name, params, body = m.group(1), m.group(2), (m.group(3) or "").strip()
        if params:
            # function-like builtin — skip; our evaluator is object-like only
            continue
        out[name] = body
    return out


def _compiler_identity(compiler: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (id_line, version_string) from ``compiler --version``."""
    import subprocess

    try:
        proc = subprocess.run(
            [compiler, "--version"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    lines = [ln.strip() for ln in (proc.stdout or proc.stderr or "").splitlines() if ln.strip()]
    if not lines:
        return None, None
    id_line = lines[0]
    # Prefer a version-looking token if present.
    ver = None
    m = re.search(r"(\d+\.\d+(?:\.\d+)?)", id_line)
    if m:
        ver = m.group(1)
    return id_line, ver


def macro_seed_digest(
    *,
    eval_mode: str,
    compile_defines: Dict[str, Optional[str]],
    header_defaults: Dict[str, Any],
    compiler_builtins: Optional[Dict[str, str]] = None,
    include_macros: Optional[Dict[str, str]] = None,
) -> str:
    """Stable hash of everything that can change liveness answers.

    ``no_compiler`` digests only package-local inputs (host-independent).
    ``compiler_builtins`` also hashes toolchain builtins + include-attributed
    macros so a different host cannot silently claim the same stamp.
    """
    import hashlib

    def _hd_items() -> List[Tuple[str, str]]:
        items: List[Tuple[str, str]] = []
        for k, v in sorted((header_defaults or {}).items()):
            if isinstance(v, tuple):
                items.append((k, str(v[0])))
            else:
                items.append((k, str(v)))
        return items

    payload: Dict[str, Any] = {
        "eval_mode": eval_mode,
        "compile_defines": {k: compile_defines[k] for k in sorted(compile_defines or {})},
        "header_defaults": _hd_items(),
    }
    if eval_mode == "compiler_builtins":
        payload["compiler_builtins"] = {
            k: (compiler_builtins or {})[k]
            for k in sorted(compiler_builtins or {})
        }
        payload["include_macros"] = {
            k: (include_macros or {})[k] for k in sorted(include_macros or {})
        }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def build_liveness_provenance(pa: PackageAnalysis) -> Dict[str, Any]:
    """Machine-readable record of how liveness labels were produced."""
    return {
        "eval_mode": pa.eval_mode,
        "compiler_path": pa.compiler_path,
        "compiler_id": pa.compiler_id,
        "compiler_version": pa.compiler_version,
        "macro_seed_digest": pa.macro_seed_digest,
        "n_compiler_builtins": len(pa.compiler_builtins or {}),
        "n_include_macros": len(pa.include_macros or {}),
        "host_independent": pa.eval_mode == "no_compiler",
    }


def analyze_package(
    package_dir: Path,
    *,
    use_compiler_builtins: bool = False,
    compiler: Optional[str] = None,
) -> PackageAnalysis:
    """Analyse a C package for preprocessor structure and weak liveness.

    ``use_compiler_builtins``:
      * ``False`` (default) — never query the compiler (``eval_mode=no_compiler``).
        Platform macros stay ``unknown``. **This is the publish default:**
        host-independent, reproducible labels.
      * ``True`` — seed from ``compiler -E -dM`` builtins (+ include-attributed
        macros when collectable). Labels become host-specific; the toolchain
        fingerprint and macro-seed digest are recorded so a stamp is never silent.

    The chosen mode and digest are always recorded on the analysis object.
    """
    package_dir = Path(package_dir).resolve()
    cc = parse_compile_commands(package_dir)
    package_defines = cc.get("*", {})

    compiler = compiler or find_c_compiler()
    builtins: Dict[str, str] = {}
    compiler_id: Optional[str] = None
    compiler_version: Optional[str] = None
    if use_compiler_builtins:
        if compiler:
            builtins = fetch_compiler_builtins(compiler)
            eval_mode = "compiler_builtins" if builtins else "no_compiler"
            if builtins:
                compiler_id, compiler_version = _compiler_identity(compiler)
        else:
            eval_mode = "no_compiler"
        if use_compiler_builtins and eval_mode != "compiler_builtins":
            # Explicit request but probe failed — stay honest.
            eval_mode = "no_compiler"
            builtins = {}
            compiler_id, compiler_version = None, None
    else:
        eval_mode = "no_compiler"

    pa = PackageAnalysis(
        package_dir=package_dir,
        compile_defines=dict(package_defines),
        compiler_builtins=dict(builtins),
        eval_mode=eval_mode,
        compiler_path=compiler if eval_mode == "compiler_builtins" else None,
        compiler_id=compiler_id,
        compiler_version=compiler_version,
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
    if eval_mode == "compiler_builtins" and compiler:
        pa.include_macros = _collect_include_macros(pa, cc, compiler=compiler)
    pa.macro_seed_digest = macro_seed_digest(
        eval_mode=pa.eval_mode,
        compile_defines=pa.compile_defines,
        header_defaults=pa.header_defaults,
        compiler_builtins=pa.compiler_builtins,
        include_macros=pa.include_macros,
    )
    return pa


def _collect_include_macros(
    pa: PackageAnalysis,
    cc: Dict[str, Dict[str, Optional[str]]],
    *,
    compiler: str,
) -> Dict[str, str]:
    """Macros the real translation units get from ``#include``, not from us.

    Empty-TU builtins miss everything the headers pull in, so a name like
    ``NAN`` reads "undefined" and its ``#ifndef`` block reads live even though
    ``math.h`` defined it first. Running ``-E -dM`` on the actual compile
    command sees those, but its output also contains our own ``#define``\\ s —
    so a name is only attributed to an include when **no** package definition
    of it matches the final replacement text. That keeps the inference
    non-circular: our own macros can never make themselves look external.
    """
    package_defs: Dict[str, Set[str]] = defaultdict(set)
    for key, fa in pa.files.items():
        if not key.endswith((".c", ".h")):
            continue
        for mac in fa.macros:
            body = mac.body_preview[len(mac.name) :]
            if mac.function_like:
                body = re.sub(r"^\([^)]*\)", "", body)
            package_defs[mac.name].add(_normalize_macro_body(body))

    entries = _load_compile_command_entries(pa.package_dir)
    final: Dict[str, str] = {}
    for ent in entries:
        if not isinstance(ent, dict):
            continue
        try:
            final.update(
                final_macro_state(
                    compiler=compiler, package_dir=pa.package_dir, entry=ent
                )
            )
        except (RuntimeError, OSError):
            continue  # a TU we cannot preprocess simply contributes nothing

    external: Dict[str, str] = {}
    for name, value in final.items():
        if name in pa.compiler_builtins:
            continue  # already modelled as a toolchain builtin
        normalized = _normalize_macro_body(value)
        if normalized in package_defs.get(name, ()):
            continue  # could be ours; refuse to guess
        external[name] = value
    return external


def _defined_env(
    pa: PackageAnalysis,
    fa: Optional[FileAnalysis],
    *,
    before_line: Optional[int] = None,
) -> Dict[str, Optional[str]]:
    """Merge builtins, header defaults, compile -D, and file-local object defines.

    Precedence (highest last):
      compiler_builtins < header_defaults < package -D < file -D <
      in-file ``#define`` that appear *before* ``before_line`` (if given).

    Using only prior in-file defines avoids treating a later ``#define true``
    as already visible to an earlier ``#ifdef true``.

    Header defaults obey the same ordering within their own file, so the
    ``#ifndef X`` that establishes a default still evaluates as live rather than
    being falsified by the ``#define X`` it guards.
    """
    env: Dict[str, Optional[str]] = {}
    # Toolchain builtins first (overridden by -D and source).
    for k, v in (pa.compiler_builtins or {}).items():
        env[k] = v
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


def _define_provenance(
    pa: PackageAnalysis,
    fa: Optional[FileAnalysis],
    name: str,
    *,
    before_line: Optional[int] = None,
) -> Optional[str]:
    """Where ``name`` is currently visible from, for basis strings."""
    # Highest precedence first (mirror _defined_env overrides).
    if fa is not None:
        for line, n, val in reversed(fa.object_defines_at or []):
            if n != name:
                continue
            if before_line is not None and line >= before_line:
                continue
            if _enclosing_regions(fa, line):
                continue
            return f"source:{name}={val!r}@{line}"
        if name in (fa.compile_defines or {}):
            v = fa.compile_defines[name]
            return f"compile_d:{name}={v!r}"
    if name in (pa.compile_defines or {}):
        v = pa.compile_defines[name]
        return f"compile_d:{name}={v!r}"
    if name in (pa.header_defaults or {}):
        value, def_file, def_line = pa.header_defaults[name]
        return f"header_default:{name}={value!r}"
    if name in (pa.compiler_builtins or {}):
        return f"builtin:{name}={pa.compiler_builtins[name]!r}"
    return None


def _enclosing_regions(fa: FileAnalysis, line: int) -> List[ConditionalRegion]:
    """Non-guard conditional regions containing ``line``."""
    return [
        reg
        for reg in fa.regions
        if not reg.is_include_guard and reg.start_line <= line <= reg.end_line
    ]


def _is_defined(
    name: str,
    env: Dict[str, Optional[str]],
    *,
    pa: Optional["PackageAnalysis"] = None,
) -> Optional[bool]:
    """True/False if decidable; None if we must not invent an answer.

    * Name present in ``env`` (builtins / -D / headers / prior source) → defined.
    * Platform name under ``compiler_builtins`` and absent from env →
      **undefined** (empty-TU probe did not define it for this toolchain).
    * Platform name under ``no_compiler`` → **unknown** (do not invent a host).
    * Name the real translation unit gets from an ``#include`` → **defined**.
      Empty-TU builtins cannot see these, and calling them undefined produced
      confidently wrong labels: ``#ifndef NAN`` / ``isinf`` / ``isnan`` in
      ``cJSON.c`` read *live* when ``math.h`` had already defined all three.
    * Any other absent name → **undefined** (ordinary C rules for project ids).
    """
    if name in env:
        return True
    if pa is not None and name in getattr(pa, "include_macros", {}):
        return True
    if name in _PLATFORM_MACROS:
        if pa is not None and getattr(pa, "eval_mode", "no_compiler") == "compiler_builtins":
            return False
        return None
    return False


def _name_basis(
    pa: Optional["PackageAnalysis"],
    fa: Optional[FileAnalysis],
    name: str,
    *,
    before_line: Optional[int],
    fallback: str,
) -> str:
    """Prefer structured provenance (builtin:…) over a plain fallback string."""
    if pa is None:
        return fallback
    prov = _define_provenance(pa, fa, name, before_line=before_line)
    if prov and prov.startswith("builtin:"):
        return prov
    if prov and prov.startswith("compile_d:"):
        return prov
    if prov and prov.startswith("header_default:"):
        return prov
    return fallback


def _eval_primary(
    expr: str,
    env: Dict[str, Optional[str]],
    *,
    pa: Optional["PackageAnalysis"] = None,
    fa: Optional[FileAnalysis] = None,
    before_line: Optional[int] = None,
) -> Tuple[Optional[bool], str]:
    """Evaluate a stripped primary condition fragment. None = unknown."""
    e = expr.strip()
    if not e:
        return None, "empty condition"
    # defined(X) / defined X / !defined(X) / !defined X
    m = re.fullmatch(r"defined\s*(?:\(\s*([A-Za-z_]\w*)\s*\)|([A-Za-z_]\w*))", e)
    if m:
        name = m.group(1) or m.group(2)
        d = _is_defined(name, env, pa=pa)
        if d is None:
            return None, f"platform macro {name} not in compile -D/builtins"
        if d and name in env:
            fb = f"defined({name})={'yes'}"
            return True, _name_basis(pa, fa, name, before_line=before_line, fallback=fb)
        if d:
            return True, f"defined({name})=yes"
        # absent after builtins probe
        if pa is not None and pa.eval_mode == "compiler_builtins" and name in _PLATFORM_MACROS:
            return False, f"defined({name})=no (absent from builtins)"
        return False, f"defined({name})=no"
    m = re.fullmatch(r"!\s*defined\s*(?:\(\s*([A-Za-z_]\w*)\s*\)|([A-Za-z_]\w*))", e)
    if m:
        name = m.group(1) or m.group(2)
        d = _is_defined(name, env, pa=pa)
        if d is None:
            return None, f"platform macro {name} not in compile -D/builtins"
        if not d:
            if pa is not None and pa.eval_mode == "compiler_builtins" and name in _PLATFORM_MACROS:
                return True, f"!defined({name})=yes (absent from builtins)"
            return True, f"!defined({name})=yes"
        fb = f"!defined({name})=no"
        return False, _name_basis(pa, fa, name, before_line=before_line, fallback=fb)
    # bare identifier (common for INI_USE_STACK style #if X)
    m = re.fullmatch(r"([A-Za-z_]\w*)", e)
    if m:
        name = m.group(1)
        if name in env:
            val = env[name]
            # defined with empty or non-zero token → true; 0 → false
            if val is None or str(val).strip() == "":
                fb = f"{name} defined (empty/flag)"
                return True, _name_basis(pa, fa, name, before_line=before_line, fallback=fb)
            tok = str(val).strip().split()[0]
            if re.fullmatch(r"0[xX]?[0-9a-fA-F]*", tok) or tok == "0":
                fb = f"{name}={tok}"
                return False, _name_basis(pa, fa, name, before_line=before_line, fallback=fb)
            if re.fullmatch(r"[0-9]+", tok) or re.fullmatch(r"0[xX][0-9a-fA-F]+", tok):
                fb = f"{name}={tok}"
                return (int(tok, 0) != 0), _name_basis(
                    pa, fa, name, before_line=before_line, fallback=fb
                )
            # non-numeric replacement (string, expression) → unknown
            return None, f"{name} has non-numeric value {tok!r}"
        if name in _PLATFORM_MACROS:
            d = _is_defined(name, env, pa=pa)
            if d is None:
                return None, f"platform macro {name} not in compile -D/builtins"
            # compiler_builtins mode: absent ⇒ 0
            return False, f"{name} undefined (absent from builtins) → 0"
        # not defined → 0 in #if
        return False, f"{name} undefined → 0"
    # unary !
    if e.startswith("!"):
        inner, basis = _eval_primary(
            e[1:].strip(), env, pa=pa, fa=fa, before_line=before_line
        )
        if inner is None:
            return None, basis
        return (not inner), f"!({basis})"
    # parentheses
    if e.startswith("(") and e.endswith(")"):
        return _eval_expr(e[1:-1], env, pa=pa, fa=fa, before_line=before_line)
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


def _eval_expr(
    expr: str,
    env: Dict[str, Optional[str]],
    *,
    pa: Optional["PackageAnalysis"] = None,
    fa: Optional[FileAnalysis] = None,
    before_line: Optional[int] = None,
) -> Tuple[Optional[bool], str]:
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
            v, b = _eval_expr(p, env, pa=pa, fa=fa, before_line=before_line)
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
            v, b = _eval_expr(p, env, pa=pa, fa=fa, before_line=before_line)
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

    return _eval_primary(e, env, pa=pa, fa=fa, before_line=before_line)


def evaluate_region_liveness(
    reg: ConditionalRegion,
    env: Dict[str, Optional[str]],
    *,
    pa: Optional["PackageAnalysis"] = None,
    fa: Optional[FileAnalysis] = None,
    before_line: Optional[int] = None,
) -> Tuple[Liveness, str]:
    """Return (live|dead|unknown, basis) for a region under define env."""
    kind = reg.kind
    cond = (reg.condition or "").strip()
    bl = before_line if before_line is not None else reg.start_line

    if kind == "ifdef":
        name = cond.split()[0] if cond else ""
        d = _is_defined(name, env, pa=pa)
        if d is None:
            return "unknown", f"platform macro {name} not in compile -D/builtins"
        if d:
            fb = f"ifdef({name}) → defined"
            return "live", _name_basis(pa, fa, name, before_line=bl, fallback=fb)
        if pa is not None and pa.eval_mode == "compiler_builtins" and name in _PLATFORM_MACROS:
            return "dead", f"ifdef({name}) → undefined (absent from builtins)"
        return "dead", f"ifdef({name}) → undefined"

    if kind == "ifndef":
        name = cond.split()[0] if cond else ""
        d = _is_defined(name, env, pa=pa)
        if d is None:
            return "unknown", f"platform macro {name} not in compile -D/builtins"
        if not d:
            if pa is not None and pa.eval_mode == "compiler_builtins" and name in _PLATFORM_MACROS:
                return "live", f"ifndef({name}) → undefined (absent from builtins)"
            return "live", f"ifndef({name}) → undefined"
        fb = f"ifndef({name}) → defined"
        return "dead", _name_basis(pa, fa, name, before_line=bl, fallback=fb)

    if kind in ("if", "elif"):
        val, basis = _eval_expr(cond, env, pa=pa, fa=fa, before_line=bl)
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
        parent_live, basis = evaluate_region_liveness(
            pseudo, env, pa=pa, fa=fa, before_line=bl
        )
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
    own, basis = evaluate_region_liveness(
        reg, env, pa=pa, fa=fa, before_line=reg.start_line
    )
    for parent in _enclosing_regions(fa, reg.start_line):
        if parent.start_line >= reg.start_line and parent.end_line <= reg.end_line:
            continue  # itself, or a sibling arm of the same chain
        parent_env = _defined_env(pa, fa, before_line=parent.start_line)
        parent_live, parent_basis = evaluate_region_liveness(
            parent, parent_env, pa=pa, fa=fa, before_line=parent.start_line
        )
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
                "eval_mode": pa.eval_mode,
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
            cond_tokens = sorted(
                set(
                    re.findall(
                        r"[A-Za-z_]\w*",
                        reg.condition or reg.chain_condition or "",
                    )
                )
            )
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


class ToolchainDriftError(RuntimeError):
    """Re-stamp would change host-specific liveness relative to a prior stamp."""


def read_graph_liveness_provenance(graph_dir: Path) -> Optional[Dict[str, Any]]:
    """Load ``preprocessor_liveness`` from the published snapshot manifest, if any."""
    graph_dir = Path(graph_dir)
    current = graph_dir / "current"
    if not current.is_file():
        return None
    try:
        snap_id = current.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    man_path = graph_dir / "snapshots" / snap_id / "manifest.json"
    if not man_path.is_file():
        return None
    try:
        man = json.loads(man_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    prov = man.get("preprocessor_liveness")
    return prov if isinstance(prov, dict) else None


def check_liveness_stamp_freshness(
    graph_dir: Path,
    package_dir: Path,
    *,
    use_compiler_builtins: bool = False,
    compiler: Optional[str] = None,
) -> Dict[str, Any]:
    """Compare a graph's recorded liveness provenance to a fresh analysis.

    Returns a report with ``ok`` / ``status``:
      * ``ok`` — no prior record, or digests match
      * ``drift`` — prior stamp's macro seed / mode differs from this host
      * ``missing`` — no prior preprocessor_liveness in the manifest

    Does not modify the graph.
    """
    prior = read_graph_liveness_provenance(graph_dir)
    pa = analyze_package(
        package_dir,
        use_compiler_builtins=use_compiler_builtins,
        compiler=compiler,
    )
    current = build_liveness_provenance(pa)
    if prior is None:
        return {
            "ok": True,
            "status": "missing",
            "prior": None,
            "current": current,
            "message": "no preprocessor_liveness in snapshot manifest",
        }
    prior_mode = prior.get("eval_mode")
    prior_digest = prior.get("macro_seed_digest")
    if prior_mode == current["eval_mode"] and prior_digest == current["macro_seed_digest"]:
        return {
            "ok": True,
            "status": "match",
            "prior": prior,
            "current": current,
            "message": "macro seed digest matches recorded stamp",
        }
    return {
        "ok": False,
        "status": "drift",
        "prior": prior,
        "current": current,
        "message": (
            f"liveness stamp drift: recorded eval_mode={prior_mode!r} "
            f"digest={prior_digest!r}; this host would produce "
            f"eval_mode={current['eval_mode']!r} digest={current['macro_seed_digest']!r} "
            f"(compiler_id={current.get('compiler_id')!r}). "
            f"Re-stamp with an explicit policy change or --allow-toolchain-drift."
        ),
    }


def annotate_byog(
    data: Dict[str, List[Dict[str, Any]]],
    package_dir: Path,
    *,
    use_compiler_builtins: bool = False,
    graph_dir: Optional[Path] = None,
    allow_toolchain_drift: bool = False,
) -> Dict[str, Any]:
    """Stamp preprocessor provenance onto entities, relationships, observations.

    Does **not** flip ``is_deterministic`` or drop edges.
    Overwrites ``preprocessor_*`` fields (never merges with prior parquet values).

    Default ``use_compiler_builtins=False`` (``no_compiler``): host-independent
    labels for published artifacts. Pass ``True`` for local host-specific
    analysis; the toolchain fingerprint and macro-seed digest are recorded in
    the analysis and (via the publisher) the snapshot manifest. When
    ``graph_dir`` is set, a digest/mode mismatch against a prior recorded
    stamp raises ``ToolchainDriftError`` unless ``allow_toolchain_drift=True``
    — so a different host cannot quietly rewrite host-specific labels, and a
    mode switch cannot look like a silent re-stamp.
    """
    if graph_dir is not None and not allow_toolchain_drift:
        freshness = check_liveness_stamp_freshness(
            graph_dir,
            package_dir,
            use_compiler_builtins=use_compiler_builtins,
        )
        prior = freshness.get("prior") or {}
        # Any recorded digest/mode that would not match this re-stamp is a
        # policy or host change — refuse rather than quietly relabel.
        if freshness["status"] == "drift" and prior.get("macro_seed_digest"):
            raise ToolchainDriftError(freshness["message"])

    pa = analyze_package(
        package_dir, use_compiler_builtins=use_compiler_builtins
    )
    liveness_prov = build_liveness_provenance(pa)
    summary: Dict[str, Any] = {
        "package": str(package_dir),
        "function_like_macros": sorted(pa.all_function_macros),
        "compile_defines": dict(pa.compile_defines),
        "header_defaults": {
            k: v[0] if isinstance(v, tuple) else v
            for k, v in (pa.header_defaults or {}).items()
        },
        "eval_mode": pa.eval_mode,
        "compiler_path": pa.compiler_path,
        "n_compiler_builtins": len(pa.compiler_builtins or {}),
        "liveness_provenance": liveness_prov,
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
        item["preprocessor_eval_mode"] = pa.eval_mode
        item["preprocessor_macro_seed_digest"] = pa.macro_seed_digest
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
        f"  eval_mode                  : {summary.get('eval_mode', 'no_compiler')} "
        f"(builtins={summary.get('n_compiler_builtins', 0)}, "
        f"digest={(summary.get('liveness_provenance') or {}).get('macro_seed_digest')}, "
        f"host_independent={(summary.get('liveness_provenance') or {}).get('host_independent')})",
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
    from graphrag_code.byog_graph import ByogGraph  # type: ignore

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


def resolve_compile_entry_cwd(entry: Dict[str, Any], package_dir: Path) -> Path:
    """Resolve the working directory for one compile_commands entry.

    Absolute paths are preserved so a missing recorded directory fails at the
    compiler boundary rather than silently running elsewhere. Relative paths
    are tried from the process CWD and from ``package_dir``; repo-relative
    entries whose suffix names ``package_dir`` resolve to the package itself.
    """
    directory = entry.get("directory") or str(package_dir)
    cwd_raw = Path(directory)
    package_dir = Path(package_dir).resolve()
    if cwd_raw.is_absolute():
        return cwd_raw.resolve()
    if cwd_raw.exists():
        return cwd_raw.resolve()
    package_relative = (package_dir / cwd_raw).resolve()
    if package_relative.exists():
        return package_relative
    parts = cwd_raw.parts
    if parts and tuple(package_dir.parts[-len(parts) :]) == parts:
        return package_dir
    # The checked-in compile databases use repo-relative package paths. If the
    # repository CWD is unavailable, the package root is their honest fallback.
    return package_dir


def compile_entry_argv(entry: Dict[str, Any]) -> List[str]:
    """Return the complete compile argv, preferring the structured form."""
    import shlex

    arguments = entry.get("arguments")
    if isinstance(arguments, list) and arguments:
        return [str(a) for a in arguments]
    raw_cmd = entry.get("command") or ""
    return shlex.split(str(raw_cmd)) if raw_cmd else []


def split_compile_entry_args(entry: Dict[str, Any]) -> List[str]:
    """Return compile argv tokens without the original compiler token.

    Prefers ``arguments`` when present; otherwise ``shlex.split``s ``command``.
    """
    args = compile_entry_argv(entry)
    # Drop the original compiler token; callers rebuild with their mode flag.
    if args and not args[0].startswith("-"):
        args = args[1:]
    return args


def resolve_compile_entry_source(
    entry: Dict[str, Any], *, cwd: Path, package_dir: Path
) -> Path:
    """Resolve the primary source path for one compile_commands entry."""
    src = entry.get("file") or ""
    src_path = Path(str(src))
    if src_path.is_absolute():
        return src_path.resolve()
    cand = (cwd / src_path).resolve()
    if not cand.exists():
        cand = (package_dir / Path(str(src)).name).resolve()
    return cand


def strip_compile_output_flags(args: Sequence[str]) -> List[str]:
    """Drop compile/output flags so dependency or -E modes do not write objects.

    Removes ``-c`` / ``-S`` / ``-E`` / ``-fsyntax-only``, ``-o`` outputs, and
    existing dependency-generation flags (``-M*`` / ``-MF`` / ``-MT`` / ``-MQ``)
    so callers can attach a single clean mode without clashing.
    """
    drop_flags = {
        "-c",
        "-S",
        "-E",
        "-fsyntax-only",
        "-M",
        "-MM",
        "-MD",
        "-MMD",
        "-MG",
        "-MP",
        "-save-temps",
    }
    drop_with_arg = {
        "-o",
        "-MF",
        "-MT",
        "-MQ",
        "-MJ",
        "--output",
        "--dependency-file",
    }
    cleaned: List[str] = []
    i = 0
    args_list = list(args)
    while i < len(args_list):
        a = args_list[i]
        if a in drop_flags:
            i += 1
            continue
        if a in drop_with_arg:
            i += 2  # skip flag and its argument
            continue
        if a.startswith("-o") and a != "-o":
            i += 1
            continue
        if any(a.startswith(prefix) and a != prefix for prefix in ("-MF", "-MT", "-MQ", "-MJ")):
            i += 1
            continue
        if a.startswith(("--output=", "--dependency-file=", "-Wp,-M")):
            i += 1
            continue
        if a.startswith("-save-temps="):
            i += 1
            continue
        cleaned.append(a)
        i += 1
    return cleaned


def ensure_source_on_argv(
    cleaned: Sequence[str], src_path: Path, cwd: Path
) -> List[str]:
    """Append the primary source if the cleaned argv does not already name it."""
    cleaned_list = list(cleaned)
    src_path = src_path.resolve()

    def names_source(arg: str) -> bool:
        if arg.startswith("-"):
            return False
        candidate = Path(arg)
        if not candidate.is_absolute():
            candidate = cwd / candidate
        return candidate.resolve() == src_path

    if not any(names_source(arg) for arg in cleaned_list):
        if (cwd / src_path.name).exists():
            cleaned_list.append(src_path.name)
        else:
            cleaned_list.append(str(src_path))
    return cleaned_list


def preprocess_command_from_entry(
    entry: Dict[str, Any], *, compiler: str, package_dir: Path
) -> Tuple[Path, List[str], Path]:
    """Build a ``compiler -E …`` argv from one compile_commands entry.

    Returns (cwd, argv, primary_source).
    """
    cwd = resolve_compile_entry_cwd(entry, package_dir)
    cleaned = strip_compile_output_flags(split_compile_entry_args(entry))
    src_path = resolve_compile_entry_source(
        entry, cwd=cwd, package_dir=package_dir
    )
    cleaned = ensure_source_on_argv(cleaned, src_path, cwd)
    argv = [compiler, "-E", *cleaned]
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
    use_compiler_builtins: Optional[bool] = True,
) -> Dict[str, Any]:
    """Compare diagnostic live/dead labels to real preprocessor line survival.

    * ``live`` regions with non-directive body lines must have ≥1 survivor.
    * ``dead`` regions must have 0 survivors among body lines.
    * ``unknown`` is **not** scored as agreement or error — only reported.
    * ``vacuous`` (empty body, unscoreable) is separate from scored agreements.

    ``use_compiler_builtins`` defaults to True so the labels under test match a
    stamp produced with the toolchain table. Pass False to score the
    toolchain-independent mode.

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

    pa = analyze_package(
        package_dir,
        use_compiler_builtins=use_compiler_builtins,
        compiler=compiler,
    )

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
                    # A `live` label whose macro the final table attributes to
                    # someone else *is* a wrong label — that is exactly the
                    # `#ifndef NAN` case, where math.h defines it first and our
                    # block never runs. Parking it as unscoreable hid three bad
                    # cJSON labels behind a "model gap" note; the labeller now
                    # consults the translation unit's include macros, so this
                    # scores like any other region.
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
        "eval_mode": pa.eval_mode,
        "n_compiler_builtins": len(pa.compiler_builtins or {}),
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
        # A zero-sized judged population is not perfect agreement. Keep the
        # value undefined so a package with nothing comparable cannot read as
        # fully verified.
        "agreement_rate_scored": (len(agreements) / n_scored) if n_scored else None,
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
            print(f"  eval_mode             : {report.get('eval_mode')} "
                  f"(builtins={report.get('n_compiler_builtins', 0)})")
            print(f"  regions total         : {report['regions_total']}")
            print(f"  scored (live/dead)    : {report['regions_scored']}")
            print(f"  unknown (unscored)    : {report['regions_unknown']} "
                  f"({100.0 * report['unknown_rate']:.1f}%)")
            print(f"  vacuous (unscoreable) : {report['regions_vacuous']}")
            print(f"  agreements            : {report['agreements']} "
                  f"{report.get('agreement_evidence') or ''}")
            print(f"  disagreements         : {report['disagreements']}")
            rate = report["agreement_rate_scored"]
            print(
                "  agreement rate (scored): "
                + (f"{100.0 * rate:.1f}%" if rate is not None else "n/a (nothing scored)")
            )
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

    from graphrag_code.extract_c import build_c_byog  # type: ignore

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


# ---------------------------------------------------------------------------
# Persisted-liveness integrity contract (read-only; no reanalyse, no compiler)
# ---------------------------------------------------------------------------

_MAX_ANOMALY_SAMPLES = 40
_MAX_ANOMALY_MESSAGE = 400
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_LIVENESS_MANIFEST_KEYS = (
    "eval_mode",
    "compiler_path",
    "compiler_id",
    "compiler_version",
    "macro_seed_digest",
    "n_compiler_builtins",
    "n_include_macros",
    "host_independent",
)
_STAMP_FIELDS = (
    "preprocessor_dependent",
    "preprocessor_reasons",
    "preprocessor_eval_mode",
    "preprocessor_macro_seed_digest",
    "preprocessor_branches",
)
_BRANCH_KEYS = (
    "kind",
    "condition",
    "start_line",
    "end_line",
    "liveness",
    "basis",
)
_BRANCH_KINDS = frozenset({"if", "ifdef", "ifndef", "elif", "else"})
_LIVENESS_VALUES = frozenset({"live", "dead", "unknown"})
_INSIDE_REASON_RE = re.compile(
    r"^inside_conditional:(if|ifdef|ifndef|elif|else)(\(.*\))?$"
)
_BRANCH_REASON_RE = re.compile(
    r"^branch_(live|dead|unknown):(if|ifdef|ifndef|elif|else)(\(.*\))?$"
)
_REASON_PATTERNS = (
    re.compile(r"^function_like_macro:[A-Za-z_]\w*$"),
    _INSIDE_REASON_RE,
    _BRANCH_REASON_RE,
    re.compile(r"^compile_define_condition:[A-Za-z_]\w*$"),
    re.compile(r"^header_default:[A-Za-z_]\w*$"),
    re.compile(r"^macro_condition:[A-Za-z_]\w*$"),
    re.compile(r"^entity_body_has_preprocessor$"),
)

LIMITATIONS = (
    "Persisted preprocessor-liveness stamp consistency only",
    "Does not reanalyse sources or reconstruct branch decisions",
    "Does not compare the recorded digest with the current host",
    "Does not invoke a compiler or read compile_commands.json",
    "Post-annotation overlay edges are exempt from the five-field stamp",
)

ANOMALY_CODES = frozenset(
    {
        "legacy_block_missing_with_fields",
        "invalid_liveness_block",
        "extra_manifest_key",
        "missing_manifest_key",
        "manifest_mode_mismatch",
        "manifest_count_mismatch",
        "digest_mismatch",
        "compiler_mismatch",
        "partial_liveness_payload",
        "liveness_field_type",
        "reason_contract",
        "branch_contract",
        "dependent_reasons_mismatch",
        "stamp_manifest_disagreement",
        "observation_file_mismatch",
    }
)


def is_material_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, float) and math.isnan(value):
        return False
    try:
        import pandas as pd

        if value is pd.NA:
            return False
    except Exception:
        pass
    return True


def strict_json_loads(text: str) -> Any:
    def reject_constant(token: str) -> None:
        raise ValueError(f"non-finite JSON constant {token}")

    def unique_object(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for key, value in pairs:
            if key in out:
                raise ValueError(f"duplicate JSON object key {key!r}")
            out[key] = value
        return out

    return json.loads(
        text, parse_constant=reject_constant, object_pairs_hook=unique_object
    )


def _strict_canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _scalar(value: Any) -> Any:
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            return value.item()
        except Exception:
            return value
    return value


def _as_int(value: Any) -> Optional[int]:
    value = _scalar(value)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return int(value)


def _as_bool(value: Any) -> Optional[bool]:
    value = _scalar(value)
    return value if isinstance(value, bool) else None


def _unexpected_keys(mapping: Any, allowed: Sequence[str]) -> List[Any]:
    try:
        keys = list(mapping)
    except Exception:
        return []
    return sorted(
        (key for key in keys if key not in allowed), key=lambda key: repr(key)
    )


def _clip(text: Any, limit: int = _MAX_ANOMALY_MESSAGE) -> str:
    s = str(text)
    return s if len(s) <= limit else s[: limit - 3] + "..."


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row.get(key, default)
    except Exception:
        try:
            return row[key]
        except Exception:
            return default


def _row_keys(row: Any) -> List[str]:
    try:
        return [str(k) for k in row.keys()]
    except Exception:
        return []


def _table_rows(table: Any, *, name: str) -> List[Dict[str, Any]]:
    if table is None:
        return []
    if hasattr(table, "to_dict") and not isinstance(table, dict):
        try:
            records = table.to_dict("records")
        except (TypeError, ValueError, AttributeError) as error:
            raise TypeError(f"{name} dataframe cannot produce records") from error
    else:
        if isinstance(table, (str, bytes, dict)):
            raise TypeError(f"{name} must be a dataframe or sequence of rows")
        try:
            records = list(table)
        except TypeError as error:
            raise TypeError(
                f"{name} must be a dataframe or sequence of rows"
            ) from error
    out: List[Dict[str, Any]] = []
    for row in records:
        if isinstance(row, dict):
            out.append(row)
        elif hasattr(row, "items"):
            out.append(dict(row))
        else:
            raise TypeError(f"expected mapping row in {name}, got {type(row)!r}")
    return out


def _normalize_list_field(value: Any) -> Optional[List[Any]]:
    if not is_material_value(value):
        return None
    if isinstance(value, (list, tuple)):
        return list(value)
    if hasattr(value, "tolist") and not isinstance(value, (str, bytes)):
        try:
            converted = value.tolist()
        except Exception:
            return None
        return converted if isinstance(converted, list) else None
    return None


def _canonical_abs_path(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        return None
    path = Path(value)
    if not path.is_absolute():
        return None
    if any(part in {".", ".."} for part in path.parts):
        return None
    if value.startswith("/") and value != path.as_posix():
        return None
    return path.as_posix() if value.startswith("/") else str(path)


def _anomaly(
    code: str,
    message: str,
    *,
    table: Optional[str] = None,
    row_index: Optional[int] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if code not in ANOMALY_CODES:
        raise AssertionError(
            f"unknown preprocessor-liveness integrity anomaly code {code!r}"
        )
    row: Dict[str, Any] = {"code": code, "message": _clip(message)}
    if table is not None:
        row["table"] = table
    if row_index is not None:
        row["row_index"] = row_index
    if extra:
        for key, value in sorted(extra.items(), key=lambda item: repr(item[0])):
            row[key] = _clip(value) if isinstance(value, str) else value
    return row


def _locator(row: Any, *, table: str, row_index: int) -> Dict[str, Any]:
    extra: Dict[str, Any] = {}
    rid = _row_get(row, "id")
    if isinstance(rid, str) and rid.strip():
        extra["id"] = rid
    title = _row_get(row, "title")
    if isinstance(title, str) and title.strip():
        extra["title"] = title
    extra["table"] = table
    extra["row_index"] = row_index
    return extra


def _reason_accepted(reason: str) -> bool:
    return any(pattern.fullmatch(reason) for pattern in _REASON_PATTERNS)


def _has_material_liveness_stamp(row: Any) -> bool:
    dependent = _row_get(row, "preprocessor_dependent")
    if is_material_value(dependent) and _as_bool(dependent) is not False:
        return True
    eval_mode = _row_get(row, "preprocessor_eval_mode")
    if is_material_value(eval_mode):
        return True
    digest = _row_get(row, "preprocessor_macro_seed_digest")
    if is_material_value(digest):
        return True
    raw_reasons = _row_get(row, "preprocessor_reasons")
    reasons = _normalize_list_field(raw_reasons)
    if is_material_value(raw_reasons) and (reasons is None or bool(reasons)):
        return True
    raw_branches = _row_get(row, "preprocessor_branches")
    branches = _normalize_list_field(raw_branches)
    if is_material_value(raw_branches) and (branches is None or bool(branches)):
        return True
    return False


def _complete_dependency_identity(row: Any) -> bool:
    return (
        str(_row_get(row, "type") or "") == "depends_on"
        and str(_row_get(row, "fact_kind") or "") == "translation_unit_dependency"
        and str(_row_get(row, "extractor") or "") == "c-compiler-deps"
    )


def _complete_include_identity(row: Any) -> bool:
    return (
        str(_row_get(row, "type") or "") == "includes"
        and str(_row_get(row, "fact_kind") or "") == "configured_direct_include"
        and str(_row_get(row, "extractor") or "") == "c-compiler-includes"
    )


def _complete_type_use_identity(row: Any) -> bool:
    return (
        str(_row_get(row, "type") or "") == "uses_type"
        and str(_row_get(row, "fact_kind") or "") == "configured_type_use"
        and str(_row_get(row, "extractor") or "") == "clang-ast-json"
        and str(_row_get(row, "clang_type_use_status") or "") == "matched"
        and str(_row_get(row, "clang_type_use_fact_kind") or "")
        == "configured_type_use"
        and str(_row_get(row, "clang_type_use_extractor") or "") == "clang-ast-json"
    )


def _is_post_stamp_exempt(row: Any) -> bool:
    return (
        _complete_dependency_identity(row)
        or _complete_include_identity(row)
        or _complete_type_use_identity(row)
    )


def _validate_liveness_manifest_block(
    block: Dict[str, Any],
    *,
    n_entities: int,
    n_relationships: int,
    n_observations: Optional[int],
    observations_present: bool,
    manifest_obj: Dict[str, Any],
    anomalies: List[Dict[str, Any]],
) -> Optional[str]:
    extra = _unexpected_keys(block, _LIVENESS_MANIFEST_KEYS)
    missing = [key for key in _LIVENESS_MANIFEST_KEYS if key not in block]
    if extra:
        anomalies.append(
            _anomaly(
                "extra_manifest_key",
                f"preprocessor_liveness block has extra keys: {extra}",
            )
        )
    if missing:
        anomalies.append(
            _anomaly(
                "missing_manifest_key",
                f"preprocessor_liveness block is missing keys: {missing}",
            )
        )

    eval_mode = block.get("eval_mode")
    if eval_mode not in {"no_compiler", "compiler_builtins"}:
        anomalies.append(
            _anomaly(
                "manifest_mode_mismatch",
                f"eval_mode={eval_mode!r} is not no_compiler or compiler_builtins",
            )
        )

    digest = block.get("macro_seed_digest")
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        anomalies.append(
            _anomaly(
                "digest_mismatch",
                f"macro_seed_digest is not a lowercase SHA-256 hex string: "
                f"{digest!r}",
            )
        )
        digest = None

    host_independent = block.get("host_independent")
    n_builtins = block.get("n_compiler_builtins")
    n_includes = block.get("n_include_macros")
    if (
        isinstance(n_builtins, bool)
        or not isinstance(n_builtins, int)
        or n_builtins < 0
    ):
        anomalies.append(
            _anomaly(
                "manifest_count_mismatch",
                f"n_compiler_builtins={n_builtins!r} is not a non-negative integer",
            )
        )
        n_builtins = None
    if (
        isinstance(n_includes, bool)
        or not isinstance(n_includes, int)
        or n_includes < 0
    ):
        anomalies.append(
            _anomaly(
                "manifest_count_mismatch",
                f"n_include_macros={n_includes!r} is not a non-negative integer",
            )
        )
        n_includes = None

    compiler_path = block.get("compiler_path")
    compiler_id = block.get("compiler_id")
    compiler_version = block.get("compiler_version")
    for field, value in (
        ("compiler_id", compiler_id),
        ("compiler_version", compiler_version),
    ):
        if value is not None and (
            not isinstance(value, str) or not value.strip() or value != value.strip()
        ):
            anomalies.append(
                _anomaly(
                    "compiler_mismatch",
                    f"{field} is not null or a nonempty canonical string: "
                    f"{value!r}",
                )
            )

    if eval_mode == "no_compiler":
        if host_independent is not True:
            anomalies.append(
                _anomaly(
                    "manifest_mode_mismatch",
                    f"no_compiler host_independent={host_independent!r} "
                    "expected True",
                )
            )
        if compiler_path is not None or compiler_id is not None or compiler_version is not None:
            anomalies.append(
                _anomaly(
                    "compiler_mismatch",
                    "no_compiler singular compiler fields must all be null",
                )
            )
        if n_builtins is not None and n_builtins != 0:
            anomalies.append(
                _anomaly(
                    "manifest_count_mismatch",
                    f"no_compiler n_compiler_builtins={n_builtins} expected 0",
                )
            )
        if n_includes is not None and n_includes != 0:
            anomalies.append(
                _anomaly(
                    "manifest_count_mismatch",
                    f"no_compiler n_include_macros={n_includes} expected 0",
                )
            )
    elif eval_mode == "compiler_builtins":
        if host_independent is not False:
            anomalies.append(
                _anomaly(
                    "manifest_mode_mismatch",
                    f"compiler_builtins host_independent={host_independent!r} "
                    "expected False",
                )
            )
        if _canonical_abs_path(compiler_path) is None:
            anomalies.append(
                _anomaly(
                    "compiler_mismatch",
                    f"compiler_builtins compiler_path is not a canonical "
                    f"absolute path: {compiler_path!r}",
                )
            )
        if n_builtins is not None and n_builtins <= 0:
            anomalies.append(
                _anomaly(
                    "manifest_count_mismatch",
                    f"compiler_builtins n_compiler_builtins={n_builtins} "
                    "must be positive",
                )
            )

    declared = manifest_obj.get("counts")
    if isinstance(declared, dict):
        for key, actual, present in (
            ("entities", n_entities, True),
            ("relationships", n_relationships, True),
            (
                "call_observations",
                n_observations if observations_present else 0,
                observations_present,
            ),
        ):
            if key not in declared:
                anomalies.append(
                    _anomaly(
                        "manifest_count_mismatch",
                        f"manifest counts.{key} is missing",
                    )
                )
                continue
            value = declared[key]
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
            ):
                anomalies.append(
                    _anomaly(
                        "manifest_count_mismatch",
                        f"manifest counts.{key}={value!r} is not a "
                        "non-negative integer",
                    )
                )
                continue
            if key == "call_observations":
                if value > 0 and not observations_present:
                    anomalies.append(
                        _anomaly(
                            "observation_file_mismatch",
                            f"counts.call_observations={value} but "
                            "call_observations.parquet is absent",
                        )
                    )
                elif observations_present and value != (n_observations or 0):
                    anomalies.append(
                        _anomaly(
                            "manifest_count_mismatch",
                            f"manifest counts.call_observations={value} != "
                            f"observation table length {n_observations}",
                        )
                    )
                elif not observations_present and value != 0:
                    anomalies.append(
                        _anomaly(
                            "observation_file_mismatch",
                            f"counts.call_observations={value} but "
                            "call_observations.parquet is absent",
                        )
                    )
            elif value != actual:
                anomalies.append(
                    _anomaly(
                        "manifest_count_mismatch",
                        f"manifest counts.{key}={value} != {key} table "
                        f"length {actual}",
                    )
                )
    else:
        anomalies.append(
            _anomaly(
                "manifest_count_mismatch",
                f"manifest counts is not an object: {type(declared).__name__}",
            )
        )
    return digest if eval_mode in {"no_compiler", "compiler_builtins"} else None


def _validate_stamp_row(
    row: Any,
    *,
    table: str,
    row_index: int,
    eval_mode: str,
    manifest_digest: Optional[str],
    anomalies: List[Dict[str, Any]],
) -> None:
    extra = _locator(row, table=table, row_index=row_index)
    present = set(_row_keys(row))
    missing = [field for field in _STAMP_FIELDS if field not in present]
    if missing:
        anomalies.append(
            _anomaly(
                "partial_liveness_payload",
                f"{table} row is missing required liveness keys: {missing}",
                table=table,
                row_index=row_index,
                extra=extra,
            )
        )

    dependent = _as_bool(_row_get(row, "preprocessor_dependent"))
    if dependent is None and "preprocessor_dependent" in present:
        anomalies.append(
            _anomaly(
                "liveness_field_type",
                "preprocessor_dependent is not a strict boolean",
                table=table,
                row_index=row_index,
                extra=extra,
            )
        )

    raw_reasons = _row_get(row, "preprocessor_reasons")
    reasons = _normalize_list_field(raw_reasons)
    if "preprocessor_reasons" in present:
        if reasons is None:
            anomalies.append(
                _anomaly(
                    "liveness_field_type",
                    "preprocessor_reasons is not a list",
                    table=table,
                    row_index=row_index,
                    extra=extra,
                )
            )
            reasons = []
        seen: Set[str] = set()
        valid_reasons: List[str] = []
        for reason in reasons:
            if not isinstance(reason, str) or not reason or reason != reason.strip():
                anomalies.append(
                    _anomaly(
                        "reason_contract",
                        f"preprocessor_reasons contains a noncanonical entry: "
                        f"{reason!r}",
                        table=table,
                        row_index=row_index,
                        extra=extra,
                    )
                )
                continue
            if not _reason_accepted(reason):
                anomalies.append(
                    _anomaly(
                        "reason_contract",
                        f"unknown preprocessor reason family: {reason!r}",
                        table=table,
                        row_index=row_index,
                        extra=extra,
                    )
                )
                continue
            if reason in seen:
                anomalies.append(
                    _anomaly(
                        "reason_contract",
                        f"duplicate preprocessor reason: {reason!r}",
                        table=table,
                        row_index=row_index,
                        extra=extra,
                    )
                )
                continue
            seen.add(reason)
            valid_reasons.append(reason)
        reasons = valid_reasons

    if dependent is not None and reasons is not None and dependent != bool(reasons):
        anomalies.append(
            _anomaly(
                "dependent_reasons_mismatch",
                f"preprocessor_dependent={dependent} disagrees with "
                f"bool(preprocessor_reasons)={bool(reasons)}",
                table=table,
                row_index=row_index,
                extra=extra,
            )
        )

    row_mode = _row_get(row, "preprocessor_eval_mode")
    if "preprocessor_eval_mode" in present and row_mode != eval_mode:
        anomalies.append(
            _anomaly(
                "stamp_manifest_disagreement",
                f"preprocessor_eval_mode={row_mode!r} != manifest "
                f"eval_mode={eval_mode!r}",
                table=table,
                row_index=row_index,
                extra=extra,
            )
        )
    row_digest = _row_get(row, "preprocessor_macro_seed_digest")
    if (
        "preprocessor_macro_seed_digest" in present
        and manifest_digest is not None
        and row_digest != manifest_digest
    ):
        anomalies.append(
            _anomaly(
                "stamp_manifest_disagreement",
                f"preprocessor_macro_seed_digest={row_digest!r} != manifest "
                f"digest={manifest_digest!r}",
                table=table,
                row_index=row_index,
                extra=extra,
            )
        )

    raw_branches = _row_get(row, "preprocessor_branches")
    branches = _normalize_list_field(raw_branches)
    if "preprocessor_branches" in present:
        if branches is None:
            anomalies.append(
                _anomaly(
                    "liveness_field_type",
                    "preprocessor_branches is not a list",
                    table=table,
                    row_index=row_index,
                    extra=extra,
                )
            )
            branches = []
        seen_ids: Set[Tuple[Any, Any, Any]] = set()
        valid_branch_kinds: Set[str] = set()
        valid_branch_pairs: Set[Tuple[str, str]] = set()
        for position, branch in enumerate(branches):
            if not isinstance(branch, dict):
                anomalies.append(
                    _anomaly(
                        "branch_contract",
                        f"preprocessor_branches[{position}] is not an object",
                        table=table,
                        row_index=row_index,
                        extra=extra,
                    )
                )
                continue
            extra_keys = _unexpected_keys(branch, _BRANCH_KEYS)
            missing_keys = [key for key in _BRANCH_KEYS if key not in branch]
            if extra_keys or missing_keys:
                anomalies.append(
                    _anomaly(
                        "branch_contract",
                        f"preprocessor_branches[{position}] keys extra="
                        f"{extra_keys} missing={missing_keys}",
                        table=table,
                        row_index=row_index,
                        extra=extra,
                    )
                )
            kind = branch.get("kind")
            liveness = branch.get("liveness")
            condition = branch.get("condition")
            basis = branch.get("basis")
            start = _as_int(branch.get("start_line"))
            end = _as_int(branch.get("end_line"))
            if kind not in _BRANCH_KINDS:
                anomalies.append(
                    _anomaly(
                        "branch_contract",
                        f"preprocessor_branches[{position}].kind={kind!r}",
                        table=table,
                        row_index=row_index,
                        extra=extra,
                    )
                )
            if liveness not in _LIVENESS_VALUES:
                anomalies.append(
                    _anomaly(
                        "branch_contract",
                        f"preprocessor_branches[{position}].liveness="
                        f"{liveness!r}",
                        table=table,
                        row_index=row_index,
                        extra=extra,
                    )
                )
            if not isinstance(condition, str) or condition != condition.strip():
                anomalies.append(
                    _anomaly(
                        "branch_contract",
                        f"preprocessor_branches[{position}].condition is not "
                        f"a canonical string: {condition!r}",
                        table=table,
                        row_index=row_index,
                        extra=extra,
                    )
                )
            if not isinstance(basis, str) or basis != basis.strip():
                anomalies.append(
                    _anomaly(
                        "branch_contract",
                        f"preprocessor_branches[{position}].basis is not "
                        f"a canonical string: {basis!r}",
                        table=table,
                        row_index=row_index,
                        extra=extra,
                    )
                )
            if start is None or start < 1 or end is None or end < 1:
                anomalies.append(
                    _anomaly(
                        "branch_contract",
                        f"preprocessor_branches[{position}] line coordinates "
                        f"are not strict positive integers: "
                        f"{branch.get('start_line')!r}-{branch.get('end_line')!r}",
                        table=table,
                        row_index=row_index,
                        extra=extra,
                    )
                )
            elif start > end:
                anomalies.append(
                    _anomaly(
                        "branch_contract",
                        f"preprocessor_branches[{position}] start_line "
                        f"{start} > end_line {end}",
                        table=table,
                        row_index=row_index,
                        extra=extra,
                    )
                )
            ident = (start, end, kind)
            if start is not None and end is not None and kind in _BRANCH_KINDS:
                if ident in seen_ids:
                    anomalies.append(
                        _anomaly(
                            "branch_contract",
                            f"duplicate branch identity {ident}",
                            table=table,
                            row_index=row_index,
                            extra=extra,
                        )
                    )
                seen_ids.add(ident)
            if kind in _BRANCH_KINDS and liveness in _LIVENESS_VALUES:
                valid_branch_kinds.add(kind)
                valid_branch_pairs.add((kind, liveness))
            if (
                kind in _BRANCH_KINDS
                and liveness in _LIVENESS_VALUES
                and reasons is not None
            ):
                prefix = f"branch_{liveness}:{kind}"
                if not any(
                    reason == prefix or reason.startswith(prefix + "(")
                    for reason in reasons
                ):
                    anomalies.append(
                        _anomaly(
                            "branch_contract",
                            f"branch {kind}/{liveness} has no compatible "
                            f"{prefix} reason",
                            table=table,
                            row_index=row_index,
                            extra=extra,
                        )
                    )
                inside_prefix = f"inside_conditional:{kind}"
                if not any(
                    reason == inside_prefix
                    or reason.startswith(inside_prefix + "(")
                    for reason in reasons
                ):
                    anomalies.append(
                        _anomaly(
                            "branch_contract",
                            f"branch {kind}/{liveness} has no compatible "
                            f"{inside_prefix} reason",
                            table=table,
                            row_index=row_index,
                            extra=extra,
                        )
                    )
        for reason in reasons or []:
            branch_match = _BRANCH_REASON_RE.fullmatch(reason)
            if branch_match is not None:
                pair = (branch_match.group(2), branch_match.group(1))
                if pair not in valid_branch_pairs:
                    anomalies.append(
                        _anomaly(
                            "branch_contract",
                            f"reason {reason!r} has no compatible branch object",
                            table=table,
                            row_index=row_index,
                            extra=extra,
                        )
                    )
                continue
            inside_match = _INSIDE_REASON_RE.fullmatch(reason)
            if (
                inside_match is not None
                and inside_match.group(1) not in valid_branch_kinds
            ):
                anomalies.append(
                    _anomaly(
                        "branch_contract",
                        f"reason {reason!r} has no compatible branch object",
                        table=table,
                        row_index=row_index,
                        extra=extra,
                    )
                )
        if branches and not reasons:
            anomalies.append(
                _anomaly(
                    "branch_contract",
                    "nonempty preprocessor_branches cannot coexist with an "
                    "empty reasons list",
                    table=table,
                    row_index=row_index,
                    extra=extra,
                )
            )


def validate_persisted_preprocessor_liveness(
    entities: Any,
    relationships: Any,
    call_observations: Any = None,
    manifest: Optional[Any] = None,
    *,
    max_anomaly_samples: int = _MAX_ANOMALY_SAMPLES,
) -> Dict[str, Any]:
    """Validate already-persisted preprocessor-liveness stamps.

    Pure and non-mutating. Never reanalyses sources, reconstructs macro
    tables or branch decisions, invokes a compiler, or compares the
    recorded digest with the current host.
    """
    if max_anomaly_samples < 0:
        max_anomaly_samples = 0

    entities_list = _table_rows(entities, name="entities")
    relationships_list = _table_rows(relationships, name="relationships")
    observations_present = call_observations is not None
    observations_list = (
        _table_rows(call_observations, name="call_observations")
        if observations_present
        else []
    )
    manifest_obj: Dict[str, Any] = {}
    if manifest is not None:
        if isinstance(manifest, dict):
            manifest_obj = manifest
        elif hasattr(manifest, "items"):
            manifest_obj = dict(manifest)
        else:
            raise TypeError("manifest must be a mapping or None")

    anomalies: List[Dict[str, Any]] = []
    has_block = "preprocessor_liveness" in manifest_obj
    block = manifest_obj.get("preprocessor_liveness")
    mode_state = "legacy_absent"

    stamped_tables = (
        ("entities", entities_list),
        ("relationships", relationships_list),
        ("call_observations", observations_list),
    )
    material_rows = 0
    for table, rows in stamped_tables:
        for row in rows:
            if table == "relationships" and _is_post_stamp_exempt(row):
                continue
            if _has_material_liveness_stamp(row):
                material_rows += 1

    if not has_block:
        if material_rows:
            anomalies.append(
                _anomaly(
                    "legacy_block_missing_with_fields",
                    "manifest lacks preprocessor_liveness but graph has "
                    f"{material_rows} material liveness stamp(s)",
                    extra={"n_stamped_rows": material_rows},
                )
            )
            mode_state = "invalid"
    elif not isinstance(block, dict):
        anomalies.append(
            _anomaly(
                "invalid_liveness_block",
                f"preprocessor_liveness manifest block is not an object: "
                f"{type(block).__name__}",
            )
        )
        mode_state = "invalid"
    else:
        eval_mode = block.get("eval_mode")
        if eval_mode in {"no_compiler", "compiler_builtins"}:
            mode_state = str(eval_mode)
        else:
            mode_state = "invalid"
        digest = _validate_liveness_manifest_block(
            block,
            n_entities=len(entities_list),
            n_relationships=len(relationships_list),
            n_observations=len(observations_list) if observations_present else None,
            observations_present=observations_present,
            manifest_obj=manifest_obj,
            anomalies=anomalies,
        )
        if mode_state in {"no_compiler", "compiler_builtins"}:
            for table, rows in stamped_tables:
                for index, row in enumerate(rows):
                    if table == "relationships" and _is_post_stamp_exempt(row):
                        continue
                    _validate_stamp_row(
                        row,
                        table=table,
                        row_index=index,
                        eval_mode=mode_state,
                        manifest_digest=digest,
                        anomalies=anomalies,
                    )

    anomalies.sort(
        key=lambda item: (
            str(item.get("code") or ""),
            str(item.get("table") or ""),
            item.get("row_index") if isinstance(item.get("row_index"), int) else -1,
            str(item.get("message") or ""),
            _strict_canonical_json(
                {
                    key: item[key]
                    for key in sorted(item, key=lambda name: repr(name))
                    if key not in {"code", "message", "table", "row_index"}
                }
            ),
        )
    )
    total = len(anomalies)
    samples = anomalies[:max_anomaly_samples]
    ok = total == 0 and mode_state in {
        "legacy_absent",
        "no_compiler",
        "compiler_builtins",
    }
    status = mode_state if ok else "invalid"
    n_stamped = 0
    if mode_state in {"no_compiler", "compiler_builtins"} and has_block:
        for table, rows in stamped_tables:
            for row in rows:
                if table == "relationships" and _is_post_stamp_exempt(row):
                    continue
                n_stamped += 1
    return {
        "ok": ok,
        "status": status,
        "mode": mode_state,
        "eval_mode": mode_state,
        "n_entities": len(entities_list),
        "n_relationships": len(relationships_list),
        "n_call_observations": len(observations_list) if observations_present else 0,
        "observations_present": observations_present,
        "n_stamped_rows": n_stamped,
        "n_anomalies": total,
        "n_anomaly_samples": len(samples),
        "anomalies_truncated": total > len(samples),
        "anomalies": samples,
        "counts": {
            "n_entities": len(entities_list),
            "n_relationships": len(relationships_list),
            "n_call_observations": (
                len(observations_list) if observations_present else 0
            ),
            "n_stamped_rows": n_stamped,
        },
        "provenance": {
            "eval_mode": (
                block.get("eval_mode") if isinstance(block, dict) else None
            ),
            "macro_seed_digest": (
                block.get("macro_seed_digest") if isinstance(block, dict) else None
            ),
            "host_independent": (
                block.get("host_independent") if isinstance(block, dict) else None
            ),
            "n_compiler_builtins": (
                block.get("n_compiler_builtins") if isinstance(block, dict) else None
            ),
            "n_include_macros": (
                block.get("n_include_macros") if isinstance(block, dict) else None
            ),
        },
        "limitations": list(LIMITATIONS),
    }
