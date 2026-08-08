#!/usr/bin/env python
"""Inventory local graph evidence and distinguish replay from historical record.

The repository has two deliberately different graph locations:

* ``output/port_gates/<profile>/graph`` is disposable gate output.  A gate
  rebuilds it from the checked-in source and golden contract.
* ``byog_*`` is a published local snapshot.  It is useful for interactive graph
  queries and for the call-observation oracle, but it is ignored by Git.

This module does not guess which one a reader meant.  It derives every row from
the documentation-claim manifest, the port-gate manifest, the call-oracle
contract registry, a scan of tracked Markdown, and the local ``byog_*`` roots.
That makes an artifact that is added to only one of those sources visible as an
unregistered reference rather than silently omitting it from the inventory.

Usage:

    uv run python scripts/evidence_durability.py
    uv run python scripts/evidence_durability.py --check
    uv run python scripts/evidence_durability.py --write
    uv run python scripts/evidence_durability.py --json
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping


ROOT = Path(__file__).resolve().parents[1]
CLAIMS_MANIFEST = ROOT / "scripts" / "doc_claims.json"
GATES_MANIFEST = ROOT / "scripts" / "port_gates.json"
OUTPUT_DOCUMENT = ROOT / "docs" / "EVIDENCE_DURABILITY.md"
BYOG_NAME = re.compile(r"\b(byog_[A-Za-z0-9_]+)\b")


def _graph_name(value: object) -> str | None:
    """Return the published graph root named by a manifest path, if any."""
    if not isinstance(value, str):
        return None
    for part in Path(value).parts:
        if part.startswith("byog_"):
            return part
    return None


def _artifact_path(name: str, snapshot: str | None, root: Path) -> Path:
    path = root / name
    return path / "snapshots" / snapshot if snapshot else path


def _display_path(path: Path, root: Path) -> str:
    """Keep test-supplied temporary manifests visible without requiring containment."""
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _replay_probe(value: object, claim_id: str) -> dict[str, Any] | None:
    """Validate an optional measured current-replay result in a claim source."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError(f"{claim_id}: replay_probe must be an object")
    package = value.get("package")
    indexer = value.get("indexer")
    output = value.get("output")
    observed = value.get("observed")
    if not isinstance(package, str) or not package:
        raise ValueError(f"{claim_id}: replay_probe needs a non-empty package")
    if indexer not in {"python", "c"}:
        raise ValueError(f"{claim_id}: replay_probe indexer must be 'python' or 'c'")
    if not isinstance(output, str) or not output.startswith("output/evidence_durability/"):
        raise ValueError(
            f"{claim_id}: replay_probe output must stay under output/evidence_durability/"
        )
    if not isinstance(observed, Mapping) or not observed:
        raise ValueError(f"{claim_id}: replay_probe needs non-empty observed values")
    checked: dict[str, int] = {}
    for key, raw in observed.items():
        if not isinstance(key, str) or not isinstance(raw, int):
            raise ValueError(f"{claim_id}: replay_probe observed values must be integer fields")
        checked[key] = raw
    return {
        "package": package,
        "indexer": indexer,
        "output": output,
        "observed": checked,
    }


