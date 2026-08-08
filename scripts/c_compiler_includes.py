#!/usr/bin/env python
"""Optional compiler-backed C *direct* include hierarchy overlay.

Extends the tree-sitter-c BYOG graph with ``includes`` edges meaning:

    including file -> directly included file

under the active ``compile_commands.json`` configuration, reconstructed from
GNU/Clang ``compiler -E -H`` traces.

This is a separate layer from the flattened TU ``depends_on`` overlay
(``c_compiler_facts``):

  * ``depends_on`` / ``translation_unit_dependency``: TU → every package-local
    direct *or* transitive dependency
  * ``includes`` / ``configured_direct_include``: parent → only its direct
    includes in the configured hierarchy

Neither layer is clang AST type resolution or unconfigured textual
``#include`` guessing.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from c_compiler_common import (  # type: ignore
    CompilerOverlayError,
    build_disabled_overlay_provenance,
    compiler_from_entry as _common_compiler_from_entry,
    compiler_identity,
    indexed_package_files,
    load_compile_entries,
    next_human_readable_id,
    path_is_under,
    prepare_compile_entry,
    resolve_compiler_path as _common_resolve_compiler_path,
    validate_compile_entry,
)
from c_identities import INDEXED_SUFFIXES  # type: ignore

FACT_KIND = "configured_direct_include"
EXTRACTOR = "c-compiler-includes"
MODE = "compiler_eh"

_INCLUDE_DESCRIPTION = (
    "compiler/configuration-derived direct include observed in the active "
    "compiler include hierarchy (not a flattened transitive dependency; "
    "deterministic only relative to the recorded compiler and compile database)"
)

# Leading dots (depth) then a single space then the full path (may contain spaces).
_H_LINE_RE = re.compile(r"^(\.+) (.*)$")
_DOTTED_LINE_RE = re.compile(r"^(\.+)(.*)$")

# GCC often appends this footer after the -H tree; ignore it and following lines.
_GCC_GUARD_FOOTER = "Multiple include guards may be useful for:"
_FRAMEWORK_SUFFIX = " (framework directory)"

# These modes may populate compiler caches or module outputs under a path from
# the compile command. Supporting them requires a separate audited redirection
# policy; silently changing their paths would also change configured semantics.
_UNSAFE_MODULE_OUTPUT_FLAGS = {
    "-fmodules",
    "-fmodules-ts",
    "-fcxx-modules",
    "-fimplicit-modules",
    "-fimplicit-module-maps",
    "-fmodules-cache-path",
    "-fmodule-output",
}
_UNSAFE_MODULE_OUTPUT_PREFIXES = (
    "-fmodules-cache-path=",
    "-fmodule-output=",
)


class CompilerIncludeError(CompilerOverlayError):
    """Raised when direct-include hierarchy collection cannot run honestly."""


def _wrap(err: CompilerOverlayError) -> CompilerIncludeError:
    if isinstance(err, CompilerIncludeError):
        return err
    return CompilerIncludeError(str(err))


def include_trace_command_from_entry(
    entry: Dict[str, Any],
    *,
    compiler: str,
    package_dir: Path,
    preprocessed_out: Path,
) -> Tuple[Path, List[str], Path]:
    """Build ``compiler -E -H … -o <temp>`` argv for one compile entry."""
    try:
        cwd, cleaned, src_path = prepare_compile_entry(
            entry, package_dir=package_dir
        )
    except CompilerOverlayError as e:
        raise _wrap(e) from e
    unsafe_module_flags = [
        arg
        for arg in cleaned
        if arg in _UNSAFE_MODULE_OUTPUT_FLAGS
        or arg.startswith(_UNSAFE_MODULE_OUTPUT_PREFIXES)
    ]
    if unsafe_module_flags:
        raise CompilerIncludeError(
            "compiler module/cache output flags are unsupported for include "
            f"tracing: {unsafe_module_flags}; refusing possible package-tree "
            "artifacts"
        )
    argv = [
        compiler,
        "-E",
        "-H",
        *cleaned,
        "-o",
        str(preprocessed_out),
    ]
    return cwd, argv, src_path


def parse_include_trace_lines(
    stderr_text: str,
) -> List[Tuple[int, str]]:
    """Parse GNU/Clang ``-H`` stderr into (depth, path) pairs.

    Format (Apple clang / GCC): leading dots encode nesting depth (one dot =
    depth 1, direct include of the TU), then a single space, then the path
    which may contain spaces. Non-trace diagnostics are ignored; the GCC
    include-guard footer ends the parse.
    """
    entries: List[Tuple[int, str]] = []
    for raw_line in stderr_text.splitlines():
        line = raw_line.rstrip("\r\n")
        if not line.strip():
            continue
        if _GCC_GUARD_FOOTER in line:
            break
        m = _H_LINE_RE.match(line)
        if not m:
            dotted = _DOTTED_LINE_RE.match(line)
            if dotted and dotted.group(2)[:1].isspace():
                raise CompilerIncludeError(
                    f"malformed -H path delimiter: {line!r}"
                )
            # Unknown diagnostics/noise are ignored. Relative diagnostic paths
            # such as './main.c:4: warning:' begin with a dot but not dot+space.
            continue
        dots, path = m.group(1), m.group(2)
        if not path:
            raise CompilerIncludeError(
                f"malformed -H line with empty path: {line!r}"
            )
        if path.endswith(_FRAMEWORK_SUFFIX):
            path = path[: -len(_FRAMEWORK_SUFFIX)]
            if not path:
                raise CompilerIncludeError(
                    f"malformed -H framework path: {line!r}"
                )
        depth = len(dots)
        if depth < 1:
            raise CompilerIncludeError(
                f"malformed -H depth < 1: {line!r}"
            )
        entries.append((depth, path))
    return entries


def reconstruct_direct_include_edges(
    tu_path: Path,
    trace: Sequence[Tuple[int, str]],
    *,
    cwd: Path,
) -> List[Tuple[Path, Path]]:
    """Rebuild parent→child include edges from a full -H depth stack.

    Outside/system paths are kept on the stack so their children attach to the
    correct parent; callers filter to package-local indexed files afterward.
    Malformed depth jumps (skipping a level) fail explicitly.
    """
    stack: List[Path] = [tu_path.resolve()]
    edges: List[Tuple[Path, Path]] = []
    for depth, raw_path in trace:
        if depth < 1:
            raise CompilerIncludeError(
                f"invalid include depth {depth} for path {raw_path!r}"
            )
        # Parent must already exist at depth-1; depth may equal len(stack)
        # when descending one level.
        if depth > len(stack):
            raise CompilerIncludeError(
                f"malformed -H depth jump to {depth} "
                f"(stack height {len(stack)}) for path {raw_path!r}; "
                "refusing to invent a parent"
            )
        parent = stack[depth - 1]
        child = Path(raw_path)
        if not child.is_absolute():
            child = (cwd / child).resolve()
        else:
            child = child.resolve()
        # Truncate deeper frames and install this path at `depth`.
        stack = stack[:depth] + [child]
        edges.append((parent, child))
    return edges


def filter_package_include_edges(
    edges: Sequence[Tuple[Path, Path]],
    *,
    package_dir: Path,
    indexed: Dict[Path, str],
) -> List[Tuple[Path, Path]]:
    """Keep edges whose endpoints are distinct indexed package-local .c/.h files."""
    package_dir = package_dir.resolve()
    kept: List[Tuple[Path, Path]] = []
    seen: Set[Tuple[Path, Path]] = set()
    for parent, child in edges:
        if parent == child:
            continue
        if (
            parent.suffix not in INDEXED_SUFFIXES
            or child.suffix not in INDEXED_SUFFIXES
        ):
            continue
        if not parent.is_file() or not child.is_file():
            continue
        if not path_is_under(parent, package_dir) or not path_is_under(
            child, package_dir
        ):
            continue
        if parent not in indexed or child not in indexed:
            continue
        key = (parent, child)
        if key in seen:
            continue
        seen.add(key)
        kept.append((parent, child))
    return kept


def collect_includes_for_entry(
    entry: Dict[str, Any],
    *,
    compiler: str,
    package_dir: Path,
    indexed: Dict[Path, str],
) -> Tuple[Path, List[Tuple[Path, Path]]]:
    """Run ``-E -H`` for one entry; return (tu, package-local direct edges)."""
    package_dir = Path(package_dir).resolve()
    with tempfile.TemporaryDirectory(prefix="c_compiler_includes_") as tmp:
        pre_out = Path(tmp) / "preprocessed.i"
        cwd, argv, tu_path = include_trace_command_from_entry(
            entry,
            compiler=compiler,
            package_dir=package_dir,
            preprocessed_out=pre_out,
        )
        try:
            proc = subprocess.run(
                argv,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                errors="replace",
                timeout=60,
            )
        except subprocess.TimeoutExpired as e:
            raise CompilerIncludeError(
                f"compiler include tracing timed out: {' '.join(argv)}"
            ) from e
        except OSError as e:
            raise CompilerIncludeError(
                f"failed to invoke compiler for include tracing: {e}"
            ) from e
        if proc.returncode != 0:
            raise CompilerIncludeError(
                f"compiler include tracing failed ({proc.returncode}): "
                f"{' '.join(argv)}\n{(proc.stderr or proc.stdout or '')[:800]}"
            )
        if not pre_out.is_file():
            raise CompilerIncludeError(
                "compiler include tracing returned success without creating "
                "the requested temporary preprocessed output"
            )
        # -H tree is on stderr; keep the full stack including outside paths.
        trace = parse_include_trace_lines(proc.stderr or "")
        raw_edges = reconstruct_direct_include_edges(
            tu_path.resolve(), trace, cwd=cwd
        )
        filtered = filter_package_include_edges(
            raw_edges, package_dir=package_dir, indexed=indexed
        )
        return tu_path.resolve(), filtered


def collect_configured_direct_includes(
    package_dir: Path,
    *,
    compiler: Optional[str] = None,
) -> Dict[str, Any]:
    """Collect package-local direct include facts for a package."""
    package_dir = Path(package_dir).resolve()
    try:
        entries, digest = load_compile_entries(package_dir)
    except CompilerOverlayError as e:
        raise _wrap(e) from e

    compiler_override = None
    if compiler:
        try:
            compiler_override = _common_resolve_compiler_path(
                compiler, cwd=package_dir
            )
        except CompilerOverlayError as e:
            raise _wrap(e) from e

    indexed = indexed_package_files(package_dir)
    edge_keys: Set[Tuple[str, str]] = set()
    edges: List[Dict[str, Any]] = []
    tu_titles: Set[str] = set()
    compiler_records: Dict[str, Dict[str, Optional[str]]] = {}
    n_entries = 0

    for entry_index, ent in enumerate(entries):
        try:
            ent = validate_compile_entry(ent, entry_index)
            entry_cwd, _, _ = prepare_compile_entry(
                ent, package_dir=package_dir
            )
            selected = compiler_override or _common_compiler_from_entry(
                ent, cwd=entry_cwd
            )
        except CompilerOverlayError as e:
            raise _wrap(e) from e
        n_entries += 1
        if selected not in compiler_records:
            cid, cver = compiler_identity(selected)
            compiler_records[selected] = {
                "compiler_path": selected,
                "compiler_id": cid,
                "compiler_version": cver,
            }
        tu_path, local_edges = collect_includes_for_entry(
            ent,
            compiler=selected,
            package_dir=package_dir,
            indexed=indexed,
        )
        if tu_path in indexed:
            tu_titles.add(indexed[tu_path])
        for parent, child in local_edges:
            src_title = indexed[parent]
            tgt_title = indexed[child]
            key = (src_title, tgt_title)
            if key in edge_keys:
                continue
            edge_keys.add(key)
            edges.append(
                {
                    "source_title": src_title,
                    "target_title": tgt_title,
                    "source_path": str(parent),
                    "target_path": str(child),
                    **compiler_records[selected],
                }
            )

    compilers = [compiler_records[p] for p in sorted(compiler_records)]
    one = compilers[0] if len(compilers) == 1 else {}
    return {
        "mode": MODE,
        "compiler_path": one.get("compiler_path"),
        "compiler_id": one.get("compiler_id"),
        "compiler_version": one.get("compiler_version"),
        "compilers": compilers,
        "compile_commands_digest": digest,
        "compile_commands_path": str(package_dir / "compile_commands.json"),
        "n_compile_entries": n_entries,
        "n_translation_units": len(tu_titles),
        "n_facts": len(edges),
        "translation_unit_titles": sorted(tu_titles),
        "edges": edges,
        "fact_kind": FACT_KIND,
        "extractor": EXTRACTOR,
        "confidence_boundary": (
            "confidence=1.0 and is_deterministic=true mean the direct-include "
            "fact is re-derivable from the recorded compiler + "
            "compile_commands.json configuration via -E -H tracing, not that "
            "it is an unconfigured textual #include or pure syntax fact."
        ),
    }


def make_includes_relationship(
    *,
    source_title: str,
    target_title: str,
    human_readable_id: int,
    compiler_path: str,
    compiler_id: Optional[str],
    compile_commands_digest: str,
    source_file: str = "",
) -> Dict[str, Any]:
    """Build one direct ``includes`` relationship row."""
    slug_src = re.sub(r"[^0-9A-Za-z_.]", "_", source_title)
    slug_tgt = re.sub(r"[^0-9A-Za-z_.]", "_", target_title)
    edge_digest = hashlib.sha256(
        f"{source_title}\0{target_title}\0{FACT_KIND}".encode("utf-8")
    ).hexdigest()[:12]
    return {
        "id": f"rel:includes:{slug_src}->{slug_tgt}:{edge_digest}",
        "source": source_title,
        "target": target_title,
        "type": "includes",
        "description": _INCLUDE_DESCRIPTION,
        "weight": 1.0,
        "text_unit_ids": [],
        "human_readable_id": human_readable_id,
        "source_file": source_file,
        "span": "",
        "extractor": EXTRACTOR,
        "confidence": 1.0,
        "is_deterministic": True,
        "document_ids": [],
        "covariate_ids": [],
        "fact_kind": FACT_KIND,
        "compiler_path": compiler_path,
        "compiler_id": compiler_id,
        "compile_commands_digest": compile_commands_digest,
        "preprocessor_dependent": True,
        "preprocessor_reasons": ["compiler_configuration_direct_include"],
    }


def append_compiler_includes(
    data: Dict[str, List[Dict[str, Any]]],
    package_dir: Path,
    *,
    compiler: Optional[str] = None,
) -> Dict[str, Any]:
    """Append deduplicated direct ``includes`` facts onto a BYOG dict."""
    collected = collect_configured_direct_includes(
        package_dir, compiler=compiler
    )
    rels = data.setdefault("relationships", [])
    existing = {
        (str(r.get("source")), str(r.get("target")))
        for r in rels
        if str(r.get("type")) == "includes"
        and str(r.get("fact_kind")) == FACT_KIND
    }
    hid = next_human_readable_id(rels)
    added = 0
    for edge in collected["edges"]:
        key = (edge["source_title"], edge["target_title"])
        if key in existing:
            continue
        existing.add(key)
        rels.append(
            make_includes_relationship(
                source_title=edge["source_title"],
                target_title=edge["target_title"],
                human_readable_id=hid,
                compiler_path=str(edge["compiler_path"]),
                compiler_id=edge.get("compiler_id"),
                compile_commands_digest=str(
                    collected["compile_commands_digest"]
                ),
                source_file=edge.get("source_path") or "",
            )
        )
        hid += 1
        added += 1

    return {
        "mode": collected["mode"],
        "enabled": True,
        "compiler_path": collected["compiler_path"],
        "compiler_id": collected.get("compiler_id"),
        "compiler_version": collected.get("compiler_version"),
        "compilers": collected.get("compilers") or [],
        "compile_commands_digest": collected["compile_commands_digest"],
        "fact_kind": FACT_KIND,
        "n_facts": collected["n_facts"],
        "n_facts_added": added,
        "n_facts_collected": collected["n_facts"],
        "n_translation_units": collected["n_translation_units"],
        "n_compile_entries": collected["n_compile_entries"],
        "translation_unit_titles": collected["translation_unit_titles"],
        "confidence_boundary": collected["confidence_boundary"],
    }


def build_disabled_provenance() -> Dict[str, Any]:
    """Manifest block when the include overlay is off."""
    return build_disabled_overlay_provenance()
