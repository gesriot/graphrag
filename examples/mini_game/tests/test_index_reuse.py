"""Content-addressed whole-snapshot reuse for CLI indexing.

Disposable graphs only. Does not write published byog_* roots.

Run:
  uv run python -m pytest examples/mini_game/tests/test_index_reuse.py -q
"""
from __future__ import annotations

import json
import multiprocessing
import os
import stat
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "src"))

from graphrag_code import index_c as pkg_index_c  # type: ignore
from graphrag_code import index_python as pkg_index_python  # type: ignore
from graphrag_code.byog_graph import is_published_snapshot_id, is_staging_snapshot_name  # type: ignore
from graphrag_code.index_reuse import (  # type: ignore
    canonical_c_options,
    canonical_python_options,
    compute_index_fingerprint,
    unsupported_index_input,
    validate_supported_index_input,
)
from graphrag_code.python_inputs import list_indexed_python_files  # type: ignore

CTX = multiprocessing.get_context("spawn")
PY_OPTS = canonical_python_options(use_advanced=False)
C_OPTS = canonical_c_options()


def _invoke(fn, **kwargs):
    stdout = StringIO()
    stderr = StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            fn(**kwargs)
        code = 0
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    out = stdout.getvalue()
    err = stderr.getvalue()
    return SimpleNamespace(exit_code=code, stdout=out, stderr=err, output=out + err)


def _write_py(pkg: Path, name: str = "demo.py", body: str | None = None) -> Path:
    pkg.mkdir(parents=True, exist_ok=True)
    path = pkg / name
    path.write_text(body or "def add(left, right):\n    return left + right\n")
    return path


def _write_c(pkg: Path, name: str = "demo.c", body: str | None = None) -> Path:
    pkg.mkdir(parents=True, exist_ok=True)
    path = pkg / name
    path.write_text(body or "int add(int left, int right) { return left + right; }\n")
    return path


def _index_py(pkg: Path, graph: Path, *, reuse: bool = False, advanced: bool = False):
    return _invoke(
        pkg_index_python.main,
        package=pkg,
        graph=graph,
        keep_snapshots=5,
        use_advanced=advanced,
        reuse_unchanged=reuse,
    )


def _index_c(
    pkg: Path,
    graph: Path,
    *,
    reuse: bool = False,
    compiler_builtins: bool = False,
):
    return _invoke(
        pkg_index_c.main,
        package=pkg,
        graph=graph,
        keep_snapshots=5,
        compiler_builtins=compiler_builtins,
        compiler_dependencies=False,
        compiler_includes=False,
        clang_signatures=False,
        clang_calls=False,
        clang_types=False,
        clang_type_uses=False,
        clang_type_shapes=False,
        allow_toolchain_drift=False,
        reuse_unchanged=reuse,
    )


def _published(graph: Path) -> list[Path]:
    snaps = graph / "snapshots"
    if not snaps.is_dir():
        return []
    return sorted(
        path
        for path in snaps.iterdir()
        if path.is_dir() and is_published_snapshot_id(path.name)
    )


def _staging(graph: Path) -> list[Path]:
    snaps = graph / "snapshots"
    if not snaps.is_dir():
        return []
    return [
        path
        for path in snaps.iterdir()
        if path.is_dir() and is_staging_snapshot_name(path.name)
    ]


def _current_id(graph: Path) -> str:
    return (graph / "current").read_text(encoding="utf-8").strip()


def _payload_stats(graph: Path) -> dict[str, tuple[int, int, int]]:
    """size, mtime_ns, mode for current + published snapshot files."""
    out: dict[str, tuple[int, int, int]] = {}
    for path in [graph / "current", *sorted((graph / "snapshots").rglob("*"))]:
        if not path.is_file() or path.is_symlink():
            continue
        info = path.lstat()
        rel = path.relative_to(graph).as_posix()
        out[rel] = (info.st_size, info.st_mtime_ns, stat.S_IMODE(info.st_mode))
    return out