def claim_durability(claim: Mapping[str, Any], root: Path = ROOT) -> dict[str, Any]:
    """Describe whether a claim's evidence can be replayed from Git alone.

    This is intentionally a classification of the *evidence source*, not a
    verdict on the truth of a documented number.  ``check_doc_claims`` still
    performs the latter derivation using its native source readers.
    """
    source = claim.get("source")
    if not isinstance(source, Mapping):
        raise ValueError(f"{claim.get('id', '<unknown>')}: source must be an object")
    claim_id = str(claim.get("id", "<unknown>"))
    kind = str(claim.get("kind", "current"))
    stype = str(source.get("type", ""))
    snapshot = source.get("snapshot")
    snapshot = snapshot if isinstance(snapshot, str) else None
    graph_name = _graph_name(source.get("graph"))
    probe = _replay_probe(source.get("replay_probe"), claim_id)

    # Historical records normally have no live artifact.  A structured
    # ``evidence_artifact`` keeps an already-lost snapshot enumerable without
    # treating prose in ``source.note`` as machine-readable state.
    archived = source.get("evidence_artifact")
    if kind == "historical":
        if archived is not None and not isinstance(archived, Mapping):
            raise ValueError(f"{claim_id}: evidence_artifact must be an object")
        if isinstance(archived, Mapping):
            archived_graph = _graph_name(archived.get("graph"))
            archived_snapshot = archived.get("snapshot")
            archived_snapshot = archived_snapshot if isinstance(archived_snapshot, str) else None
            state = archived.get("state")
            if state not in {"lost", "historical"}:
                raise ValueError(
                    f"{claim_id}: historical evidence_artifact needs state='lost' or 'historical'"
                )
            archived_probe = _replay_probe(archived.get("replay_probe"), claim_id)
            return {
                "claim": claim_id,
                "tier": "historical-record",
                "replay": "not reproducible from Git",
                "artifact": archived_graph,
                "snapshot": archived_snapshot,
                "present": (
                    _artifact_path(archived_graph, archived_snapshot, root).is_dir()
                    if archived_graph is not None
                    else False
                ),
                "detail": str(
                    archived.get(
                        "detail",
                        "Recorded historical evidence; no live artifact is asserted.",
                    )
                ),
                "replay_probe": archived_probe,
            }
        return {
            "claim": claim_id,
            "tier": "historical-record",
            "replay": "not reproducible from Git",
            "artifact": None,
            "snapshot": None,
            "present": False,
            "detail": "Recorded historical evidence; no live artifact is asserted.",
            "replay_probe": probe,
        }

    if stype == "graph_counts" and graph_name is not None:
        present = _artifact_path(graph_name, snapshot, root).is_dir()
        if kind == "frozen_snapshot":
            return {
                "claim": claim_id,
                "tier": "local-frozen-snapshot",
                "replay": "not reproducible from Git",
                "artifact": graph_name,
                "snapshot": snapshot,
                "present": present,
                "detail": "Named snapshot is locally protected from retention, not versioned.",
                "replay_probe": probe,
            }
        return {
            "claim": claim_id,
            "tier": "local-published-graph",
            "replay": "reindex can produce new current evidence, not this snapshot identity",
            "artifact": graph_name,
            "snapshot": snapshot,
            "present": present,
            "detail": "Published graph is a local graph source rather than a gate output.",
            "replay_probe": probe,
        }

    if stype == "ablation_adequacy" and graph_name is not None:
        if kind == "frozen_snapshot":
            return {
                "claim": claim_id,
                "tier": "local-frozen-snapshot",
                "replay": "not reproducible from Git",
                "artifact": graph_name,
                "snapshot": snapshot,
                "present": _artifact_path(graph_name, snapshot, root).is_dir(),
                "detail": "Protected closed-experiment graph; the archive is intentionally not regenerated.",
                "replay_probe": probe,
            }
        return {
            "claim": claim_id,
            "tier": "local-published-graph",
            "replay": "reindex can produce new current evidence, not this snapshot identity",
            "artifact": graph_name,
            "snapshot": snapshot,
            "present": _artifact_path(graph_name, snapshot, root).is_dir(),
            "detail": "Mutable adequacy measurement from a published local graph.",
            "replay_probe": probe,
        }

    if stype == "graph_audit":
        fallback = _graph_name(source.get("fallback_graph"))
        graph = str(source.get("graph", ""))
        if graph.startswith("output/port_gates/"):
            return {
                "claim": claim_id,
                "tier": "disposable-gate-output",
                "replay": "reproducible current measurement from Git via the named gate",
                "artifact": fallback,
                "snapshot": None,
                "present": _artifact_path(fallback, None, root).is_dir() if fallback else False,
                "detail": "Fresh gate output is authoritative; the published graph is a standalone fallback.",
                "replay_probe": probe,
            }
        if graph_name is not None:
            return {
                "claim": claim_id,
                "tier": "local-published-graph",
                "replay": "reindex can produce new current evidence, not this snapshot identity",
                "artifact": graph_name,
                "snapshot": None,
                "present": _artifact_path(graph_name, None, root).is_dir(),
                "detail": "Published graph is a local graph source rather than a gate output.",
                "replay_probe": probe,
            }

    if stype == "oracle_residuals":
        # The aggregate does not name a graph in doc_claims, but its native
        # call-observation child deliberately reads a published graph.  Import
        # the selected package from the adapter and resolve it through the
        # oracle registry so this is not a second hand-maintained list.
        sys.path.insert(0, str(ROOT / "scripts"))
        from call_graph_oracle import known_contracts  # type: ignore
        from oracle_summary import CALL_GRAPH_ORACLE_PACKAGE  # type: ignore

        contracts = known_contracts()
        contract = contracts.get(CALL_GRAPH_ORACLE_PACKAGE)
        if contract is None:
            raise ValueError(
                f"{claim_id}: oracle summary selects unknown call-oracle package "
                f"{CALL_GRAPH_ORACLE_PACKAGE!r}"
            )
        artifact = _graph_name(str(contract.graph_dir))
        if artifact is None:
            raise ValueError(f"{claim_id}: call-oracle graph is not a byog_* artifact")
        return {
            "claim": claim_id,
            "tier": "local-published-oracle-input",
            "replay": "not reproducible from Git; reindex changes the call-oracle baseline",
            "artifact": artifact,
            "snapshot": None,
            "present": _artifact_path(artifact, None, root).is_dir(),
            "detail": "The aggregate residual report invokes the published call-observation oracle.",
            "replay_probe": probe,
        }

    if stype == "call_graph_miss_audit":
        # The exhaustive audit invokes the native call oracle for each of its
        # declared packages.  Resolve every graph through both registries so
        # its three local baselines remain discoverable without a second
        # package-to-artifact list in this inventory.
        sys.path.insert(0, str(ROOT / "scripts"))
        from call_graph_miss_audit import PACKAGES  # type: ignore
        from call_graph_oracle import known_contracts  # type: ignore

        contracts = known_contracts()
        artifacts: list[str] = []
        for package in PACKAGES:
            contract = contracts.get(package)
            if contract is None:
                raise ValueError(
                    f"{claim_id}: miss audit selects unknown call-oracle package {package!r}"
                )
            artifact = _graph_name(str(contract.graph_dir))
            if artifact is None:
                raise ValueError(f"{claim_id}: call-oracle graph is not a byog_* artifact")
            artifacts.append(artifact)
        artifacts = sorted(set(artifacts))
        return {
            "claim": claim_id,
            "tier": "local-published-oracle-input",
            "replay": "not reproducible from Git; reindex changes the call-oracle baseline",
            # A multi-graph claim deliberately has no misleading singular
            # artifact field.  ``build_inventory`` indexes every entry below.
            "artifact": None,
            "artifacts": artifacts,
            "snapshot": None,
            "present": all(_artifact_path(artifact, None, root).is_dir() for artifact in artifacts),
            "detail": "Exhaustive call-miss audit invokes three current local published call-oracle graphs.",
            "replay_probe": probe,
        }

    if stype == "call_graph_oracle":
        # The named oracle's own contract registry supplies the graph; do not
        # duplicate a package-to-artifact map in the durability inventory.
        package = source.get("package")
        if not isinstance(package, str) or not package:
            raise ValueError(f"{claim_id}: call_graph_oracle needs a package")
        sys.path.insert(0, str(ROOT / "scripts"))
        from call_graph_oracle import known_contracts  # type: ignore

        contract = known_contracts().get(package)
        if contract is None:
            raise ValueError(f"{claim_id}: unknown call-oracle package {package!r}")
        artifact = _graph_name(str(contract.graph_dir))
        if artifact is None:
            raise ValueError(f"{claim_id}: call-oracle graph is not a byog_* artifact")
        return {
            "claim": claim_id,
            "tier": "local-published-oracle-input",
            "replay": "not reproducible from Git; reindex changes the call-oracle baseline",
            "artifact": artifact,
            "snapshot": None,
            "present": _artifact_path(artifact, None, root).is_dir(),
            "detail": "Named call-oracle measurement reads its current local published graph.",
            "replay_probe": probe,
        }

    return {
        "claim": claim_id,
        "tier": "git-derived",
        "replay": "reproducible from Git and declared toolchain",
        "artifact": None,
        "snapshot": None,
        "present": True,
        "detail": "This claim does not read a published byog_* artifact.",
        "replay_probe": probe,
    }


