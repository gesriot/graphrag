"""CAS-guarded snapshot transfer apply.

Disposable tmp graphs only. Does not write published byog_* roots.

Run:
  uv run python -m pytest examples/mini_game/tests/test_snapshot_transfer_apply.py -q
"""
from __future__ import annotations

import ast
import errno
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from byog_graph import (  # type: ignore
    PUBLICATION_LOCK_NAME,
    STAGING_NAME_PREFIX,
    STAGING_WRITER_LOCK_NAME,
    graph_exclusive_lease,
    graph_read_lease,
    publish_byog_snapshot,
)
from graphrag_code.mcp_server import TOOL_NAMES, build_mcp_server, build_session  # type: ignore
from graphrag_code.snapshot_pins import OPERATOR_PINS_NAME  # type: ignore
from graphrag_code.snapshot_transfer_apply import (  # type: ignore
    SnapshotTransferApplyError,
    SnapshotTransferApplyIntegrityError,
    format_result,
    result_to_json,
    snapshot_transfer_apply,
)
from graphrag_code.snapshot_transfer_plan import (  # type: ignore
    ordered_graph_lease_pair,
    snapshot_transfer_plan,
)

SCRIPT = ROOT / "scripts" / "snapshot_transfer_apply.py"
CLI = ROOT / "scripts" / "graphrag_code.py"
MODULE = ROOT / "src" / "graphrag_code" / "snapshot_transfer_apply.py"
BYOG_ROOTS = tuple(
    sorted(path for path in ROOT.iterdir() if path.is_dir() and path.name.startswith("byog_"))
)
FORBIDDEN = frozenset(
    {
        "index_python",
        "index_c",
        "extract_c",
        "extract_python",
        "publish_byog_snapshot",
        "cleanup_old_snapshots",
        "snapshot_activate",
        "snapshot_pin",
        "snapshot_unpin",
        "snapshot_prune",
        "snapshot_staging_cleanup",
        "snapshot_maintenance_apply",
        "snapshot_maintenance_plan",
        "snapshot_history",
        "snapshot_diff",
        "retained_snapshot_read",
        "audit_graph_root",
        "resolve_snapshot",
        "doctor_fingerprint",
        "validate_persisted_graph_integrity",
        "c_clang_ast_capture",
        "c_compiler_facts",
        "_publication_lock",
        "graph_read_lease",
        "graph_exclusive_lease",
        "rmtree",
        "read_bytes",
        "os.rename",
    }
)
FORBIDDEN_WORDS = ("orphaned", "abandoned", "expired", "safe to delete")
REQUIRED_RESULT_KEYS = (
    "schema_version",
    "ok",
    "partial",
    "source_graph",
    "target_graph",
    "requested_snapshot",
    "snapshot_id",
    "expected_transfer_revision",
    "observed_transfer_revision",
    "source_export_revision",
    "planned_files",
    "file_count",
    "total_size_bytes",
    "transfer_confirmed",
    "transfer_performed",
    "publication_attempted",
    "publication_performed",
    "snapshot_verified_after_publication",
    "source_graph_mutated",
    "target_graph_mutated",
    "source_current_before",
    "source_current_after",
    "source_current_unchanged",
    "target_current_before",
    "target_current_after",
    "target_current_unchanged",
    "staging_created",
    "staging_cleanup_attempted",
    "staging_remaining",
    "target_snapshots_fsync_confirmed",
    "activation_performed",
    "retention_performed",
    "filesystem_may_have_changed",
    "retry_requires_fresh_plan",
    "error",
    "notices",
)


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    src = str(ROOT / "src")
    current = env.get("PYTHONPATH", "")
    parts = [part for part in current.split(os.pathsep) if part]
    if src not in parts:
        env["PYTHONPATH"] = src + ((os.pathsep + current) if current else "")
    return env


def _rows(marker: str, *, observations: bool = False):
    ents = [
        {
            "id": f"ent:{marker}",
            "title": f"demo:{marker}",
            "type": "function",
            "source_file": f"{marker}.py",
            "extractor": "tree-sitter-python",
            "description": f"desc-{marker}",
        }
    ]
    rels = [
        {
            "id": f"rel:{marker}",
            "source": f"demo:{marker}.py",
            "target": f"demo:{marker}",
            "type": "contains",
            "extractor": "tree-sitter-python",
        }
    ]
    tus = [
        {
            "id": f"tu:{marker}",
            "title": f"{marker}.py",
            "source_file": f"{marker}.py",
            "entity_id": f"ent:{marker}",
        }
    ]
    obs = [
        {
            "id": f"obs:{marker}",
            "source": f"demo:{marker}.py",
            "target": f"demo:{marker}",
            "kind": "call",
        }
    ]
    return ents, rels, tus, obs if observations else None


