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
import json
import math
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from graphrag_code.c_compiler_common import (  # type: ignore
    CompilerOverlayError,
    build_disabled_overlay_provenance,
    compiler_from_entry as _common_compiler_from_entry,
    compiler_identity,
    indexed_package_files,
    load_compile_entries,
    next_human_readable_id,
    path_is_under,
    prepare_compile_entry,
    reject_hidden_compiler_outputs,
    resolve_compiler_path as _common_resolve_compiler_path,
    validate_compile_entry,
)
from graphrag_code.c_identities import INDEXED_SUFFIXES  # type: ignore

FACT_KIND = "configured_direct_include"
EXTRACTOR = "c-compiler-includes"
MODE = "compiler_eh"

_INCLUDE_DESCRIPTION = (
    "compiler/configuration-derived direct include observed in the active "
    "compiler include hierarchy (not a flattened transitive dependency; "
    "deterministic only relative to the recorded compiler and compile database)"
)

CONFIDENCE_BOUNDARY = (
    "confidence=1.0 and is_deterministic=true mean the direct-include "
    "fact is re-derivable from the recorded compiler + "
    "compile_commands.json configuration via -E -H tracing, not that "
    "it is an unconfigured textual #include or pure syntax fact."
)

# Audit-report contract text only; not a persisted manifest field.
LIMITATIONS = (
    "Configured direct includes edges only",
    "Not a flattened translation-unit depends_on edge",
    "Not compiler_dependencies reconstruction",
    "Not multi-configuration coverage",
    "Not Clang AST type resolution",
)

# Leading dots (depth) then a single space then the full path (may contain spaces).
_H_LINE_RE = re.compile(r"^(\.+) (.*)$")
_DOTTED_LINE_RE = re.compile(r"^(\.+)(.*)$")

# GCC often appends this footer after the -H tree; ignore it and following lines.
_GCC_GUARD_FOOTER = "Multiple include guards may be useful for:"
_FRAMEWORK_SUFFIX = " (framework directory)"

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
        reject_hidden_compiler_outputs(cleaned)
    except CompilerOverlayError as e:
        raise _wrap(e) from e
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
        "confidence_boundary": CONFIDENCE_BOUNDARY,
    }