def _markdown_references(root: Path) -> dict[str, list[str]]:
    """Find every byog_* mention in repository Markdown, including archives."""
    refs: dict[str, set[str]] = {}
    for path in sorted(root.rglob("*.md")):
        # The generated inventory necessarily names every artifact.  Counting
        # that self-description as an independent document consumer would make
        # the scan circular and would hide the useful "no other document"
        # finding.
        if ".git" in path.parts or path == root / "docs" / "EVIDENCE_DURABILITY.md":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as error:
            raise RuntimeError(f"could not read Markdown document {path}: {error}") from error
        for name in BYOG_NAME.findall(text):
            # ``byog_graph`` is the helper module, not a graph root.  It is
            # deliberately mentioned in provenance documents and must not turn
            # a code reference into a phantom local artifact.
            if name == "byog_graph":
                continue
            refs.setdefault(name, set()).add(str(path.relative_to(root)))
    return {name: sorted(paths) for name, paths in refs.items()}


def _new_artifact(name: str, root: Path) -> dict[str, Any]:
    return {
        "artifact": name,
        "present": (root / name).is_dir(),
        "claim_uses": [],
        "oracle_uses": [],
        "health": [],
        "documents": [],
        "discovered_only": False,
    }


def _load_gate_manifest(path: Path) -> dict[str, dict[str, Any]]:
    sys.path.insert(0, str(ROOT / "scripts"))
    from port_eval import load_gate_manifest  # type: ignore

    return load_gate_manifest(path)


