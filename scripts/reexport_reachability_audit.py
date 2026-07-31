#!/usr/bin/env python
"""Measure whether initializer re-exports have a graph target or trace consumer.

The initializer audit calls every public name beyond a direct function/class
definition a ``reexport``. This audit refines that residual: some bindings have
an imported source identity, while immutable module values (for example version
strings) do not. Bare ``__init__.py`` aliases are deliberately not duplicate
graph entities. This audit keeps both populations concrete instead of calling
the residual cosmetic by default. It independently:

* discovers Python targets and initializer modules from ``port_gates.json``;
* imports every initializer in a clean subprocess and records each re-export's
  runtime identity (source file plus qualified name);
* resolves that identity to an existing fresh graph entity, when one exists;
  and
* runs each available golden-workload call trace and reports whether the
  *defining target* executed.

The final item is deliberately an upper bound on alias use. ``sys.setprofile``
sees the defining function/class code object after attribute lookup, not whether
the caller wrote ``package.name`` or imported the defining module directly.
An observed target therefore proves that its implementation ran, not that the
package alias routed the call.  This distinction is why adding an ``exports``
edge cannot improve the call oracle's title mapper without a separate alias
node or lookup tracer.

Usage:
    uv run python scripts/reexport_reachability_audit.py
    uv run python scripts/reexport_reachability_audit.py --json
    uv run python scripts/reexport_reachability_audit.py --check
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
sys.path.insert(0, str(ROOT / "scripts"))

from call_graph_oracle import known_contracts, trace_contract_calls  # type: ignore
from init_api_runtime_audit import initializer_modules, python_targets  # type: ignore
from mini_game_to_byog import build_byog_for_package  # type: ignore


def _runtime_probe_script() -> str:
    """Return a clean-process probe for public re-export identities."""
    return textwrap.dedent(
        r"""
        import importlib
        import inspect
        import json
        import sys
        from pathlib import Path

        package = Path(sys.argv[1]).resolve()
        modules = json.loads(sys.argv[2])
        sys.path.insert(0, str(package.parent))
        rows = []

        def origin_for(value):
            try:
                path = inspect.getsourcefile(value)
            except (OSError, TypeError):
                path = None
            if path is None:
                path = getattr(value, "__file__", None)
            return str(Path(path).resolve()) if path else None

        for spec in modules:
            row = {
                "module": spec["module"],
                "path": spec["path"],
                "title_prefix": spec["title_prefix"],
                "direct_definitions": spec["direct_definitions"],
            }
            try:
                imported = importlib.import_module(spec["module"])
                origin = Path(getattr(imported, "__file__", "")).resolve()
                if package not in origin.parents and origin != package / "__init__.py":
                    raise ImportError(f"module resolved outside vendored package: {origin}")
                raw_all = getattr(imported, "__all__", None)
                if raw_all is None:
                    public = [name for name in imported.__dict__ if not name.startswith("_")]
                elif isinstance(raw_all, (list, tuple)) and all(isinstance(name, str) for name in raw_all):
                    public = list(raw_all)
                else:
                    raise TypeError("__all__ must be a list/tuple of strings")
                public = sorted(set(public) | set(spec["direct_definitions"]))
                direct = set(spec["direct_definitions"])
                exports = []
                for name in public:
                    if name in direct:
                        continue
                    if not hasattr(imported, name):
                        raise AttributeError(f"public re-export has no binding: {name}")
                    value = getattr(imported, name)
                    if inspect.ismodule(value):
                        kind = "module"
                    elif inspect.isclass(value):
                        kind = "class"
                    elif inspect.isroutine(value):
                        kind = "routine"
                    else:
                        kind = "value"
                    exports.append({
                        "name": name,
                        "kind": kind,
                        "origin": origin_for(value),
                        "qualname": getattr(value, "__qualname__", None),
                        "module_name": getattr(value, "__module__", None),
                    })
                row.update(status="ok", exports=exports)
            except Exception as exc:
                row.update(status="error", detail=f"{type(exc).__name__}: {exc}")
            rows.append(row)
        print(json.dumps({"ok": True, "rows": rows}, sort_keys=True))
        """
    ).strip()


def runtime_reexports(package: Path, modules: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return imported initializer re-exports with their runtime identities."""
    payload = json.dumps(list(modules), sort_keys=True)
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _runtime_probe_script(), str(package), payload],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"re-export runtime probe timed out for {package}") from exc
    except OSError as exc:
        raise RuntimeError(f"could not start re-export runtime probe for {package}: {exc}") from exc
    if proc.returncode != 0:
        raise RuntimeError(
            f"re-export runtime probe failed for {package}: "
            f"{(proc.stderr or proc.stdout).strip()[-800:]}"
        )
    try:
        data = json.loads((proc.stdout or "").strip())
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"re-export runtime probe returned non-JSON for {package}") from exc
    rows = data.get("rows") if isinstance(data, dict) else None
    if not isinstance(data, dict) or data.get("ok") is not True or not isinstance(rows, list):
        raise RuntimeError(f"re-export runtime probe failed for {package}: {data!r}")
    return [dict(row) for row in rows if isinstance(row, dict)]


