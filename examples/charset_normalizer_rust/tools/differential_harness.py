#!/usr/bin/env python3
"""Seeded Python-oracle differential harness for the charset-normalizer port.

This is deliberately a differential test, not a new golden file: every run
generates its byte corpus from a fixed seed, executes the vendored Python
reference and the Rust helper, and compares their live observations.

Agreement policy
----------------
For a *confident* Python result (at least 32 bytes, chaos <= 5%, and one
de-duplicated candidate), the public result must agree on canonical encoding,
decoded Unicode text, language, BOM status, and both scores within 0.5
percentage points. A label alone is not enough: two codecs can share a label
alias while decoding different text.

For tiny, noisy, or tied inputs, the same fields are still compared and printed,
but a divergence is classified as an ambiguous finding rather than silently
treated as success.  This keeps genuine heuristic uncertainty visible while
making a confident Python-oracle mismatch fail ``--assert-clean``.

The default corpus is intentionally bounded for the regular handoff check.
``--full`` adds every one-byte payload and a larger seeded mutation sweep for
an on-demand run.
"""

from __future__ import annotations

import argparse
import codecs
import hashlib
import random
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
RUST_MANIFEST = REPO / "examples" / "charset_normalizer_rust" / "Cargo.toml"
RUST_PROBE = (
    REPO / "examples" / "charset_normalizer_rust" / "target" / "debug" / "parity_probe"
)

sys.path.insert(0, str(REPO / "examples"))

from charset_normalizer import from_bytes  # noqa: E402
from charset_normalizer.constant import TOO_SMALL_SEQUENCE  # noqa: E402
from charset_normalizer.utils import iana_name  # noqa: E402


DEFAULT_SEED = 20_260_725
SCORE_TOLERANCE = 0.005


@dataclass(frozen=True)
class Case:
    name: str
    family: str
    source_encoding: str
    payload: bytes


@dataclass(frozen=True)
class Observation:
    encoding: str | None
    decoded: str | None
    language: str | None
    chaos: float | None
    coherence: float | None
    bom: bool | None
    candidate_encodings: tuple[str, ...]


@dataclass(frozen=True)
class Finding:
    case: Case
    classification: str
    diagnosis: str
    python: Observation
    rust: Observation


def canonical_encoding(name: str | None) -> str | None:
    if name is None:
        return None
    try:
        return iana_name(name, False)
    except (LookupError, ValueError):
        return name.lower().replace("-", "_")


def _encoded_cases() -> list[Case]:
    """Real prose in detector-relevant encodings, including both UTF-16 orders."""
    texts = [
        ("utf8", "utf_8", "Café naïve — русский текст — 日本語テスト。 "),
        ("latin1", "latin_1", "Molière déjà façade Noël élève. "),
        ("cp1251", "cp1251", "Привет мир. Это проверка кодировки. "),
        ("shift_jis", "shift_jis", "日本語の文章です。文字コードを確認します。 "),
        ("gb2312", "gb2312", "中文编码检测测试。简体中文内容。 "),
        ("utf16le", "utf_16_le", "UTF sixteen little endian: café русский 中文. "),
        ("utf16be", "utf_16_be", "UTF sixteen big endian: café русский 中文. "),
    ]
    cases: list[Case] = []
    for name, encoding, text in texts:
        payload = (text * 18).encode(encoding)
        cases.append(Case(f"real_{name}", "real-text", encoding, payload))
        if name not in {"utf16le", "utf16be"}:
            cases.append(
                Case(f"trunc_{name}_one", "truncated-multibyte", encoding, payload[:-1])
            )

    utf8 = ("UTF-8 BOM survives while the prose stays valid. Café 日本語. " * 14).encode(
        "utf_8"
    )
    cases.append(Case("real_utf8_bom", "real-text", "utf_8_sig", codecs.BOM_UTF8 + utf8))
    cases.append(
        Case(
            "real_utf16le_bom",
            "real-text",
            "utf_16_le_bom",
            codecs.BOM_UTF16_LE + ("BOM UTF-16 LE. Привет. " * 16).encode("utf_16_le"),
        )
    )
    cases.append(
        Case(
            "real_utf16be_bom",
            "real-text",
            "utf_16_be_bom",
            codecs.BOM_UTF16_BE + ("BOM UTF-16 BE. 中文。 " * 16).encode("utf_16_be"),
        )
    )
    return cases