def _file_hashes(graph: Path) -> dict[str, str]:
    import hashlib

    out: dict[str, str] = {}
    for path in sorted((graph / "snapshots").rglob("*")):
        if path.is_file() and not path.is_symlink():
            out[path.relative_to(graph).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    out["current"] = hashlib.sha256((graph / "current").read_bytes()).hexdigest()
    return out


def test_fingerprint_independent_of_mtime_and_enumeration(tmp_path: Path):
    first = tmp_path / "a"
    second = tmp_path / "b"
    for pkg, names in ((first, ("z.py", "a.py", "m.py")), (second, ("a.py", "m.py", "z.py"))):
        for name in names:
            _write_py(pkg, name, f"def {name[0]}():\n    return 1\n")
    left = compute_index_fingerprint("python", first, PY_OPTS)
    os.utime(first / "a.py", (0, 0))
    os.utime(first / "z.py", (10**9, 10**9))
    again = compute_index_fingerprint("python", first, PY_OPTS)
    right = compute_index_fingerprint("python", second, PY_OPTS)
    assert left["digest"] == again["digest"] == right["digest"]
    assert left["n_files"] == 3
    assert validate_supported_index_input(left) == left


def test_content_path_add_delete_change_digest(tmp_path: Path):
    pkg = tmp_path / "pkg"
    _write_py(pkg, "a.py")
    _write_py(pkg, "b.py", "def b():\n    return 1\n")
    base = compute_index_fingerprint("python", pkg, PY_OPTS)
    (pkg / "a.py").write_text("def add(left, right):\n    return left - right\n")
    assert compute_index_fingerprint("python", pkg, PY_OPTS)["digest"] != base["digest"]
    (pkg / "a.py").write_text("def add(left, right):\n    return left + right\n")
    (pkg / "a.py").rename(pkg / "renamed.py")
    assert compute_index_fingerprint("python", pkg, PY_OPTS)["digest"] != base["digest"]
    (pkg / "renamed.py").rename(pkg / "a.py")
    (pkg / "c.py").write_text("def c():\n    return 3\n")
    assert compute_index_fingerprint("python", pkg, PY_OPTS)["digest"] != base["digest"]
    (pkg / "c.py").unlink()
    assert compute_index_fingerprint("python", pkg, PY_OPTS)["digest"] == base["digest"]
    (pkg / "b.py").unlink()
    assert compute_index_fingerprint("python", pkg, PY_OPTS)["digest"] != base["digest"]


def test_ignored_python_tests_do_not_change_digest(tmp_path: Path):
    pkg = tmp_path / "pkg"
    _write_py(pkg)
    before = compute_index_fingerprint("python", pkg, PY_OPTS)
    tests = pkg / "tests"
    tests.mkdir()
    (tests / "test_demo.py").write_text("def test_add():\n    assert True\n")
    after = compute_index_fingerprint("python", pkg, PY_OPTS)
    assert before["digest"] == after["digest"]
    assert before["n_files"] == after["n_files"] == 1


def test_init_py_reexport_versus_direct_definition(tmp_path: Path):
    pkg = tmp_path / "pkg"
    _write_py(pkg, "mod.py", "def helper():\n    return 1\n")
    init = pkg / "__init__.py"
    init.write_text("from .mod import helper\n")
    assert list_indexed_python_files(pkg) == [pkg.resolve() / "mod.py"]
    reexport = compute_index_fingerprint("python", pkg, PY_OPTS)
    init.write_text("def split(text):\n    return text.split()\n")
    selected = list_indexed_python_files(pkg)
    assert pkg.resolve() / "__init__.py" in selected
    defined = compute_index_fingerprint("python", pkg, PY_OPTS)
    assert defined["digest"] != reexport["digest"]
    assert defined["n_files"] == reexport["n_files"] + 1

    # Preserve the pre-reuse indexer's lexical identity for a symlinked file.
    external = tmp_path / "external.py"
    external.write_text("def linked():\n    return 1\n")
    linked = pkg / "linked.py"
    linked.symlink_to(external)
    assert linked in list_indexed_python_files(pkg)
    with_link = compute_index_fingerprint("python", pkg, PY_OPTS)
    external.write_text("def linked():\n    return 2\n")
    assert compute_index_fingerprint("python", pkg, PY_OPTS)["digest"] != with_link["digest"]
    indexed = _index_py(pkg, tmp_path / "symlink-graph")
    assert indexed.exit_code == 0, indexed.output


def test_c_compile_commands_changes_digest(tmp_path: Path):
    pkg = tmp_path / "pkg"
    _write_c(pkg)
    before = compute_index_fingerprint("c", pkg, C_OPTS)
    (pkg / "compile_commands.json").write_text("[]\n")
    mid = compute_index_fingerprint("c", pkg, C_OPTS)
    assert mid["digest"] != before["digest"]
    (pkg / "compile_commands.json").write_text('[{"directory": ".", "file": "demo.c"}]\n')
    after = compute_index_fingerprint("c", pkg, C_OPTS)
    assert after["digest"] != mid["digest"]


def test_producer_digest_mismatch_is_miss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.index_reuse as reuse

    baseline_producer = reuse.producer_digest()
    real_version = reuse._distribution_version
    with monkeypatch.context() as patch:
        patch.setattr(
            reuse,
            "_distribution_version",
            lambda name: "999.changed" if name == "tree-sitter" else real_version(name),
        )
        assert reuse.producer_digest() != baseline_producer

    pkg = tmp_path / "pkg"
    graph = tmp_path / "graph"
    _write_py(pkg)
    built = _index_py(pkg, graph)
    assert built.exit_code == 0, built.output
    first = _current_id(graph)
    manifest_path = graph / "snapshots" / first / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["index_input"]["producer_digest"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest, indent=2))
    reused = _index_py(pkg, graph, reuse=True)
    assert reused.exit_code == 0, reused.output
    assert "Unchanged. Reusing snapshot:" not in reused.stdout
    assert _current_id(graph) != first