def _resolved_path(value: object) -> Path | None:
    """Normalize source-file fields without treating absent provenance as a path."""
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    try:
        return path.resolve()
    except OSError:
        return None


def _target_candidates(
    export: dict[str, Any], entities: list[dict[str, Any]]
) -> list[str]:
    """Find existing entity titles with the export's runtime source identity."""
    origin = _resolved_path(export.get("origin"))
    if origin is None:
        return []
    source_entities = [
        entity
        for entity in entities
        if _resolved_path(entity.get("source_file")) == origin
    ]
    if export.get("kind") == "module":
        return sorted(
            str(entity["title"])
            for entity in source_entities
            if entity.get("type") == "module"
        )
    qualname = export.get("qualname")
    if not isinstance(qualname, str) or not qualname:
        return []
    return sorted(
        str(entity["title"])
        for entity in source_entities
        if str(entity.get("title") or "").endswith(f":{qualname}")
    )


def _classify_export(
    export: dict[str, Any], entities: list[dict[str, Any]], package: Path
) -> dict[str, Any]:
    """Attach a unique existing target, or a measured reason it has none."""
    row = dict(export)
    candidates = _target_candidates(row, entities)
    row["target_candidates"] = candidates
    if len(candidates) == 1:
        row["target_status"] = "resolved"
        row["target"] = candidates[0]
    elif len(candidates) > 1:
        row["target_status"] = "ambiguous"
        row["target"] = None
    else:
        origin = _resolved_path(row.get("origin"))
        if origin is None:
            row["target_status"] = "no_source_identity"
        elif package not in origin.parents and origin != package / "__init__.py":
            row["target_status"] = "outside_package"
        elif origin.name == "__init__.py":
            row["target_status"] = "initializer_not_indexed"
        else:
            row["target_status"] = "no_matching_entity"
        row["target"] = None
    return row


def _trace_endpoints(package_id: str, package: Path, titles: set[str]) -> dict[str, Any]:
    """Trace a registered golden workload, or report that none exists."""
    contract = known_contracts().get(package_id)
    if contract is None:
        return {"status": "not_registered", "endpoints": set(), "cases": 0}
    trace = trace_contract_calls(package, contract.workload, titles)
    if not trace.get("ok"):
        return {
            "status": "error",
            "detail": str(trace.get("error") or "trace failed"),
            "endpoints": set(),
            "cases": 0,
        }
    endpoints = {
        endpoint
        for edge in trace.get("observed_mapped") or set()
        for endpoint in edge
    }
    return {
        "status": "measured",
        "endpoints": endpoints,
        "cases": int(trace.get("n_workload_cases") or 0),
        "workload": trace.get("workload"),
    }