def _call_oracle_contracts() -> Iterable[tuple[str, str]]:
    sys.path.insert(0, str(ROOT / "scripts"))
    from call_graph_oracle import known_contracts  # type: ignore

    for package, contract in known_contracts().items():
        try:
            graph = str(contract.graph_dir.relative_to(ROOT))
        except ValueError as error:
            raise ValueError(f"call-oracle graph is outside the repository: {contract.graph_dir}") from error
        graph_name = _graph_name(graph)
        if graph_name is None:
            raise ValueError(f"call-oracle graph is not a published byog_* root: {graph}")
        yield package, graph_name


def build_inventory(
    *,
    root: Path = ROOT,
    claims_manifest: Path = CLAIMS_MANIFEST,
    gates_manifest: Path = GATES_MANIFEST,
) -> dict[str, Any]:
    """Build the manifest/config/document-derived durability report.

    The inventory includes on-disk graph roots too.  A root that is only on disk
    is a finding (an unregistered local artifact), not a reason to omit it.
    """
    manifest = json.loads(claims_manifest.read_text(encoding="utf-8"))
    claims = manifest.get("claims")
    if not isinstance(claims, list):
        raise ValueError(f"{claims_manifest}: claims must be a list")
    gates = _load_gate_manifest(gates_manifest)
    documents = _markdown_references(root)
    artifacts: dict[str, dict[str, Any]] = {}

    def get(name: str) -> dict[str, Any]:
        return artifacts.setdefault(name, _new_artifact(name, root))

    for claim in claims:
        if not isinstance(claim, Mapping):
            raise ValueError(f"{claims_manifest}: claim entry must be an object")
        durability = claim_durability(claim, root)
        artifact_names: list[str] = []
        artifact = durability.get("artifact")
        if isinstance(artifact, str):
            artifact_names.append(artifact)
        additional_artifacts = durability.get("artifacts")
        if isinstance(additional_artifacts, list):
            if not all(isinstance(name, str) for name in additional_artifacts):
                raise ValueError(f"{durability['claim']}: artifacts must contain only strings")
            artifact_names.extend(additional_artifacts)
        for name in sorted(set(artifact_names)):
            get(name)["claim_uses"].append(durability)

    for package, name in _call_oracle_contracts():
        get(name)["oracle_uses"].append(package)

    # The same gate manifest declares which published roots are mutable local
    # evidence and which are deliberately frozen.  This is separate from a
    # port profile's disposable graph output: the aggregate full gate compares
    # mutable roots with the current extractor without rewriting them.
    for ident, gate in gates.items():
        published = gate["published_graph"]
        name = str(published["path"])
        get(name)["health"].append(
            {
                "id": ident,
                "mode": str(published["mode"]),
                "reason": published.get("reason"),
            }
        )

    for name, paths in documents.items():
        get(name)["documents"] = paths

    for path in sorted(root.glob("byog_*")):
        if path.is_dir():
            get(path.name)["discovered_only"] = True

    for artifact in artifacts.values():
        artifact["claim_uses"].sort(key=lambda row: str(row["claim"]))
        artifact["oracle_uses"].sort()
        artifact["health"].sort(key=lambda row: str(row["id"]))
        artifact["documents"].sort()
        artifact["discovered_only"] = bool(
            artifact["discovered_only"]
            and not artifact["claim_uses"]
            and not artifact["oracle_uses"]
            and not artifact["health"]
            and not artifact["documents"]
        )

    gate_outputs: list[dict[str, Any]] = []
    for ident, gate in gates.items():
        if gate.get("kind", "port") == "gap":
            gate_outputs.append(
                {
                    "id": ident,
                    "kind": "gap",
                    "output": None,
                    "replay": "no Rust-port gate; named source-only gap",
                }
            )
        else:
            gate_outputs.append(
                {
                    "id": ident,
                    "kind": "port",
                    "output": f"output/port_gates/{ident}/graph",
                    "replay": "disposable output rebuilt from Git by this gate",
                }
            )

    return {
        "schema_version": 1,
        "claims_manifest": _display_path(claims_manifest, root),
        "gates_manifest": _display_path(gates_manifest, root),
        "artifacts": [artifacts[name] for name in sorted(artifacts)],
        "gate_outputs": gate_outputs,
    }


