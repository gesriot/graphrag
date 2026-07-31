"""Regression tests for runtime identities behind initializer public bindings."""
from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

from init_api_runtime_audit import initializer_modules  # type: ignore
from mini_game_to_byog import build_byog_for_package  # type: ignore
from reexport_reachability_audit import (  # type: ignore
    _classify_export,
    runtime_reexports,
)


def _exports(package: Path) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Return one initializer's runtime bindings and fresh graph entities."""
    modules = initializer_modules(package)
    assert len(modules) == 1
    runtime = runtime_reexports(package, modules)
    assert len(runtime) == 1
    assert runtime[0]["status"] == "ok"
    data = build_byog_for_package(package_dir=package)
    return list(runtime[0]["exports"]), list(data["entities"])


def test_imported_reexport_resolves_to_its_defining_entity(tmp_path: Path):
    """The graph target comes from source identity, never an alias title guess."""
    package = tmp_path / "export_identity"
    package.mkdir()
    (package / "impl.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    (package / "__init__.py").write_text(
        "from .impl import helper\n__all__ = ['helper']\n", encoding="utf-8"
    )

    exports, entities = _exports(package)
    assert len(exports) == 1
    classified = _classify_export(exports[0], entities, package)

    assert classified["name"] == "helper"
    assert classified["target_status"] == "resolved"
    assert classified["target"] == "impl:helper"
    assert "export_identity:helper" not in {str(entity["title"]) for entity in entities}


def test_static_initializer_value_is_not_misreported_as_an_identity_alias(tmp_path: Path):
    """A public scalar has no defining code target for an exports relationship."""
    package = tmp_path / "export_value"
    package.mkdir()
    (package / "__init__.py").write_text(
        "VERSION = '1.0'\n__all__ = ['VERSION']\n", encoding="utf-8"
    )

    exports, entities = _exports(package)
    assert len(exports) == 1
    classified = _classify_export(exports[0], entities, package)

    assert classified["name"] == "VERSION"
    assert classified["target_status"] == "no_source_identity"
    assert classified["target"] is None