def _edge_cases() -> list[Case]:
    """Input shapes where a detector should expose uncertainty, not hide it."""
    cases = [
        Case("empty", "empty", "none", b""),
        Case("single_nul", "single-byte", "raw", b"\x00"),
        Case("single_ascii", "single-byte", "raw", b"A"),
        Case("single_high", "single-byte", "raw", b"\xff"),
        Case("single_lead", "single-byte", "raw", b"\xe3"),
        Case("tiny_utf8_lead", "very-short", "raw", b"\xe3\x81"),
        Case("tiny_utf16le", "very-short", "raw", b"A\x00B\x00C"),
        Case("tiny_ascii", "very-short", "ascii", b"short text"),
        Case("multi_valid_ascii", "multi-valid", "ascii", b"plain ASCII is valid in many codecs. " * 3),
        Case(
            "multi_valid_western",
            "multi-valid",
            "latin_1/cp1252",
            b"Moli\xe8re d\xe9j\xe0 fa\xe7ade No\xebl. " * 6,
        ),
        Case("invalid_utf8_runs", "mixed-invalid", "raw", (b"text\xff\x80\xc0\xaf" * 18)),
        Case(
            "mixed_text_binary",
            "mixed-invalid",
            "raw",
            (b"readable text" + bytes([0, 255, 128, 1, 31]) + b"more text ") * 14,
        ),
        Case(
            "trunc_utf8_tail",
            "truncated-multibyte",
            "utf_8",
            "valid 中文 tail".encode("utf_8") + b"\xe4\xb8",
        ),
        Case("trunc_shift_jis", "truncated-multibyte", "shift_jis", b"\x93\xfa\x96\x7b\x8c"),
        Case("trunc_gb2312", "truncated-multibyte", "gb2312", b"\xd6\xd0\xce\xc4\xb1"),
        Case(
            "bom_invalid_continuations",
            "mixed-invalid",
            "raw",
            codecs.BOM_UTF8 + b"\x80\x81\x82text\xff",
        ),
    ]
    return cases


def _seeded_cases(seed: int, full: bool) -> list[Case]:
    """Byte mutations with a local PRNG: reproducible without recorded answers."""
    rng = random.Random(seed)
    cases: list[Case] = []
    base = (
        "Seeded text: café déjà. Привет мир. 日本語のテスト。 中文测试。 " * 6
    ).encode("utf_8")
    count = 48 if not full else 240
    for index in range(count):
        payload = bytearray(base[: 40 + (index * 37) % len(base)])
        mutations = 1 + index % 7
        for _ in range(mutations):
            position = rng.randrange(len(payload))
            payload[position] = rng.randrange(256)
        if index % 5 == 0:
            payload.extend(bytes(rng.randrange(0x80, 256) for _ in range(1 + index % 4)))
        cases.append(
            Case(
                f"seeded_mutation_{index:03d}",
                "seeded-mutation",
                "utf_8+invalid",
                bytes(payload),
            )
        )
    if full:
        for value in range(256):
            cases.append(
                Case(f"every_single_byte_{value:02x}", "single-byte-full", "raw", bytes([value]))
            )
    return cases


def _long_cases() -> list[Case]:
    return [
        Case(
            "long_utf8_mixed",
            "long",
            "utf_8",
            ("Long UTF-8 prose: café Привет 日本語 中文. " * 2_000).encode("utf_8"),
        ),
        Case(
            "long_cp1251",
            "long",
            "cp1251",
            ("Длинный русский текст для проверки. " * 4_000).encode("cp1251"),
        ),
        Case(
            "long_multi_valid_ascii",
            "long",
            "ascii",
            b"Long ASCII valid under many single-byte encodings.\n" * 8_000,
        ),
    ]


def build_corpus(seed: int, full: bool) -> list[Case]:
    cases = _encoded_cases() + _edge_cases() + _seeded_cases(seed, full)
    if full:
        cases.extend(_long_cases())
    return cases


def python_observation(payload: bytes) -> Observation:
    matches = from_bytes(payload)
    best = matches.best()
    candidates = tuple(canonical_encoding(match.encoding) or match.encoding for match in matches)
    if best is None:
        return Observation(None, None, None, None, None, None, candidates)
    return Observation(
        canonical_encoding(best.encoding),
        str(best),
        best.language,
        best.chaos,
        best.coherence,
        best.bom,
        candidates,
    )


def build_probe() -> Path:
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
    if not RUST_PROBE.exists():
        raise RuntimeError(f"differential probe was not built: {RUST_PROBE}")
    return RUST_PROBE


def _from_hex(value: str) -> str | None:
    if value == "-":
        return None
    try:
        return bytes.fromhex(value).decode("utf_8", "strict")
    except UnicodeDecodeError as error:
        raise RuntimeError(f"Rust probe emitted invalid UTF-8 hex: {error}") from error