def _claim_use_text(use: Mapping[str, Any]) -> str:
    snapshot = use.get("snapshot")
    suffix = f" @ `{snapshot}`" if isinstance(snapshot, str) else ""
    return f"`{use['claim']}` ({use['tier']}){suffix}"


def _availability(item: Mapping[str, Any]) -> str:
    uses = item["claim_uses"]
    if item["present"] and any(use["tier"] == "historical-record" for use in uses):
        return "root present; historical snapshot absent"
    if item["present"]:
        return "present locally (ignored)"
    if any(use["tier"] == "historical-record" for use in uses):
        return "recorded lost artifact"
    return "absent locally"


def _replay_class(item: Mapping[str, Any]) -> str:
    uses = item["claim_uses"]
    if any(use["tier"] == "local-frozen-snapshot" for use in uses):
        details = [str(use["replay"]) for use in uses if use["tier"] == "local-frozen-snapshot"]
        return details[0]
    if any(use["tier"] == "historical-record" for use in uses):
        return "historical record; not reproducible from Git"
    if any(use["tier"] == "disposable-gate-output" for use in uses):
        return "claim is replayed from fresh gate output; this root is fallback only"
    if item["oracle_uses"]:
        return "published oracle input; reindex creates a new baseline, not this snapshot"
    if item["health"]:
        return "declared mutable local evidence; no direct current claim"
    if item["discovered_only"]:
        return "unregistered local artifact"
    return "document reference only; no registered live consumer"