def test_legacy_and_malformed_index_input_are_misses(tmp_path: Path):
    pkg = tmp_path / "pkg"
    graph = tmp_path / "graph"
    _write_py(pkg)
    assert _index_py(pkg, graph).exit_code == 0
    snap = graph / "snapshots" / _current_id(graph)
    manifest_path = snap / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    first = _current_id(graph)

    del manifest["index_input"]
    manifest_path.write_text(json.dumps(manifest, indent=2))
    legacy = _index_py(pkg, graph, reuse=True)
    assert legacy.exit_code == 0, legacy.output
    assert "Unchanged. Reusing snapshot:" not in legacy.stdout
    second = _current_id(graph)
    assert second != first

    manifest = json.loads((graph / "snapshots" / second / "manifest.json").read_text())
    manifest["index_input"] = {"schema_version": 1, "reuse_supported": True}
    (graph / "snapshots" / second / "manifest.json").write_text(
        json.dumps(manifest, indent=2)
    )
    malformed = _index_py(pkg, graph, reuse=True)
    assert malformed.exit_code == 0, malformed.output
    assert "Unchanged. Reusing snapshot:" not in malformed.stdout
    assert _current_id(graph) != second


def _assert_true_noop(graph: Path, before_stats, before_hashes, result) -> None:
    assert result.exit_code == 0, result.output
    assert result.stdout.strip().startswith("Unchanged. Reusing snapshot:")
    assert _staging(graph) == []
    assert _payload_stats(graph) == before_stats
    assert _file_hashes(graph) == before_hashes


def test_python_true_noop_does_not_call_extractor_or_publisher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    pkg = tmp_path / "pkg"
    graph = tmp_path / "graph"
    _write_py(pkg)
    built = _index_py(pkg, graph)
    assert built.exit_code == 0, built.output
    assert "index_input" in json.loads(
        (graph / "snapshots" / _current_id(graph) / "manifest.json").read_text()
    )
    stats = _payload_stats(graph)
    hashes = _file_hashes(graph)
    published = [path.name for path in _published(graph)]

    def boom(*_args, **_kwargs):
        raise AssertionError("extractor or publisher invoked on reuse hit")

    monkeypatch.setattr(pkg_index_python, "build_byog_for_package", boom)
    monkeypatch.setattr(pkg_index_python, "publish_byog_snapshot", boom)
    reused = _index_py(pkg, graph, reuse=True)
    _assert_true_noop(graph, stats, hashes, reused)
    assert [path.name for path in _published(graph)] == published


