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

    lines.extend(render_hz_tables())
    lines.extend(render_euc_jis_tables())

    existing = (SRC / "python_codecs.rs").read_text()
    helper_start = existing.index("pub(crate) fn is_charmap_encoding")
    lines.append(existing[helper_start:].rstrip())
    return "\n".join(lines) + "\n"


def collect_hz_pairs() -> list[tuple[int, int]]:
    """Return Python's complete HZ shifted-pair map, keyed by encoded bytes."""
    pairs: list[tuple[int, int]] = []
    for lead in range(0x21, 0x7F):
        for trail in range(0x21, 0x7F):
            pair = bytes((lead, trail))
            try:
                decoded = (b"~{" + pair + b"~}").decode("hz", "strict")
            except UnicodeDecodeError:
                continue
            if len(decoded) != 1:
                raise RuntimeError(f"HZ pair {pair.hex()} decoded to {decoded!r}")
            encoded = decoded.encode("hz", "strict")
            expected = b"~{" + pair + b"~}"
            if encoded != expected:
                raise RuntimeError(
                    f"HZ pair {pair.hex()} is not Python's canonical encoding for {decoded!r}: "
                    f"{encoded.hex()} != {expected.hex()}"
                )
            pairs.append(((lead << 8) | trail, ord(decoded)))
    return pairs


def render_hz_tables() -> list[str]:
    """Render exact HZ/GB2312 tables from the running Python codec oracle."""
    decode_pairs = collect_hz_pairs()
    encode_pairs = sorted((scalar, pair) for pair, scalar in decode_pairs)
    if len({scalar for _, scalar in decode_pairs}) != len(decode_pairs):
        raise RuntimeError("Python HZ shifted-pair table is not one-to-one")

    lines = [
        "// Generated from Python stdlib's strict `hz` codec shifted-pair table.",
        "// The legacy `encoding` crate HZ table is GBK-like; this exact GB2312 map",
        "// keeps strict decoding and CharsetMatch.output() aligned with Python.",
        "",
        "const HZ_DECODE_PAIRS: &[(u16, u32)] = &[",
    ]
    for index in range(0, len(decode_pairs), 4):
        row = decode_pairs[index : index + 4]
        lines.append(
            "    "
            + ", ".join(f"(0x{pair:04X}, 0x{scalar:X})" for pair, scalar in row)
            + ","
        )
    lines.extend(["];", "", "const HZ_ENCODE_PAIRS: &[(u32, u16)] = &["])
    for index in range(0, len(encode_pairs), 4):
        row = encode_pairs[index : index + 4]
        lines.append(
            "    "
            + ", ".join(f"(0x{scalar:X}, 0x{pair:04X})" for scalar, pair in row)
            + ","
        )
    lines.extend(["];", ""])
    return lines


def euc_jis_2004_payloads() -> list[bytes]:
    """Return every non-ASCII byte form accepted by Python's EUC-JIS-2004 codec."""
    payloads = [
        bytes((lead, trail))
        for lead in range(0xA1, 0xFF)
        for trail in range(0xA1, 0xFF)
    ]
    payloads.extend(bytes((0x8E, trail)) for trail in range(0xA1, 0xE0))
    payloads.extend(
        bytes((0x8F, lead, trail))
        for lead in range(0xA1, 0xFF)
        for trail in range(0xA1, 0xFF)
    )
    return payloads


def pack_bytes(payload: bytes) -> int:
    value = 0
    for byte in payload:
        value = (value << 8) | byte
    return value


def rust_string(value: str) -> str:
    return '"' + "".join(f"\\u{{{ord(character):X}}}" for character in value) + '"'


def collect_euc_jis_decode(encoding: str) -> list[tuple[int, str]]:
    decoded: list[tuple[int, str]] = []
    for payload in euc_jis_2004_payloads():
        try:
            text = payload.decode(encoding, "strict")
        except UnicodeDecodeError:
            continue
        decoded.append((pack_bytes(payload), text))
    return sorted(decoded)


def collect_euc_jis_encode(encoding: str) -> list[tuple[int, int, int]]:
    encoded: list[tuple[int, int, int]] = []
    for scalar in range(0x80, 0x110000):
        if 0xD800 <= scalar <= 0xDFFF:
            continue
        try:
            payload = chr(scalar).encode(encoding, "strict")
        except UnicodeEncodeError:
            continue
        encoded.append((scalar, pack_bytes(payload), len(payload)))
    return encoded


def render_euc_jis_table(encoding: str, const_prefix: str) -> list[str]:
    """Render one Python EUC-JIS-X-0213 codec map, including sequences."""
    decoded = collect_euc_jis_decode(encoding)
    encoded = collect_euc_jis_encode(encoding)
    sequence_encodes: list[tuple[str, int, int]] = []
    for packed, text in decoded:
        if len(text) <= 1:
            continue
        payload = text.encode(encoding, "strict")
        expected_length = 3 if packed > 0xFFFF else 2
        if (pack_bytes(payload), len(payload)) != (packed, expected_length):
            raise RuntimeError(
                f"{encoding} sequence {text!r} does not encode canonically to {packed:X}"
            )
        sequence_encodes.append((text, packed, expected_length))

    if len({scalar for scalar, _, _ in encoded}) != len(encoded):
        raise RuntimeError(f"Python {encoding} scalar encoder is not one-to-one")

    lines = [
        f"// Generated from Python stdlib's strict `{encoding}` codec.",
        "// encoding_rs has an EUC-JP profile, not this JIS X 0213 mapping.",
        "",
        f"const {const_prefix}_DECODE: &[(u32, &str)] = &[",
    ]
    for index in range(0, len(decoded), 2):
        row = decoded[index : index + 2]
        lines.append(
            "    "
            + ", ".join(f"(0x{packed:X}, {rust_string(text)})" for packed, text in row)
            + ","
        )
    lines.extend(["];", "", f"const {const_prefix}_ENCODE: &[(u32, u32, u8)] = &["])
    for index in range(0, len(encoded), 4):
        row = encoded[index : index + 4]
        lines.append(
            "    "
            + ", ".join(
                f"(0x{scalar:X}, 0x{packed:X}, {length})"
                for scalar, packed, length in row
            )
            + ","
        )
    lines.extend(["];", "", f"const {const_prefix}_SEQUENCE_ENCODE: &[(&str, u32, u8)] = &["])
    for text, packed, length in sequence_encodes:
        lines.append(f"    ({rust_string(text)}, 0x{packed:X}, {length}),")
    lines.extend(["];", ""])
    return lines


def render_euc_jis_tables() -> list[str]:
    """Render both distinct Python codecs named by currently supported aliases."""
    lines = render_euc_jis_table("euc_jis_2004", "EUC_JIS_2004")
    lines.extend(render_euc_jis_table("euc_jisx0213", "EUC_JISX0213"))
    lines.extend(render_euc_jis_table("euc_jp", "EUC_JP"))
    return lines


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
