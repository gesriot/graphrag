#!/usr/bin/env python
"""Optional compiler-backed C translation-unit dependency overlay.

Extends the tree-sitter-c BYOG graph with flattened ``depends_on`` edges from
each compile_commands translation unit to package-local headers/sources the
configured compiler reports as dependencies (``-M``).

This is **not**:
  * clang AST type resolution
  * direct textual ``#include`` provenance
  * multi-configuration coverage
  * production C/C++ semantic completeness

Facts are compiler/configuration-derived relative to the recorded toolchain and
``compile_commands.json``. ``confidence=1.0`` / ``is_deterministic=true`` only
mean "re-derivable given that toolchain + configuration", not "source-syntax
only" or "direct include".
"""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from c_identities import (  # type: ignore
    INDEXED_SUFFIXES,
    build_module_key_map,
    file_entity_title as identity_file_entity_title,
    file_title_map,
    list_indexed_c_files,
)
from c_preprocessor import (  # type: ignore
    _compiler_identity,
    _load_compile_command_entries,
    compile_entry_argv,
    ensure_source_on_argv,
    resolve_compile_entry_cwd,
    resolve_compile_entry_source,
    split_compile_entry_args,
    strip_compile_output_flags,
)

FACT_KIND = "translation_unit_dependency"
EXTRACTOR = "c-compiler-deps"

# Explicit wording required on every overlay edge.
_DEP_DESCRIPTION = (
    "compiler/configuration-derived translation-unit dependency "
    "(may be transitive via nested includes; not a direct textual #include edge)"
)


class CompilerDependencyError(RuntimeError):
    """Raised when compiler dependency collection cannot run honestly."""


def compile_commands_digest(package_dir: Path) -> str:
    """Stable SHA-256 of the package's compile_commands.json bytes."""
    cc_path = Path(package_dir) / "compile_commands.json"
    raw = cc_path.read_bytes()
    return hashlib.sha256(raw).hexdigest()


def file_entity_title(path: Path, module_key: Optional[str] = None) -> str:
    """File-entity title matching ``extract_c`` / ``c_identities``.

    When ``module_key`` is omitted the legacy single-file stem form is used
    only for call sites that already know the path is non-colliding; package
    indexing must go through :func:`indexed_package_files`.
    """
    if module_key is None:
        module_key = Path(path).stem
    return identity_file_entity_title(path, module_key)


def indexed_package_files(package_dir: Path) -> Dict[Path, str]:
    """Map resolved package path -> file-entity title (shared identity map)."""
    package_dir = Path(package_dir).resolve()
    files = list_indexed_c_files(package_dir)
    module_keys = build_module_key_map(package_dir, files)
    return file_title_map(package_dir, module_keys)


def dependency_command_from_entry(
    entry: Dict[str, Any],
    *,
    compiler: str,
    package_dir: Path,
    depfile: Path,
) -> Tuple[Path, List[str], Path]:
    """Build a ``compiler -M -MF <depfile> …`` argv from one compile_commands entry.

    Reuses the same directory/file resolution and output-flag stripping as
    ``c_preprocessor.preprocess_command_from_entry`` so dependency discovery
    does not write ``.o`` files into the package tree.
    """
    cwd = resolve_compile_entry_cwd(entry, package_dir)
    cleaned = strip_compile_output_flags(split_compile_entry_args(entry))
    if any(arg.startswith("@") for arg in cleaned):
        raise CompilerDependencyError(
            "response-file compile arguments are unsupported because hidden "
            "output flags cannot be audited safely"
        )
    src_path = resolve_compile_entry_source(
        entry, cwd=cwd, package_dir=package_dir
    )
    cleaned = ensure_source_on_argv(cleaned, src_path, cwd)
    # -M reports the complete configured dependency set. Filtering below drops
    # paths outside the package, while retaining package-local headers reached
    # through -isystem (which -MM would incorrectly suppress).
    # -MF: write deps to a temp path; never default object paths under examples/.
    # -MT: fixed target name so parsing is independent of source basename.
    argv = [
        compiler,
        "-M",
        "-MF",
        str(depfile),
        "-MT",
        "tu",
        *cleaned,
    ]
    return cwd, argv, src_path