def test_c_true_noop_does_not_call_extractor_or_publisher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    pkg = tmp_path / "pkg"
    graph = tmp_path / "graph"
    _write_c(pkg)
    built = _index_c(pkg, graph)
    assert built.exit_code == 0, built.output
    stats = _payload_stats(graph)
    hashes = _file_hashes(graph)
    published = [path.name for path in _published(graph)]

    def boom(*_args, **_kwargs):
        raise AssertionError("extractor or publisher invoked on reuse hit")

    monkeypatch.setattr(pkg_index_c, "build_c_byog", boom)
    monkeypatch.setattr(pkg_index_c, "publish_byog_snapshot", boom)
    reused = _index_c(pkg, graph, reuse=True)
    _assert_true_noop(graph, stats, hashes, reused)
    assert [path.name for path in _published(graph)] == published


def test_invalid_current_snapshot_is_never_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    pkg = tmp_path / "pkg"
    graph = tmp_path / "graph"
    _write_py(pkg)
    assert _index_py(pkg, graph).exit_code == 0
    first = _current_id(graph)
    snap = graph / "snapshots" / first
    manifest = json.loads((snap / "manifest.json").read_text())
    manifest["counts"]["entities"] = 0
    (snap / "manifest.json").write_text(json.dumps(manifest, indent=2))
    rebuilt = _index_py(pkg, graph, reuse=True)
    assert rebuilt.exit_code == 0, rebuilt.output
    assert "Unchanged. Reusing snapshot:" not in rebuilt.stdout
    assert _current_id(graph) != first
    new_manifest = json.loads(
        (graph / "snapshots" / _current_id(graph) / "manifest.json").read_text()
    )
    assert new_manifest["counts"]["entities"] > 0

    # Controlled doctor data failures are cache misses, matching its CLI.
    import graphrag_code.persisted_graph_doctor as doctor

    monkeypatch.setattr(
        doctor,
        "audit_graph_root",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyError("broken row")),
    )
    valid = _current_id(graph)
    retry = _index_py(pkg, graph, reuse=True)
    assert retry.exit_code == 0, retry.output
    assert "Unchanged. Reusing snapshot:" not in retry.stdout
    assert _current_id(graph) != valid


def test_source_change_publishes_exactly_one_new_snapshot(tmp_path: Path):
    pkg = tmp_path / "pkg"
    graph = tmp_path / "graph"
    _write_py(pkg)
    assert _index_py(pkg, graph).exit_code == 0
    first = {_current_id(graph)}
    (pkg / "demo.py").write_text("def add(left, right):\n    return left * right\n")
    changed = _index_py(pkg, graph, reuse=True)
    assert changed.exit_code == 0, changed.output
    assert "Unchanged. Reusing snapshot:" not in changed.stdout
    ids = {path.name for path in _published(graph)}
    assert _current_id(graph) not in first
    assert ids == first | {_current_id(graph)}
    assert len(ids) == 2


def test_source_change_during_extraction_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    pkg = tmp_path / "pkg"
    graph = tmp_path / "graph"
    _write_py(pkg)
    real = pkg_index_python.build_byog_for_package

    def mutate(use_advanced=False, package_dir=None):
        (Path(package_dir) / "demo.py").write_text("def add(left, right):\n    return 0\n")
        return real(use_advanced=use_advanced, package_dir=package_dir)

    monkeypatch.setattr(pkg_index_python, "build_byog_for_package", mutate)
    result = _index_py(pkg, graph)
    assert result.exit_code == 2, result.output
    assert "source changed during indexing" in result.output
    assert _published(graph) == []
    assert _staging(graph) == []
    assert not (graph / "current").exists()

    c_pkg = tmp_path / "c-pkg"
    c_graph = tmp_path / "c-graph"
    _write_c(c_pkg)
    real_c = pkg_index_c.build_c_byog

    def mutate_c(package_dir):
        (Path(package_dir) / "demo.c").write_text(
            "int add(int left, int right) { return 0; }\n"
        )
        return real_c(package_dir)

    monkeypatch.setattr(pkg_index_c, "build_c_byog", mutate_c)
    c_result = _index_c(c_pkg, c_graph)
    assert c_result.exit_code == 2, c_result.output
    assert "source changed during indexing" in c_result.output
    assert _published(c_graph) == []
    assert _staging(c_graph) == []
    assert not (c_graph / "current").exists()


