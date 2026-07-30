#!/usr/bin/env python
"""Audit same-file inherited-member edges against imported Python runtime state.

The extractor emits a deterministic ``inherits`` relationship from a subclass
to the effective same-file C3-MRO declaration of an unoverridden base member.
This tool deliberately does not reimplement that AST logic.  It first obtains
the extractor's fresh candidate population, then starts a clean Python process
which imports the vendored package and asks the runtime which class actually
owns each member in ``Child.__mro__``.

That distinction matters: a class-level assignment can shadow an inherited
method even though it is not an AST FunctionDef, and multiple inheritance has
to follow Python's C3 order.  Both become explicit mismatches rather than a
second AST reading agreeing with itself.

Usage:
    uv run python scripts/inherited_member_runtime_audit.py
    uv run python scripts/inherited_member_runtime_audit.py --package sqlparse --json
    uv run python scripts/inherited_member_runtime_audit.py --check
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import textwrap
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PACKAGES = ("sqlparse", "semantic_version")


def package_dir(name_or_path: str) -> Path:
    """Resolve a named vendored package or an explicit package directory."""
    named = ROOT / "examples" / name_or_path
    candidate = named if named.is_dir() else Path(name_or_path)
    candidate = candidate.resolve()
    if not candidate.is_dir() or not (candidate / "__init__.py").is_file():
        raise FileNotFoundError(
            f"expected importable vendored package directory for {name_or_path!r}: {candidate}"
        )
    return candidate


def inherited_member_candidates(package: Path) -> list[dict[str, str]]:
    """Enumerate the extractor's member-edge population from a fresh graph."""
    sys.path.insert(0, str(ROOT / "scripts"))
    from mini_game_to_byog import build_byog_for_package  # type: ignore

    data = build_byog_for_package(package_dir=package)
    entity_types = {
        str(entity.get("title") or ""): str(entity.get("type") or "unknown")
        for entity in data["entities"]
    }
    candidates: list[dict[str, str]] = []
    for relation in data["relationships"]:
        if relation.get("type") != "inherits":
            continue
        # ``build_byog_for_package`` deliberately assigns published sequential
        # ids, so the extractor-local ``rel:inherits-member:...`` id is not
        # available here. The description is the stable relationship contract.
        if "inherits unoverridden member" not in str(relation.get("description") or ""):
            continue
        source = str(relation.get("source") or "")
        target = str(relation.get("target") or "")
        if ":" not in source or ":" not in target or "." not in target:
            raise ValueError(f"malformed inherited-member relationship: {relation!r}")
        child = source.split(":", 1)[1]
        base_member = target.split(":", 1)[1]
        base, member = base_member.rsplit(".", 1)
        source_file = str(relation.get("source_file") or "")
        if not source_file:
            raise ValueError(f"member relationship has no source file: {relation!r}")
        candidates.append(
            {
                "source": source,
                "target": target,
                "child": child,
                "base": base,
                "member": member,
                "member_kind": entity_types.get(target, "unknown"),
                "source_file": source_file,
            }
        )
    return sorted(candidates, key=lambda row: (row["source"], row["target"]))


def _runtime_probe_script() -> str:
    """Return the isolated runtime checker used for every candidate edge."""
    return textwrap.dedent(
        r"""
        import importlib
        import json
        import sys
        from pathlib import Path

        package = Path(sys.argv[1]).resolve()
        candidates = json.loads(sys.argv[2])
        sys.path.insert(0, str(package.parent))

        def module_name(source_file):
            source = Path(source_file).resolve()
            try:
                relative = source.relative_to(package)
            except ValueError:
                return None
            parts = list(relative.with_suffix("").parts)
            if relative.name == "__init__.py":
                suffix = parts[:-1]
            else:
                suffix = parts
            return ".".join([package.name] + suffix) if suffix else package.name

        modules = {}
        rows = []
        for candidate in candidates:
            module = module_name(candidate["source_file"])
            row = {"source": candidate["source"], "target": candidate["target"],
                   "child": candidate["child"], "base": candidate["base"],
                   "member": candidate["member"], "member_kind": candidate["member_kind"],
                   "module": module}
            if module is None:
                row.update(status="error", reason="source_file_outside_package")
                rows.append(row)
                continue
            try:
                imported = modules.get(module)
                if imported is None:
                    imported = importlib.import_module(module)
                    modules[module] = imported
                origin = Path(getattr(imported, "__file__", "")).resolve()
                if package not in origin.parents and origin != package / "__init__.py":
                    raise ImportError(f"module resolved outside vendored package: {origin}")
                child = getattr(imported, candidate["child"])
                base = getattr(imported, candidate["base"])
            except Exception as exc:
                row.update(status="error", reason="runtime_import_or_class_lookup",
                           detail=f"{type(exc).__name__}: {exc}")
                rows.append(row)
                continue
            if not isinstance(child, type) or not isinstance(base, type):
                row.update(status="error", reason="runtime_symbol_not_class")
                rows.append(row)
                continue
            row["multiple_inheritance"] = len(child.__bases__) > 1
            row["slotted_child"] = "__slots__" in child.__dict__
            if base not in child.__mro__:
                row.update(status="mismatch", reason="declared_base_not_in_runtime_mro",
                           runtime_mro=[cls.__qualname__ for cls in child.__mro__])
                rows.append(row)
                continue
            owner = next((cls for cls in child.__mro__ if candidate["member"] in cls.__dict__), None)
            if owner is None:
                row.update(status="mismatch", reason="member_absent_from_runtime_mro",
                           runtime_mro=[cls.__qualname__ for cls in child.__mro__])
            elif owner is not base:
                row.update(status="mismatch", reason="runtime_member_owner_differs",
                           runtime_owner=owner.__qualname__,
                           runtime_mro=[cls.__qualname__ for cls in child.__mro__])
            else:
                descriptor = owner.__dict__[candidate["member"]]
                row.update(
                           status="confirmed", runtime_owner=owner.__qualname__,
                           runtime_member_kind=(
                               "property" if isinstance(descriptor, property) else "other"
                           ),
                           runtime_mro=[cls.__qualname__ for cls in child.__mro__])
            rows.append(row)
        print(json.dumps({"ok": True, "rows": rows}, sort_keys=True))
        """
    ).strip()


