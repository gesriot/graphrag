#!/usr/bin/env python
"""Audit public ``__init__.py`` APIs against fresh Python graph entities.

The generic Python bridge historically skipped every initializer to avoid
creating alias entities for bare re-exports. That also removed a package's own
public functions when they are defined in ``__init__.py`` (for example
``sqlparse.split``). This audit keeps those populations distinct:

* the gate manifest independently enumerates every Python target;
* an isolated subprocess imports every initializer and obtains its public names
  from ``__all__`` plus direct public definitions (or visible names where no
  ``__all__`` exists);
* a fresh graph supplies entity titles; and
* direct initializer definitions and bare re-exports are reported separately.

The ``--check`` contract is deliberately narrow: every runtime-visible direct
function/class definition in an initializer must have a graph entity. Bare
re-exports remain visible in the report but are not silently treated as
missing definitions, because representing them requires a separate alias edge
or duplicate entity policy.

Usage:
    uv run python scripts/init_api_runtime_audit.py
    uv run python scripts/init_api_runtime_audit.py --json
    uv run python scripts/init_api_runtime_audit.py --check
"""
from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any, Iterable, Sequence


ROOT = Path(__file__).resolve().parents[1]
GATES = ROOT / "scripts" / "port_gates.json"


def python_targets(manifest: Path = GATES) -> list[dict[str, str]]:
    """Discover Python source targets from the validated gate manifest input."""
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    targets: list[dict[str, str]] = []
    for entry in raw.get("ports") or []:
        if not isinstance(entry, dict) or entry.get("indexer") != "python":
            continue
        ident = entry.get("id")
        source = entry.get("source")
        if not isinstance(ident, str) or not isinstance(source, str):
            raise ValueError(f"invalid Python target in {manifest}: {entry!r}")
        targets.append({"id": ident, "source": source})
    if not targets:
        raise ValueError(f"no Python targets found in {manifest}")
    return sorted(targets, key=lambda target: target["id"])


def initializer_modules(package: Path) -> list[dict[str, Any]]:
    """Enumerate initializer modules and their direct public function/classes."""
    if not package.is_dir():
        raise FileNotFoundError(f"Python target source is absent: {package}")
    modules: list[dict[str, Any]] = []
    for init in sorted(package.rglob("__init__.py")):
        relative = init.relative_to(package)
        parts = relative.parts[:-1]
        module = ".".join([package.name, *parts])
        title_prefix = ".".join(parts) if parts else package.name
        try:
            tree = ast.parse(init.read_text(encoding="utf-8"), filename=str(init))
        except (OSError, SyntaxError) as exc:
            raise RuntimeError(f"cannot parse initializer {init}: {exc}") from exc
        definitions = sorted(
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and not node.name.startswith("_")
        )
        modules.append(
            {
                "path": str(init),
                "module": module,
                "title_prefix": title_prefix,
                "direct_definitions": definitions,
            }
        )
    return modules


def _runtime_probe_script() -> str:
    """Create a clean-process initializer public-name probe."""
    return textwrap.dedent(
        r"""
        import importlib
        import json
        import sys
        from pathlib import Path

        package = Path(sys.argv[1]).resolve()
        modules = json.loads(sys.argv[2])
        sys.path.insert(0, str(package.parent))
        rows = []
        for spec in modules:
            row = {"module": spec["module"], "title_prefix": spec["title_prefix"],
                   "path": spec["path"], "direct_definitions": spec["direct_definitions"]}
            try:
                imported = importlib.import_module(spec["module"])
                origin = Path(getattr(imported, "__file__", "")).resolve()
                if package not in origin.parents and origin != package / "__init__.py":
                    raise ImportError(f"module resolved outside vendored package: {origin}")
                raw_all = getattr(imported, "__all__", None)
                if raw_all is None:
                    explicit = []
                    visible = [name for name in imported.__dict__ if not name.startswith("_")]
                elif isinstance(raw_all, (list, tuple)) and all(isinstance(name, str) for name in raw_all):
                    explicit = list(raw_all)
                    visible = []
                else:
                    raise TypeError("__all__ must be a list/tuple of strings")
                public = sorted(set(explicit) | set(visible) | set(spec["direct_definitions"]))
                missing_bindings = [name for name in public if not hasattr(imported, name)]
                if missing_bindings:
                    raise AttributeError(f"public names without runtime binding: {missing_bindings}")
                row.update(
                    status="ok",
                    explicit_all=sorted(set(explicit)),
                    visible_without_all=sorted(set(visible)),
                    public_names=public,
                )
            except Exception as exc:
                row.update(status="error", detail=f"{type(exc).__name__}: {exc}")
            rows.append(row)
        print(json.dumps({"ok": True, "rows": rows}, sort_keys=True))
        """
    ).strip()