def parse_makefile_dependencies(text: str) -> List[str]:
    """Parse compiler ``-M`` make syntax, including escaped path characters."""
    # The target is fixed to ``tu`` by dependency_command_from_entry, so the
    # first colon is unambiguous. Remove make continuations before tokenizing.
    joined = text.replace("\\\r\n", "").replace("\\\n", "")
    if not joined.strip():
        return []
    if ":" in joined:
        joined = joined.split(":", 1)[1]

    tokens: List[str] = []
    current: List[str] = []
    i = 0
    while i < len(joined):
        char = joined[i]
        if char == "\\" and i + 1 < len(joined):
            # GCC/Clang escape spaces, '#', ':' and backslashes in make words.
            current.append(joined[i + 1])
            i += 2
            continue
        if char == "$" and i + 1 < len(joined) and joined[i + 1] == "$":
            # Make represents a literal dollar as '$$'.
            current.append("$")
            i += 2
            continue
        if char.isspace():
            if current:
                tokens.append("".join(current))
                current = []
            i += 1
            continue
        current.append(char)
        i += 1
    if current:
        tokens.append("".join(current))
    return tokens


_SUPPORTED_COMPILER = re.compile(
    r"(?:.+-)?(?:clang|gcc|cc)(?:-\d+(?:\.\d+)*)?\Z"
)


def _resolve_compiler_path(token: str, *, cwd: Path) -> str:
    """Resolve one GNU/Clang-compatible compiler token or fail explicitly."""
    name = Path(token).name
    if not _SUPPORTED_COMPILER.fullmatch(name):
        raise CompilerDependencyError(
            f"unsupported compiler command {token!r}; expected clang/cc/gcc "
            "(compiler wrappers and MSVC are not supported)"
        )
    path = Path(token)
    if path.is_absolute():
        resolved = path
    elif path.parent != Path("."):
        resolved = (cwd / path).resolve()
    else:
        found = shutil.which(token)
        if not found:
            raise CompilerDependencyError(
                f"compiler from compile_commands.json is not on PATH: {token!r}"
            )
        resolved = Path(found).resolve()
    if not resolved.is_file():
        raise CompilerDependencyError(f"compiler does not exist: {resolved}")
    return str(resolved)


def compiler_from_entry(entry: Dict[str, Any], *, cwd: Path) -> str:
    """Resolve the compiler actually named by a compile database entry."""
    argv = compile_entry_argv(entry)
    if not argv:
        raise CompilerDependencyError(
            "compile_commands entry has neither arguments nor command"
        )
    return _resolve_compiler_path(argv[0], cwd=cwd)


def _path_is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def filter_package_dependencies(
    dep_paths: Sequence[str],
    *,
    cwd: Path,
    package_dir: Path,
    tu_path: Path,
    indexed: Dict[Path, str],
) -> List[Path]:
    """Keep existing indexed package .c/.h deps; drop self, system, outside."""
    package_dir = package_dir.resolve()
    tu_resolved = tu_path.resolve()
    kept: List[Path] = []
    seen: Set[Path] = set()
    for raw in dep_paths:
        p = Path(raw)
        if not p.is_absolute():
            p = (cwd / p).resolve()
        else:
            p = p.resolve()
        if p == tu_resolved:
            continue
        if p.suffix not in INDEXED_SUFFIXES:
            continue
        if not p.is_file():
            continue
        if not _path_is_under(p, package_dir):
            continue
        if p not in indexed:
            continue
        if p in seen:
            continue
        seen.add(p)
        kept.append(p)
    return kept