def verify_against_runtime(package: Path, candidates: Iterable[dict[str, str]]) -> list[dict[str, Any]]:
    """Import *package* in a clean process and classify each candidate edge."""
    payload = json.dumps(list(candidates), sort_keys=True)
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _runtime_probe_script(), str(package), payload],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"runtime audit timed out after 120s for {package}") from exc
    except OSError as exc:
        raise RuntimeError(f"could not start runtime audit for {package}: {exc}") from exc
    if proc.returncode != 0:
        raise RuntimeError(
            f"runtime audit subprocess failed for {package}: "
            f"{(proc.stderr or proc.stdout).strip()[-800:]}"
        )
    try:
        data = json.loads((proc.stdout or "").strip())
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"runtime audit returned non-JSON for {package}: {(proc.stdout or '')[-400:]!r}"
        ) from exc
    if not isinstance(data, dict) or data.get("ok") is not True:
        raise RuntimeError(f"runtime audit failed for {package}: {data!r}")
    rows = data.get("rows")
    if not isinstance(rows, list):
        raise RuntimeError(f"runtime audit returned no rows for {package}")
    return [dict(row) for row in rows if isinstance(row, dict)]


def build_report(packages: Sequence[str] = DEFAULT_PACKAGES) -> dict[str, Any]:
    """Return a full, fresh-extraction/runtime report for the requested packages."""
    reports: list[dict[str, Any]] = []
    for requested in packages:
        package = package_dir(requested)
        candidates = inherited_member_candidates(package)
        rows = verify_against_runtime(package, candidates)
        if len(rows) != len(candidates):
            raise RuntimeError(
                f"runtime audit returned {len(rows)} rows for {len(candidates)} candidates in {package}"
            )
        counts = Counter(str(row.get("status") or "error") for row in rows)
        mismatch_shapes = Counter(
            str(row.get("reason") or "unknown")
            for row in rows
            if row.get("status") == "mismatch"
        )
        runtime_shapes = {
            "method_candidates": sum(
                1 for row in rows if row.get("runtime_member_kind") != "property"
            ),
            "property_candidates": sum(
                1 for row in rows if row.get("runtime_member_kind") == "property"
            ),
            "multiple_inheritance_candidates": sum(
                1 for row in rows if row.get("multiple_inheritance") is True
            ),
            "slotted_child_candidates": sum(
                1 for row in rows if row.get("slotted_child") is True
            ),
        }
        reports.append(
            {
                "package": package.name,
                "path": str(package),
                "candidates": len(candidates),
                "confirmed": counts["confirmed"],
                "mismatches": counts["mismatch"],
                "errors": counts["error"],
                "mismatch_shapes": dict(sorted(mismatch_shapes.items())),
                "runtime_shapes": runtime_shapes,
                "mismatch_rows": [row for row in rows if row.get("status") != "confirmed"],
            }
        )
    total = {
        key: sum(int(report[key]) for report in reports)
        for key in ("candidates", "confirmed", "mismatches", "errors")
    }
    return {
        # A zero-candidate run is a disclosed non-measurement, never evidence
        # that the rule happened to be correct. ``--check`` must fail it.
        "ok": (
            total["candidates"] > 0
            and total["mismatches"] == 0
            and total["errors"] == 0
        ),
        "packages": reports,
        "totals": total,
        "error_rate": (
            (total["mismatches"] + total["errors"]) / total["candidates"]
            if total["candidates"]
            else None
        ),
    }


def format_report(report: dict[str, Any]) -> str:
    """Render the population and residuals without hiding a zero denominator."""
    lines = ["Inherited-member runtime audit"]
    for package in report["packages"]:
        lines.append(
            "  {package}: candidates={candidates}, confirmed={confirmed}, "
            "mismatches={mismatches}, errors={errors}".format(**package)
        )
        if package["mismatch_shapes"]:
            lines.append(f"    mismatch shapes: {package['mismatch_shapes']}")
        lines.append(f"    runtime shapes: {package['runtime_shapes']}")
        for row in package["mismatch_rows"][:12]:
            lines.append(
                f"    {row.get('status')}: {row.get('source')} -> {row.get('target')} "
                f"({row.get('reason')})"
            )
    total = report["totals"]
    rate = report["error_rate"]
    rate_text = "n/a (no candidates)" if rate is None else f"{100.0 * rate:.2f}%"
    lines.append(
        f"  total: candidates={total['candidates']}, confirmed={total['confirmed']}, "
        f"mismatches={total['mismatches']}, errors={total['errors']}, error_rate={rate_text}"
    )
    if total["candidates"] == 0:
        lines.append("  RESULT: NO COMPARABLE EDGES")
    else:
        lines.append("  RESULT: PASS" if report["ok"] else "  RESULT: FAIL")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--package",
        action="append",
        dest="packages",
        help="vendored package name (under examples/) or importable package directory; repeatable",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable report")
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero when any emitted edge disagrees with the imported runtime",
    )
    args = parser.parse_args(argv)
    report = build_report(tuple(args.packages or DEFAULT_PACKAGES))
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else format_report(report))
    return 0 if not args.check or report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