def build_report() -> dict[str, Any]:
    """Measure all manifest-declared Python initializer re-exports."""
    packages: list[dict[str, Any]] = []
    trace_errors: list[str] = []
    for target in python_targets():
        package = (ROOT / target["source"]).resolve()
        modules = initializer_modules(package)
        if not modules:
            packages.append(
                {"id": target["id"], "status": "no_initializer", "modules": [], "trace": None}
            )
            continue
        graph = build_byog_for_package(package_dir=package)
        entities = [dict(entity) for entity in graph["entities"]]
        titles = {str(entity.get("title") or "") for entity in entities}
        trace = _trace_endpoints(target["id"], package, titles)
        if trace["status"] == "error":
            trace_errors.append(f"{target['id']}: {trace.get('detail')}")
        runtime_rows = runtime_reexports(package, modules)
        if len(runtime_rows) != len(modules):
            raise RuntimeError(
                f"runtime probe returned {len(runtime_rows)} rows for {len(modules)} initializers in {package}"
            )
        reports: list[dict[str, Any]] = []
        for module in runtime_rows:
            if module.get("status") != "ok":
                reports.append(
                    {
                        "module": module.get("module"),
                        "path": module.get("path"),
                        "status": "error",
                        "detail": module.get("detail"),
                        "exports": [],
                    }
                )
                continue
            exports = [
                _classify_export(export, entities, package)
                for export in module.get("exports") or []
                if isinstance(export, dict)
            ]
            observed = set(trace["endpoints"])
            for export in exports:
                export["target_observed"] = bool(
                    trace["status"] == "measured" and export.get("target") in observed
                )
            reports.append(
                {
                    "module": str(module["module"]),
                    "path": str(module["path"]),
                    "status": "ok",
                    "module_entity_present": any(
                        entity.get("type") == "module"
                        and _resolved_path(entity.get("source_file"))
                        == _resolved_path(module.get("path"))
                        for entity in entities
                    ),
                    "exports": exports,
                }
            )
        packages.append(
            {
                "id": target["id"],
                "status": "ok" if all(row["status"] == "ok" for row in reports) else "error",
                "modules": reports,
                "trace": {
                    key: value
                    for key, value in trace.items()
                    if key != "endpoints"
                },
            }
        )

    exports = [
        export
        for package in packages
        for module in package.get("modules") or []
        for export in module.get("exports") or []
    ]
    statuses = Counter(str(export.get("target_status")) for export in exports)
    by_package: dict[str, int] = {}
    observed_by_package: dict[str, int] = {}
    traced_reexports = 0
    untraced_reexports = 0
    for package in packages:
        package_exports = [
            export for module in package.get("modules") or [] for export in module.get("exports") or []
        ]
        by_package[str(package["id"])] = len(package_exports)
        trace_status = (package.get("trace") or {}).get("status")
        if trace_status == "measured":
            traced_reexports += len(package_exports)
            observed_by_package[str(package["id"])] = sum(
                bool(export.get("target_observed")) for export in package_exports
            )
        else:
            untraced_reexports += len(package_exports)

    runtime_errors = sum(
        1
        for package in packages
        for module in package.get("modules") or []
        if module.get("status") != "ok"
    )
    module_rows = [
        module for package in packages for module in package.get("modules") or []
    ]
    exporting_module_rows = [
        module for module in module_rows if len(module.get("exports") or []) > 0
    ]
    measured_traces = [
        package.get("trace") or {}
        for package in packages
        if (package.get("trace") or {}).get("status") == "measured"
    ]
    totals = {
        "reexports": len(exports),
        "target_resolved": statuses["resolved"],
        "target_ambiguous": statuses["ambiguous"],
        "target_initializer_not_indexed": statuses["initializer_not_indexed"],
        "target_outside_package": statuses["outside_package"],
        "target_no_source_identity": statuses["no_source_identity"],
        "target_no_matching_entity": statuses["no_matching_entity"],
        "initializer_modules": len(module_rows),
        "exporting_initializer_module_entities": sum(
            bool(module.get("module_entity_present")) for module in exporting_module_rows
        ),
        "exporting_initializer_module_nodes_needed": sum(
            not bool(module.get("module_entity_present")) for module in module_rows
            if len(module.get("exports") or []) > 0
        ),
        "identity_export_edges_if_modules_added": (
            statuses["resolved"] + statuses["initializer_not_indexed"]
        ),
        "traced_reexports": traced_reexports,
        "untraced_reexports": untraced_reexports,
        "resolved_targets_observed": len(
            {
                str(export["target"])
                for export in exports
                if export.get("target_observed") and isinstance(export.get("target"), str)
            }
        ),
        "traced_workloads": len(measured_traces),
        "traced_cases": sum(int(trace.get("cases") or 0) for trace in measured_traces),
        "humanize_observed_targets": observed_by_package.get("humanize", 0),
        "semantic_version_observed_targets": observed_by_package.get(
            "semantic_version", 0
        ),
        "sqlparse_observed_targets": observed_by_package.get("sqlparse", 0),
        "runtime_errors": runtime_errors,
        "trace_errors": len(trace_errors),
    }
    return {
        "ok": runtime_errors == 0 and not trace_errors,
        "packages": packages,
        "totals": totals,
        "by_package": by_package,
        "observed_by_package": observed_by_package,
        "trace_errors": trace_errors,
    }


