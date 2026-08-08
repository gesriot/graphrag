#!/usr/bin/env python
"""Classify every scoreable call-oracle miss by runtime construct.

The call oracle's ``missed`` set is small enough to review exhaustively, but a
package-level recall number hides its construct mix.  The miss population comes
directly from ``call_graph_oracle`` rather than copied documentation counts;
this tool is a classification layer over that oracle, not an independent
runtime oracle.  It fails closed if a live edge has no classification, a
classified edge disappears, or a cold-import artifact returns to the workload
population.

The three entries in ``COLD_IMPORT_ARTIFACTS`` belonged to the 22-miss
2026-07-31 baseline.  They were class-body execution during initial import, not
source-level calls, and are retained here so that historical count stays
accountable after profiling was moved behind package import.

Usage:
    uv run python scripts/call_graph_miss_audit.py
    uv run python scripts/call_graph_miss_audit.py --json
    uv run python scripts/call_graph_miss_audit.py --check
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass
from typing import Any, Sequence

from call_graph_oracle import compare_named_package


PACKAGES = ("jsonpatch", "sqlparse", "semantic_version")
BASELINE_MISSES_BEFORE_IMPORT_PRIMING = 22
SUPER_CONSTRUCTOR_MISS_CLOSED = 1


@dataclass(frozen=True)
class Classification:
    """One source-level explanation for one observed-but-unlisted pair."""

    category: str
    construct: str
    extractor_treatment: str


# These are exact edges from the workload-only oracle.  They are deliberately
# keyed by the full pair rather than a wildcard: a new edge must be reviewed,
# not absorbed into a broad label merely because it resembles an old one.
CURRENT_MISS_CLASSIFICATIONS: dict[tuple[str, str, str], Classification] = {
    (
        "jsonpatch",
        "jsonpatch:JsonPatch._ops",
        "jsonpatch:JsonPatch._get_operation",
    ): Classification(
        "higher_order_callback",
        "bound callback passed to built-in map(self._get_operation, ...)",
        "no calls edge: the extractor records syntactic Call targets, not callbacks invoked by map",
    ),
    (
        "jsonpatch",
        "jsonpatch:JsonPatch.apply",
        "jsonpatch:JsonPatch._ops",
    ): Classification(
        "property_relation",
        "read of @property self._ops",
        "modeled as a property relationship, intentionally not a calls edge",
    ),
    (
        "jsonpatch",
        "jsonpatch:MoveOperation.apply",
        "jsonpointer:JsonPointer.to_last",
    ): Classification(
        "branch_refined_receiver",
        "from_ptr.to_last after isinstance branch refines a configurable pointer class",
        "no calls edge: receiver tracking is not flow-sensitive through isinstance branches or custom pointer_cls overrides",
    ),
    (
        "sqlparse",
        "engine.filter_stack:FilterStack.run",
        "filters.others:StripTrailingSemicolonFilter.process",
    ): Classification(
        "collection_element_dispatch",
        "filter_.process for an element appended to self.stmtprocess",
        "no calls edge: class-attribute tracking does not infer element types of mutable filter lists",
    ),
    (
        "sqlparse",
        "engine.statement_splitter:StatementSplitter._change_splitlevel",
        "tokens:_TokenType.__contains__",
    ): Classification(
        "implicit_contains_protocol",
        "ttype not in T.Keyword invokes _TokenType.__contains__",
        "no calls edge: the operator bridge does not model In/NotIn protocol dispatch",
    ),
    (
        "sqlparse",
        "engine.statement_splitter:StatementSplitter.process",
        "lexer:Lexer.get_tokens",
    ): Classification(
        "generator_resume_attribution",
        "iterating the generator returned by lexer.tokenize resumes Lexer.get_tokens",
        "not a lexical calls edge from process: sys.setprofile attributes generator resume to its consumer",
    ),
    (
        "sqlparse",
        "engine.statement_splitter:StatementSplitter.process",
        "sql:TokenList",
    ): Classification(
        "cross_module_inherited_constructor",
        "constructing sql.Statement executes inherited TokenList.__init__",
        "no calls edge: imported-class constructor resolution does not follow a cross-module base MRO to its body owner",
    ),
    (
        "sqlparse",
        "sql:Token",
        "tokens:_TokenType.__contains__",
    ): Classification(
        "implicit_contains_protocol",
        "ttype in T.Keyword invokes _TokenType.__contains__",
        "no calls edge: the operator bridge does not model In/NotIn protocol dispatch",
    ),
    (
        "sqlparse",
        "sql:TokenList",
        "sql:TokenList.__str__",
    ): Classification(
        "implicit_str_protocol",
        "str(self) in TokenList.__init__ invokes TokenList.__str__",
        "no calls edge: the extractor does not dispatch built-in str(...) to __str__",
    ),
    (
        "sqlparse",
        "sqlparse:split",
        "sql:TokenList.__str__",
    ): Classification(
        "implicit_str_protocol",
        "str(stmt) in the split list comprehension invokes TokenList.__str__",
        "no calls edge: the extractor does not dispatch built-in str(...) to __str__",
    ),
    (
        "semantic_version",
        "base:Version.__gt__",
        "base:AlphaIdentifier.__eq__",
    ): Classification(
        "rich_tuple_compare_protocol",
        "tuple precedence comparison dispatches AlphaIdentifier.__eq__",
        "no calls edge: rich ordering over dynamically assembled tuple members has no statically known receiver type",
    ),
    (
        "semantic_version",
        "base:Version.__gt__",
        "base:MaxIdentifier.__eq__",
    ): Classification(
        "rich_tuple_compare_protocol",
        "tuple precedence comparison dispatches MaxIdentifier.__eq__",
        "no calls edge: rich ordering over dynamically assembled tuple members has no statically known receiver type",
    ),
    (
        "semantic_version",
        "base:Version.__gt__",
        "base:NumericIdentifier.__eq__",
    ): Classification(
        "rich_tuple_compare_protocol",
        "tuple precedence comparison dispatches NumericIdentifier.__eq__",
        "no calls edge: rich ordering over dynamically assembled tuple members has no statically known receiver type",
    ),
    (
        "semantic_version",
        "base:Version.__lt__",
        "base:AlphaIdentifier.__eq__",
    ): Classification(
        "rich_tuple_compare_protocol",
        "tuple precedence comparison dispatches AlphaIdentifier.__eq__",
        "no calls edge: rich ordering over dynamically assembled tuple members has no statically known receiver type",
    ),
    (
        "semantic_version",
        "base:Version.__lt__",
        "base:AlphaIdentifier.__lt__",
    ): Classification(
        "rich_tuple_compare_protocol",
        "tuple precedence comparison dispatches AlphaIdentifier.__lt__",
        "no calls edge: rich ordering over dynamically assembled tuple members has no statically known receiver type",
    ),
    (
        "semantic_version",
        "base:Version.__lt__",
        "base:MaxIdentifier.__eq__",
    ): Classification(
        "rich_tuple_compare_protocol",
        "tuple precedence comparison dispatches MaxIdentifier.__eq__",
        "no calls edge: rich ordering over dynamically assembled tuple members has no statically known receiver type",
    ),
    (
        "semantic_version",
        "base:Version.__lt__",
        "base:NumericIdentifier.__eq__",
    ): Classification(
        "rich_tuple_compare_protocol",
        "tuple precedence comparison dispatches NumericIdentifier.__eq__",
        "no calls edge: rich ordering over dynamically assembled tuple members has no statically known receiver type",
    ),
    (
        "semantic_version",
        "base:Version.__lt__",
        "base:NumericIdentifier.__lt__",
    ): Classification(
        "rich_tuple_compare_protocol",
        "tuple precedence comparison dispatches NumericIdentifier.__lt__",
        "no calls edge: rich ordering over dynamically assembled tuple members has no statically known receiver type",
    ),
}


# The old trace armed its profiler before importing the workload package.  These
# are ClassDef/nested-ClassDef execution frames, not function-call edges.  Keep
# the full historic set explicit and assert priming removes all of it.
COLD_IMPORT_ARTIFACTS = {
    ("sqlparse", "sql:TypedLiteral", "tokens:_TokenType.__getattr__"),
    ("semantic_version", "base:SimpleSpec", "base:SimpleSpec.Parser"),
    ("semantic_version", "base:NpmSpec", "base:NpmSpec.Parser"),
}


def _edge_set(report: dict[str, Any]) -> set[tuple[str, str]]:
    """Extract the whole scored miss population; the oracle truncates only display."""
    return {
        (str(edge["caller"]), str(edge["callee"]))
        for edge in report.get("missed_edges") or []
        if isinstance(edge, dict) and isinstance(edge.get("caller"), str) and isinstance(edge.get("callee"), str)
    }


def _observed_edge_set(report: dict[str, Any]) -> set[tuple[str, str]]:
    """Return confirmed + missed pairs so artifacts cannot hide as confirmed."""
    edges: set[tuple[str, str]] = set()
    for key in ("confirmed_edges", "missed_edges"):
        edges.update(
            (str(edge["caller"]), str(edge["callee"]))
            for edge in report.get(key) or []
            if isinstance(edge, dict)
            and isinstance(edge.get("caller"), str)
            and isinstance(edge.get("callee"), str)
        )
    return edges


def build_report() -> dict[str, Any]:
    """Classify the complete live population and detect a changing boundary."""
    reports = {package: compare_named_package(package) for package in PACKAGES}
    failures: list[str] = []
    actual: set[tuple[str, str, str]] = set()
    observed_population: set[tuple[str, str, str]] = set()
    for package, report in reports.items():
        if report.get("status") != "ok":
            failures.append(f"{package}: oracle did not run: {report.get('skip_reason')}")
            continue
        pairs = _edge_set(report)
        if len(pairs) != int(report.get("missed") or 0):
            failures.append(
                f"{package}: oracle exposed {len(pairs)} missed edges but reports {report.get('missed')}"
            )
        actual.update((package, caller, callee) for caller, callee in pairs)
        observed_pairs = _observed_edge_set(report)
        if len(observed_pairs) != int(report.get("n_observed_mapped") or 0):
            failures.append(
                f"{package}: oracle exposed {len(observed_pairs)} scored pairs but "
                f"reports {report.get('n_observed_mapped')}"
            )
        observed_population.update(
            (package, caller, callee) for caller, callee in observed_pairs
        )

    expected = set(CURRENT_MISS_CLASSIFICATIONS)
    unexpected = sorted(actual - expected)
    disappeared = sorted(expected - actual)
    returning_import_artifacts = sorted(observed_population & COLD_IMPORT_ARTIFACTS)
    if unexpected:
        failures.append(f"unclassified live misses: {unexpected}")
    if disappeared:
        failures.append(f"classified misses no longer live: {disappeared}")
    if returning_import_artifacts:
        failures.append(f"cold-import artifacts returned to workload trace: {returning_import_artifacts}")
    accounted_baseline = (
        len(actual) + len(COLD_IMPORT_ARTIFACTS) + SUPER_CONSTRUCTOR_MISS_CLOSED
    )
    if accounted_baseline != BASELINE_MISSES_BEFORE_IMPORT_PRIMING:
        failures.append(
            "historical miss accounting changed: "
            f"{accounted_baseline} != {BASELINE_MISSES_BEFORE_IMPORT_PRIMING}"
        )

    # Keep constructing a report after a classification is removed.  The
    # point of this tool is to present that missing label as a fail-closed
    # audit result, not to leak an incidental KeyError from report rendering.
    classified_actual = actual & expected
    categories = Counter(
        CURRENT_MISS_CLASSIFICATIONS[edge].category for edge in sorted(classified_actual)
    )
    by_package = Counter(package for package, _caller, _callee in actual)
    by_category_package: dict[str, dict[str, int]] = {}
    for category in sorted(categories):
        by_category_package[category] = {
            package: sum(
                1
                for edge in classified_actual
                if edge[0] == package
                and CURRENT_MISS_CLASSIFICATIONS[edge].category == category
            )
            for package in PACKAGES
        }
    return {
        "ok": not failures,
        "reports": reports,
        "live_misses": len(actual),
        "baseline_misses_before_import_priming": BASELINE_MISSES_BEFORE_IMPORT_PRIMING,
        "cold_import_artifacts_excluded": len(COLD_IMPORT_ARTIFACTS),
        "super_constructor_miss_closed": SUPER_CONSTRUCTOR_MISS_CLOSED,
        "categories": dict(sorted(categories.items())),
        "by_package": {package: by_package[package] for package in PACKAGES},
        "by_category_package": by_category_package,
        "unexpected": unexpected,
        "disappeared": disappeared,
        "returning_import_artifacts": returning_import_artifacts,
        "failures": failures,
    }


def format_report(report: dict[str, Any]) -> str:
    """Render category mix without collapsing it into an accuracy score."""
    lines = [
        "Call-oracle miss audit (exhaustive workload population)",
        "  current misses={live_misses}; baseline={baseline_misses_before_import_priming} "
        "= {cold_import_artifacts_excluded} cold-import artifacts + "
        "{super_constructor_miss_closed} observed super-constructor miss matched by a "
        "lexical candidate + current {live_misses}".format(
            **report
        ),
        "  package counts: " + ", ".join(
            f"{package}={count}" for package, count in report["by_package"].items()
        ),
        "  categories:",
    ]
    for category, count in report["categories"].items():
        per_package = report["by_category_package"][category]
        examples = [
            detail
            for edge, detail in CURRENT_MISS_CLASSIFICATIONS.items()
            if detail.category == category
        ]
        lines.append(
            f"    {category}: {count} "
            f"(jsonpatch={per_package['jsonpatch']}, sqlparse={per_package['sqlparse']}, "
            f"semantic_version={per_package['semantic_version']}) — {examples[0].extractor_treatment}"
        )
    for failure in report["failures"]:
        lines.append(f"  FAIL: {failure}")
    lines.append("  RESULT: PASS" if report["ok"] else "  RESULT: FAIL")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit the complete machine-readable audit")
    parser.add_argument("--check", action="store_true", help="fail on any unclassified or stale miss")
    args = parser.parse_args(argv)
    report = build_report()
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else format_report(report))
    return 0 if not args.check or report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
