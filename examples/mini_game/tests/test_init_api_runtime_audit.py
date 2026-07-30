"""Regression tests for direct public APIs defined in package initializers."""
from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

from init_api_runtime_audit import (  # type: ignore
    _fresh_entity_titles,
    _module_report,
    initializer_modules,
    runtime_public_names,
)


def _audit_initializer(package: Path) -> tuple[set[str], dict[str, object]]:
    """Run the independent runtime-to-fresh-graph comparison for one package."""
    modules = initializer_modules(package)
    assert len(modules) == 1
    runtime = runtime_public_names(package, modules)
    assert len(runtime) == 1
    titles = _fresh_entity_titles(package)
    return titles, _module_report(runtime[0], titles)


def test_direct_initializer_api_is_indexed_under_the_package_title(tmp_path: Path):
    """A root ``__init__.py`` definition is an executable API, not a re-export."""
    package = tmp_path / "initializer_direct"
    package.mkdir()
    (package / "impl.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    (package / "__init__.py").write_text(
        "from .impl import helper\n"
        "\n"
        "def entry():\n"
        "    return helper()\n"
        "\n"
        "class Endpoint:\n"
        "    pass\n"
        "\n"
        "__all__ = ['entry', 'Endpoint', 'helper']\n",
        encoding="utf-8",
    )

    titles, report = _audit_initializer(package)

    assert {"initializer_direct:entry", "initializer_direct:Endpoint"} <= titles
    assert report["direct_definitions"] == ["Endpoint", "entry"]
    assert report["direct_present"] == ["Endpoint", "entry"]
    assert report["direct_missing"] == []
    # The imported helper remains the implementation entity, not a duplicate
    # initializer alias. Its visible absence is an explicit report boundary.
    assert report["reexports"] == ["helper"]
    assert report["reexport_missing"] == ["helper"]


def test_reexport_only_initializer_does_not_create_a_duplicate_alias(tmp_path: Path):
    """The narrow rule keeps a bare re-export out while still reporting it."""
    package = tmp_path / "initializer_reexport"
    package.mkdir()
    (package / "impl.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    (package / "__init__.py").write_text(
        "from .impl import helper\n__all__ = ['helper']\n",
        encoding="utf-8",
    )

    titles, report = _audit_initializer(package)

    assert "initializer_reexport:helper" not in titles
    assert "impl:helper" in titles
    assert report["direct_definitions"] == []
    assert report["direct_missing"] == []
    assert report["reexports"] == ["helper"]
    assert report["reexport_missing"] == ["helper"]