def rust_observation(probe: Path, payload_path: Path) -> Observation:
    proc = subprocess.run(
        [str(probe), "detect-file", str(payload_path)],
        cwd=REPO,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    fields = proc.stdout.rstrip("\n").split("\t")
    if not fields:
        raise RuntimeError("Rust probe emitted no differential observation")
    if fields[0] == "NONE":
        if len(fields) != 3:
            raise RuntimeError(f"malformed no-match observation: {proc.stdout!r}")
        candidates = tuple(filter(None, fields[2].split("|")))
        return Observation(None, None, None, None, None, None, candidates)
    if fields[0] != "BEST" or len(fields) != 9:
        raise RuntimeError(f"malformed match observation: {proc.stdout!r}")
    return Observation(
        canonical_encoding(fields[1]),
        _from_hex(fields[8]),
        fields[2],
        float(fields[3]),
        float(fields[4]),
        fields[5] == "1",
        tuple(filter(None, (canonical_encoding(item) or item for item in fields[7].split("|")))),
    )


def python_is_confident(case: Case, observation: Observation) -> bool:
    if len(case.payload) < TOO_SMALL_SEQUENCE or observation.encoding is None:
        return False
    if observation.chaos is None or observation.chaos > 0.05:
        return False
    # CharsetMatches has already de-duplicated codecs that decode to the same
    # payload. A remaining single candidate is a decisive oracle choice.
    return len(observation.candidate_encodings) == 1


def classify(case: Case, python: Observation, rust: Observation) -> Finding | None:
    confident = python_is_confident(case, python)
    if python.encoding is None and rust.encoding is None:
        return None

    same_fields = (
        python.encoding == rust.encoding
        and python.decoded == rust.decoded
        and python.language == rust.language
        and python.bom == rust.bom
    )
    same_scores = (
        python.chaos is not None
        and rust.chaos is not None
        and python.coherence is not None
        and rust.coherence is not None
        and abs(python.chaos - rust.chaos) <= SCORE_TOLERANCE
        and abs(python.coherence - rust.coherence) <= SCORE_TOLERANCE
    )
    if same_fields and same_scores:
        return None

    differences: list[str] = []
    if python.encoding != rust.encoding:
        differences.append("selected encoding")
    if python.decoded != rust.decoded:
        differences.append("decoded Unicode")
    if python.language != rust.language:
        differences.append("language")
    if python.bom != rust.bom:
        differences.append("BOM flag")
    if not same_scores:
        differences.append("score")
    scope = "confident" if confident else "ambiguous"
    classification = f"{scope}-divergence"
    diagnosis = (
        f"{scope} Python-oracle result differs in {', '.join(differences)}; "
        f"payload is {len(case.payload)} bytes, family={case.family}, source={case.source_encoding}."
    )
    return Finding(case, classification, diagnosis, python, rust)


def _fingerprint(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()[:16]


def _decoded_summary(value: str | None) -> str:
    if value is None:
        return "none"
    return f"len={len(value)} sha256={_fingerprint(value.encode('utf_8'))}"


def run(seed: int, full: bool, probe: Path | None = None) -> tuple[list[Case], list[Finding]]:
    probe = probe or build_probe()
    cases = build_corpus(seed, full)
    findings: list[Finding] = []
    with tempfile.TemporaryDirectory(prefix="charset-normalizer-diff-") as temp_dir:
        directory = Path(temp_dir)
        for index, case in enumerate(cases):
            payload_path = directory / f"{index:04d}-{case.name}.bin"
            payload_path.write_bytes(case.payload)
            finding = classify(
                case,
                python_observation(case.payload),
                rust_observation(probe, payload_path),
            )
            if finding is not None:
                findings.append(finding)
    return cases, findings


def render_report(cases: list[Case], findings: list[Finding], seed: int, full: bool) -> str:
    families = Counter(case.family for case in cases)
    encodings = sorted({case.source_encoding for case in cases})
    by_classification = Counter(finding.classification for finding in findings)
    lines = [
        "charset-normalizer seeded differential report",
        f"mode={'full' if full else 'default'} seed={seed}",
        f"inputs={len(cases)} source_encodings={len(encodings)} ({', '.join(encodings)})",
        "families=" + ", ".join(f"{name}:{count}" for name, count in sorted(families.items())),
        f"agreements={len(cases) - len(findings)} disagreements={len(findings)}",
    ]
    if by_classification:
        lines.append(
            "disagreement_classes="
            + ", ".join(f"{name}:{count}" for name, count in sorted(by_classification.items()))
        )
    for finding in findings:
        lines.extend(
            [
                f"DISAGREEMENT {finding.case.name}: {finding.diagnosis}",
                f"  payload=sha256:{_fingerprint(finding.case.payload)}",
                "  python="
                f"encoding={finding.python.encoding!r} chaos={finding.python.chaos!r} "
                f"coherence={finding.python.coherence!r} language={finding.python.language!r} "
                f"bom={finding.python.bom!r} decoded={_decoded_summary(finding.python.decoded)} "
                f"candidates={finding.python.candidate_encodings!r}",
                "  rust="
                f"encoding={finding.rust.encoding!r} chaos={finding.rust.chaos!r} "
                f"coherence={finding.rust.coherence!r} language={finding.rust.language!r} "
                f"bom={finding.rust.bom!r} decoded={_decoded_summary(finding.rust.decoded)} "
                f"candidates={finding.rust.candidate_encodings!r}",
            ]
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--full", action="store_true", help="include the long and every-byte sweeps")
    parser.add_argument(
        "--assert-clean",
        action="store_true",
        help="fail only for confident Python-oracle divergences",
    )
    args = parser.parse_args()
    cases, findings = run(args.seed, args.full)
    print(render_report(cases, findings, args.seed, args.full))
    hard_findings = [
        finding for finding in findings if finding.classification == "confident-divergence"
    ]
    if args.assert_clean and hard_findings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
