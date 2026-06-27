#!/usr/bin/env python3
"""
Regenerate Rust codec tables from the local Python stdlib codecs.

Run (from examples/charset_normalizer_rust/):
    python3 tools/generate_codecs.py && cargo fmt

Outputs:
    src/python_codecs.rs  (single-byte + utf32/utf7/hz)
    src/korean_codecs.rs  (johab + iso2022_kr)

See also: README.md (regeneration section) and PORT_STATUS.md.
"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
INVALID = "INVALID"

SINGLE_BYTE_ENCODINGS = [
    "ascii",
    "cp037",
    "cp1006",
    "cp1026",
    "cp1125",
    "cp1140",
    "cp1250",
    "cp1251",
    "cp1252",
    "cp1253",
    "cp1254",
    "cp1255",
    "cp1256",
    "cp1257",
    "cp1258",
    "cp273",
    "cp424",
    "cp437",
    "cp500",
    "cp720",
    "cp737",
    "cp775",
    "cp850",
    "cp852",
    "cp855",
    "cp856",
    "cp857",
    "cp858",
    "cp860",
    "cp861",
    "cp862",
    "cp863",
    "cp864",
    "cp865",
    "cp866",
    "cp869",
    "cp874",
    "cp875",
    "hp_roman8",
    "iso8859_10",
    "iso8859_11",
    "iso8859_13",
    "iso8859_14",
    "iso8859_15",
    "iso8859_16",
    "iso8859_2",
    "iso8859_3",
    "iso8859_4",
    "iso8859_5",
    "iso8859_6",
    "iso8859_7",
    "iso8859_8",
    "iso8859_9",
    "koi8_r",
    "koi8_t",
    "koi8_u",
    "kz1048",
    "latin_1",
    "mac_cyrillic",
    "mac_greek",
    "mac_iceland",
    "mac_latin2",
    "mac_roman",
    "mac_turkish",
    "ptcp154",
    "tis_620",
]


def const_name(encoding: str) -> str:
    return encoding.upper().replace("-", "_")


def render_python_codecs() -> str:
    lines = [
        "// Generated from Python stdlib codecs for charset-normalizer IANA single-byte encodings.",
        "// INVALID marks byte values rejected by Python strict decoding.",
        "",
        "use encoding::{DecoderTrap, EncoderTrap, Encoding as LegacyEncoding};",
        "",
        "const INVALID: u32 = 0xFFFF_FFFF;",
        "",
    ]

    for encoding in SINGLE_BYTE_ENCODINGS:
        values = []
        for byte in range(256):
            try:
                decoded = bytes([byte]).decode(encoding)
            except UnicodeDecodeError:
                values.append(INVALID)
            else:
                assert len(decoded) == 1, (encoding, byte, decoded)
                values.append(f"0x{ord(decoded):X}")

        lines.append(f"const {const_name(encoding)}: [u32; 256] = [")
        for index in range(0, 256, 8):
            lines.append("    " + ", ".join(values[index : index + 8]) + ",")
        lines.extend(["];", ""])

    lines.extend(
        [
            "fn table(name: &str) -> Option<&'static [u32; 256]> {",
            "    match name {",
        ]
    )
    for encoding in SINGLE_BYTE_ENCODINGS:
        lines.append(f'        "{encoding}" => Some(&{const_name(encoding)}),')
    lines.extend(
        [
            "        _ => None,",
            "    }",
            "}",
            "",
        ]
    )

    existing = (SRC / "python_codecs.rs").read_text()
    helper_start = existing.index("pub(crate) fn is_charmap_encoding")
    lines.append(existing[helper_start:].rstrip())
    return "\n".join(lines) + "\n"


def collect_johab_pairs() -> list[tuple[int, int]]:
    pairs = []
    for b1 in range(256):
        for b2 in range(256):
            try:
                decoded = bytes([b1, b2]).decode("johab")
            except UnicodeDecodeError:
                continue
            if b1 >= 0x80 and len(decoded) == 1:
                pairs.append(((b1 << 8) | b2, ord(decoded)))
    return pairs


def collect_iso2022_kr_pairs() -> list[tuple[int, int]]:
    pairs = []
    for b1 in range(0x21, 0x7F):
        for b2 in range(0x21, 0x7F):
            payload = b"\x1b$)C\x0e" + bytes([b1, b2]) + b"\x0f"
            try:
                decoded = payload.decode("iso2022_kr")
            except UnicodeDecodeError:
                continue
            if len(decoded) == 1:
                pairs.append(((b1 << 8) | b2, ord(decoded)))
    return pairs


def render_pair_table(name: str, pairs: list[tuple[int, int]]) -> list[str]:
    lines = [f"const {name}: &[(u16, u32)] = &["]
    for index in range(0, len(pairs), 4):
        row = pairs[index : index + 4]
        lines.append(
            "    "
            + ", ".join(f"(0x{key:04X}, 0x{value:X})" for key, value in row)
            + ","
        )
    lines.extend(["];", ""])
    return lines


def render_korean_codecs() -> str:
    lines = [
        "// Generated from Python stdlib johab and iso2022_kr codecs.",
        "// Tables are sorted by encoded pair for binary-search decoding.",
        "",
        'const ISO2022_KR_DESIGNATOR: &[u8; 4] = b"\\x1b$)C";',
        "",
    ]
    lines.extend(render_pair_table("JOHAB_PAIRS", collect_johab_pairs()))
    lines.extend(render_pair_table("ISO2022_KR_PAIRS", collect_iso2022_kr_pairs()))

    existing = (SRC / "korean_codecs.rs").read_text()
    helper_start = existing.index("pub(crate) fn decode_johab_strict")
    lines.append(existing[helper_start:].rstrip())
    return "\n".join(lines) + "\n"


def main() -> None:
    (SRC / "python_codecs.rs").write_text(render_python_codecs())
    (SRC / "korean_codecs.rs").write_text(render_korean_codecs())


if __name__ == "__main__":
    main()