def _publish(graph: Path, marker: str, *, observations: bool = False) -> Path:
    ents, rels, tus, obs = _rows(marker, observations=observations)
    return publish_byog_snapshot(
        pd.DataFrame(ents),
        pd.DataFrame(rels),
        pd.DataFrame(tus),
        graph,
        settings_text=f"transfer-apply: {marker}\n",
        keep_last=10,
        call_observations_df=pd.DataFrame(obs) if obs is not None else None,
    )


def _current(graph: Path) -> str:
    return (graph / "current").read_text(encoding="utf-8").strip()


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd or ROOT,
        capture_output=True,
        text=True,
        env=_child_env(),
    )


def _staging_leftovers(graph: Path) -> list[Path]:
    snaps = graph / "snapshots"
    if not snaps.is_dir():
        return []
    return sorted(
        path for path in snaps.iterdir() if path.name.startswith(STAGING_NAME_PREFIX)
    )


def _prepare(tmp_path: Path, *, observations: bool = False):
    source = tmp_path / "source"
    target = tmp_path / "target"
    live = _publish(source, "src", observations=observations)
    dest_live = _publish(target, "dst")
    plan = snapshot_transfer_plan(source, "current", target)
    return source, target, live, dest_live, plan


def _payload_hashes(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        filenames.sort()
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_file() and not path.is_symlink():
                out[path.relative_to(root).as_posix()] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
    return out


def _file_revision(path: Path) -> tuple[int, str]:
    data = path.read_bytes()
    return len(data), "sha256:" + hashlib.sha256(data).hexdigest()


def _assert_success_shape(
    result: dict,
    source: Path,
    target: Path,
    snapshot_id: str,
    expected: str,
    *,
    requested: str = "current",
) -> None:
    for key in REQUIRED_RESULT_KEYS:
        assert key in result
    assert result["schema_version"] == 1
    assert result["ok"] is True
    assert result["partial"] is False
    assert result["source_graph"] == str(source.resolve())
    assert result["target_graph"] == str(target.resolve())
    assert result["requested_snapshot"] == requested
    assert result["snapshot_id"] == snapshot_id
    assert result["expected_transfer_revision"] == expected
    assert result["observed_transfer_revision"] == expected
    assert result["transfer_confirmed"] is True
    assert result["transfer_performed"] is True
    assert result["publication_attempted"] is True
    assert result["publication_performed"] is True
    assert result["snapshot_verified_after_publication"] is True
    assert result["source_graph_mutated"] is False
    assert result["target_graph_mutated"] is True
    assert result["source_current_before"] == result["source_current_after"]
    assert result["source_current_unchanged"] is True
    assert result["target_current_before"] == result["target_current_after"]
    assert result["target_current_unchanged"] is True
    assert result["staging_created"] is True
    assert result["staging_remaining"] is False
    assert result["target_snapshots_fsync_confirmed"] is True
    assert result["activation_performed"] is False
    assert result["retention_performed"] is False
    assert result["error"] is None
    published = target / "snapshots" / snapshot_id
    assert published.is_dir() and not published.is_symlink()
    assert not (target / "snapshots" / f"{STAGING_NAME_PREFIX}{snapshot_id}").exists()
    src_dir = source / "snapshots" / snapshot_id
    for item in result["planned_files"]:
        src_size, src_rev = _file_revision(src_dir / item["path"])
        dst_size, dst_rev = _file_revision(published / item["path"])
        assert item["size_bytes"] == src_size == dst_size
        assert item["content_revision"] == src_rev == dst_rev
    assert json.loads((published / "manifest.json").read_text(encoding="utf-8"))["id"] == snapshot_id
    codes = [notice["code"] for notice in result["notices"]]
    assert codes[:8] == [
        "transfer_is_not_backup",
        "transfer_revision_is_cas_only",
        "transfer_is_not_activation",
        "crash_may_leave_private_staging",
        "staging_writer_lease_not_ownership",
        "advisory_locks_cooperating_only",
        "source_envelope_language_independent_only",
        "cli_only_not_mcp",
    ]
    text = format_result(result)
    assert "transfer_performed=true" in text
    assert "not a backup" in text
    assert "not an activation" in text
    assert json.loads(result_to_json(result)) == result


def test_three_cli_surfaces_and_installed_packaging(
    tmp_path: Path, built_wheel_and_sdist
):
    from conftest import install_wheel

    here = tmp_path / "here"
    here.mkdir()
    source = here / "source"
    target = here / "target"
    live = _publish(source, "src")
    _publish(target, "dst")
    plan = snapshot_transfer_plan(source, "current", target)
    args = [
        "--source-graph",
        "source",
        "--snapshot",
        "current",
        "--target-graph",
        "target",
        "--expected-transfer-revision",
        plan["transfer_revision"],
        "--transfer-confirmed",
        "--json",
    ]
    module = subprocess.run(
        [sys.executable, "-m", "graphrag_code.snapshot_transfer_apply", *args],
        cwd=here,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert module.returncode == 0, module.stderr
    body = json.loads(module.stdout)
    assert body["snapshot_id"] == live.name
    assert result_to_json(body) == module.stdout

    source2 = here / "source2"
    target2 = here / "target2"
    _publish(source2, "src2")
    _publish(target2, "dst2")
    plan2 = snapshot_transfer_plan(source2, "current", target2)
    script = _run(
        "--source-graph",
        "source2",
        "--snapshot",
        "current",
        "--target-graph",
        "target2",
        "--expected-transfer-revision",
        plan2["transfer_revision"],
        "--transfer-confirmed",
        "--json",
        cwd=here,
    )
    source3 = here / "source3"
    target3 = here / "target3"
    _publish(source3, "src3")
    _publish(target3, "dst3")
    plan3 = snapshot_transfer_plan(source3, "current", target3)
    cli = subprocess.run(
        [
            sys.executable,
            str(CLI),
            "snapshot-transfer-apply",
            "--source-graph",
            "source3",
            "--snapshot",
            "current",
            "--target-graph",
            "target3",
            "--expected-transfer-revision",
            plan3["transfer_revision"],
            "--transfer-confirmed",
            "--json",
        ],
        cwd=here,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert script.returncode == cli.returncode == 0, (script.stderr, cli.stderr)
    assert json.loads(script.stdout)["ok"] is True
    assert json.loads(cli.stdout)["ok"] is True

    env = install_wheel(built_wheel_and_sdist[0], tmp_path / "site")
    outside = tmp_path / "outside"
    outside.mkdir()
    wheel_source = outside / "wsrc"
    wheel_target = outside / "wdst"
    _publish(wheel_source, "wheel")
    _publish(wheel_target, "wdst")
    wheel_plan = snapshot_transfer_plan(wheel_source, "current", wheel_target)
    installed = subprocess.run(
        [
            "graphrag-code",
            "snapshot-transfer-apply",
            "--source-graph",
            str(wheel_source),
            "--snapshot",
            "current",
            "--target-graph",
            str(wheel_target),
            "--expected-transfer-revision",
            wheel_plan["transfer_revision"],
            "--transfer-confirmed",
            "--json",
        ],
        cwd=outside,
        capture_output=True,
        text=True,
        env=env,
    )
    assert installed.returncode == 0, installed.stderr
    assert json.loads(installed.stdout)["ok"] is True
    help_proc = subprocess.run(
        [sys.executable, str(CLI), "snapshot-transfer-apply", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert help_proc.returncode == 0
    assert "--transfer-confirmed" in help_proc.stdout
    assert "--expected-transfer-revision" in help_proc.stdout


def test_fresh_revision_succeeds_and_does_not_mutate_source(tmp_path: Path):
    source, target, live, dest, plan = _prepare(tmp_path)
    before_source = _payload_hashes(source)
    source_current = _current(source)
    target_current = _current(target)
    result = snapshot_transfer_apply(
        source,
        "current",
        target,
        plan["transfer_revision"],
        transfer_confirmed=True,
    )
    _assert_success_shape(result, source, target, live.name, plan["transfer_revision"])
    assert _payload_hashes(source) == before_source
    assert _current(source) == source_current
    assert _current(target) == target_current
    assert dest.name in {path.name for path in (target / "snapshots").iterdir()}
    assert live.name in {path.name for path in (target / "snapshots").iterdir()}
    assert not (source / OPERATOR_PINS_NAME).exists()
    assert not (target / OPERATOR_PINS_NAME).exists()


def test_missing_confirmation_and_malformed_revision_create_nothing(tmp_path: Path):
    source, target, live, _dest, plan = _prepare(tmp_path)
    before = list((target / "snapshots").iterdir())
    missing = _run(
        "--source-graph",
        str(source),
        "--snapshot",
        "current",
        "--target-graph",
        str(target),
        "--expected-transfer-revision",
        plan["transfer_revision"],
        "--json",
    )
    assert missing.returncode == 2
    assert missing.stdout == ""
    assert list((target / "snapshots").iterdir()) == before
    with pytest.raises(SnapshotTransferApplyError, match="transfer-confirmed"):
        snapshot_transfer_apply(
            source, "current", target, plan["transfer_revision"], transfer_confirmed=False
        )
    for bad in ("", "sha256:ABC", " sha256:" + "a" * 64, "sha256:" + "a" * 63):
        proc = _run(
            "--source-graph",
            str(source),
            "--snapshot",
            "current",
            "--target-graph",
            str(target),
            "--expected-transfer-revision",
            bad,
            "--transfer-confirmed",
            "--json",
        )
        assert proc.returncode == 2
        assert proc.stdout == ""
    assert not (target / "snapshots" / live.name).exists()


def test_stale_and_blocked_revisions_create_nothing(tmp_path: Path):
    source, target, live, _dest, plan = _prepare(tmp_path)
    stale = "sha256:" + "ab" * 32
    proc = _run(
        "--source-graph",
        str(source),
        "--snapshot",
        "current",
        "--target-graph",
        str(target),
        "--expected-transfer-revision",
        stale,
        "--transfer-confirmed",
        "--json",
    )
    assert proc.returncode == 1
    assert proc.stdout == ""
    assert not (target / "snapshots" / live.name).exists()

    shutil.copytree(live, target / "snapshots" / live.name)
    blocked = snapshot_transfer_plan(source, "current", target)
    assert blocked["transfer_ready"] is False
    with pytest.raises(SnapshotTransferApplyIntegrityError, match="blocked|already"):
        snapshot_transfer_apply(
            source,
            "current",
            target,
            blocked["transfer_revision"],
            transfer_confirmed=True,
        )
    shutil.rmtree(target / "snapshots" / live.name)
    staging = target / "snapshots" / f".staging-{live.name}"
    staging.mkdir()
    staged_plan = snapshot_transfer_plan(source, "current", target)
    with pytest.raises(SnapshotTransferApplyIntegrityError, match="blocked|already"):
        snapshot_transfer_apply(
            source,
            "current",
            target,
            staged_plan["transfer_revision"],
            transfer_confirmed=True,
        )
    assert staging.is_dir()


def test_same_graph_and_alias_rejected_before_leases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.byog_graph as graph_mod

    graph = tmp_path / "graph"
    _publish(graph, "only")
    dummy = "sha256:" + "ab" * 32
    calls = {"leases": 0}
    original = graph_mod.graph_source_shared_target_exclusive_leases

    @contextmanager
    def counted(*args, **kwargs):
        calls["leases"] += 1
        with original(*args, **kwargs) as lease:
            yield lease

    monkeypatch.setattr(graph_mod, "graph_source_shared_target_exclusive_leases", counted)
    with pytest.raises(SnapshotTransferApplyError, match="different directory identities"):
        snapshot_transfer_apply(
            graph, "current", graph, dummy, transfer_confirmed=True
        )
    with pytest.raises(SnapshotTransferApplyError, match="different directory identities"):
        snapshot_transfer_apply(
            graph,
            "current",
            graph / ".",
            dummy,
            transfer_confirmed=True,
        )
    assert calls["leases"] == 0


def test_mixed_mode_order_both_orientations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.byog_graph as graph_mod

    aaa = tmp_path / "aaa"
    zzz = tmp_path / "zzz"
    _publish(aaa, "aaa")
    _publish(zzz, "zzz")
    observed: list[list[tuple[str, bool]]] = []

    def capture(root, exclusive):
        if not observed or len(observed[-1]) == 2:
            observed.append([])
        observed[-1].append((str(Path(root).resolve()), exclusive))

    monkeypatch.setattr(graph_mod, "_after_graph_mixed_lease_one_held", capture)
    plan_fwd = snapshot_transfer_plan(aaa, "current", zzz)
    snapshot_transfer_apply(
        aaa, "current", zzz, plan_fwd["transfer_revision"], transfer_confirmed=True
    )
    plan_rev = snapshot_transfer_plan(zzz, "current", aaa)
    snapshot_transfer_apply(
        zzz, "current", aaa, plan_rev["transfer_revision"], transfer_confirmed=True
    )
    assert len(observed) == 2
    assert [item[0] for item in observed[0]] == [item[0] for item in observed[1]]
    expected_first, expected_second = ordered_graph_lease_pair(
        aaa.resolve(),
        (aaa.stat().st_dev, aaa.stat().st_ino),
        zzz.resolve(),
        (zzz.stat().st_dev, zzz.stat().st_ino),
    )
    assert observed[0][0][0] == str(expected_first)
    assert observed[0][1][0] == str(expected_second)
    fwd_modes = {path: exclusive for path, exclusive in observed[0]}
    rev_modes = {path: exclusive for path, exclusive in observed[1]}
    assert fwd_modes[str(aaa.resolve())] is False
    assert fwd_modes[str(zzz.resolve())] is True
    assert rev_modes[str(zzz.resolve())] is False
    assert rev_modes[str(aaa.resolve())] is True


def test_concurrent_ab_ba_no_deadlock(tmp_path: Path):
    a = tmp_path / "graph-a"
    b = tmp_path / "graph-b"
    live_a = _publish(a, "snap-a")
    live_b = _publish(b, "snap-b")
    plan_ab = snapshot_transfer_plan(a, live_a.name, b)
    plan_ba = snapshot_transfer_plan(b, live_b.name, a)
    errors: list[BaseException] = []
    results: dict[str, dict] = {}

    def run_ab():
        try:
            results["ab"] = snapshot_transfer_apply(
                a,
                live_a.name,
                b,
                plan_ab["transfer_revision"],
                transfer_confirmed=True,
            )
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

    def run_ba():
        try:
            results["ba"] = snapshot_transfer_apply(
                b,
                live_b.name,
                a,
                plan_ba["transfer_revision"],
                transfer_confirmed=True,
            )
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

    first = threading.Thread(target=run_ab)
    second = threading.Thread(target=run_ba)
    first.start()
    second.start()
    first.join(timeout=20)
    second.join(timeout=20)
    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert results["ab"]["ok"] is True
    assert results["ba"]["ok"] is True
    assert (b / "snapshots" / live_a.name).is_dir()
    assert (a / "snapshots" / live_b.name).is_dir()


def test_source_readers_coexist_target_exclusive(tmp_path: Path, monkeypatch):
    import graphrag_code.snapshot_transfer_apply as apply_mod

    source, target, _live, _dest, plan = _prepare(tmp_path)
    held = {"source": False, "target_blocked": False, "proc": None}

    def during_plan(_source, _target, _plan):
        with graph_read_lease(source, allow_unlocked_managed=False):
            held["source"] = True
        held["proc"] = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import sys; from pathlib import Path; "
                    "sys.path.insert(0, sys.argv[1]); "
                    "from graphrag_code.byog_graph import graph_read_lease; "
                    "root = Path(sys.argv[2]); "
                    "graph_read_lease(root, allow_unlocked_managed=False).__enter__(); "
                    "print('acquired', flush=True)"
                ),
                str(ROOT / "src"),
                str(target),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_child_env(),
        )
        time.sleep(0.3)
        held["target_blocked"] = held["proc"].poll() is None

    monkeypatch.setattr(apply_mod, "_after_transfer_apply_plan_computed", during_plan)
    result = snapshot_transfer_apply(
        source, "current", target, plan["transfer_revision"], transfer_confirmed=True
    )
    proc = held["proc"]
    if proc is not None:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    assert result["ok"] is True
    assert held["source"] is True
    assert held["target_blocked"] is True


def test_missing_replaced_symlinked_nonregular_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.byog_graph as graph_mod

    source, target, _live, _dest, plan = _prepare(tmp_path)
    (source / PUBLICATION_LOCK_NAME).unlink()
    missing = _run(
        "--source-graph",
        str(source),
        "--snapshot",
        "current",
        "--target-graph",
        str(target),
        "--expected-transfer-revision",
        plan["transfer_revision"],
        "--transfer-confirmed",
        "--json",
    )
    assert missing.returncode == 2
    assert missing.stdout == ""

    source2, target2, _l2, _d2, plan2 = _prepare(tmp_path / "sym")
    lock = target2 / PUBLICATION_LOCK_NAME
    lock.unlink()
    lock.symlink_to(source2 / PUBLICATION_LOCK_NAME)
    linked = _run(
        "--source-graph",
        str(source2),
        "--snapshot",
        "current",
        "--target-graph",
        str(target2),
        "--expected-transfer-revision",
        plan2["transfer_revision"],
        "--transfer-confirmed",
        "--json",
    )
    assert linked.returncode == 2
    assert linked.stdout == ""

    source3, target3, _l3, _d3, plan3 = _prepare(tmp_path / "fifo")
    lock3 = target3 / PUBLICATION_LOCK_NAME
    lock3.unlink()
    os.mkfifo(lock3)
    fifo = _run(
        "--source-graph",
        str(source3),
        "--snapshot",
        "current",
        "--target-graph",
        str(target3),
        "--expected-transfer-revision",
        plan3["transfer_revision"],
        "--transfer-confirmed",
        "--json",
    )
    assert fifo.returncode == 2
    assert fifo.stdout == ""

    source4, target4, live4, _d4, plan4 = _prepare(tmp_path / "replaced")
    first, second = ordered_graph_lease_pair(
        source4.resolve(),
        (source4.stat().st_dev, source4.stat().st_ino),
        target4.resolve(),
        (target4.stat().st_dev, target4.stat().st_ino),
    )
    replaced = {"done": False}

    def replace_second_lock(root: Path, _exclusive: bool) -> None:
        if Path(root) != first or replaced["done"]:
            return
        replaced["done"] = True
        lock_path = second / PUBLICATION_LOCK_NAME
        lock_path.unlink()
        lock_path.write_bytes(b"")

    monkeypatch.setattr(
        graph_mod, "_after_graph_mixed_lease_one_held", replace_second_lock
    )
    with pytest.raises(
        SnapshotTransferApplyIntegrityError, match="publication lock changed"
    ):
        snapshot_transfer_apply(
            source4,
            "current",
            target4,
            plan4["transfer_revision"],
            transfer_confirmed=True,
        )
    assert replaced["done"] is True
    assert not (target4 / "snapshots" / live4.name).exists()


def test_existing_target_entries_never_replaced(tmp_path: Path):
    source, target, live, _dest, _plan = _prepare(tmp_path)
    shutil.copytree(live, target / "snapshots" / live.name)
    blocked = snapshot_transfer_plan(source, "current", target)
    assert blocked["transfer_ready"] is False
    with pytest.raises(SnapshotTransferApplyIntegrityError, match="blocked|already"):
        snapshot_transfer_apply(
            source, "current", target, blocked["transfer_revision"], transfer_confirmed=True
        )
    assert (target / "snapshots" / live.name).is_dir()

    source2, target2, live2, _d2, _plan2 = _prepare(tmp_path / "stage")
    staging = target2 / "snapshots" / f".staging-{live2.name}"
    staging.mkdir()
    (staging / "witness.bin").write_bytes(b"keep")
    staged_plan = snapshot_transfer_plan(source2, "current", target2)
    assert staged_plan["transfer_ready"] is False
    with pytest.raises(SnapshotTransferApplyIntegrityError, match="blocked|already"):
        snapshot_transfer_apply(
            source2, "current", target2, staged_plan["transfer_revision"], transfer_confirmed=True
        )
    assert (staging / "witness.bin").read_bytes() == b"keep"

    source3, target3, live3, _d3, plan3 = _prepare(tmp_path / "fifo-dest")
    fifo = target3 / "snapshots" / live3.name
    os.mkfifo(fifo)
    with pytest.raises(SnapshotTransferApplyError):
        snapshot_transfer_apply(
            source3, "current", target3, plan3["transfer_revision"], transfer_confirmed=True
        )
    assert fifo.exists() and not fifo.is_dir()

    source4, target4, live4, _d4, plan4 = _prepare(tmp_path / "empty-dir")
    empty = target4 / "snapshots" / live4.name
    empty.mkdir()
    with pytest.raises(SnapshotTransferApplyError):
        snapshot_transfer_apply(
            source4, "current", target4, plan4["transfer_revision"], transfer_confirmed=True
        )
    assert empty.is_dir()
    assert list(empty.iterdir()) == []


def test_source_payload_rewrite_and_injected_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_transfer_apply as apply_mod

    source, target, live, _dest, plan = _prepare(tmp_path)
    payload = live / "entities.parquet"
    original = payload.read_bytes()
    original_stat = payload.stat()

    def rewrite(_source, _records):
        replacement = bytes([original[0] ^ 1]) + original[1:]
        payload.write_bytes(replacement)
        os.utime(payload, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    monkeypatch.setattr(apply_mod, "_after_transfer_apply_copied", rewrite)
    result = snapshot_transfer_apply(
        source, "current", target, plan["transfer_revision"], transfer_confirmed=True
    )
    assert result["ok"] is False
    assert result["partial"] is True
    assert result["publication_performed"] is False
    for path in _staging_leftovers(target):
        shutil.rmtree(path)

    source2, target2, live2, _d2, plan2 = _prepare(tmp_path / "after-hash")
    payload2 = live2 / "entities.parquet"
    original2 = payload2.read_bytes()
    stat2 = payload2.stat()

    def rewrite_one(name):
        if name == "entities.parquet":
            changed = bytes([original2[0] ^ 1]) + original2[1:]
            payload2.write_bytes(changed)
            os.utime(payload2, ns=(stat2.st_atime_ns, stat2.st_mtime_ns))

    monkeypatch.setattr(apply_mod, "_after_transfer_apply_copied", lambda *_: None)
    monkeypatch.setattr(apply_mod, "_after_transfer_apply_payload_copied", rewrite_one)
    result = snapshot_transfer_apply(
        source2, "current", target2, plan2["transfer_revision"], transfer_confirmed=True
    )
    assert result["ok"] is False
    assert result["partial"] is True
    for path in _staging_leftovers(target2):
        shutil.rmtree(path)

    def boom_mkdir(*_args, **_kwargs):
        raise OSError(errno.EIO, "injected mkdir failure")

    source3, target3, _l3, _d3, plan3 = _prepare(tmp_path / "mkdir")
    monkeypatch.setattr(apply_mod, "_after_transfer_apply_payload_copied", lambda *_: None)
    monkeypatch.setattr(apply_mod, "_mkdir_exact_staging", boom_mkdir)
    proc_state = list((target3 / "snapshots").iterdir())
    with pytest.raises(SnapshotTransferApplyError, match="mkdir|failed"):
        snapshot_transfer_apply(
            source3, "current", target3, plan3["transfer_revision"], transfer_confirmed=True
        )
    assert list((target3 / "snapshots").iterdir()) == proc_state


def test_pre_publication_cleanup_and_post_publication_never_deletes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_transfer_apply as apply_mod

    original_fsync_snapshots = apply_mod._fsync_snapshots_directory
    source, target, live, _dest, plan = _prepare(tmp_path)

    def boom_verify(*_args, **_kwargs):
        raise apply_mod.SnapshotTransferApplyIntegrityError("injected staged verify")

    original_verify = apply_mod._verify_staged_envelope
    monkeypatch.setattr(apply_mod, "_verify_staged_envelope", boom_verify)
    result = snapshot_transfer_apply(
        source, "current", target, plan["transfer_revision"], transfer_confirmed=True
    )
    assert result["ok"] is False
    assert result["partial"] is True
    assert result["staging_cleanup_attempted"] is True
    assert result["publication_performed"] is False
    assert not (target / "snapshots" / live.name).exists()

    source2, target2, live2, _d2, plan2 = _prepare(tmp_path / "post")

    def boom_fsync(_fd):
        raise apply_mod.SnapshotTransferApplyError("fsync snapshots directory failed: injected")

    monkeypatch.setattr(apply_mod, "_verify_staged_envelope", original_verify)
    monkeypatch.setattr(apply_mod, "_fsync_snapshots_directory", boom_fsync)
    result = snapshot_transfer_apply(
        source2, "current", target2, plan2["transfer_revision"], transfer_confirmed=True
    )
    assert result["ok"] is False
    assert result["partial"] is True
    assert result["publication_performed"] is True
    assert (target2 / "snapshots" / live2.name).is_dir()
    assert result["staging_cleanup_attempted"] is False

    source3, target3, live3, _d3, plan3 = _prepare(tmp_path / "root-swap")
    replacement = tmp_path / "replacement-target"
    shutil.copytree(target3, replacement)
    detached = tmp_path / "detached-target"

    def replace_target_root(_target: Path, _snapshot_id: str) -> None:
        target3.rename(detached)
        replacement.rename(target3)

    monkeypatch.setattr(
        apply_mod, "_fsync_snapshots_directory", original_fsync_snapshots
    )
    monkeypatch.setattr(
        apply_mod,
        "_after_transfer_apply_post_publication_target_observed",
        replace_target_root,
    )
    result = snapshot_transfer_apply(
        source3,
        "current",
        target3,
        plan3["transfer_revision"],
        transfer_confirmed=True,
    )
    assert result["ok"] is False
    assert result["partial"] is True
    assert result["publication_performed"] is True
    assert not (target3 / "snapshots" / live3.name).exists()
    assert (detached / "snapshots" / live3.name).is_dir()


def test_descriptors_and_leases_held_through_flush(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.byog_graph as graph_mod
    import graphrag_code.snapshot_transfer_apply as apply_mod

    real_stdout = sys.stdout
    source, target, _live, _dest, plan = _prepare(tmp_path)
    original_lease = graph_mod.graph_source_shared_target_exclusive_leases
    original_json = apply_mod.result_to_json
    state = {
        "lease": False,
        "source_fd": None,
        "target_fd": None,
        "payload_fds": {},
        "flushes": 0,
    }

    @contextmanager
    def tracked(*args, **kwargs):
        with original_lease(*args, **kwargs) as lease:
            state["lease"] = True
            try:
                yield lease
            finally:
                state["lease"] = False

    def capture(*args):
        state["source_fd"] = args[2]
        state["target_fd"] = args[3]
        state["payload_fds"] = dict(args[8])
        os.fstat(state["source_fd"])
        os.fstat(state["target_fd"])
        for fd in state["payload_fds"].values():
            os.fstat(fd)
        assert state["lease"] is True

    def guarded_json(*args, **kwargs):
        assert state["lease"] is True
        os.fstat(state["source_fd"])
        os.fstat(state["target_fd"])
        return original_json(*args, **kwargs)

    class GuardedStdout:
        def write(self, text: str) -> int:
            assert state["lease"] is True
            os.fstat(state["source_fd"])
            return len(text)

        def flush(self) -> None:
            assert state["lease"] is True
            os.fstat(state["source_fd"])
            os.fstat(state["target_fd"])
            state["flushes"] += 1

    monkeypatch.setattr(graph_mod, "graph_source_shared_target_exclusive_leases", tracked)
    monkeypatch.setattr(apply_mod, "graph_source_shared_target_exclusive_leases", tracked)
    monkeypatch.setattr(apply_mod, "_after_transfer_apply_result_ready", capture)
    monkeypatch.setattr(apply_mod, "result_to_json", guarded_json)
    monkeypatch.setattr(apply_mod.sys, "stdout", GuardedStdout())
    assert (
        apply_mod.main(
            [
                "--source-graph",
                str(source),
                "--snapshot",
                "current",
                "--target-graph",
                str(target),
                "--expected-transfer-revision",
                plan["transfer_revision"],
                "--transfer-confirmed",
                "--json",
            ]
        )
        == 0
    )
    assert state["lease"] is False
    assert state["flushes"] == 1

    source2, target2, live2, _d2, plan2 = _prepare(tmp_path / "serialize-fail")

    def broken_json(_result):
        assert state["lease"] is True
        raise ValueError("injected serialization failure")

    class SinkStdout:
        def write(self, text: str) -> int:
            return len(text)

        def flush(self) -> None:
            return None

    monkeypatch.setattr(apply_mod, "result_to_json", broken_json)
    monkeypatch.setattr(apply_mod.sys, "stdout", SinkStdout())
    assert (
        apply_mod.main(
            [
                "--source-graph",
                str(source2),
                "--snapshot",
                "current",
                "--target-graph",
                str(target2),
                "--expected-transfer-revision",
                plan2["transfer_revision"],
                "--transfer-confirmed",
                "--json",
            ]
        )
        == 1
    )
    assert state["lease"] is False
    assert (target2 / "snapshots" / live2.name).is_dir()

    source3, target3, live3, _d3, plan3 = _prepare(tmp_path / "flush-fail")

    class FailingFlushStdout:
        def write(self, text: str) -> int:
            assert state["lease"] is True
            return len(text)

        def flush(self) -> None:
            assert state["lease"] is True
            raise OSError(errno.EIO, "injected flush failure")

    monkeypatch.setattr(apply_mod, "result_to_json", original_json)
    monkeypatch.setattr(apply_mod.sys, "stdout", FailingFlushStdout())
    assert (
        apply_mod.main(
            [
                "--source-graph",
                str(source3),
                "--snapshot",
                "current",
                "--target-graph",
                str(target3),
                "--expected-transfer-revision",
                plan3["transfer_revision"],
                "--transfer-confirmed",
                "--json",
            ]
        )
        == 1
    )
    assert state["lease"] is False
    assert (target3 / "snapshots" / live3.name).is_dir()
    monkeypatch.setattr(apply_mod.sys, "stdout", real_stdout)


def test_no_mcp_tool_and_implementation_constraints(tmp_path: Path):
    source, target, _live, _dest, plan = _prepare(tmp_path)
    snapshot_transfer_apply(
        source, "current", target, plan["transfer_revision"], transfer_confirmed=True
    )
    source_text = MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source_text)
    imported: set[str] = set()
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                called.add(func.id)
            elif isinstance(func, ast.Attribute):
                called.add(func.attr)
    assert "graph_source_shared_target_exclusive_leases" in imported
    assert "graph_read_lease" not in imported
    assert "graph_exclusive_lease" not in imported
    assert "snapshot_transfer_plan(" not in source_text
    assert "os.rename" not in source_text
    lowered = source_text.lower()
    for word in FORBIDDEN_WORDS:
        assert word not in lowered
    from anyio import run as anyio_run

    assert len(TOOL_NAMES) == 13
    assert "snapshot_transfer_apply" not in TOOL_NAMES
    session = build_session(target, "python")
    server = build_mcp_server(session)

    async def _body():
        from mcp import Client

        async with Client(server) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert names == set(TOOL_NAMES)
            assert "snapshot_transfer_apply" not in names

    anyio_run(_body)


def test_byog_roots_unchanged(tmp_path: Path):
    before = {path.name: path.stat().st_mtime_ns for path in BYOG_ROOTS}
    source, target, _live, _dest, plan = _prepare(tmp_path)
    snapshot_transfer_apply(
        source, "current", target, plan["transfer_revision"], transfer_confirmed=True
    )
    after = {path.name: path.stat().st_mtime_ns for path in BYOG_ROOTS}
    assert after == before
    leftovers: list[Path] = []
    for dirpath, dirnames, _filenames in os.walk(ROOT, followlinks=False):
        rel = Path(dirpath).relative_to(ROOT)
        if ".git" in rel.parts or "output" in rel.parts:
            dirnames[:] = []
            continue
        leftovers.extend(
            Path(dirpath) / name
            for name in dirnames
            if name.startswith(".staging-") or name.startswith(".graphrag-export-")
        )
    assert leftovers == []
