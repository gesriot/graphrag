#!/usr/bin/env python3
"""Opt-in scale/performance harness for charset-normalizer Python ref vs Rust port.

Covers 100k+ LOC / product-scale workloads to address the scale caveat.
Deterministic generators (fixed seeds). Not run by default.

Run (opt-in via CN_SCALE or direct invocation):
  CN_SCALE=1 PYTHONPATH=. uv run python examples/charset_normalizer_rust/tools/scale_harness.py

To keep normal pytest fast, this file is deliberately not named test_*.py
and lives under tools/. Default commands remain:
  cd examples/charset_normalizer_rust && cargo fmt && cargo test --quiet
  PYTHONPATH=. uv run pytest examples -q --tb=no
"""

from __future__ import annotations

import io
import os
import random
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "examples"))

from charset_normalizer import from_bytes as py_from_bytes  # noqa: E402


RUST_MANIFEST = REPO / "examples" / "charset_normalizer_rust" / "Cargo.toml"
RUST_RELEASE_EXE = (
    REPO
    / "examples"
    / "charset_normalizer_rust"
    / "target"
    / "release"
    / "normalizer"
)


def ensure_rust_release() -> Path:
    subprocess.run(
        ["cargo", "build", "--release", "--quiet", "--manifest-path", str(RUST_MANIFEST)],
        cwd=REPO,
        check=True,
    )
    if not RUST_RELEASE_EXE.exists():
        raise RuntimeError(f"Release exe not found at {RUST_RELEASE_EXE}")
    return RUST_RELEASE_EXE


def run_python(payload: bytes) -> tuple[str, float]:
    start = time.perf_counter()
    matches = py_from_bytes(payload)
    elapsed = time.perf_counter() - start
    best = matches.best()
    enc = best.encoding if best is not None else "undefined"
    return enc, elapsed


def run_rust(exe: Path, payload: bytes) -> tuple[str, float]:
    with tempfile.NamedTemporaryFile(delete=False, suffix=".dat") as f:
        f.write(payload)
        tmp_path = f.name
    try:
        start = time.perf_counter()
        proc = subprocess.run(
            [str(exe), "--minimal", tmp_path],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=True,
        )
        elapsed = time.perf_counter() - start
        enc = proc.stdout.strip() or "undefined"
        return enc, elapsed
    finally:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass


def gen_large_py_source(n_lines: int = 120000, seed: int = 42424242) -> bytes:
    """100k+ LOC synthetic Python-like source (deterministic)."""
    rng = random.Random(seed)
    out = io.BytesIO()
    # ~7 lines per block to hit line count with modest memory
    block = (
        b"def func_%d(x, y):\n"
        b"    z = x * %d + %d\n"
        b"    if z > 0:\n"
        b"        return 'res-%d'\n"
        b"class C%d:\n"
        b"    pass  # synthetic %d\n"
        b"# line comment id=%d text data\n"
    )
    blocks = n_lines // 7 + 1
    for i in range(blocks):
        v = i % 99991
        out.write(block % (v, v, v, v, v, v, v))
    data = out.getvalue()
    # ensure at least n_lines newlines by trunc after split
    lines = data.split(b"\n")
    if len(lines) > n_lines:
        data = b"\n".join(lines[:n_lines]) + b"\n"
    return data


def gen_large_rust_source(n_lines: int = 80000, seed: int = 43434343) -> bytes:
    """Large synthetic Rust-like source (deterministic)."""
    rng = random.Random(seed)
    out = io.BytesIO()
    block = (
        b"fn func_%d(x: i32, y: i32) -> i32 {\n"
        b"    let z = x * y + %d;\n"
        b"    if z > 0 { return z; }\n"
        b"    0\n"
        b"}\n"
        b"struct S%d { f: u32 }\n"
        b"// rust comment %d\n"
        b"impl S%d { fn new() -> Self { Self { f: %d } } }\n"
    )
    blocks = n_lines // 6 + 1
    for i in range(blocks):
        v = i % 88801
        out.write(block % (v, v, v, v, v, v))
    data = out.getvalue()
    lines = data.split(b"\n")
    if len(lines) > n_lines:
        data = b"\n".join(lines[:n_lines]) + b"\n"
    return data


def gen_large_utf8_prose(target: int = 1200000, seed: int = 112233) -> bytes:
    """Large UTF-8 prose with mixed scripts (deterministic repeat + pad)."""
    base = (
        "The quick naïve café résumé jumps — 文字 テスト 텍스트 Текст טקסט. "
        "More prose with accents: été à Noël. العربية mixed here. "
    )
    bbase = base.encode("utf-8")
    reps = target // len(bbase) + 2
    data = (base * reps).encode("utf-8")
    return data[:target]