def test_source_root_mismatch_rebuilds(tmp_path: Path):
    src_a = tmp_path / "a"
    src_b = tmp_path / "b"
    graph = tmp_path / "graph"
    _write_py(src_a)
    _write_py(src_b)
    assert _index_py(src_a, graph).exit_code == 0
    first = _current_id(graph)
    rebuilt = _index_py(src_b, graph, reuse=True)
    assert rebuilt.exit_code == 0, rebuilt.output
    assert "Unchanged. Reusing snapshot:" not in rebuilt.stdout
    assert _current_id(graph) != first
    manifest = json.loads(
        (graph / "snapshots" / _current_id(graph) / "manifest.json").read_text()
    )
    assert Path(manifest["source_root"]).resolve() == src_b.resolve()


def test_unsupported_reuse_exits_2_without_graph_mutation(tmp_path: Path):
    pkg = tmp_path / "pkg"
    graph = tmp_path / "graph"
    _write_py(pkg)
    assert _index_py(pkg, graph).exit_code == 0
    before = _payload_stats(graph)
    hashes = _file_hashes(graph)
    refused = _index_py(pkg, graph, reuse=True, advanced=True)
    assert refused.exit_code == 2, refused.output
    assert "use_advanced" in refused.output
    assert _payload_stats(graph) == before
    assert _file_hashes(graph) == hashes

    c_pkg = tmp_path / "c"
    c_graph = tmp_path / "cgraph"
    _write_c(c_pkg)
    empty = _index_c(c_pkg, c_graph, reuse=True, compiler_builtins=True)
    assert empty.exit_code == 2, empty.output
    assert "compiler_builtins" in empty.output
    assert not c_graph.exists()

    lock_graph = tmp_path / "lock-graph"
    lock_graph.mkdir()
    external_lock = tmp_path / "external.lock"
    external_lock.write_text("do not touch")
    (lock_graph / ".index.lock").symlink_to(external_lock)
    locked = _index_py(pkg, lock_graph)
    assert locked.exit_code == 2, locked.output
    assert "symlinked index lock" in locked.output
    assert external_lock.read_text() == "do not touch"
    assert not (lock_graph / "snapshots").exists()


def _concurrent_worker(package: str, graph: str, result_path: str) -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "graphrag_code.index_python",
            "--package",
            package,
            "--graph",
            graph,
            "--keep-snapshots",
            "5",
            "--reuse-unchanged",
        ],
        capture_output=True,
        text=True,
        cwd=str(Path(package).parent),
    )
    Path(result_path).write_text(
        json.dumps(
            {
                "code": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
            }
        )
    )


def test_two_concurrent_reuse_indexers_publish_one_snapshot(tmp_path: Path):
    pkg = tmp_path / "pkg"
    graph = tmp_path / "graph"
    _write_py(pkg)
    left = tmp_path / "left.json"
    right = tmp_path / "right.json"
    workers = [
        CTX.Process(
            target=_concurrent_worker,
            args=(str(pkg), str(graph), str(path)),
        )
        for path in (left, right)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=60)
        assert worker.exitcode == 0
    payloads = [json.loads(path.read_text()) for path in (left, right)]
    assert all(item["code"] == 0 for item in payloads), payloads
    published = _published(graph)
    assert len(published) == 1
    assert _staging(graph) == []
    texts = [item["stdout"] + item["stderr"] for item in payloads]
    assert sum("Unchanged. Reusing snapshot:" in text for text in texts) == 1
    assert sum("Done. Snapshot:" in text for text in texts) == 1


def test_script_and_package_modules_are_the_same_object():
    scripts = str(ROOT / "scripts")
    if scripts in sys.path:
        sys.path.remove(scripts)
    sys.path.insert(0, scripts)
    import index_c
    import index_python
    import persisted_graph_doctor

    assert index_python is pkg_index_python
    assert index_c is pkg_index_c
    from graphrag_code import persisted_graph_doctor as packaged_doctor

    assert persisted_graph_doctor is packaged_doctor


def test_unsupported_block_has_no_reuse_digest():
    block = unsupported_index_input("python", canonical_python_options(use_advanced=True))
    assert block["reuse_supported"] is False
    assert block["digest"] is None
    assert block["producer_digest"] is None
    assert "use_advanced" in block["reason"]
    assert validate_supported_index_input(block) is None
