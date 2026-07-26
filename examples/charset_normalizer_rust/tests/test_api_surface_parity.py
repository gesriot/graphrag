"""Targeted Python-oracle checks for the cheap API-surface parity additions."""
from __future__ import annotations

import json
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
from charset_normalizer import cd as py_cd  # type: ignore
from charset_normalizer.models import CharsetMatch as PyCharsetMatch  # type: ignore
from charset_normalizer.models import CliDetectionResult as PyCliDetectionResult  # type: ignore
from charset_normalizer import utils as py_utils  # type: ignore


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

    python_cli_result = PyCliDetectionResult(
        "input/é.txt",
        "cp1252",
        ["windows_1252"],
        ["latin_1", "iso8859_15"],
        "French",
        ["Basic Latin", "Latin-1 Supplement"],
        True,
        0.125,
        1.0,
        "output/😀.utf8",
        False,
    )
    assert run_probe(rust_probe, "cli-result-fixture") == python_cli_result.to_json()

    distinct_parent = PyCharsetMatch(b"same text", "utf_8", 0.0, False, [])
    distinct_parent.add_submatch(PyCharsetMatch(b"same text", "ascii", 0.0, False, []))
    assert distinct_parent.has_submatch
    assert run_probe(
        rust_probe, "api-submatch", "utf_8", b"same text".hex(), "ascii", b"same text".hex()
    ) == "OK\t1"

    same_parent = PyCharsetMatch(b"same text", "utf_8", 0.0, False, [])
    with pytest.raises(ValueError):
        same_parent.add_submatch(PyCharsetMatch(b"same text", "utf_8", 0.0, False, []))
    assert run_probe(
        rust_probe, "api-submatch", "utf_8", b"same text".hex(), "utf_8", b"same text".hex()
    ) == "ERR\tSameMatch"

    output_payload = "€ and 漢".encode("utf_8")
    output_match = PyCharsetMatch(output_payload, "utf_8", 0.0, False, [])
    for target in ("ascii", "cp1252", "shift_jis", "johab", "iso2022_kr"):
        python_output = output_match.output(target)
        rust_output = run_probe(rust_probe, "api-output", "utf_8", output_payload.hex(), target)
        assert rust_output == f"OK:{python_output.hex()}", f"output mismatch for {target}"
    assert run_probe(rust_probe, "api-output-default", "utf_8", output_payload.hex()) == (
        f"OK:{output_match.output().hex()}"
    )

    char_helpers = (
        py_utils.is_accentuated,
        py_utils.is_latin,
        py_utils.is_punctuation,
        py_utils.is_symbol,
        py_utils.is_emoticon,
        py_utils.is_separator,
        py_utils.is_case_variable,
        py_utils.is_cjk,
        py_utils.is_hiragana,
        py_utils.is_katakana,
        py_utils.is_hangul,
        py_utils.is_thai,
        py_utils.is_arabic,
        py_utils.is_arabic_isolated_form,
        py_utils.is_cjk_uncommon,
        py_utils.is_unprintable,
    )
    for character in ("A", "é", "—", "€", "😀", " ", "+", "一", "あ", "ア", "한", "ก", "ا", "\x00", "\x1a", "\ufeff"):
        python_values = [
            py_utils.unicode_range(character) or "-",
            *(str(int(helper(character))) for helper in char_helpers),
            f"{ord(py_utils.remove_accent(character)):x}",
        ]
        rust_values = run_probe(rust_probe, "helper-char", f"{ord(character):x}").split("\t")
        assert rust_values == python_values, f"helper-char mismatch for U+{ord(character):04X}"
    with pytest.raises(ValueError):
        py_utils.remove_accent("\ufefb")
    assert run_probe(rust_probe, "helper-char", "fefb").split("\t")[-1] == "ERR"

    for name, strict in (("windows-1252", True), ("unknown_codec", False), ("unknown_codec", True)):
        try:
            python_iana = f"OK:{py_utils.iana_name(name, strict)}"
        except ValueError:
            python_iana = "ERR"
        assert run_probe(rust_probe, "helper-iana", name, str(int(strict))) == python_iana

    for name in ("utf_8", "cp1252", "shift_jis"):
        assert run_probe(rust_probe, "helper-multibyte", name) == str(
            int(py_utils.is_multi_byte_encoding(name))
        )

    for payload in (b"plain", b"\xef\xbb\xbftext", b"\xff\xfe\x00\x00A\x00\x00\x00"):
        py_encoding, py_mark = py_utils.identify_sig_or_bom(payload)
        assert run_probe(rust_probe, "helper-bom", payload.hex()).split("\t") == [
            py_encoding or "-",
            py_mark.hex() or "-",
        ]
    for encoding in ("utf_8", "utf_16", "utf_32"):
        assert run_probe(rust_probe, "helper-strip", encoding) == str(
            int(py_utils.should_strip_sig_or_bom(encoding))
        )
    for unicode_range in ("Cyrillic", "General Punctuation"):
        assert run_probe(rust_probe, "helper-secondary", unicode_range) == str(
            int(py_utils.is_unicode_range_secondary(unicode_range))
        )

    for left, right in (("cp1252", "latin_1"), ("cp1251", "cp1251"), ("utf_8", "cp1252")):
        rust_similarity, rust_similar = run_probe(rust_probe, "helper-cp", left, right).split("\t")
        assert float(rust_similarity) == pytest.approx(py_utils.cp_similarity(left, right))
        assert rust_similar == str(int(py_utils.is_cp_similar(left, right)))

    declared = b"<meta charset=windows-1252>"
    for search_zone in (8, len(declared)):
        assert run_probe(rust_probe, "helper-specified", declared.hex(), str(search_zone)) == (
            py_utils.any_specified_encoding(declared, search_zone) or "-"
        )

    chunk_payload = b"abcdef"
    python_chunks = list(
        py_utils.cut_sequence_chunks(
            chunk_payload,
            "ascii",
            range(0, 4),
            2,
            False,
            True,
            b"",
            False,
            "abcdef",
        )
    )
    assert run_probe(
        rust_probe,
        "helper-chunks",
        "ascii",
        chunk_payload.hex(),
        "0",
        "4",
        "2",
        "0",
        b"abcdef".hex(),
    ) == "|".join(chunk.encode("utf_8").hex() for chunk in python_chunks)

    for encoding in ("cp1251", "cp1252"):
        assert run_probe(rust_probe, "cd-encoding-range", encoding) == json.dumps(
            py_cd.encoding_unicode_range(encoding), separators=(",", ":")
        )
    for unicode_range in ("Cyrillic", "Basic Latin"):
        assert run_probe(rust_probe, "cd-range-languages", unicode_range) == json.dumps(
            py_cd.unicode_range_languages(unicode_range), separators=(",", ":")
        )
    assert run_probe(rust_probe, "cd-target-features", "French") == "\t".join(
        str(int(value)) for value in py_cd.get_target_features("French")
    )
    python_filtered = py_cd.filter_alt_coherence_matches(
        [("English", 0.2), ("English—", 0.8), ("French", 0.5)]
    )
    assert run_probe(rust_probe, "cd-filter-alt") == "|".join(
        f"{language}:{score:.17f}" for language, score in python_filtered
    )