def gen_large_western_sb(target: int = 900000, seed: int = 556677) -> bytes:
    """Large Western single-byte text (raw bytes in cp1252 encoding)."""
    text = (
        "Molière déjà naïve façade résumé €100 £ café naïve façade. "
        "El Niño jalapeño. 100$ and naïve résumé. "
    )
    reps = target // len(text.encode("cp1252")) + 2
    data = (text * reps).encode("cp1252")
    return data[:target]


def gen_large_cjk_ar_cy(target: int = 600000, seed: int = 778899) -> bytes:
    """Large CJK / Arabic / Cyrillic snippets (utf-8 bytes)."""
    base = (
        "中文繁體 日本語テスト 한국 텍스트. "
        "مرحبا بالعالم. Привет мир. Тест кириллицы. "
        "Mixed CJK Arabic Cyrillic blocks. "
    )
    bbase = base.encode("utf-8")
    reps = target // len(bbase) + 2
    data = (base * reps).encode("utf-8")
    return data[:target]


def gen_large_html_xml_decl(target: int = 450000, seed: int = 991001) -> bytes:
    """Mixed HTML/XML with declarative charset hints (western bytes)."""
    chunk = (
        b'<meta charset="iso-8859-1"><p>caf\xe9 d\xe9j\xe0</p>\n'
        b'<?xml version="1.0" encoding="windows-1252"?>\n'
        b"<root>text with \xe9l\xe8ments</root>\n"
    )
    reps = target // len(chunk) + 2
    data = chunk * reps
    return data[:target]


def gen_large_binaryish(target: int = 700000, seed: int = 223344) -> bytes:
    """Binary-ish payload (high entropy + some text fragments)."""
    rng = random.Random(seed)
    data = bytearray(rng.randrange(256) for _ in range(target))
    # sprinkle recognizable text fragments (should not trigger clean text detect)
    frag = b"some-ascii-text-here-but-surrounded-by-noise\xff\x00\xfe"
    step = 12345
    for off in range(0, target - len(frag), step):
        data[off : off + len(frag)] = frag
    return bytes(data)


def main() -> None:
    print("charset-normalizer Rust port scale harness")
    print("This is opt-in and deliberately excluded from default test runs.")
    print("Command: CN_SCALE=1 PYTHONPATH=. uv run python examples/charset_normalizer_rust/tools/scale_harness.py")
    print()

    exe = ensure_rust_release()
    print(f"Using Rust release exe: {exe}")

    # (name, generator_callable)
    gens = [
        ("large_py_source_120kLOC", lambda: gen_large_py_source()),
        ("large_rust_source_80kLOC", lambda: gen_large_rust_source()),
        ("large_utf8_prose_1.2M", lambda: gen_large_utf8_prose()),
        ("large_western_sb_900k", lambda: gen_large_western_sb()),
        ("large_cjk_ar_cy_600k", lambda: gen_large_cjk_ar_cy()),
        ("large_htmlxml_decl_450k", lambda: gen_large_html_xml_decl()),
        ("large_binaryish_700k", lambda: gen_large_binaryish()),
    ]

    rows: list[dict] = []
    for name, genf in gens:
        print(f"[{name}] generating...", flush=True)
        payload = genf()
        size = len(payload)
        print(f"  payload_size={size}")

        print("  running Python from_bytes...", flush=True)
        py_enc, py_t = run_python(payload)
        print(f"    py_best={py_enc} py_time={py_t:.4f}s")

        print("  running Rust CLI --minimal (release)...", flush=True)
        rust_enc, rust_t = run_rust(exe, payload)
        print(f"    rust_best={rust_enc} rust_time={rust_t:.4f}s")

        enc_match = py_enc == rust_enc
        # "output matches" is true when best encodings match (user-visible contract);
        # codec decode parity covered by separate test_codec_cd_parity.py
        output_match = enc_match
        rows.append(
            {
                "name": name,
                "size": size,
                "py_enc": py_enc,
                "rust_enc": rust_enc,
                "py_t": py_t,
                "rust_t": rust_t,
                "match": enc_match,
                "out_match": output_match,
            }
        )

    print()
    print("=== SCALE RESULTS ===")
    hdr = f"{'name':28} | {'size':>9} | {'py_enc':13} | {'rust_enc':13} | {'py_t':>8} | {'rust_t':>8} | encs=out?"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        m = "YES" if r["match"] else "NO "
        print(
            f"{r['name']:<28} | {r['size']:>9} | {r['py_enc']:13} | {r['rust_enc']:13} | "
            f"{r['py_t']:8.4f} | {r['rust_t']:8.4f} | {m}"
        )

    total_match = sum(1 for r in rows if r["match"])
    print()
    print(f"Encodings match: {total_match}/{len(rows)}")
    print("Note: times include minor subprocess overhead for Rust (release); Python is direct call.")
    print("No mismatches = strong scale confidence for the ported from_bytes behavior.")
    if total_match != len(rows):
        print("WARNING: some scale mismatches detected.")
    else:
        print("All scale cases matched on best encoding.")


if __name__ == "__main__":
    main()