def _health_class(item: Mapping[str, Any]) -> str:
    declarations = item["health"]
    if not declarations:
        return "not declared; inventory-only"
    if any(declaration["mode"] == "frozen" for declaration in declarations):
        reason = next(
            (str(declaration["reason"]) for declaration in declarations if declaration.get("reason")),
            "deliberately frozen",
        )
        return f"frozen exemption — {reason}"
    profiles = ", ".join(f"`{declaration['id']}`" for declaration in declarations)
    return f"mutable — full aggregate health check ({profiles})"


def _probe_command(probe: Mapping[str, Any], root: Path) -> list[str]:
    indexer = str(probe["indexer"])
    return [
        sys.executable,
        str(root / "scripts" / f"index_{indexer}.py"),
        "--package",
        str(root / str(probe["package"])),
        "--graph",
        str(root / str(probe["output"])),
    ]


def _probe_command_text(probe: Mapping[str, Any]) -> str:
    return " ".join(
        [
            "uv run python",
            f"scripts/index_{probe['indexer']}.py",
            "--package",
            str(probe["package"]),
            "--graph",
            str(probe["output"]),
        ]
    )


def verify_replay_probes(report: Mapping[str, Any], root: Path = ROOT) -> list[str]:
    """Reindex each declared probe and return mismatches; never touch byog_*.

    A probe may be shared by a frozen and a historical claim.  Deduplication is
    intentional: the one disposable index is the comparison for both records.
    """
    probes: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    for item in report["artifacts"]:
        for use in item["claim_uses"]:
            probe = use.get("replay_probe")
            if not isinstance(probe, Mapping):
                continue
            key = (str(probe["indexer"]), str(probe["package"]), str(probe["output"]))
            previous = probes.get(key)
            if previous is not None and previous["observed"] != probe["observed"]:
                raise ValueError(f"conflicting replay_probe expectations for {key}")
            probes[key] = probe

    errors: list[str] = []
    for key, probe in probes.items():
        proc = subprocess.run(
            _probe_command(probe, root),
            cwd=root,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()
            errors.append(f"replay probe {key} exited {proc.returncode}: {detail[-500:]}")
            continue
        try:
            import pandas as pd  # type: ignore

            graph = root / str(probe["output"])
            snapshot = (graph / "current").read_text(encoding="utf-8").strip()
            base = graph / "snapshots" / snapshot
            entities = pd.read_parquet(base / "entities.parquet")
            relationships = pd.read_parquet(base / "relationships.parquet")
            actual = {
                "entities": int(len(entities)),
                "relationships": int(len(relationships)),
                "calls": int((relationships["type"].astype(str) == "calls").sum()),
            }
        except (OSError, KeyError, ValueError) as error:
            errors.append(f"replay probe {key} produced unreadable output: {error}")
            continue
        expected = dict(probe["observed"])
        if actual != expected:
            errors.append(f"replay probe {key}: expected {expected}, got {actual}")
    return errors


def render_markdown(report: Mapping[str, Any]) -> str:
    """Render the checked-in reader view; all rows come from ``build_inventory``."""
    lines = [
        "# Local graph-evidence durability",
        "",
        "This inventory is generated by `uv run python scripts/evidence_durability.py --write` "
        "from `scripts/doc_claims.json`, `scripts/port_gates.json`, the call-oracle "
        "registry, tracked Markdown references, and local `byog_*` discovery. "
        "Verify it with `uv run python scripts/evidence_durability.py --check`.",
        "",
        "A graph can be valuable evidence without being durable evidence.  The portfolio "
        "gate deliberately writes disposable, fresh graphs under `output/port_gates/`; "
        "those reproduce a **current measurement** from checked-in source and contracts. "
        "A published `byog_*` directory is ignored local state.  Reindexing it creates a "
        "new snapshot and must not be described as replaying a named historical snapshot.",
        "",
        "## Policy",
        "",
        "- A current claim may use a disposable gate graph when the gate rebuilds it before "
        "the claim check.  It must not silently fall back to a published snapshot without "
        "saying so.",
        "- A frozen claim names its exact local snapshot and is protected from local retention, "
        "but is **not reproducible from Git alone**.  A clean checkout may report an explicit "
        "source skip while still checking the historical prose.",
        "- A live claim that invokes a published call-oracle graph is not a source skip: if that "
        "local baseline is absent, its check fails rather than substituting a fresh graph.",
        "- A mutable published graph is **current-local evidence**.  It remains distinct from "
        "a frozen snapshot: the full aggregate gate compares it with the current extractor, "
        "and a present stale graph fails rather than becoming a historical record.  The "
        "current `oracle_residuals` and `call_graph_miss_audit` call-observation baselines "
        "follow this rule; they are not frozen historical numbers.",
        "- A historical record is never presented as live verification.  If its source snapshot "
        "has gone, the record retains the number and the loss is named here.",
        "- Proposed durability rule for future frozen claims: before a frozen snapshot becomes "
        "load-bearing, publish a content-addressed archive outside this ignored workspace and "
        "record its digest and retrieval location in the claim manifest.  This repository has "
        "no authority to create that external archive in this change.",
        "",
        "## Extractor drift",
        "",
        "Availability is not the only way a local graph stops being evidence: it can "
        "also fall behind the code that generates it.  On 2026-07-30 two semantic "
        "extractor changes — the cross-module import resolver, then registry-dispatch "
        "promotion — turned out to have been published to some graphs and not others, "
        "leaving four published roots disagreeing with a fresh index by 2 to 72 calls "
        "(`byog_semver` 185/187, `byog_mini_game` 16/27, `byog_dmp` 55/60, "
        "`byog_charset_normalizer` 110/182).",
        "",
        "Nothing caught it, and the reason is in the table below: every port profile "
        "rebuilds a disposable graph under `output/port_gates/`, so the gates stayed "
        "green while the published artifacts drifted.  The full aggregate gate now runs "
        "`scripts/published_graph_health.py --check`: it compares every declared mutable "
        "published graph's structural entities, relationships, and text units with the "
        "current extractor without rewriting the artifact.  An absent local root is an "
        "explicit health skip; a present stale `current` snapshot is a failure.",
        "",
        "`byog_isodate` is exempt by design: it is pinned to the older extractor "
        "because it backs the closed experiment's `isodate_adequacy_v3` claim "
        "(closure 16).  A fresh index yields 61 calls against its 48, and that "
        "difference is the point of freezing it.",
        "",
        "## Published local artifacts",
        "",
        "| Artifact | Consumers derived from manifests/registry | Availability | Replay classification | Local-health policy | Markdown references |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in report["artifacts"]:
        consumers: list[str] = [_claim_use_text(use) for use in item["claim_uses"]]
        consumers.extend(f"call oracle: `{name}`" for name in item["oracle_uses"])
        if not consumers:
            consumers.append("—")
        docs = ", ".join(f"`{path}`" for path in item["documents"]) or "—"
        lines.append(
            "| `{artifact}` | {consumers} | {availability} | {replay} | {health} | {docs} |".format(
                artifact=item["artifact"],
                consumers="<br>".join(consumers),
                availability=_availability(item),
                replay=_replay_class(item),
                health=_health_class(item),
                docs=docs,
            )
        )

    lines += [
        "",
        "## Source-gate artifacts",
        "",
        "Port profiles create only the disposable output below; they do not consume a "
        "published `byog_*` graph.  The distinct full aggregate local-health stage above "
        "is what checks mutable published graphs.  Gap rows make their lack of a Rust-port "
        "gate explicit.",
        "",
        "| Profile | Declared graph output | Durability |",
        "| --- | --- | --- |",
    ]
    for gate in report["gate_outputs"]:
        output = f"`{gate['output']}`" if gate["output"] else "—"
        lines.append(f"| `{gate['id']}` | {output} | {gate['replay']} |")

    probe_groups: dict[tuple[str, str, str], tuple[Mapping[str, Any], list[Mapping[str, Any]]]] = {}
    for item in report["artifacts"]:
        for use in item["claim_uses"]:
            probe = use.get("replay_probe")
            if not isinstance(probe, Mapping):
                continue
            key = (str(probe["indexer"]), str(probe["package"]), str(probe["output"]))
            if key not in probe_groups:
                probe_groups[key] = (probe, [])
            probe_groups[key][1].append(use)
    if probe_groups:
        lines += [
            "",
            "## Measured non-replays",
            "",
            "These commands rebuild disposable output and document why a current reindex is "
            "not a replay of the named local snapshot:",
            "",
        ]
        for probe, uses in probe_groups.values():
            observed = probe["observed"]
            values = ", ".join(f"{key}={value}" for key, value in observed.items())
            claims = ", ".join(f"`{use['claim']}`" for use in uses)
            lines.extend(
                [
                    f"- Current reindex for {claims}: {values} (different from the recorded snapshot).",
                    "",
                    "```bash",
                    _probe_command_text(probe),
                    "```",
                    "",
                ]
            )

    lines += [
        "",
        "## How to act on the table",
        "",
        "- `disposable output` can be deleted: run the corresponding port gate again.",
        "- `published oracle input` is required to replay that exact call-observation comparison; "
        "reindexing is useful, but changes the baseline under comparison.",
        "- `mutable — full aggregate health check` means the graph is local current evidence: "
        "run `uv run python scripts/port_eval.py --all-gates --full` with the artifact present "
        "before relying on it.",
        "- `local-frozen-snapshot` and `historical record` are the durable-evidence risk.  The "
        "former survives local retention only; the latter is already a record whose original "
        "artifact is unavailable.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="emit the inventory as JSON")
    output.add_argument("--check", action="store_true", help="fail if the generated reader view is stale")
    output.add_argument("--write", action="store_true", help="rewrite the generated reader view")
    output.add_argument(
        "--verify-replay-probes",
        action="store_true",
        help="reindex declared disposable probes and compare their measured counts",
    )
    args = parser.parse_args(argv)

    try:
        report = build_inventory()
        rendered = render_markdown(report)
    except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as error:
        print(f"evidence durability: FAIL\n{error}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    if args.write:
        OUTPUT_DOCUMENT.write_text(rendered, encoding="utf-8")
        print(f"evidence durability: wrote {OUTPUT_DOCUMENT.relative_to(ROOT)}")
        return 0
    if args.verify_replay_probes:
        errors = verify_replay_probes(report)
        if errors:
            print("evidence durability replay probes: FAIL", file=sys.stderr)
            for error in errors:
                print(f"  {error}", file=sys.stderr)
            return 1
        count = len(
            {
                (
                    str(use["replay_probe"]["indexer"]),
                    str(use["replay_probe"]["package"]),
                    str(use["replay_probe"]["output"]),
                )
                for item in report["artifacts"]
                for use in item["claim_uses"]
                if isinstance(use.get("replay_probe"), Mapping)
            }
        )
        print(f"evidence durability replay probes: PASS ({count})")
        return 0
    if args.check:
        if not OUTPUT_DOCUMENT.is_file():
            print(f"evidence durability: FAIL\nmissing {OUTPUT_DOCUMENT.relative_to(ROOT)}", file=sys.stderr)
            return 1
        if OUTPUT_DOCUMENT.read_text(encoding="utf-8") != rendered:
            print(
                "evidence durability: FAIL\n"
                "generated inventory is stale; run "
                "uv run python scripts/evidence_durability.py --write",
                file=sys.stderr,
            )
            return 1
        print("evidence durability: PASS")
        return 0

    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
