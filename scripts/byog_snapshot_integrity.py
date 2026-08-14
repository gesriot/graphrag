#!/usr/bin/env python
"""Read-only integrity for a persisted BYOG snapshot envelope.

Proves that the snapshot directory, manifest core fields, and loaded table
census still agree with what ``publish_byog_snapshot()`` writes. Extra
top-level manifest keys are allowed because overlay blocks live there.

This module never reads files, sources, or compile databases, never invokes
an extractor, compiler, or Clang, never reconstructs overlays, and never
repairs or republishes a snapshot.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

_MAX_ANOMALY_SAMPLES = 40
_MAX_ANOMALY_MESSAGE = 400

SCHEMA_VERSION = 1
REQUIRED_CORE_KEYS: Tuple[str, ...] = (
    "id",
    "created_at",
    "schema_version",
    "counts",
    "files",
    "source_root",
    "git_commit",
    "total_size_bytes",
    "corpus_hash",
)
COUNT_KEYS: Tuple[str, ...] = (
    "entities",
    "relationships",
    "text_units",
    "call_observations",
)
REQUIRED_PARQUETS: Tuple[str, ...] = (
    "entities.parquet",
    "relationships.parquet",
    "text_units.parquet",
)
OBS_PARQUET = "call_observations.parquet"
MANIFEST_NAME = "manifest.json"
SETTINGS_NAME = "settings.yaml"
ALLOWED_NON_PARQUET = frozenset({MANIFEST_NAME, SETTINGS_NAME})
CORE_INPUTS = frozenset(REQUIRED_PARQUETS + (OBS_PARQUET, MANIFEST_NAME))
_GIT_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_PRODUCER_CREATED_AT = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{6})?$"
)

LIMITATIONS = (
    "Persisted snapshot directory, core manifest fields, and table census only",
    "Does not hash corpus contents or populate corpus_hash",
    "Does not compare source_root, git_commit, or created_at with the host",
    "Does not invoke an extractor, compiler, or overlay reconstruction",
    "Does not reindex, repair, publish, or rewrite snapshots",
)

ANOMALY_CODES = frozenset(
    {
        "missing_core_key",
        "malformed_core_field",
        "invalid_schema_version",
        "invalid_snapshot_id",
        "snapshot_id_mismatch",
        "invalid_created_at",
        "invalid_counts",
        "count_mismatch",
        "invalid_files",
        "files_mismatch",
        "missing_required_file",
        "unexpected_observation_file",
        "missing_observation_file",
        "zero_row_observation_file",
        "invalid_total_size_bytes",
        "total_size_mismatch",
        "invalid_corpus_hash",
        "invalid_git_commit",
        "invalid_source_root",
        "undeclared_parquet",
        "unexpected_file",
        "temp_remnant",
        "symlinked_core_input",
        "symlinked_snapshot_entry",
        "unexpected_entry",
    }
)


def _strict_canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _clip(text: Any, limit: int = _MAX_ANOMALY_MESSAGE) -> str:
    s = str(text)
    return s if len(s) <= limit else s[: limit - 3] + "..."


def _anomaly(
    code: str,
    message: str,
    *,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if code not in ANOMALY_CODES:
        raise AssertionError(f"unknown snapshot-envelope anomaly code {code!r}")
    row: Dict[str, Any] = {"code": code, "message": _clip(message)}
    if extra:
        for key, value in sorted(extra.items(), key=lambda item: repr(item[0])):
            row[key] = _clip(value) if isinstance(value, str) else value
    return row


def _as_mapping(manifest: Optional[Any]) -> Optional[Dict[str, Any]]:
    if manifest is None:
        return None
    if isinstance(manifest, dict):
        return manifest
    if hasattr(manifest, "items") and not isinstance(manifest, (str, bytes, list, tuple)):
        try:
            return dict(manifest)
        except Exception:
            return None
    return None


def _n_rows(table: Any, *, name: str) -> int:
    if table is None:
        return 0
    if isinstance(table, (str, bytes, dict)):
        raise TypeError(f"{name} must be a dataframe or sequence of rows")
    try:
        return len(table)
    except TypeError as error:
        raise TypeError(f"{name} must be a dataframe or sequence of rows") from error


def _is_strict_int(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return True


def _is_non_negative_int(value: Any) -> bool:
    return _is_strict_int(value) and value >= 0


def _is_safe_snapshot_id(value: str) -> bool:
    if not value or value in {".", ".."}:
        return False
    if value.startswith("."):
        return False
    if value.startswith(".staging-"):
        return False
    if "/" in value or "\\" in value or "\x00" in value:
        return False
    return Path(value).name == value


def _is_temp_remnant(name: str) -> bool:
    return name.endswith(".tmp")


def _is_parquet_name(name: str) -> bool:
    return name.endswith(".parquet") and not name.endswith(".parquet.tmp")


def _file_names(values: Optional[Iterable[Any]]) -> Optional[List[str]]:
    if values is None:
        return None
    names: List[str] = []
    for item in values:
        names.append(str(item))
    return names


def _file_size_map(values: Optional[Mapping[Any, Any]]) -> Optional[Dict[str, int]]:
    if values is None:
        return None
    out: Dict[str, int] = {}
    for key, raw in values.items():
        name = str(key)
        size = raw
        if hasattr(size, "item") and not isinstance(size, (str, bytes, bool)):
            try:
                size = size.item()
            except Exception:
                pass
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            continue
        out[name] = int(size)
    return out


def _parse_created_at(value: Any) -> Optional[datetime]:
    if (
        not isinstance(value, str)
        or _PRODUCER_CREATED_AT.fullmatch(value) is None
    ):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed


def _producer_files(*, has_observations: bool) -> List[str]:
    files = list(REQUIRED_PARQUETS)
    if has_observations:
        files.append(OBS_PARQUET)
    return files


def _sort_anomalies(anomalies: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    def key(item: Mapping[str, Any]) -> Tuple[str, str, str]:
        rest = {k: v for k, v in item.items() if k not in {"code", "message"}}
        return (
            str(item.get("code") or ""),
            str(item.get("message") or ""),
            _strict_canonical_json(rest),
        )

    return [dict(item) for item in sorted(anomalies, key=key)]


def validate_persisted_byog_snapshot(
    entities: Any,
    relationships: Any,
    text_units: Any,
    call_observations: Any,
    manifest: Any,
    *,
    snapshot_id: Optional[str] = None,
    present_files: Optional[Iterable[Any]] = None,
    file_sizes: Optional[Mapping[Any, Any]] = None,
    max_anomaly_samples: int = _MAX_ANOMALY_SAMPLES,
    symlinked_files: Optional[Iterable[Any]] = None,
    unexpected_entries: Optional[Iterable[Any]] = None,
) -> Dict[str, Any]:
    """Validate a persisted BYOG snapshot against the publisher contract.

    Pure and non-mutating. Callers that have a graph root may supply the
    resolved snapshot identity, regular-file inventory, byte sizes, and
    symlink names; this function never reads the filesystem.
    """
    if max_anomaly_samples < 0:
        max_anomaly_samples = 0

    n_entities = _n_rows(entities, name="entities")
    n_relationships = _n_rows(relationships, name="relationships")
    n_text_units = _n_rows(text_units, name="text_units")
    n_observations = _n_rows(call_observations, name="call_observations")
    has_observations = n_observations > 0
    expected_files = _producer_files(has_observations=has_observations)
    inventory = _file_names(present_files)
    sizes = _file_size_map(file_sizes)
    symlinks = _file_names(symlinked_files) or []
    other_entries = _file_names(unexpected_entries) or []
    directory_identity = "unavailable"
    anomalies: List[Dict[str, Any]] = []

    manifest_obj = _as_mapping(manifest)
    if manifest_obj is None:
        anomalies.append(
            _anomaly(
                "malformed_core_field",
                "manifest is not a JSON object",
                extra={"field": "manifest"},
            )
        )
        for key in REQUIRED_CORE_KEYS:
            anomalies.append(
                _anomaly(
                    "missing_core_key",
                    f"manifest is missing required core key {key!r}",
                    extra={"field": key},
                )
            )
    else:
        for key in REQUIRED_CORE_KEYS:
            if key not in manifest_obj:
                anomalies.append(
                    _anomaly(
                        "missing_core_key",
                        f"manifest is missing required core key {key!r}",
                        extra={"field": key},
                    )
                )

        schema_version = manifest_obj.get("schema_version")
        if "schema_version" in manifest_obj:
            if not _is_strict_int(schema_version) or schema_version != SCHEMA_VERSION:
                anomalies.append(
                    _anomaly(
                        "invalid_schema_version",
                        "schema_version must be the strict integer 1",
                        extra={
                            "field": "schema_version",
                            "value": schema_version
                            if isinstance(schema_version, (str, int, float, bool))
                            or schema_version is None
                            else type(schema_version).__name__,
                        },
                    )
                )

        snap_value = manifest_obj.get("id")
        if "id" in manifest_obj:
            if not isinstance(snap_value, str) or not snap_value:
                anomalies.append(
                    _anomaly(
                        "invalid_snapshot_id",
                        "id must be a nonempty safe snapshot id",
                        extra={"field": "id", "value": snap_value},
                    )
                )
            elif not _is_safe_snapshot_id(snap_value):
                anomalies.append(
                    _anomaly(
                        "invalid_snapshot_id",
                        "id is not a safe snapshot directory name",
                        extra={"field": "id", "value": snap_value},
                    )
                )
            elif snapshot_id is not None:
                directory_identity = "matched"
                if snapshot_id != snap_value:
                    directory_identity = "mismatched"
                    anomalies.append(
                        _anomaly(
                            "snapshot_id_mismatch",
                            "manifest.id differs from the snapshot directory",
                            extra={
                                "field": "id",
                                "manifest_id": snap_value,
                                "snapshot_id": snapshot_id,
                            },
                        )
                    )

        created_at = manifest_obj.get("created_at")
        if "created_at" in manifest_obj:
            if _parse_created_at(created_at) is None:
                anomalies.append(
                    _anomaly(
                        "invalid_created_at",
                        "created_at must be a finite producer-style ISO datetime string",
                        extra={"field": "created_at", "value": created_at},
                    )
                )

        counts = manifest_obj.get("counts")
        loaded_counts = {
            "entities": n_entities,
            "relationships": n_relationships,
            "text_units": n_text_units,
            "call_observations": n_observations,
        }
        if "counts" in manifest_obj:
            if not isinstance(counts, dict):
                anomalies.append(
                    _anomaly(
                        "invalid_counts",
                        "counts must be an object with the four producer keys",
                        extra={"field": "counts"},
                    )
                )
            else:
                missing = [key for key in COUNT_KEYS if key not in counts]
                extra_keys = sorted(
                    str(key) for key in counts if key not in COUNT_KEYS
                )
                if missing or extra_keys:
                    anomalies.append(
                        _anomaly(
                            "invalid_counts",
                            "counts must have exactly the producer keys "
                            "entities, relationships, text_units, call_observations",
                            extra={
                                "field": "counts",
                                "missing": missing,
                                "extra": extra_keys,
                            },
                        )
                    )
                for key in COUNT_KEYS:
                    if key not in counts:
                        continue
                    value = counts[key]
                    if not _is_non_negative_int(value):
                        anomalies.append(
                            _anomaly(
                                "invalid_counts",
                                f"counts.{key} must be a strict non-negative integer",
                                extra={
                                    "field": f"counts.{key}",
                                    "value": value
                                    if isinstance(value, (str, int, float, bool))
                                    or value is None
                                    else type(value).__name__,
                                },
                            )
                        )
                    elif value != loaded_counts[key]:
                        anomalies.append(
                            _anomaly(
                                "count_mismatch",
                                f"counts.{key} disagrees with the loaded table",
                                extra={
                                    "field": f"counts.{key}",
                                    "manifest": value,
                                    "loaded": loaded_counts[key],
                                },
                            )
                        )

        files_value = manifest_obj.get("files")
        if "files" in manifest_obj:
            if not isinstance(files_value, list) or any(
                not isinstance(item, str) for item in files_value
            ):
                anomalies.append(
                    _anomaly(
                        "invalid_files",
                        "files must be the ordered producer parquet list",
                        extra={"field": "files"},
                    )
                )
            elif list(files_value) != expected_files:
                anomalies.append(
                    _anomaly(
                        "files_mismatch",
                        "files is not the exact ordered producer parquet list",
                        extra={
                            "field": "files",
                            "expected": expected_files,
                            "actual": list(files_value),
                        },
                    )
                )

        total_size = manifest_obj.get("total_size_bytes")
        if "total_size_bytes" in manifest_obj:
            if not _is_non_negative_int(total_size):
                anomalies.append(
                    _anomaly(
                        "invalid_total_size_bytes",
                        "total_size_bytes must be a strict non-negative integer",
                        extra={
                            "field": "total_size_bytes",
                            "value": total_size
                            if isinstance(total_size, (str, int, float, bool))
                            or total_size is None
                            else type(total_size).__name__,
                        },
                    )
                )
            elif sizes is not None:
                expected_size = 0
                missing_sizes: List[str] = []
                for name in expected_files:
                    if name not in sizes:
                        missing_sizes.append(name)
                        continue
                    expected_size += sizes[name]
                if missing_sizes:
                    anomalies.append(
                        _anomaly(
                            "total_size_mismatch",
                            "total_size_bytes cannot be summed; named parquet sizes are missing",
                            extra={
                                "field": "total_size_bytes",
                                "missing": missing_sizes,
                            },
                        )
                    )
                elif total_size != expected_size:
                    anomalies.append(
                        _anomaly(
                            "total_size_mismatch",
                            "total_size_bytes disagrees with the producer parquet byte total",
                            extra={
                                "field": "total_size_bytes",
                                "manifest": total_size,
                                "computed": expected_size,
                            },
                        )
                    )

        corpus_hash = manifest_obj.get("corpus_hash")
        if "corpus_hash" in manifest_obj and corpus_hash is not None:
            anomalies.append(
                _anomaly(
                    "invalid_corpus_hash",
                    "corpus_hash is currently always null",
                    extra={"field": "corpus_hash", "value": corpus_hash},
                )
            )

        git_commit = manifest_obj.get("git_commit")
        if "git_commit" in manifest_obj and git_commit is not None:
            if not isinstance(git_commit, str) or _GIT_OBJECT_ID.fullmatch(git_commit) is None:
                anomalies.append(
                    _anomaly(
                        "invalid_git_commit",
                        "git_commit must be null or a canonical lowercase Git object id",
                        extra={"field": "git_commit", "value": git_commit},
                    )
                )

        source_root = manifest_obj.get("source_root")
        if "source_root" in manifest_obj and source_root is not None:
            if not isinstance(source_root, str):
                anomalies.append(
                    _anomaly(
                        "invalid_source_root",
                        "source_root must be null or the persisted producer string",
                        extra={"field": "source_root"},
                    )
                )

    if inventory is not None:
        present_set = set(inventory)
        for name in REQUIRED_PARQUETS:
            if name not in present_set:
                anomalies.append(
                    _anomaly(
                        "missing_required_file",
                        f"required parquet {name} is absent",
                        extra={"file": name},
                    )
                )
        if MANIFEST_NAME not in present_set:
            anomalies.append(
                _anomaly(
                    "missing_required_file",
                    "manifest.json is absent from the snapshot inventory",
                    extra={"file": MANIFEST_NAME},
                )
            )
        obs_present = OBS_PARQUET in present_set
        if has_observations and not obs_present:
            anomalies.append(
                _anomaly(
                    "missing_observation_file",
                    "call_observations.parquet must be present for a nonempty table",
                    extra={"file": OBS_PARQUET, "loaded": n_observations},
                )
            )
        if not has_observations and obs_present:
            anomalies.append(
                _anomaly(
                    "zero_row_observation_file"
                    if n_observations == 0
                    else "unexpected_observation_file",
                    "call_observations.parquet must be absent when the observation "
                    "table is empty",
                    extra={"file": OBS_PARQUET, "loaded": n_observations},
                )
            )
        allowed = set(expected_files) | ALLOWED_NON_PARQUET
        for name in sorted(present_set):
            if name == OBS_PARQUET:
                continue
            if _is_temp_remnant(name):
                anomalies.append(
                    _anomaly(
                        "temp_remnant",
                        f"atomic temp remnant {name} must not remain in the snapshot",
                        extra={"file": name},
                    )
                )
                continue
            if _is_parquet_name(name) and name not in expected_files:
                anomalies.append(
                    _anomaly(
                        "undeclared_parquet",
                        f"undeclared parquet {name} is not in the producer file list",
                        extra={"file": name},
                    )
                )
                continue
            if name not in allowed:
                anomalies.append(
                    _anomaly(
                        "unexpected_file",
                        f"file {name} is outside the publisher snapshot inventory",
                        extra={"file": name},
                    )
                )
    for name in sorted(set(symlinks)):
        if name in CORE_INPUTS or _is_parquet_name(name):
            anomalies.append(
                _anomaly(
                    "symlinked_core_input",
                    f"core snapshot input {name} must be a regular file, not a symlink",
                    extra={"file": name},
                )
            )
        elif _is_temp_remnant(name):
            anomalies.append(
                _anomaly(
                    "temp_remnant",
                    f"atomic temp remnant {name} must not remain in the snapshot",
                    extra={"file": name},
                )
            )
        else:
            anomalies.append(
                _anomaly(
                    "symlinked_snapshot_entry",
                    f"snapshot entry {name} must not be a symlink",
                    extra={"file": name},
                )
            )

    for name in sorted(set(other_entries)):
        if _is_temp_remnant(name):
            anomalies.append(
                _anomaly(
                    "temp_remnant",
                    f"atomic temp remnant {name} must not remain in the snapshot",
                    extra={"file": name},
                )
            )
        else:
            anomalies.append(
                _anomaly(
                    "unexpected_entry",
                    f"non-file entry {name} is outside the publisher snapshot inventory",
                    extra={"file": name},
                )
            )

    anomalies = _sort_anomalies(anomalies)
    n_anomalies = len(anomalies)
    samples = anomalies[:max_anomaly_samples]
    ok = n_anomalies == 0
    status = "valid" if ok else "invalid"
    layout = "flat" if snapshot_id is None else "snapshot"
    mode = layout if ok else "invalid"
    return {
        "ok": ok,
        "status": status,
        "state": status,
        "mode": mode,
        "classification": mode,
        "directory_identity": directory_identity,
        "n_anomalies": n_anomalies,
        "n_anomaly_samples": len(samples),
        "anomalies_truncated": n_anomalies > len(samples),
        "anomalies": samples,
        "census": {
            "entities": n_entities,
            "relationships": n_relationships,
            "text_units": n_text_units,
            "call_observations": n_observations,
            "n_present_files": None if inventory is None else len(inventory),
            "n_symlinked_files": len(symlinks),
            "n_unexpected_entries": len(other_entries),
            "n_expected_files": len(expected_files),
            "has_settings_yaml": False
            if inventory is None
            else SETTINGS_NAME in inventory,
        },
        "expected_files": expected_files,
        "limitations": list(LIMITATIONS),
    }
