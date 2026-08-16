"""Installable graphrag-code console entry and wheel layout.

Disposable graphs and isolated venvs only. Does not write published byog_* roots.

Run:
  uv run python -m pytest examples/mini_game/tests/test_graphrag_code_packaging.py -q
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from byog_graph import publish_byog_snapshot  # type: ignore
from graphrag_code.cli import app as packaged_app  # type: ignore


def _py_rows():
    ents = [
        {
            "id": "ent:fn:demo.main",
            "title": "demo:main",
            "type": "function",
            "source_file": "demo.py",
            "extractor": "tree-sitter-python",
        }
    ]
    rels = [
        {
            "id": "rel:contains:1",
            "source": "demo:demo.py",
            "target": "demo:main",
            "type": "contains",
            "extractor": "tree-sitter-python",
        }
    ]
    tus = [
        {
            "id": "tu:1",
            "title": "demo.py",
            "source_file": "demo.py",
            "entity_id": "ent:fn:demo.main",
        }
    ]
    return ents, rels, tus


def _publish(tmp_path: Path) -> Path:
    ents, rels, tus = _py_rows()
    graph = tmp_path / "byog_demo"
    publish_byog_snapshot(
        pd.DataFrame(ents),
        pd.DataFrame(rels),
        pd.DataFrame(tus),
        graph,
        settings_text="packaging: true\n",
        keep_last=1,
    )
    return graph


def test_pyproject_has_exactly_one_console_entry():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    scripts = data["project"]["scripts"]
    assert scripts == {"graphrag-code": "graphrag_code.cli:main"}
    assert data["build-system"]["build-backend"] == "hatchling.build"


def test_python_module_help():
    proc = subprocess.run(
        [sys.executable, "-m", "graphrag_code", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "doctor" in proc.stdout
    assert "query-symbol" in proc.stdout
    assert "index-python" in proc.stdout
    assert "mcp" in proc.stdout
    assert "adopt-publication-lock" in proc.stdout
    assert "snapshot-history" in proc.stdout
    assert "snapshot-diff" in proc.stdout
    assert "snapshot-activate" in proc.stdout
    assert "snapshot-pins" in proc.stdout
    assert "snapshot-pin" in proc.stdout
    assert "snapshot-unpin" in proc.stdout
    assert "snapshot-retention-plan" in proc.stdout
    assert "snapshot-prune" in proc.stdout
    assert "snapshot-staging" in proc.stdout
    assert "snapshot-staging-cleanup-plan" in proc.stdout
    assert "snapshot-staging-cleanup" in proc.stdout
    assert "snapshot-maintenance-plan" in proc.stdout
    assert "snapshot-maintenance-apply" in proc.stdout


def test_source_script_and_package_expose_same_commands():
    script = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "graphrag_code.py"), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert script.returncode == 0, script.stderr
    packaged = sorted(cmd.name for cmd in packaged_app.registered_commands)
    required = (
        "doctor",
        "query-symbol",
        "callers",
        "callees",
        "context-pack",
        "index-python",
        "index-c",
        "port-eval",
        "mcp",
        "adopt-publication-lock",
        "snapshot-history",
        "snapshot-diff",
        "snapshot-activate",
        "snapshot-pins",
        "snapshot-pin",
        "snapshot-unpin",
        "snapshot-retention-plan",
        "snapshot-prune",
        "snapshot-staging",
        "snapshot-staging-cleanup-plan",
        "snapshot-staging-cleanup",
        "snapshot-maintenance-plan",
        "snapshot-maintenance-apply",
    )
    for name in required:
        assert name in packaged
        assert name in script.stdout
    for name in (
        "byog_graph",
        "persisted_graph_integrity",
        "persisted_graph_doctor",
        "c_preprocessor",
    ):
        assert importlib.import_module(name) is importlib.import_module(
            f"graphrag_code.{name}"
        )


def test_packaged_doc_claims_matches_checkout_manifest():
    packaged = (ROOT / "src" / "graphrag_code" / "doc_claims.json").read_bytes()
    checkout = (ROOT / "scripts" / "doc_claims.json").read_bytes()
    assert packaged == checkout


def test_wheel_and_sdist_contents(built_wheel_and_sdist):
    wheel, sdist = built_wheel_and_sdist
    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()
        assert "graphrag_code/cli.py" in names
        assert "graphrag_code/persisted_graph_doctor.py" in names
        assert "graphrag_code/index_reuse.py" in names
        assert "graphrag_code/python_inputs.py" in names
        assert "graphrag_code/mcp_server.py" in names
        assert "graphrag_code/adopt_publication_lock.py" in names
        assert "graphrag_code/snapshot_compare.py" in names
        assert "graphrag_code/snapshot_activate.py" in names
        assert "graphrag_code/snapshot_read.py" in names
        assert "graphrag_code/snapshot_pins.py" in names
        assert "graphrag_code/snapshot_retention.py" in names
        assert "graphrag_code/snapshot_prune.py" in names
        assert "graphrag_code/snapshot_staging.py" in names
        assert "graphrag_code/snapshot_staging_cleanup_plan.py" in names
        assert "graphrag_code/snapshot_staging_cleanup.py" in names
        assert "graphrag_code/snapshot_maintenance_plan.py" in names
        assert "graphrag_code/snapshot_maintenance_apply.py" in names
        assert "graphrag_code/doc_claims.json" in names
        assert not any(
            "scripts/" in name
            or "examples/" in name
            or "output/" in name
            or "byog_" in Path(name).parts[:-1]
            or "__pycache__" in name
            or name.endswith(".pyc")
            for name in names
        )
        entry_points = zf.read(
            next(name for name in names if name.endswith(".dist-info/entry_points.txt"))
        ).decode("utf-8")
        assert entry_points == (
            "[console_scripts]\n"
            "graphrag-code = graphrag_code.cli:main\n"
        )
        repo = str(ROOT)
        for info in zf.infolist():
            if info.filename.endswith((".py", ".json", ".txt")):
                payload = zf.read(info.filename).decode("utf-8", "replace")
                assert repo not in payload, info.filename
    with tarfile.open(sdist, "r:gz") as tf:
        snames = tf.getnames()
    sjoined = "\n".join(snames)
    assert "src/graphrag_code/cli.py" in sjoined or any(
        n.endswith("graphrag_code/cli.py") for n in snames
    )
    assert "examples/mini_game" not in sjoined
    assert "byog_cjson" not in sjoined
    assert "__pycache__" not in sjoined
    assert not any(name.endswith(".pyc") for name in snames)


def test_installed_cli_from_outside_checkout(tmp_path: Path, built_wheel_and_sdist):
    from conftest import install_wheel

    wheel, _ = built_wheel_and_sdist
    env = install_wheel(wheel, tmp_path / "site")
    outside = tmp_path / "outside"
    outside.mkdir()
    graph = _publish(tmp_path / "graphs")

    help_proc = subprocess.run(
        ["graphrag-code", "--help"],
        cwd=outside,
        capture_output=True,
        text=True,
        env=env,
    )
    assert help_proc.returncode == 0, help_proc.stderr
    assert "doctor" in help_proc.stdout

    mod_help = subprocess.run(
        [sys.executable, "-m", "graphrag_code", "--help"],
        cwd=outside,
        capture_output=True,
        text=True,
        env=env,
    )
    assert mod_help.returncode == 0, mod_help.stderr

    installed = subprocess.run(
        [
            "graphrag-code",
            "doctor",
            "--graph",
            str(graph),
            "--indexer",
            "python",
            "--json",
        ],
        cwd=outside,
        capture_output=True,
        text=True,
        env=env,
    )
    standalone = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "persisted_graph_doctor.py"),
            "--graph",
            str(graph),
            "--indexer",
            "python",
            "--json",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert installed.returncode == standalone.returncode == 0, installed.stderr
    inst = json.loads(installed.stdout)
    stand = json.loads(standalone.stdout)
    assert inst == stand
    assert inst["ok"] is True

    bad = subprocess.run(
        ["graphrag-code", "doctor", "--graph", str(graph), "--indexer", "nope"],
        cwd=outside,
        capture_output=True,
        text=True,
        env=env,
    )
    assert bad.returncode == 2

    broken = _publish(tmp_path / "broken")
    broken_snap = broken / "snapshots" / (broken / "current").read_text().strip()
    broken_manifest = json.loads((broken_snap / "manifest.json").read_text())
    broken_manifest["counts"]["entities"] = 0
    (broken_snap / "manifest.json").write_text(json.dumps(broken_manifest, indent=2))
    installed_broken = subprocess.run(
        [
            "graphrag-code",
            "doctor",
            "--graph",
            str(broken),
            "--indexer",
            "python",
        ],
        cwd=outside,
        capture_output=True,
        text=True,
        env=env,
    )
    standalone_broken = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "persisted_graph_doctor.py"),
            "--graph",
            str(broken),
            "--indexer",
            "python",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert installed_broken.returncode == standalone_broken.returncode == 1
    assert "Traceback" not in installed_broken.stderr

    query = subprocess.run(
        ["graphrag-code", "query-symbol", "demo:main", "--graph", str(graph), "--json"],
        cwd=outside,
        capture_output=True,
        text=True,
        env=env,
    )
    assert query.returncode == 0, query.stderr
    assert json.loads(query.stdout)["title"] == "demo:main"

    pack = subprocess.run(
        ["graphrag-code", "context-pack", "demo:main", "--graph", str(graph), "--json"],
        cwd=outside,
        capture_output=True,
        text=True,
        env=env,
    )
    assert pack.returncode == 0, pack.stderr
    body = json.loads(pack.stdout)
    assert body.get("symbol") == "demo:main" or (body.get("entity") or {}).get("title") == "demo:main"

    for command in ("callers", "callees"):
        related = subprocess.run(
            [
                "graphrag-code",
                command,
                "demo:main",
                "--graph",
                str(graph),
                "--json",
            ],
            cwd=outside,
            capture_output=True,
            text=True,
            env=env,
        )
        assert related.returncode == 0, related.stderr
        assert isinstance(json.loads(related.stdout), list)

    python_source = outside / "python_source"
    python_source.mkdir()
    (python_source / "demo.py").write_text(
        "def add(left: int, right: int) -> int:\n    return left + right\n"
    )
    python_index = subprocess.run(
        [
            "graphrag-code",
            "index-python",
            "--package",
            python_source.name,
            "--graph",
            "python_graph",
            "--keep-snapshots",
            "1",
        ],
        cwd=outside,
        capture_output=True,
        text=True,
        env=env,
    )
    assert python_index.returncode == 0, python_index.stderr
    python_graph = outside / "python_graph"
    python_current = (python_graph / "current").read_bytes()
    python_snaps = sorted(path.name for path in (python_graph / "snapshots").iterdir())
    python_reuse = subprocess.run(
        [
            "graphrag-code",
            "index-python",
            "--package",
            python_source.name,
            "--graph",
            "python_graph",
            "--keep-snapshots",
            "1",
            "--reuse-unchanged",
        ],
        cwd=outside,
        capture_output=True,
        text=True,
        env=env,
    )
    assert python_reuse.returncode == 0, python_reuse.stderr
    assert python_reuse.stdout.strip().startswith("Unchanged. Reusing snapshot:")
    assert (python_graph / "current").read_bytes() == python_current
    assert sorted(path.name for path in (python_graph / "snapshots").iterdir()) == python_snaps
    loc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import graphrag_code.index_reuse as r; print(r.__file__)",
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(outside),
    )
    assert loc.returncode == 0, loc.stderr
    assert str(tmp_path / "site") in loc.stdout
    assert str(ROOT / "src") not in loc.stdout
    python_doctor = subprocess.run(
        [
            "graphrag-code",
            "doctor",
            "--graph",
            "python_graph",
            "--indexer",
            "auto",
            "--json",
        ],
        cwd=outside,
        capture_output=True,
        text=True,
        env=env,
    )
    assert python_doctor.returncode == 0, python_doctor.stderr
    assert json.loads(python_doctor.stdout)["indexer"] == "python"

    c_source = outside / "c_source"
    c_source.mkdir()
    (c_source / "demo.c").write_text("int add(int left, int right) { return left + right; }\n")
    c_index = subprocess.run(
        [
            "graphrag-code",
            "index-c",
            "--package",
            c_source.name,
            "--graph",
            "c_graph",
            "--keep-snapshots",
            "1",
        ],
        cwd=outside,
        capture_output=True,
        text=True,
        env=env,
    )
    assert c_index.returncode == 0, c_index.stderr
    c_graph = outside / "c_graph"
    c_current = (c_graph / "current").read_bytes()
    c_snaps = sorted(path.name for path in (c_graph / "snapshots").iterdir())
    c_reuse = subprocess.run(
        [
            "graphrag-code",
            "index-c",
            "--package",
            c_source.name,
            "--graph",
            "c_graph",
            "--keep-snapshots",
            "1",
            "--reuse-unchanged",
        ],
        cwd=outside,
        capture_output=True,
        text=True,
        env=env,
    )
    assert c_reuse.returncode == 0, c_reuse.stderr
    assert c_reuse.stdout.strip().startswith("Unchanged. Reusing snapshot:")
    assert (c_graph / "current").read_bytes() == c_current
    assert sorted(path.name for path in (c_graph / "snapshots").iterdir()) == c_snaps
    c_doctor = subprocess.run(
        [
            "graphrag-code",
            "doctor",
            "--graph",
            "c_graph",
            "--indexer",
            "auto",
            "--json",
        ],
        cwd=outside,
        capture_output=True,
        text=True,
        env=env,
    )
    assert c_doctor.returncode == 0, c_doctor.stderr
    c_report = json.loads(c_doctor.stdout)
    assert c_report["indexer"] == "c"
    assert len(c_report["components"]) == 9

    port = subprocess.run(
        ["graphrag-code", "port-eval", "--all-gates"],
        cwd=outside,
        capture_output=True,
        text=True,
        env=env,
    )
    assert port.returncode == 2, port.stdout + port.stderr
    assert "source checkout" in (port.stderr + port.stdout)

    explicit_port = subprocess.run(
        [
            "graphrag-code",
            "port-eval",
            "--graph",
            str(graph),
            "--source",
            str(outside / "source"),
            "--port",
            str(outside / "port"),
            "--skip-rust",
        ],
        cwd=outside,
        capture_output=True,
        text=True,
        env=env,
    )
    assert explicit_port.returncode == 2, explicit_port.stdout + explicit_port.stderr
    assert "source checkout" in (explicit_port.stderr + explicit_port.stdout)
    assert "Traceback" not in explicit_port.stderr


def test_delegated_exit_codes_propagate(tmp_path: Path):
    graph = _publish(tmp_path)
    snap = graph / "snapshots" / (graph / "current").read_text().strip()
    man = json.loads((snap / "manifest.json").read_text())
    man["counts"]["entities"] = 0
    (snap / "manifest.json").write_text(json.dumps(man, indent=2))
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "graphrag_code",
            "doctor",
            "--graph",
            str(graph),
            "--indexer",
            "python",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert "Traceback" not in proc.stderr
