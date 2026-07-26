"""Targeted Python-oracle checks for the cheap API-surface parity additions."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[3]
RUST_MANIFEST = REPO / "examples" / "charset_normalizer_rust" / "Cargo.toml"
RUST_PROBE = (
    REPO / "examples" / "charset_normalizer_rust" / "target" / "debug" / "parity_probe"
)

sys.path.insert(0, str(REPO / "examples"))
from charset_normalizer import VERSION, __version__, from_bytes, is_binary  # type: ignore


@pytest.fixture(scope="session")
def rust_probe() -> Path:
    subprocess.run(
        [
            "cargo",
            "build",
            "--quiet",
            "--manifest-path",
            str(RUST_MANIFEST),
            "--bin",
            "parity_probe",
        ],
        cwd=REPO,
        check=True,
        text=True,
    )
    assert RUST_PROBE.exists(), f"probe binary not found at {RUST_PROBE}"
    return RUST_PROBE


def run_probe(probe: Path, *args: str) -> str:
    proc = subprocess.run(
        [str(probe), *args],
        cwd=REPO,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc.stdout.strip()


def test_cheap_public_api_surface_matches_python_oracle(
    rust_probe: Path, tmp_path: Path
) -> None:
    """Compare version, BOM alias, and reader/path binary dispatch to vendored Python."""
    assert run_probe(rust_probe, "version").split("\t") == [__version__, ".".join(VERSION)]

    bom_payload = b"\xef\xbb\xbfAPI surface parity\n"
    python_match = from_bytes(bom_payload).best()
    assert python_match is not None
    assert run_probe(rust_probe, "byte-order-mark", bom_payload.hex()).split("\t") == [
        str(int(python_match.bom)),
        str(int(python_match.byte_order_mark)),
    ]

    for name, payload in {
        "text.txt": bom_payload,
        "binary.bin": b"abc\x00\x01\xffdef",
    }.items():
        payload_path = tmp_path / name
        payload_path.write_bytes(payload)
        with payload_path.open("rb") as fp:
            python_reader_result = is_binary(fp)
        python_path_result = is_binary(payload_path)

        assert run_probe(rust_probe, "is-binary-reader", str(payload_path)) == str(
            int(python_reader_result)
        )
        assert run_probe(rust_probe, "is-binary-path-options", str(payload_path)) == str(
            int(python_path_result)
        )
