"""Shared in-memory Clang AST capture for function and call audits.

One compile_commands.json load and one ``-ast-dump=json`` per entry. The
function-definition and call-site audit builders consume the same capture
without re-invoking the compiler.

This is **not** a disk cache: AST roots live only in process memory and must
never appear in manifests, parquet columns, logs, or exception messages.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from graphrag_code.c_clang_ast_audit import (  # type: ignore
    ClangAstAuditError,
    run_ast_dump_for_entry,
)
from graphrag_code.c_compiler_common import (  # type: ignore
    CompilerOverlayError,
    compiler_from_entry,
    load_compile_entries,
    path_is_under,
    prepare_compile_entry,
    reject_hidden_compiler_outputs,
    require_clang_identity,
    validate_compile_entry,
)
from graphrag_code.c_identities import package_relative_posix  # type: ignore


class ClangAstCaptureError(CompilerOverlayError):
    """Raised when a shared AST package capture cannot be built honestly."""


@dataclass(frozen=True)
class CapturedCompileEntry:
    """One validated compile entry with an in-memory AST root."""

    entry_index: int
    cwd: Path
    tu_path: Path
    compiler_path: str
    compiler_id: str
    compiler_version: Optional[str]
    # Never include AST JSON in repr/str/logs.
    ast_root: Any = field(repr=False, compare=False)

    def __str__(self) -> str:
        return (
            f"CapturedCompileEntry(entry_index={self.entry_index}, "
            f"tu_path={self.tu_path!s}, compiler_path={self.compiler_path!r})"
        )


@dataclass(frozen=True)
class ClangAstPackageCapture:
    """In-memory package AST capture (not JSON-serializable by design)."""

    package_dir: Path
    compile_commands_digest: str
    n_compile_entries: int
    compilers: Tuple[Dict[str, Optional[str]], ...]
    entries: Tuple[CapturedCompileEntry, ...] = field(repr=False)

    def __repr__(self) -> str:
        digest = self.compile_commands_digest
        short = digest if len(digest) <= 16 else f"{digest[:12]}…"
        return (
            f"ClangAstPackageCapture(package={self.package_dir.name!r}, "
            f"n_entries={self.n_compile_entries}, digest={short!r}, "
            f"n_compilers={len(self.compilers)})"
        )

    def __str__(self) -> str:
        return repr(self)

    def assert_package(self, package_dir: Path) -> None:
        """Fail if this capture was built for a different package path."""
        want = Path(package_dir).resolve()
        if self.package_dir != want:
            raise ClangAstCaptureError(
                f"AST capture package {self.package_dir} does not match "
                f"requested package {want}"
            )

    def compiler_identity_set(self) -> set[Tuple[str, str]]:
        out: set[Tuple[str, str]] = set()
        for c in self.compilers:
            path = c.get("compiler_path")
            cid = c.get("compiler_id")
            if isinstance(path, str) and path and isinstance(cid, str) and cid:
                out.add((path, cid))
        return out


def _compiler_provenance(
    raw: Any,
    *,
    context: str,
) -> Tuple[str, str, Optional[str]]:
    """Return one canonical compiler provenance triple or fail closed."""
    if not isinstance(raw, dict):
        raise ClangAstCaptureError(f"{context} must be an object")
    path = raw.get("compiler_path")
    compiler_id = raw.get("compiler_id")
    version = raw.get("compiler_version")
    if not isinstance(path, str) or not path.strip():
        raise ClangAstCaptureError(f"{context}.compiler_path is empty")
    path = path.strip()
    if not Path(path).is_absolute():
        raise ClangAstCaptureError(
            f"{context}.compiler_path is not absolute: {path!r}"
        )
    if not isinstance(compiler_id, str) or not compiler_id.strip():
        raise ClangAstCaptureError(f"{context}.compiler_id is empty")
    compiler_id = compiler_id.strip()
    if version is not None and (
        not isinstance(version, str) or not version.strip()
    ):
        raise ClangAstCaptureError(
            f"{context}.compiler_version must be null or a non-empty string"
        )
    return (
        path,
        compiler_id,
        version.strip() if isinstance(version, str) else None,
    )


def validate_clang_ast_capture(capture: ClangAstPackageCapture) -> None:
    """Validate the complete package/entry/compiler census before reuse.

    The dataclasses prevent accidental scalar-field reassignment, but their AST
    dictionaries and compiler records intentionally remain in-memory Python
    objects. Builders therefore re-check the capture boundary before reading it.
    """
    if not isinstance(capture, ClangAstPackageCapture):
        raise ClangAstCaptureError(
            "expected a ClangAstPackageCapture instance"
        )
    package_dir = capture.package_dir
    if (
        not isinstance(package_dir, Path)
        or not package_dir.is_absolute()
        or package_dir != package_dir.resolve()
    ):
        raise ClangAstCaptureError(
            f"capture package_dir is not a canonical absolute path: "
            f"{package_dir!r}"
        )
    digest = capture.compile_commands_digest
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(ch not in "0123456789abcdef" for ch in digest)
    ):
        raise ClangAstCaptureError(
            "capture compile_commands_digest is not a lowercase SHA-256"
        )
    n_entries = capture.n_compile_entries
    if (
        isinstance(n_entries, bool)
        or not isinstance(n_entries, int)
        or n_entries <= 0
    ):
        raise ClangAstCaptureError(
            "capture n_compile_entries must be a positive integer"
        )
    if (
        not isinstance(capture.entries, tuple)
        or len(capture.entries) != n_entries
    ):
        raise ClangAstCaptureError(
            f"capture entry census {len(capture.entries)} disagrees with "
            f"n_compile_entries={n_entries}"
        )
    if not isinstance(capture.compilers, tuple) or not capture.compilers:
        raise ClangAstCaptureError(
            "capture compilers must be a non-empty tuple"
        )

    compiler_records: Set[Tuple[str, str, Optional[str]]] = set()
    compiler_paths: Set[str] = set()
    for position, raw in enumerate(capture.compilers):
        record = _compiler_provenance(
            raw, context=f"capture.compilers[{position}]"
        )
        if record[0] in compiler_paths:
            raise ClangAstCaptureError(
                f"duplicate compiler path in capture.compilers: {record[0]!r}"
            )
        compiler_paths.add(record[0])
        compiler_records.add(record)

    used_records: Set[Tuple[str, str, Optional[str]]] = set()
    for position, entry in enumerate(capture.entries):
        context = f"capture.entries[{position}]"
        if not isinstance(entry, CapturedCompileEntry):
            raise ClangAstCaptureError(f"{context} has the wrong type")
        if entry.entry_index != position:
            raise ClangAstCaptureError(
                f"{context}.entry_index={entry.entry_index!r}; expected {position}"
            )
        for field_name, path in (("cwd", entry.cwd), ("tu_path", entry.tu_path)):
            if (
                not isinstance(path, Path)
                or not path.is_absolute()
                or path != path.resolve()
            ):
                raise ClangAstCaptureError(
                    f"{context}.{field_name} is not a canonical absolute path"
                )
        if not isinstance(entry.ast_root, dict):
            raise ClangAstCaptureError(
                f"{context}.ast_root must be one parsed JSON object"
            )
        record = _compiler_provenance(
            {
                "compiler_path": entry.compiler_path,
                "compiler_id": entry.compiler_id,
                "compiler_version": entry.compiler_version,
            },
            context=context,
        )
        if record not in compiler_records:
            raise ClangAstCaptureError(
                f"{context} names compiler provenance absent from "
                "capture.compilers"
            )
        used_records.add(record)
    if used_records != compiler_records:
        raise ClangAstCaptureError(
            "capture.compilers includes provenance unused by capture.entries"
        )


def capture_clang_ast_package(
    package_dir: Path,
    *,
    timeout: int = 120,
) -> ClangAstPackageCapture:
    """Load compile_commands once and AST-dump each entry exactly once.

    Results are retained in memory only. Fail-closed on timeout, missing DB,
    non-Clang compilers, and existing unsafe compile-argument cases.
    """
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int)
        or timeout <= 0
    ):
        raise ClangAstCaptureError(
            f"timeout must be a positive integer, got {timeout!r}"
        )
    package_dir = Path(package_dir).resolve()
    try:
        raw_entries, digest = load_compile_entries(package_dir)
    except CompilerOverlayError as e:
        raise ClangAstCaptureError(str(e)) from e

    if not raw_entries:
        raise ClangAstCaptureError(
            f"compile_commands.json for {package_dir} has no entries"
        )
    if not isinstance(digest, str) or not digest.strip():
        raise ClangAstCaptureError("compile_commands digest is empty")

    compilers: Dict[str, Dict[str, Optional[str]]] = {}
    captured: List[CapturedCompileEntry] = []
    seen_indices: set[int] = set()

    for entry_index, raw in enumerate(raw_entries):
        if entry_index in seen_indices:
            raise ClangAstCaptureError(
                f"duplicate compile entry index {entry_index}"
            )
        seen_indices.add(entry_index)
        try:
            ent = validate_compile_entry(raw, entry_index)
            cwd, cleaned, src_path = prepare_compile_entry(
                ent, package_dir=package_dir
            )
            reject_hidden_compiler_outputs(cleaned)
            compiler_path = compiler_from_entry(ent, cwd=cwd)
            _, compiler_id, compiler_version = require_clang_identity(
                compiler_path
            )
        except CompilerOverlayError as e:
            raise ClangAstCaptureError(str(e)) from e
        except ClangAstAuditError as e:
            raise ClangAstCaptureError(str(e)) from e

        if not isinstance(compiler_id, str) or not compiler_id.strip():
            raise ClangAstCaptureError(
                f"entry {entry_index} has empty compiler_id"
            )

        if compiler_path not in compilers:
            compilers[compiler_path] = {
                "compiler_path": compiler_path,
                "compiler_id": compiler_id,
                "compiler_version": compiler_version,
            }

        try:
            tu_path, root = run_ast_dump_for_entry(
                ent,
                compiler=compiler_path,
                package_dir=package_dir,
                timeout=timeout,
            )
        except ClangAstAuditError as e:
            # Do not include AST text; audit errors already omit dumps.
            raise ClangAstCaptureError(str(e)) from e
        if tu_path.resolve() != src_path.resolve():
            raise ClangAstCaptureError(
                f"entry {entry_index} AST translation unit disagrees with "
                "the validated compile-command source"
            )

        captured.append(
            CapturedCompileEntry(
                entry_index=entry_index,
                cwd=cwd.resolve(),
                tu_path=tu_path.resolve(),
                compiler_path=compiler_path,
                compiler_id=compiler_id,
                compiler_version=compiler_version,
                ast_root=root,
            )
        )

    if len(captured) != len(raw_entries):
        raise ClangAstCaptureError(
            f"capture entry census {len(captured)} disagrees with "
            f"compile_commands entry count {len(raw_entries)}"
        )

    compiler_list = tuple(compilers[k] for k in sorted(compilers))
    capture = ClangAstPackageCapture(
        package_dir=package_dir,
        compile_commands_digest=digest.strip(),
        n_compile_entries=len(captured),
        compilers=compiler_list,
        entries=tuple(captured),
    )
    validate_clang_ast_capture(capture)
    return capture


def assert_audit_report_matches_capture(
    report: Dict[str, Any],
    capture: ClangAstPackageCapture,
    *,
    context: str,
) -> None:
    """Verify a built audit report agrees with the shared capture census."""
    validate_clang_ast_capture(capture)
    if not isinstance(report, dict):
        raise ClangAstCaptureError(f"{context} report is not an object")
    if str(report.get("package") or "") != capture.package_dir.name:
        raise ClangAstCaptureError(
            f"{context} package {report.get('package')!r} disagrees with "
            f"capture package {capture.package_dir.name!r}"
        )
    digest = report.get("compile_commands_digest")
    if not isinstance(digest, str) or digest != capture.compile_commands_digest:
        raise ClangAstCaptureError(
            f"{context} compile_commands_digest disagrees with capture"
        )
    n = report.get("n_compile_entries")
    if (
        isinstance(n, bool)
        or not isinstance(n, int)
        or n != capture.n_compile_entries
    ):
        raise ClangAstCaptureError(
            f"{context} n_compile_entries={n!r} disagrees with capture "
            f"n={capture.n_compile_entries}"
        )
    report_compilers = report.get("compilers")
    if not isinstance(report_compilers, list) or not report_compilers:
        raise ClangAstCaptureError(
            f"{context} compilers must be a non-empty list"
        )
    report_records: Set[Tuple[str, str, Optional[str]]] = set()
    report_paths: Set[str] = set()
    for position, compiler in enumerate(report_compilers):
        record = _compiler_provenance(
            compiler, context=f"{context}.compilers[{position}]"
        )
        if record[0] in report_paths:
            raise ClangAstCaptureError(
                f"duplicate compiler path in {context}.compilers: {record[0]!r}"
            )
        report_paths.add(record[0])
        report_records.add(record)
    capture_records = {
        _compiler_provenance(c, context="capture compiler")
        for c in capture.compilers
    }
    if report_records != capture_records:
        raise ClangAstCaptureError(
            f"{context} compiler provenance disagrees with capture"
        )

    one = next(iter(capture_records)) if len(capture_records) == 1 else None
    top_level = (
        report.get("compiler_path"),
        report.get("compiler_id"),
        report.get("compiler_version"),
    )
    if one is not None and top_level != one:
        raise ClangAstCaptureError(
            f"{context} singular compiler provenance disagrees with capture"
        )
    if one is None and any(value is not None for value in top_level):
        raise ClangAstCaptureError(
            f"{context} multi-compiler report exposes singular provenance"
        )

    translation_units = report.get("translation_units")
    if (
        not isinstance(translation_units, list)
        or len(translation_units) != capture.n_compile_entries
        or not all(isinstance(tu, dict) for tu in translation_units)
    ):
        raise ClangAstCaptureError(
            f"{context} translation-unit census disagrees with capture"
        )
    by_index: Dict[int, Dict[str, Any]] = {}
    for tu in translation_units:
        index = tu.get("entry_index")
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or index < 0
            or index >= capture.n_compile_entries
            or index in by_index
        ):
            raise ClangAstCaptureError(
                f"{context} has invalid/duplicate translation-unit index {index!r}"
            )
        by_index[index] = tu
    for entry in capture.entries:
        tu = by_index.get(entry.entry_index)
        if tu is None:
            raise ClangAstCaptureError(
                f"{context} omits translation-unit index {entry.entry_index}"
            )
        is_local = path_is_under(entry.tu_path, capture.package_dir)
        expected_file = (
            package_relative_posix(entry.tu_path, capture.package_dir)
            if is_local
            else None
        )
        if (
            tu.get("file") != expected_file
            or tu.get("package_local") is not is_local
            or tu.get("compiler_path") != entry.compiler_path
            or tu.get("compiler_id") != entry.compiler_id
        ):
            raise ClangAstCaptureError(
                f"{context} translation-unit {entry.entry_index} disagrees "
                "with capture"
            )


def assert_function_and_call_reports_agree(
    function_report: Dict[str, Any],
    call_report: Dict[str, Any],
    capture: ClangAstPackageCapture,
) -> None:
    """Cross-check both audit reports against the same capture."""
    assert_audit_report_matches_capture(
        function_report, capture, context="function audit"
    )
    assert_audit_report_matches_capture(
        call_report, capture, context="call audit"
    )
    for field_name in (
        "package",
        "compile_commands_digest",
        "n_compile_entries",
    ):
        if function_report.get(field_name) != call_report.get(field_name):
            raise ClangAstCaptureError(
                f"function/call audit disagree on {field_name}: "
                f"{function_report.get(field_name)!r} vs "
                f"{call_report.get(field_name)!r}"
            )
