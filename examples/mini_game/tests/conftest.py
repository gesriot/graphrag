"""Shared disposable fixtures for packaging and MCP tests."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]


@pytest.fixture(scope="session")
def built_wheel_and_sdist(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    dist = tmp_path_factory.mktemp("dist")
    env = os.environ.copy()
    env["UV_CACHE_DIR"] = str(tmp_path_factory.mktemp("uv-build-cache"))
    proc = subprocess.run(
        [
            "uv",
            "build",
            "--offline",
            "--no-build-isolation",
            "--out-dir",
            str(dist),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    wheels = list(dist.glob("*.whl"))
    sdists = list(dist.glob("*.tar.gz"))
    assert len(wheels) == 1, wheels
    assert len(sdists) == 1, sdists
    return wheels[0], sdists[0]


def install_wheel(wheel: Path, target: Path) -> dict[str, str]:
    """Install the wheel into an isolated prefix; reuse the test interpreter's deps."""
    install_env = os.environ.copy()
    install_env["UV_CACHE_DIR"] = str(target.parent / "uv-install-cache")
    proc = subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            sys.executable,
            "--no-deps",
            "--offline",
            "--target",
            str(target),
            str(wheel),
        ],
        capture_output=True,
        text=True,
        env=install_env,
    )
    assert proc.returncode == 0, proc.stderr
    env = os.environ.copy()
    env["PATH"] = (
        str(target / "bin")
        + os.pathsep
        + str(Path(sys.executable).parent)
        + os.pathsep
        + env.get("PATH", "")
    )
    env["PYTHONPATH"] = str(target)
    loc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import graphrag_code, graphrag_code.byog_graph as b; print(b.__file__)",
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(Path.home()),
    )
    assert loc.returncode == 0, loc.stderr
    assert str(target) in loc.stdout
    assert str(ROOT / "src") not in loc.stdout
    return env