def include_relationship_id(source_title: str, target_title: str) -> str:
    """Deterministic ``includes`` relationship id for one source/target pair.

    The digest input includes ``FACT_KIND`` so include IDs cannot collide with
    flattened ``depends_on`` IDs that share the same endpoint titles.
    """
    slug_src = re.sub(r"[^0-9A-Za-z_.]", "_", source_title)
    slug_tgt = re.sub(r"[^0-9A-Za-z_.]", "_", target_title)
    edge_digest = hashlib.sha256(
        f"{source_title}\0{target_title}\0{FACT_KIND}".encode("utf-8")
    ).hexdigest()[:12]
    return f"rel:includes:{slug_src}->{slug_tgt}:{edge_digest}"


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
    return {
        "id": include_relationship_id(source_title, target_title),
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


# ---------------------------------------------------------------------------
# Persisted-overlay integrity contract (read-only; no compiler, no reindex)
# ---------------------------------------------------------------------------

_MAX_ANOMALY_SAMPLES = 40
_MAX_ANOMALY_MESSAGE = 400
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ENABLED_MANIFEST_KEYS = (
    "mode",
    "enabled",
    "compiler_path",
    "compiler_id",
    "compiler_version",
    "compilers",
    "compile_commands_digest",
    "fact_kind",
    "n_facts",
    "n_facts_added",
    "n_facts_collected",
    "n_translation_units",
    "n_compile_entries",
    "translation_unit_titles",
    "confidence_boundary",
)
_OFF_MANIFEST_KEYS = (
    "mode",
    "enabled",
    "n_facts",
    "n_translation_units",
)
_COMPILER_RECORD_KEYS = (
    "compiler_path",
    "compiler_id",
    "compiler_version",
)
_INCLUDE_FIELDS = (
    "id",
    "source",
    "target",
    "type",
    "description",
    "weight",
    "text_unit_ids",
    "human_readable_id",
    "source_file",
    "span",
    "extractor",
    "confidence",
    "is_deterministic",
    "document_ids",
    "covariate_ids",
    "fact_kind",
    "compiler_path",
    "compiler_id",
    "compile_commands_digest",
    "preprocessor_dependent",
    "preprocessor_reasons",
)
_INCOMPATIBLE_PREFIXES = ("clang_call_", "clang_type_use_")

ANOMALY_CODES = frozenset(
    {
        "empty_relationship_id",
        "duplicate_relationship_id",
        "legacy_block_missing_with_fields",
        "off_with_decorated_relationships",
        "invalid_enabled_block",
        "extra_manifest_key",
        "missing_manifest_key",
        "contradictory_carrier",
        "partial_include_payload",
        "unknown_include_field",
        "incompatible_overlay_field",
        "include_field_type",
        "identity_mismatch",
        "description_mismatch",
        "endpoint_mismatch",
        "source_file_mismatch",
        "duplicate_endpoint_pair",
        "human_readable_id",
        "digest_mismatch",
        "compiler_mismatch",
        "preprocessor_provenance",
        "translation_unit_titles",
        "manifest_mode_mismatch",
        "manifest_identity_mismatch",
        "manifest_count_mismatch",
        "manifest_contract_claim",
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


def _unexpected_keys(mapping: Dict[Any, Any], allowed: Sequence[str]) -> List[Any]:
    """Return deterministic unexpected-key diagnostics for arbitrary mappings."""
    return sorted(
        (key for key in mapping if key not in allowed), key=lambda key: repr(key)
    )


def _as_bool(value: Any) -> Optional[bool]:
    value = _scalar(value)
    return value if isinstance(value, bool) else None


def _is_one(value: Any) -> bool:
    value = _scalar(value)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, float) and not math.isfinite(value):
        return False
    return float(value) == 1.0


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


def _anomaly(
    code: str,
    message: str,
    *,
    relationship_id: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if code not in ANOMALY_CODES:
        raise AssertionError(
            f"unknown compiler-include integrity anomaly code {code!r}"
        )
    row: Dict[str, Any] = {"code": code, "message": _clip(message)}
    if relationship_id is not None:
        row["relationship_id"] = relationship_id
    if extra:
        for key, value in sorted(extra.items(), key=lambda item: repr(item[0])):
            row[key] = _clip(value) if isinstance(value, str) else value
    return row


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


def _is_empty_list(value: Any) -> bool:
    items = _normalize_list_field(value)
    return items is not None and items == []


def _has_include_marker(row: Any) -> bool:
    return (
        str(_row_get(row, "type") or "") == "includes"
        or str(_row_get(row, "fact_kind") or "") == FACT_KIND
        or str(_row_get(row, "extractor") or "") == EXTRACTOR
    )


def _complete_include_identity(row: Any) -> bool:
    return (
        str(_row_get(row, "type") or "") == "includes"
        and str(_row_get(row, "fact_kind") or "") == FACT_KIND
        and str(_row_get(row, "extractor") or "") == EXTRACTOR
    )


def _validate_decorated_relationship(
    row: Any,
    *,
    relationship_id: Optional[str],
    file_entities: Dict[str, List[Dict[str, Any]]],
    manifest_digest: Optional[str],
    compiler_records: List[Dict[str, Any]],
    anomalies: List[Dict[str, Any]],
) -> Optional[Tuple[str, str]]:
    present = set(_row_keys(row))
    missing = [field for field in _INCLUDE_FIELDS if field not in present]
    if missing:
        anomalies.append(
            _anomaly(
                "partial_include_payload",
                f"include relationship is missing required keys: {missing}",
                relationship_id=relationship_id,
            )
        )
    for key in present:
        if any(key.startswith(prefix) for prefix in _INCOMPATIBLE_PREFIXES):
            if is_material_value(_row_get(row, key)):
                anomalies.append(
                    _anomaly(
                        "incompatible_overlay_field",
                        f"include relationship carries material {key}",
                        relationship_id=relationship_id,
                    )
                )
        if (
            key.startswith("compiler_")
            and key not in {"compiler_path", "compiler_id"}
            and is_material_value(_row_get(row, key))
        ):
            anomalies.append(
                _anomaly(
                    "unknown_include_field",
                    f"unknown material compiler-include provenance: {key}",
                    relationship_id=relationship_id,
                )
            )

    source = _row_get(row, "source")
    target = _row_get(row, "target")
    if not isinstance(source, str) or not source.strip():
        anomalies.append(
            _anomaly(
                "endpoint_mismatch",
                "include source is empty",
                relationship_id=relationship_id,
            )
        )
        source = None
    if not isinstance(target, str) or not target.strip():
        anomalies.append(
            _anomaly(
                "endpoint_mismatch",
                "include target is empty",
                relationship_id=relationship_id,
            )
        )
        target = None
    if source is not None and target is not None and source == target:
        anomalies.append(
            _anomaly(
                "endpoint_mismatch",
                "include source and target must be distinct",
                relationship_id=relationship_id,
            )
        )
    for endpoint, label in ((source, "source"), (target, "target")):
        if endpoint is None:
            continue
        matches = file_entities.get(endpoint) or []
        if len(matches) != 1:
            anomalies.append(
                _anomaly(
                    "endpoint_mismatch",
                    f"include {label} {endpoint!r} does not identify a "
                    "unique persisted file entity",
                    relationship_id=relationship_id,
                )
            )

    if str(_row_get(row, "type") or "") != "includes":
        anomalies.append(
            _anomaly(
                "identity_mismatch",
                f"type={_row_get(row, 'type')!r} expected 'includes'",
                relationship_id=relationship_id,
            )
        )
    if str(_row_get(row, "fact_kind") or "") != FACT_KIND:
        anomalies.append(
            _anomaly(
                "identity_mismatch",
                f"fact_kind={_row_get(row, 'fact_kind')!r} expected {FACT_KIND!r}",
                relationship_id=relationship_id,
            )
        )
    if str(_row_get(row, "extractor") or "") != EXTRACTOR:
        anomalies.append(
            _anomaly(
                "identity_mismatch",
                f"extractor={_row_get(row, 'extractor')!r} expected "
                f"{EXTRACTOR!r}",
                relationship_id=relationship_id,
            )
        )
    if _row_get(row, "description") != _INCLUDE_DESCRIPTION:
        anomalies.append(
            _anomaly(
                "description_mismatch",
                "description does not match the producer constant",
                relationship_id=relationship_id,
            )
        )
    if not _is_one(_row_get(row, "weight")):
        anomalies.append(
            _anomaly(
                "include_field_type",
                f"weight={_row_get(row, 'weight')!r} expected 1.0",
                relationship_id=relationship_id,
            )
        )
    if not _is_one(_row_get(row, "confidence")):
        anomalies.append(
            _anomaly(
                "include_field_type",
                f"confidence={_row_get(row, 'confidence')!r} expected 1.0",
                relationship_id=relationship_id,
            )
        )
    if _as_bool(_row_get(row, "is_deterministic")) is not True:
        anomalies.append(
            _anomaly(
                "include_field_type",
                "is_deterministic is not boolean true",
                relationship_id=relationship_id,
            )
        )
    for field in ("text_unit_ids", "document_ids", "covariate_ids"):
        if not _is_empty_list(_row_get(row, field)):
            anomalies.append(
                _anomaly(
                    "include_field_type",
                    f"{field} must be an empty list",
                    relationship_id=relationship_id,
                )
            )
    if _row_get(row, "span") != "":
        anomalies.append(
            _anomaly(
                "include_field_type",
                f"span={_row_get(row, 'span')!r} must be an empty string",
                relationship_id=relationship_id,
            )
        )

    hid = _as_int(_row_get(row, "human_readable_id"))
    if hid is None or hid < 1:
        anomalies.append(
            _anomaly(
                "human_readable_id",
                f"human_readable_id={_row_get(row, 'human_readable_id')!r} "
                "is not a positive integer",
                relationship_id=relationship_id,
            )
        )

    rel_source_file = _row_get(row, "source_file")
    if _canonical_abs_path(rel_source_file) is None:
        anomalies.append(
            _anomaly(
                "source_file_mismatch",
                f"source_file is not a canonical absolute path: "
                f"{rel_source_file!r}",
                relationship_id=relationship_id,
            )
        )
    elif source is not None:
        matches = file_entities.get(source) or []
        if len(matches) == 1:
            entity_src = _row_get(matches[0], "source_file")
            if str(entity_src) != str(rel_source_file):
                anomalies.append(
                    _anomaly(
                        "source_file_mismatch",
                        f"relationship source_file {rel_source_file!r} disagrees "
                        f"with file entity {entity_src!r}",
                        relationship_id=relationship_id,
                    )
                )

    digest = _row_get(row, "compile_commands_digest")
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        anomalies.append(
            _anomaly(
                "digest_mismatch",
                f"relationship compile_commands_digest is not a lowercase "
                f"SHA-256 hex string: {digest!r}",
                relationship_id=relationship_id,
            )
        )
    elif manifest_digest is not None and digest != manifest_digest:
        anomalies.append(
            _anomaly(
                "digest_mismatch",
                f"relationship digest {digest!r} != manifest {manifest_digest!r}",
                relationship_id=relationship_id,
            )
        )

    if _as_bool(_row_get(row, "preprocessor_dependent")) is not True:
        anomalies.append(
            _anomaly(
                "preprocessor_provenance",
                "preprocessor_dependent is not boolean true",
                relationship_id=relationship_id,
            )
        )
    reasons = _normalize_list_field(_row_get(row, "preprocessor_reasons"))
    if reasons != ["compiler_configuration_direct_include"]:
        anomalies.append(
            _anomaly(
                "preprocessor_provenance",
                f"preprocessor_reasons={reasons!r} expected "
                "['compiler_configuration_direct_include']",
                relationship_id=relationship_id,
            )
        )

    rel_path = _row_get(row, "compiler_path")
    rel_id = _row_get(row, "compiler_id")
    if not is_material_value(rel_id):
        rel_id = None
    matched_compiler = [
        compiler
        for compiler in compiler_records
        if compiler.get("compiler_path") == rel_path
        and compiler.get("compiler_id") == rel_id
    ]
    if len(matched_compiler) != 1:
        anomalies.append(
            _anomaly(
                "compiler_mismatch",
                "relationship compiler_path/compiler_id do not identify one "
                "manifest compiler record",
                relationship_id=relationship_id,
            )
        )

    if source is not None and target is not None:
        expected_id = include_relationship_id(source, target)
        if _row_get(row, "id") != expected_id:
            anomalies.append(
                _anomaly(
                    "identity_mismatch",
                    f"id={_row_get(row, 'id')!r} expected {expected_id!r}",
                    relationship_id=relationship_id,
                )
            )
        return (source, target)
    return None


def _validate_include_manifest_block(
    block: Dict[str, Any],
    *,
    n_decorated: int,
    n_relationships: int,
    file_entities: Dict[str, List[Dict[str, Any]]],
    manifest_obj: Dict[str, Any],
    anomalies: List[Dict[str, Any]],
) -> Tuple[Optional[str], List[Dict[str, Any]], Set[str]]:
    extra = _unexpected_keys(block, _ENABLED_MANIFEST_KEYS)
    missing = [key for key in _ENABLED_MANIFEST_KEYS if key not in block]
    if extra:
        anomalies.append(
            _anomaly(
                "extra_manifest_key",
                f"enabled compiler_includes block has extra keys: {extra}",
            )
        )
    if missing:
        anomalies.append(
            _anomaly(
                "missing_manifest_key",
                f"enabled compiler_includes block is missing keys: {missing}",
            )
        )
    if block.get("mode") != MODE:
        anomalies.append(
            _anomaly(
                "manifest_mode_mismatch",
                f"enabled block mode={block.get('mode')!r} expected {MODE!r}",
            )
        )
    if block.get("enabled") is not True:
        anomalies.append(
            _anomaly(
                "manifest_mode_mismatch",
                f"enabled block enabled={block.get('enabled')!r} expected True",
            )
        )
    if block.get("fact_kind") != FACT_KIND:
        anomalies.append(
            _anomaly(
                "manifest_identity_mismatch",
                f"manifest fact_kind={block.get('fact_kind')!r} expected "
                f"{FACT_KIND!r}",
            )
        )
    if block.get("confidence_boundary") != CONFIDENCE_BOUNDARY:
        anomalies.append(
            _anomaly(
                "manifest_contract_claim",
                "manifest confidence_boundary differs from the producer contract",
            )
        )

    digest = block.get("compile_commands_digest")
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        anomalies.append(
            _anomaly(
                "digest_mismatch",
                f"manifest compile_commands_digest is not a lowercase SHA-256 "
                f"hex string: {digest!r}",
            )
        )
        digest = None

    n_compile_entries = block.get("n_compile_entries")
    if (
        isinstance(n_compile_entries, bool)
        or not isinstance(n_compile_entries, int)
        or n_compile_entries <= 0
    ):
        anomalies.append(
            _anomaly(
                "manifest_count_mismatch",
                f"n_compile_entries={n_compile_entries!r} is not a positive "
                "integer",
            )
        )
        n_compile_entries = None
    n_translation_units = block.get("n_translation_units")
    if (
        isinstance(n_translation_units, bool)
        or not isinstance(n_translation_units, int)
        or n_translation_units < 0
    ):
        anomalies.append(
            _anomaly(
                "manifest_count_mismatch",
                f"n_translation_units={n_translation_units!r} is not a "
                "non-negative integer",
            )
        )
        n_translation_units = None
    if (
        n_compile_entries is not None
        and n_translation_units is not None
        and n_translation_units > n_compile_entries
    ):
        anomalies.append(
            _anomaly(
                "manifest_count_mismatch",
                f"n_translation_units={n_translation_units} exceeds "
                f"n_compile_entries={n_compile_entries}",
            )
        )
    n_facts = block.get("n_facts")
    n_collected = block.get("n_facts_collected")
    n_added = block.get("n_facts_added")
    if isinstance(n_facts, bool) or not isinstance(n_facts, int) or n_facts < 0:
        anomalies.append(
            _anomaly(
                "manifest_count_mismatch",
                f"n_facts={n_facts!r} is not a non-negative integer",
            )
        )
        n_facts = None
    elif n_facts != n_decorated:
        anomalies.append(
            _anomaly(
                "manifest_count_mismatch",
                f"n_facts={n_facts} != {n_decorated} persisted include "
                "relationships",
            )
        )
    if (
        isinstance(n_collected, bool)
        or not isinstance(n_collected, int)
        or n_collected < 0
        or n_facts is None
        or n_collected != n_facts
    ):
        anomalies.append(
            _anomaly(
                "manifest_count_mismatch",
                f"n_facts_collected={n_collected!r} != n_facts={n_facts!r}",
            )
        )
    if (
        isinstance(n_added, bool)
        or not isinstance(n_added, int)
        or n_facts is None
        or n_added < 0
        or n_added > n_facts
    ):
        anomalies.append(
            _anomaly(
                "manifest_count_mismatch",
                f"n_facts_added={n_added!r} is not within [0, {n_facts}]",
            )
        )

    titles = block.get("translation_unit_titles")
    tu_titles: Set[str] = set()
    if not isinstance(titles, list):
        anomalies.append(
            _anomaly(
                "translation_unit_titles",
                f"translation_unit_titles is not a list: {type(titles).__name__}",
            )
        )
    else:
        valid_titles = [
            title
            for title in titles
            if isinstance(title, str)
            and bool(title.strip())
            and title == title.strip()
        ]
        if len(valid_titles) != len(titles):
            anomalies.append(
                _anomaly(
                    "translation_unit_titles",
                    "translation_unit_titles must contain nonempty canonical "
                    "strings",
                )
            )
        if len(valid_titles) == len(titles):
            try:
                ordered = sorted(titles)
            except TypeError:
                ordered = None
            if ordered is None or titles != ordered:
                anomalies.append(
                    _anomaly(
                        "translation_unit_titles",
                        "translation_unit_titles is not sorted",
                    )
                )
        if len(valid_titles) != len(set(valid_titles)):
            anomalies.append(
                _anomaly(
                    "translation_unit_titles",
                    "translation_unit_titles contains duplicates",
                )
            )
        if n_translation_units is not None and len(titles) != n_translation_units:
            anomalies.append(
                _anomaly(
                    "translation_unit_titles",
                    f"translation_unit_titles length {len(titles)} != "
                    f"n_translation_units {n_translation_units}",
                )
            )
        invalid_entities = [
            title
            for title in valid_titles
            if len(file_entities.get(title) or []) != 1
        ]
        if invalid_entities:
            anomalies.append(
                _anomaly(
                    "translation_unit_titles",
                    "translation_unit_titles do not identify exactly one "
                    f"persisted file entity: {invalid_entities}",
                )
            )
        tu_titles = set(valid_titles)

    compilers = block.get("compilers")
    records: List[Dict[str, Any]] = []
    if (
        not isinstance(compilers, list)
        or not compilers
        or not all(isinstance(item, dict) for item in compilers)
    ):
        anomalies.append(
            _anomaly(
                "compiler_mismatch",
                "manifest compilers must be a non-empty list of objects",
            )
        )
    else:
        seen_paths: Set[str] = set()
        for position, compiler in enumerate(compilers):
            extra_keys = _unexpected_keys(compiler, _COMPILER_RECORD_KEYS)
            missing_keys = [
                key for key in _COMPILER_RECORD_KEYS if key not in compiler
            ]
            if extra_keys or missing_keys:
                anomalies.append(
                    _anomaly(
                        "compiler_mismatch",
                        f"compilers[{position}] keys extra={extra_keys} "
                        f"missing={missing_keys}",
                    )
                )
                continue
            path = compiler.get("compiler_path")
            cid = compiler.get("compiler_id")
            version = compiler.get("compiler_version")
            if _canonical_abs_path(path) is None:
                anomalies.append(
                    _anomaly(
                        "compiler_mismatch",
                        f"compilers[{position}].compiler_path is not a "
                        f"canonical absolute path: {path!r}",
                    )
                )
                continue
            if cid is not None and (
                not isinstance(cid, str) or not cid.strip() or cid != cid.strip()
            ):
                anomalies.append(
                    _anomaly(
                        "compiler_mismatch",
                        f"compilers[{position}].compiler_id is not null or a "
                        f"nonempty canonical string: {cid!r}",
                    )
                )
                continue
            if version is not None and (
                not isinstance(version, str)
                or not version.strip()
                or version != version.strip()
            ):
                anomalies.append(
                    _anomaly(
                        "compiler_mismatch",
                        f"compilers[{position}].compiler_version is not null "
                        f"or a nonempty canonical string: {version!r}",
                    )
                )
                continue
            if path in seen_paths:
                anomalies.append(
                    _anomaly(
                        "compiler_mismatch",
                        f"duplicate compiler_path in compilers: {path!r}",
                    )
                )
                continue
            seen_paths.add(str(path))
            records.append(
                {
                    "compiler_path": path,
                    "compiler_id": cid,
                    "compiler_version": version,
                }
            )
        ordered = sorted(
            compilers,
            key=lambda item: str(item.get("compiler_path") or ""),
        )
        if compilers != ordered:
            anomalies.append(
                _anomaly(
                    "compiler_mismatch",
                    "manifest compilers is not in producer order by compiler_path",
                )
            )
        if len(records) == 1:
            only = records[0]
            for field in _COMPILER_RECORD_KEYS:
                if block.get(field) != only.get(field):
                    anomalies.append(
                        _anomaly(
                            "compiler_mismatch",
                            f"singular manifest {field}={block.get(field)!r} "
                            f"!= compilers[0].{field}={only.get(field)!r}",
                        )
                    )
        elif len(records) > 1:
            for field in _COMPILER_RECORD_KEYS:
                if block.get(field) is not None:
                    anomalies.append(
                        _anomaly(
                            "compiler_mismatch",
                            f"multi-compiler manifest exposes singular "
                            f"{field}={block.get(field)!r}",
                        )
                    )
        if n_compile_entries is not None and len(records) > n_compile_entries:
            anomalies.append(
                _anomaly(
                    "manifest_count_mismatch",
                    f"compiler census {len(records)} exceeds "
                    f"n_compile_entries={n_compile_entries}",
                )
            )

    declared = manifest_obj.get("counts")
    if isinstance(declared, dict) and "relationships" in declared:
        declared_rels = declared.get("relationships")
        if (
            isinstance(declared_rels, bool)
            or not isinstance(declared_rels, int)
            or declared_rels != n_relationships
        ):
            anomalies.append(
                _anomaly(
                    "manifest_count_mismatch",
                    f"manifest counts.relationships={declared_rels!r} != "
                    f"relationship table length {n_relationships}",
                )
            )
    return digest, records, tu_titles


def validate_persisted_compiler_include_overlay(
    entities: Any,
    relationships: Any,
    manifest: Optional[Any] = None,
    *,
    max_anomaly_samples: int = _MAX_ANOMALY_SAMPLES,
) -> Dict[str, Any]:
    """Validate already-persisted compiler ``includes`` overlay evidence.

    Pure and non-mutating. Never invokes a compiler, reads
    ``compile_commands.json`` or C sources, reconstructs ``-E -H`` output,
    reindexes, or repairs rows.
    """
    if max_anomaly_samples < 0:
        max_anomaly_samples = 0

    entities_list = _table_rows(entities, name="entities")
    relationships_list = _table_rows(relationships, name="relationships")
    manifest_obj: Dict[str, Any] = {}
    if manifest is not None:
        if isinstance(manifest, dict):
            manifest_obj = manifest
        elif hasattr(manifest, "items"):
            manifest_obj = dict(manifest)
        else:
            raise TypeError("manifest must be a mapping or None")

    anomalies: List[Dict[str, Any]] = []

    file_entities: Dict[str, List[Dict[str, Any]]] = {}
    for entity in entities_list:
        if str(_row_get(entity, "type") or "") != "file":
            continue
        title = _row_get(entity, "title")
        if not isinstance(title, str) or not title:
            continue
        file_entities.setdefault(title, []).append(entity)

    seen_ids: Dict[str, int] = {}
    seen_hr: Dict[int, str] = {}
    for index, row in enumerate(relationships_list):
        raw_id = _row_get(row, "id")
        if not is_material_value(raw_id) or not str(raw_id).strip():
            anomalies.append(
                _anomaly(
                    "empty_relationship_id",
                    f"relationship at index {index} has empty id",
                )
            )
            continue
        relationship_id = str(raw_id)
        if relationship_id in seen_ids:
            anomalies.append(
                _anomaly(
                    "duplicate_relationship_id",
                    f"duplicate relationship id {relationship_id!r}",
                    relationship_id=relationship_id,
                    extra={"other_index": seen_ids[relationship_id]},
                )
            )
        else:
            seen_ids[relationship_id] = index
        hid = _as_int(_row_get(row, "human_readable_id"))
        if hid is not None:
            other = seen_hr.get(hid)
            if other is not None:
                anomalies.append(
                    _anomaly(
                        "human_readable_id",
                        f"duplicate human_readable_id {hid}",
                        relationship_id=relationship_id,
                        extra={"other_id": other},
                    )
                )
            else:
                seen_hr[hid] = relationship_id

    carrying: List[Dict[str, Any]] = []
    decorated: List[Dict[str, Any]] = []
    for row in relationships_list:
        if not _has_include_marker(row):
            continue
        carrying.append(row)
        relationship_id = str(_row_get(row, "id") or "") or None
        if _complete_include_identity(row):
            decorated.append(row)
        else:
            anomalies.append(
                _anomaly(
                    "contradictory_carrier",
                    "compiler-include identity markers are partial or "
                    f"contradictory: type={_row_get(row, 'type')!r} "
                    f"fact_kind={_row_get(row, 'fact_kind')!r} "
                    f"extractor={_row_get(row, 'extractor')!r}",
                    relationship_id=relationship_id,
                )
            )

    has_block = "compiler_includes" in manifest_obj
    block = manifest_obj.get("compiler_includes")
    mode_state = "legacy_absent"
    block_enabled = False

    if not has_block:
        if carrying:
            anomalies.append(
                _anomaly(
                    "legacy_block_missing_with_fields",
                    f"manifest lacks compiler_includes but graph has "
                    f"{len(carrying)} compiler-include relationship(s)",
                    extra={"n_relationships": len(carrying)},
                )
            )
            mode_state = "invalid"
    elif not isinstance(block, dict):
        anomalies.append(
            _anomaly(
                "invalid_enabled_block",
                f"compiler_includes manifest block is not an object: "
                f"{type(block).__name__}",
            )
        )
        mode_state = "invalid"
    else:
        mode = block.get("mode")
        enabled = block.get("enabled")
        if mode == "off" and enabled is False:
            mode_state = "off"
            extra = _unexpected_keys(block, _OFF_MANIFEST_KEYS)
            missing = [key for key in _OFF_MANIFEST_KEYS if key not in block]
            if extra:
                anomalies.append(
                    _anomaly(
                        "extra_manifest_key",
                        f"off compiler_includes block has extra keys: {extra}",
                    )
                )
            if missing:
                anomalies.append(
                    _anomaly(
                        "missing_manifest_key",
                        f"off compiler_includes block is missing keys: "
                        f"{missing}",
                    )
                )
            if carrying:
                anomalies.append(
                    _anomaly(
                        "off_with_decorated_relationships",
                        f"compiler_includes is off but graph has "
                        f"{len(carrying)} compiler-include relationship(s)",
                        extra={"n_relationships": len(carrying)},
                    )
                )
            disabled_counts = {
                key: block.get(key) for key in ("n_facts", "n_translation_units")
            }
            if any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value != 0
                for value in disabled_counts.values()
            ):
                anomalies.append(
                    _anomaly(
                        "manifest_count_mismatch",
                        f"off block has invalid zero census {disabled_counts!r}",
                    )
                )
        elif mode == MODE and enabled is True:
            mode_state = "enabled"
            block_enabled = True
        else:
            anomalies.append(
                _anomaly(
                    "invalid_enabled_block",
                    f"compiler_includes enablement inconsistent: "
                    f"mode={mode!r} enabled={enabled!r}",
                )
            )
            mode_state = "invalid"
            block_enabled = bool(enabled) or mode == MODE

    manifest_digest: Optional[str] = None
    compiler_records: List[Dict[str, Any]] = []
    tu_titles: Set[str] = set()
    if block_enabled and isinstance(block, dict):
        manifest_digest, compiler_records, tu_titles = (
            _validate_include_manifest_block(
                block,
                n_decorated=len(decorated),
                n_relationships=len(relationships_list),
                file_entities=file_entities,
                manifest_obj=manifest_obj,
                anomalies=anomalies,
            )
        )

    pairs: Set[Tuple[str, str]] = set()
    if block_enabled or (mode_state == "invalid" and carrying):
        for row in sorted(
            decorated,
            key=lambda item: (
                str(_row_get(item, "source") or ""),
                str(_row_get(item, "target") or ""),
                str(_row_get(item, "id") or ""),
            ),
        ):
            pair = _validate_decorated_relationship(
                row,
                relationship_id=str(_row_get(row, "id") or "") or None,
                file_entities=file_entities,
                manifest_digest=manifest_digest,
                compiler_records=compiler_records,
                anomalies=anomalies,
            )
            if pair is not None:
                if pair in pairs:
                    anomalies.append(
                        _anomaly(
                            "duplicate_endpoint_pair",
                            f"duplicate include pair {pair[0]} -> {pair[1]}",
                            relationship_id=str(_row_get(row, "id") or "") or None,
                        )
                    )
                pairs.add(pair)

    anomalies.sort(
        key=lambda item: (
            str(item.get("code") or ""),
            str(item.get("relationship_id") or ""),
            str(item.get("message") or ""),
            _strict_canonical_json(
                {
                    key: item[key]
                    for key in sorted(item, key=lambda name: repr(name))
                    if key not in {"code", "message", "relationship_id"}
                }
            ),
        )
    )
    total = len(anomalies)
    samples = anomalies[:max_anomaly_samples]
    ok = total == 0 and mode_state in {"legacy_absent", "off", "enabled"}
    status = mode_state if ok else "invalid"
    return {
        "ok": ok,
        "status": status,
        "mode": mode_state,
        "n_entities": len(entities_list),
        "n_relationships": len(relationships_list),
        "n_decorated_relationships": len(decorated),
        "n_include_carriers": len(carrying),
        "n_translation_units": len(tu_titles),
        "n_anomalies": total,
        "n_anomaly_samples": len(samples),
        "anomalies_truncated": total > len(samples),
        "anomalies": samples,
        "counts": {
            "n_facts": len(decorated),
            "n_translation_units": len(tu_titles),
            "n_include_carriers": len(carrying),
        },
        "provenance": {
            "compile_commands_digest": manifest_digest,
            "compilers": [
                {
                    "compiler_path": compiler.get("compiler_path"),
                    "compiler_id": compiler.get("compiler_id"),
                }
                for compiler in compiler_records
            ],
        },
        "overlay_mode": MODE,
        "fact_kind": FACT_KIND,
        "extractor": EXTRACTOR,
        "limitations": list(LIMITATIONS),
        "confidence_boundary": CONFIDENCE_BOUNDARY,
    }
