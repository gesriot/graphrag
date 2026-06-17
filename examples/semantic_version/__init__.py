# -*- coding: utf-8 -*-
# Copyright (c) The python-semanticversion project
# This code is distributed under the two-clause BSD License.
#
# VENDORED for the GraphRAG Python->Rust porting experiment. See PROVENANCE.md.
# Adapted from upstream __init__.py: the importlib.metadata / pkg_resources
# version lookup (which requires the package to be pip-installed) is replaced
# with a static __version__, so the vendored copy imports without installation.

from .base import compare, match, validate, SimpleSpec, NpmSpec, Spec, SpecItem, Version

__author__ = "Raphaël Barrois <raphael.barrois+semver@polytechnique.org>"
__version__ = "2.10.0"

__all__ = [
    "compare",
    "match",
    "validate",
    "SimpleSpec",
    "NpmSpec",
    "Spec",
    "SpecItem",
    "Version",
]