def format_report(report: dict[str, Any]) -> str:
    """Render the static identity and trace-reachability populations separately."""
    lines = ["Re-export reachability audit"]
    for package in report["packages"]:
        if package["status"] == "no_initializer":
            lines.append(f"  {package['id']}: no __init__.py")
            continue
        trace = package.get("trace") or {}
        lines.append(
            f"  {package['id']}: {package['status']}; trace={trace.get('status')}"
            + (f" cases={trace.get('cases')}" if trace.get("status") == "measured" else "")
        )
        for module in package["modules"]:
            if module["status"] != "ok":
                lines.append(f"    {module['module']}: ERROR {module.get('detail')}")
                continue
            statuses = Counter(str(export.get("target_status")) for export in module["exports"])
            observed = sum(bool(export.get("target_observed")) for export in module["exports"])
            lines.append(
                f"    {module['module']}: exports={len(module['exports'])}, "
                f"resolved={statuses['resolved']}, observed-targets={observed}, "
                f"unresolved={len(module['exports']) - statuses['resolved']}, "
                f"module-entity={'yes' if module.get('module_entity_present') else 'no'}"
            )
    totals = report["totals"]
    lines.append(
        "  total: reexports={reexports}, targets={target_resolved} resolved/"
        "{target_ambiguous} ambiguous/{target_initializer_not_indexed} initializer-not-indexed/"
        "{target_outside_package} outside/{target_no_source_identity} no-source/"
        "{target_no_matching_entity} no-matching-entity; initializer-modules="
        "{initializer_modules} total, export-sources={exporting_initializer_module_entities} present/"
        "{exporting_initializer_module_nodes_needed} needed, "
        "identity-edges-if-added={identity_export_edges_if_modules_added}; traced={traced_reexports}, "
        "untraced={untraced_reexports}, observed-targets={resolved_targets_observed}, "
        "workloads={traced_workloads}/{traced_cases} cases, runtime_errors={runtime_errors}, "
        "trace_errors={trace_errors}".format(**totals)
    )
    lines.append("  RESULT: PASS" if report["ok"] else "  RESULT: FAIL")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit the complete machine-readable audit")
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero on a runtime binding/import error or failed registered trace",
    )
    args = parser.parse_args(argv)
    report = build_report()
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else format_report(report))
    return 0 if not args.check or report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