def collect_tu_dependencies_for_entry(
    entry: Dict[str, Any],
    *,
    compiler: str,
    package_dir: Path,
    indexed: Dict[Path, str],
) -> Tuple[Path, List[Path]]:
    """Run the compiler in dependency mode for one entry; return (tu, deps)."""
    package_dir = Path(package_dir).resolve()
    with tempfile.TemporaryDirectory(prefix="c_compiler_deps_") as tmp:
        depfile = Path(tmp) / "deps.d"
        cwd, argv, tu_path = dependency_command_from_entry(
            entry, compiler=compiler, package_dir=package_dir, depfile=depfile
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
            raise CompilerDependencyError(
                f"compiler dependency generation timed out: {' '.join(argv)}"
            ) from e
        except OSError as e:
            raise CompilerDependencyError(
                f"failed to invoke compiler for dependencies: {e}"
            ) from e
        if proc.returncode != 0:
            raise CompilerDependencyError(
                f"compiler dependency generation failed ({proc.returncode}): "
                f"{' '.join(argv)}\n{(proc.stderr or proc.stdout or '')[:800]}"
            )
        if not depfile.is_file():
            # Some toolchains print deps on stdout even with -MF; accept that.
            text = proc.stdout or ""
        else:
            text = depfile.read_text(encoding="utf-8", errors="replace")
        raw_deps = parse_makefile_dependencies(text)
        filtered = filter_package_dependencies(
            raw_deps,
            cwd=cwd,
            package_dir=package_dir,
            tu_path=tu_path,
            indexed=indexed,
        )
        return tu_path.resolve(), filtered


def collect_translation_unit_dependencies(
    package_dir: Path,
    *,
    compiler: Optional[str] = None,
) -> Dict[str, Any]:
    """Collect flattened package-local TU dependency facts for a package.

    Requires ``compile_commands.json`` and a working C compiler. Does not
    silently fall back to tree-sitter include guesses.
    """
    package_dir = Path(package_dir).resolve()
    cc_path = package_dir / "compile_commands.json"
    if not cc_path.is_file():
        raise CompilerDependencyError(
            f"compile_commands.json not found under {package_dir}; "
            "compiler dependency overlay requires a compile database"
        )

    try:
        entries = _load_compile_command_entries(package_dir)
        digest = compile_commands_digest(package_dir)
    except OSError as e:
        raise CompilerDependencyError(
            f"cannot read compile_commands.json under {package_dir}: {e}"
        ) from e
    if not entries:
        raise CompilerDependencyError(
            f"compile_commands.json under {package_dir} is empty or unreadable"
        )

    compiler_override = (
        _resolve_compiler_path(compiler, cwd=package_dir) if compiler else None
    )
    indexed = indexed_package_files(package_dir)

    # Dedup edges across TUs / duplicate compile entries.
    edge_keys: Set[Tuple[str, str]] = set()
    edges: List[Dict[str, Any]] = []
    tu_titles: Set[str] = set()
    compiler_records: Dict[str, Dict[str, Optional[str]]] = {}
    n_entries = 0

    for entry_index, ent in enumerate(entries):
        if not isinstance(ent, dict):
            raise CompilerDependencyError(
                "invalid compile_commands.json: "
                f"entry {entry_index} is not an object"
            )
        n_entries += 1
        entry_cwd = resolve_compile_entry_cwd(ent, package_dir)
        selected_compiler = compiler_override or compiler_from_entry(
            ent, cwd=entry_cwd
        )
        if selected_compiler not in compiler_records:
            compiler_id, compiler_version = _compiler_identity(selected_compiler)
            compiler_records[selected_compiler] = {
                "compiler_path": selected_compiler,
                "compiler_id": compiler_id,
                "compiler_version": compiler_version,
            }
        tu_path, deps = collect_tu_dependencies_for_entry(
            ent,
            compiler=selected_compiler,
            package_dir=package_dir,
            indexed=indexed,
        )
        if tu_path not in indexed:
            # Primary source not indexed as a file entity — skip edges from it.
            continue
        src_title = indexed[tu_path]
        tu_titles.add(src_title)
        for dep in deps:
            tgt_title = indexed[dep]
            key = (src_title, tgt_title)
            if key in edge_keys:
                continue
            edge_keys.add(key)
            edges.append(
                {
                    "source_title": src_title,
                    "target_title": tgt_title,
                    "source_path": str(tu_path),
                    "target_path": str(dep),
                    **compiler_records[selected_compiler],
                }
            )

    compilers = [compiler_records[path] for path in sorted(compiler_records)]
    one_compiler = compilers[0] if len(compilers) == 1 else {}

    return {
        "mode": "compiler_m",
        "compiler_path": one_compiler.get("compiler_path"),
        "compiler_id": one_compiler.get("compiler_id"),
        "compiler_version": one_compiler.get("compiler_version"),
        "compilers": compilers,
        "compile_commands_digest": digest,
        "compile_commands_path": str(cc_path),
        "n_compile_entries": n_entries,
        "n_translation_units": len(tu_titles),
        "n_facts": len(edges),
        "translation_unit_titles": sorted(tu_titles),
        "edges": edges,
        "fact_kind": FACT_KIND,
        "extractor": EXTRACTOR,
        # Boundary: high confidence only relative to recorded toolchain/config.
        "confidence_boundary": (
            "confidence=1.0 and is_deterministic=true mean the fact is "
            "re-derivable from the recorded compiler + compile_commands.json "
            "configuration, not that it is a direct textual #include or pure "
            "syntax fact independent of the toolchain."
        ),
    }


def _next_human_readable_id(relationships: Sequence[Dict[str, Any]]) -> int:
    max_id = 0
    for r in relationships:
        try:
            max_id = max(max_id, int(r.get("human_readable_id") or 0))
        except (TypeError, ValueError):
            continue
    return max_id + 1


def make_depends_on_relationship(
    *,
    source_title: str,
    target_title: str,
    human_readable_id: int,
    compiler_path: str,
    compiler_id: Optional[str],
    compile_commands_digest: str,
    source_file: str = "",
) -> Dict[str, Any]:
    """Build one flattened ``depends_on`` relationship row."""
    # Keep the id readable while preventing punctuation-normalization collisions.
    slug_src = re.sub(r"[^0-9A-Za-z_.]", "_", source_title)
    slug_tgt = re.sub(r"[^0-9A-Za-z_.]", "_", target_title)
    edge_digest = hashlib.sha256(
        f"{source_title}\0{target_title}".encode("utf-8")
    ).hexdigest()[:12]
    return {
        "id": f"rel:depends_on:{slug_src}->{slug_tgt}:{edge_digest}",
        "source": source_title,
        "target": target_title,
        "type": "depends_on",
        "description": _DEP_DESCRIPTION,
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
        # Provenance sufficient to identify kind + toolchain + configuration.
        "fact_kind": FACT_KIND,
        "compiler_path": compiler_path,
        "compiler_id": compiler_id,
        "compile_commands_digest": compile_commands_digest,
        # Schema-compatible with preprocessor stamps on other edges.
        "preprocessor_dependent": True,
        "preprocessor_reasons": ["compiler_configuration_dependency"],
    }


def append_compiler_dependencies(
    data: Dict[str, List[Dict[str, Any]]],
    package_dir: Path,
    *,
    compiler: Optional[str] = None,
) -> Dict[str, Any]:
    """Append deduplicated compiler TU ``depends_on`` facts onto a BYOG dict.

    Mutates ``data["relationships"]`` in place. Returns the provenance summary
    suitable for a snapshot ``extra_manifest`` block.
    """
    collected = collect_translation_unit_dependencies(
        package_dir, compiler=compiler
    )
    rels = data.setdefault("relationships", [])
    # Also dedupe against any edges already present (idempotent re-append).
    existing = {
        (str(r.get("source")), str(r.get("target")))
        for r in rels
        if str(r.get("type")) == "depends_on"
        and str(r.get("fact_kind")) == FACT_KIND
    }
    hid = _next_human_readable_id(rels)
    added = 0
    for edge in collected["edges"]:
        key = (edge["source_title"], edge["target_title"])
        if key in existing:
            continue
        existing.add(key)
        rels.append(
            make_depends_on_relationship(
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
    """Manifest block when the overlay is off (default publish path)."""
    return {
        "mode": "off",
        "enabled": False,
        "n_facts": 0,
        "n_translation_units": 0,
    }