def runtime_public_names(package: Path, modules: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Import initializers in a clean process and return their public surface."""
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
        raise RuntimeError(f"initializer runtime probe timed out for {package}") from exc
    except OSError as exc:
        raise RuntimeError(f"could not start initializer runtime probe for {package}: {exc}") from exc
    if proc.returncode != 0:
        raise RuntimeError(
            f"initializer runtime probe failed for {package}: "
            f"{(proc.stderr or proc.stdout).strip()[-800:]}"
        )
    try:
        data = json.loads((proc.stdout or "").strip())
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"initializer runtime probe returned non-JSON for {package}") from exc
    if not isinstance(data, dict) or data.get("ok") is not True or not isinstance(data.get("rows"), list):
        raise RuntimeError(f"initializer runtime probe failed for {package}: {data!r}")
    return [dict(row) for row in data["rows"] if isinstance(row, dict)]


def _fresh_entity_titles(package: Path) -> set[str]:
    """Build a disposable fresh graph and return its independently named entities."""
    sys.path.insert(0, str(ROOT / "scripts"))
    from mini_game_to_byog import build_byog_for_package  # type: ignore

    data = build_byog_for_package(package_dir=package)
    return {str(entity.get("title") or "") for entity in data["entities"]}


def _module_report(row: dict[str, Any], entity_titles: set[str]) -> dict[str, Any]:
    """Compare one runtime initializer module with fresh graph titles."""
    if row.get("status") != "ok":
        return {
            "module": row.get("module"),
            "path": row.get("path"),
            "status": "error",
            "detail": row.get("detail"),
        }
    prefix = str(row["title_prefix"])
    public = [str(name) for name in row["public_names"]]
    direct = [str(name) for name in row["direct_definitions"]]
    direct_set = set(direct)
    present = sorted(name for name in public if f"{prefix}:{name}" in entity_titles)
    missing = sorted(name for name in public if f"{prefix}:{name}" not in entity_titles)
    direct_present = sorted(name for name in direct if f"{prefix}:{name}" in entity_titles)
    direct_missing = sorted(name for name in direct if f"{prefix}:{name}" not in entity_titles)
    reexports = sorted(name for name in public if name not in direct_set)
    reexport_present = sorted(name for name in reexports if f"{prefix}:{name}" in entity_titles)
    reexport_missing = sorted(name for name in reexports if f"{prefix}:{name}" not in entity_titles)
    return {
        "module": str(row["module"]),
        "path": str(row["path"]),
        "status": "ok",
        "public_names": public,
        "direct_definitions": direct,
        "reexports": reexports,
        "present": present,
        "missing": missing,
        "direct_present": direct_present,
        "direct_missing": direct_missing,
        "reexport_present": reexport_present,
        "reexport_missing": reexport_missing,
    }


def build_report(manifest: Path = GATES) -> dict[str, Any]:
    """Audit every manifest-declared Python target against a fresh graph."""
    packages: list[dict[str, Any]] = []
    for target in python_targets(manifest):
        package = (ROOT / target["source"]).resolve()
        modules = initializer_modules(package)
        if not modules:
            packages.append(
                {
                    "id": target["id"],
                    "source": target["source"],
                    "status": "no_initializer",
                    "modules": [],
                }
            )
            continue
        runtime_rows = runtime_public_names(package, modules)
        if len(runtime_rows) != len(modules):
            raise RuntimeError(
                f"runtime probe returned {len(runtime_rows)} rows for {len(modules)} initializers in {package}"
            )
        titles = _fresh_entity_titles(package)
        reports = [_module_report(row, titles) for row in runtime_rows]
        packages.append(
            {
                "id": target["id"],
                "source": target["source"],
                "status": "ok" if all(row["status"] == "ok" for row in reports) else "error",
                "modules": reports,
            }
        )

    module_rows = [module for package in packages for module in package["modules"]]
    direct_missing = sum(len(row.get("direct_missing") or []) for row in module_rows)
    direct_total = sum(len(row.get("direct_definitions") or []) for row in module_rows)
    totals = {
        "python_targets": len(packages),
        "targets_with_initializer": sum(1 for package in packages if package["modules"]),
        "initializer_modules": len(module_rows),
        "public_names": sum(len(row.get("public_names") or []) for row in module_rows),
        "direct_definitions": direct_total,
        "direct_present": sum(len(row.get("direct_present") or []) for row in module_rows),
        "direct_missing": direct_missing,
        "reexports": sum(len(row.get("reexports") or []) for row in module_rows),
        "reexport_present": sum(len(row.get("reexport_present") or []) for row in module_rows),
        "reexport_missing": sum(len(row.get("reexport_missing") or []) for row in module_rows),
        "runtime_errors": sum(1 for row in module_rows if row.get("status") != "ok"),
    }
    return {
        "ok": totals["direct_missing"] == 0 and totals["runtime_errors"] == 0,
        "packages": packages,
        "totals": totals,
    }


def format_report(report: dict[str, Any]) -> str:
    """Render both direct definitions and deliberate re-export boundary."""
    lines = ["Initializer API runtime audit"]
    for package in report["packages"]:
        if package["status"] == "no_initializer":
            lines.append(f"  {package['id']}: no __init__.py")
            continue
        lines.append(f"  {package['id']}: {package['status']}")
        for module in package["modules"]:
            if module["status"] != "ok":
                lines.append(f"    {module['module']}: ERROR {module.get('detail')}")
                continue
            lines.append(
                f"    {module['module']}: public={len(module['public_names'])}, "
                f"direct={len(module['direct_definitions'])} "
                f"({len(module['direct_present'])} present/{len(module['direct_missing'])} missing), "
                f"reexports={len(module['reexports'])} "
                f"({len(module['reexport_present'])} present/{len(module['reexport_missing'])} missing)"
            )
            if module["direct_missing"]:
                lines.append(f"      missing direct: {', '.join(module['direct_missing'])}")
    totals = report["totals"]
    lines.append(
        "  total: targets={python_targets}, initializers={initializer_modules}, "
        "public={public_names}, direct={direct_definitions} "
        "({direct_present} present/{direct_missing} missing), reexports={reexports} "
        "({reexport_present} present/{reexport_missing} missing), runtime_errors={runtime_errors}".format(**totals)
    )
    lines.append("  RESULT: PASS" if report["ok"] else "  RESULT: FAIL")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit machine-readable report")
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if a direct initializer definition is absent or a runtime import fails",
    )
    args = parser.parse_args(argv)
    report = build_report()
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else format_report(report))
    return 0 if not args.check or report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
