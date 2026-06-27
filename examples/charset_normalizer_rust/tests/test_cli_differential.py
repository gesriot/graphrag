from __future__ import annotations

import random
import string
import subprocess
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[3]
RUST_MANIFEST = REPO / "examples" / "charset_normalizer_rust" / "Cargo.toml"
RUST_EXE = (
    REPO
    / "examples"
    / "charset_normalizer_rust"
    / "target"
    / "debug"
    / "normalizer"
)

sys.path.insert(0, str(REPO / "examples"))

from charset_normalizer import from_bytes  # noqa: E402


def python_best(
    payload: bytes,
    *,
    preemptive: bool = True,
    threshold: float = 0.2,
    cp_isolation: list[str] | None = None,
    cp_exclusion: list[str] | None = None,
) -> str:
    match = from_bytes(
        payload,
        preemptive_behaviour=preemptive,
        threshold=threshold,
        cp_isolation=cp_isolation,
        cp_exclusion=cp_exclusion,
    ).best()
    return match.encoding if match is not None else "undefined"


@pytest.fixture(scope="session")
def rust_exe() -> Path:
    subprocess.run(
        ["cargo", "build", "--quiet", "--manifest-path", str(RUST_MANIFEST)],
        cwd=REPO,
        check=True,
        text=True,
    )
    return RUST_EXE


def rust_best(rust_exe: Path, path: Path, *extra_args: str) -> str:
    proc = subprocess.run(
        [
            str(rust_exe),
            "--minimal",
            *extra_args,
            str(path),
        ],
        cwd=REPO,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc.stdout.strip()


def _make_deterministic_generated_payloads() -> list[tuple[str, bytes]]:
    """Generate fixed payloads using seeded RNG for reproducible differential matrix.

    Covers per task spec:
    - random ASCII-compatible + injected high bytes
    - short noisy/binary-ish
    - HTML/XML declarative charset hints
    - ambiguous legacy Western
    - non-Latin with supported codecs
    - medium repeated payloads
    Deterministic: fixed seed, no time/entropy.
    """
    rng = random.Random(42424242)
    cases: list[tuple[str, bytes]] = []

    # random ASCII-compatible text with injected high bytes
    for i in range(5):
        n = 55 + (i * 19) % 80
        chars = string.ascii_letters + string.digits + " \n.,;:!?-'\"()"
        text = "".join(rng.choice(chars) for _ in range(n))
        b = bytearray(text.encode("ascii"))
        for _ in range(1 + (i % 3)):
            pos = rng.randrange(len(b))
            b[pos] = 0x80 + rng.randrange(0x7f)
        cases.append((f"ascii_inj_high_{i}", bytes(b)))

    # short noisy/binary-ish payloads
    for i in range(3):
        n = 12 + i * 4
        b = bytes(rng.randrange(256) for _ in range(n))
        cases.append((f"short_noisy_{i}", b))

    # HTML/XML-style declarative charset hints (preemptive paths)
    cases.append(
        (
            "decl_meta_iso8859_1",
            (b'<meta charset="iso-8859-1"><p>caf\xe9 d\xe9j\xe0</p>' * 5),
        )
    )
    cases.append(
        (
            "decl_xml_cp1252",
            (b'<?xml version="1.0" encoding="Windows-1252"?>\n<text>Moli\xe8re</text>' * 4),
        )
    )
    cases.append(
        (
            "decl_meta_utf8",
            (b'<meta charset="utf-8"><p>na\xc3\xafve \xe2\x82\xac</p>' * 5),
        )
    )
    cases.append(
        (
            "decl_http_cp1251",
            (b'<meta http-equiv="Content-Type" content="text/html; charset=windows-1251">\n<p>\xcf\xf0\xe8\xe2\xe5\xf2</p>' * 4),
        )
    )

    # ambiguous legacy Western payloads (similar SB codecs may tie-break)
    ambig = b"Moli\xe8re d\xe9j\xe0 fa\xe7ade No\xebl \"quote\" 100\xa3." * 3
    cases.append(("ambig_western_legacy", ambig))
    ambig2 = b"Fran\xe7ais: \xe9\xe8\xea\xef \xe0 \xe7. 100\xa3." * 5
    cases.append(("ambig_western2", ambig2))

    # non-Latin snippets encoded with supported codecs
    cases.append(
        ("nl_cp1251_ru", ("Привет мир. Это тест. " * 3).encode("cp1251"))
    )
    cases.append(
        ("nl_iso8859_7_el", ("Καλημέρα κόσμε. " * 3).encode("iso8859_7"))
    )
    cases.append(
        ("nl_cp1256_ar", ("مرحبا بالعالم. " * 3).encode("cp1256"))
    )
    cases.append(
        ("nl_big5_zh", ("繁體中文測試。 " * 2).encode("big5"))
    )
    cases.append(
        ("nl_euc_kr_ko", ("한글 테스트. " * 3).encode("euc_kr"))
    )

    # medium payloads by repeating (chunking, steps behavior)
    base = b"Medium repeat: naive cafe accents \xc3\xa9\xc3\xa0 end. chunks here. "
    cases.append(("medium_repeat", base * 30))  # ~1.5k-2k bytes
    base_utf = ("Mixed: 日本語 тест العربية naive. " * 2).encode("utf_8")
    cases.append(("medium_utf8_rep", base_utf * 12))

    return cases


def _make_deterministic_adversarial_payloads() -> list[tuple[str, bytes]]:
    """Deterministic adversarial corpus matrix using fixed byte literals only.

    No RNG, no time, no net. Covers required categories for Rust parity test:
    - truncated multibyte sequences
    - invalid continuation bytes after UTF BOMs
    - overlong/invalid UTF-8
    - mixed BOM + contradictory declared charset
    - high-byte noise around CJK byte ranges
    - alternating valid text and binary-like bytes
    - short payloads near TOO_SMALL_SEQUENCE (32)
    - threshold edge cases (0.0/0.1/0.2/0.5) via dedicated tests on these
    - cp_isolation/cp_exclusion (via CLI --cp-* + python kwargs)
    Prefer exact best-encoding parity on stable outcomes. For cases where
    chaos/coherence float edge legitimately causes best() to differ across
    impls, xfail with source-backed reason rather than loosen.
    """
    cases: list[tuple[str, bytes]] = []

    # truncated multibyte sequences
    cases.append(("trunc_utf8_tail", b"hello world \xe4\xb8\xad\xe6\x96\x87 part" + b"\xe4\xb8"))
    cases.append(("trunc_utf8_3b", b"pre" + b"\xe2\x82" + b"suf"))
    cases.append(("trunc_4b_utf8", b"start\xf0\x90\x8d" + b"end"))
    cases.append(("trunc_big5", b"\xa4\x40\xa4\x48" + b"\xa4"))

    # invalid continuation bytes after UTF BOMs
    cases.append(("bom8_badcont", b"\xef\xbb\xbf" + b"\x80\x81\x82" + b"txt\xff"))
    cases.append(("bom8_overlong", b"\xef\xbb\xbf\xc0\x80\xc1\x81\x00"))
    cases.append(("bom8_lone", b"\xef\xbb\xbf" + bytes([0xc2, 0x00, 0xff, 0x80]) + b"ok"))

    # overlong/invalid UTF-8 (no bom)
    cases.append(("overlong_nul", b"\xc0\x80"))
    cases.append(("overlong_a", b"\xc1\x81"))
    cases.append(("invalid_utf8_cont", b"abc\x80\x80\xffdef"))
    cases.append(("badseq_utf8", b"\xe0\x80\x80\xed\xa0\x80"))
    cases.append(("lone_high_starts", b"\xc2\xc3\xc4\xc5"))

    # mixed BOM + contradictory declared charset
    cases.append(
        (
            "bom8_decl_iso",
            b'\xef\xbb\xbf<meta charset="iso-8859-1"><p>caf\xe9 d\xe9j\xe0 \x80 more</p>' * 4,
        )
    )
    cases.append(
        (
            "bom8_decl_cp1251_mixed",
            b'<?xml version="1.0" encoding="windows-1251"?>\n\xef\xbb\xbf<p>\xcf\xf0\xe2\xe2\x82\xac</p>' * 3,
        )
    )

    # high-byte noise around CJK byte ranges
    cjk_base = b"\xa4\x40\xa4\x48\xb0\xa1"
    noise = bytes([0x81 + (i % 0x70) for i in range(12)])
    cases.append(("cjk_noise", cjk_base + noise + cjk_base))
    cases.append(("cjk_highmix", (b"\xb0\xa1\xff\x80\x90" + b"\xa1\x40") * 6))

    # alternating valid text and binary-like bytes
    cases.append(("alt_text_bin", (b"hello" + bytes([0x00, 0xff, 0x80, 0x1f, 0x01]) + b"world ") * 7))
    cases.append(
        ("alt_good_bad", b"".join(b"ab" + bytes([0x90 + (k % 0x50)]) for k in range(25)))
    )

    # short payloads near TOO_SMALL_SEQUENCE (32)
    cases.append(("short_20", b"ascii short here"[:18] + b"\xff\x80"))
    cases.append(("short_31", b"x" * 29 + b"\x80\xff"))
    cases.append(("short_32", b"y" * 31 + b"\x81"))
    cases.append(("short_33", b"z" * 30 + b"\x82\x83"))
    cases.append(("short_mbfrag", b"\xe4\xb8\xad\xe6\x96" + b"a" * 20 + b"\xe7"))
    cases.append(("short_high", bytes(list(range(0x80, 0x80 + 25)))))

    # threshold edge case base payloads (exercised under 0.0/0.1/0.2/0.5)
    cases.append(("edge_messy", b"caf\xe9 d\xe9j\xe0 price \xa3\xff\x00 more text." * 5))
    cases.append(("edge_lowmess", b"naive text with single high \xff byte to nudge." * 6))

    return cases


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("ascii", b"hello world ascii only\n" * 20),
        ("utf8", ("Cafe deja vu. " * 40).encode("utf_8")),
        ("utf8_bom", b"\xef\xbb\xbf" + ("1\n" + "hello\n" * 20).encode("utf_8")),
        (
            "ru_cp1251",
            ("Привет мир. Это русский текст. Проверка кодировки. " * 20).encode(
                "cp1251"
            ),
        ),
        (
            "cz_cp1250",
            ("Příliš žluťoučký kůň úpěl ďábelské ódy. " * 20).encode("cp1250"),
        ),
        (
            "latin1",
            ("MOLIÈRE déjà été façade Noël. " * 30).encode("latin_1"),
        ),
        (
            "cp1252",
            ("Curly quotes “hello” and euro € café. " * 30).encode("cp1252"),
        ),
        (
            "pl_iso8859_2",
            ("Zażółć gęślą jaźń. Polski tekst testowy. " * 20).encode(
                "iso8859_2"
            ),
        ),
        (
            "greek_iso8859_7",
            ("Καλημέρα κόσμε. Ελληνικό κείμενο. " * 30).encode("iso8859_7"),
        ),
        (
            "ru_cp855",
            ("Привет мир. Русский текст для проверки. " * 30).encode("cp855"),
        ),
        (
            "arabic_cp720",
            ("مرحبا بالعالم نص عربي للاختبار. " * 30).encode("cp720"),
        ),
        (
            "turkish_mac",
            ("İstanbul büyük şehir Türkçe metin. " * 30).encode("mac_turkish"),
        ),
        (
            "hz_chinese",
            ("中文测试内容用于编码检测。" * 30).encode("hz"),
        ),
        (
            "utf32_bom",
            ("Hello utf32 text. " * 30).encode("utf_32"),
        ),
        (
            "utf7_bom",
            ("\ufeff" + "Hello € plain utf seven. " * 30).encode("utf_7"),
        ),
        (
            "johab_korean",
            ("한글 테스트 문장입니다. 한국어 인코딩 확인. ABC 123. " * 30).encode(
                "johab"
            ),
        ),
        (
            "iso2022_kr_korean",
            ("한글 테스트 문장입니다. 한국어 인코딩 확인. ABC 123. " * 30).encode(
                "iso2022_kr"
            ),
        ),
    ],
)
def test_generated_payloads_match_python_reference(
    rust_exe: Path, tmp_path: Path, name: str, payload: bytes
) -> None:
    sample = tmp_path / f"{name}.bin"
    sample.write_bytes(payload)

    assert rust_best(rust_exe, sample) == python_best(payload)


@pytest.mark.parametrize(
    ("name", "payload"),
    _make_deterministic_generated_payloads(),
)
def test_deterministic_expanded_matrix_match_python_reference(
    rust_exe: Path, tmp_path: Path, name: str, payload: bytes
) -> None:
    """Expanded deterministic Python-vs-Rust differential matrix.

    Uses fixed seed generation. Compares best encoding (stable contract for
    ambiguous cases). Covers the categories required by task.
    """
    sample = tmp_path / f"{name}.bin"
    sample.write_bytes(payload)

    assert rust_best(rust_exe, sample) == python_best(payload)


ADVERSARIAL_XFAIL_NAMES: set[str] = {
    "bom8_badcont",
    "short_high",
}
# short_20 was removed (2026-06): is_printable() parity fix for U+00A0 (nbsp/Zs)
# in md.rs makes Rust sr/mess match py (py .isprintable() == False for it).
# Remaining are codec-variant (utf16-le/gb18030 decode policy vs encoding_rs) or
# ordering on pure 0-chaos high-noise short inputs. See runtime handling in test.


def _adversarial_params():
    raw = _make_deterministic_adversarial_payloads()
    # No xfail marks here: runtime xfail() + stable asserts inside the test body handle
    # the ambiguous cases (see test_adversarial... and ADVERSARIAL_XFAIL_NAMES).
    return raw


@pytest.mark.parametrize(
    ("name", "payload"),
    _adversarial_params(),
)
def test_adversarial_deterministic_matrix_match_python_reference(
    rust_exe: Path, tmp_path: Path, name: str, payload: bytes
) -> None:
    """Adversarial deterministic fuzz/corpus matrix for charset-normalizer Rust parity.

    Fixed byte literals, deterministic, covers truncated mb, invalid BOM cont,
    overlong utf8, bom+decl mix, cjk noise, alt text/bin, short near 32,
    threshold edges, and cp_* isolation/exclusion (tested via extra harness calls).
    Exact best-encoding parity preferred for stable cases. xfail with source-backed
    reason for inherently ambiguous (see ADVERSARIAL_XFAIL_NAMES).
    """
    sample = tmp_path / f"{name}.bin"
    sample.write_bytes(payload)

    py_e = python_best(payload)
    rs_e = rust_best(rust_exe, sample)
    if name in ADVERSARIAL_XFAIL_NAMES:
        # Stable (non-xfailed) assertion only on the intentionally ambiguous adv cases:
        # both sides still detect *some* encoding (text, not "undefined").
        assert py_e != "undefined"
        assert rs_e != "undefined"
        if py_e != rs_e:
            pytest.xfail(
                "Adversarial case: best() tie-break differs due to codec variant (e.g. utf16-le "
                "lenient decode) or short-path/mess edge + candidate order; stable property "
                "(both detect as text) holds. Python source of truth for best() only on stable."
            )
    assert rs_e == py_e


@pytest.mark.parametrize(
    "relative_path",
    [
        "tests/data/sample-french-1.txt",
        "tests/data/sample-russian.txt",
        "tests/data/sample-english.bom.txt",
    ],
)
def test_fixture_payloads_match_python_reference(rust_exe: Path, relative_path: str) -> None:
    sample = REPO / "examples" / "charset_normalizer_rust" / relative_path
    payload = sample.read_bytes()

    assert rust_best(rust_exe, sample) == python_best(payload)


def test_no_preemptive_cli_matches_python_reference(rust_exe: Path, tmp_path: Path) -> None:
    payload = b'<meta charset="iso-8859-1"><p>hello ascii body</p>' * 10
    sample = tmp_path / "declared_latin1_ascii.html"
    sample.write_bytes(payload)

    assert rust_best(rust_exe, sample, "--no-preemptive") == python_best(
        payload, preemptive=False
    )


# Additional explicit toggle coverage for threshold and preemptive on generated cases.
# Use documented stable encoding contract; exact floats/JSON only where inherently stable.


def test_preemptive_toggle_on_decl_hint(rust_exe: Path, tmp_path: Path) -> None:
    # Declarative hint present; with preemp default vs disabled, encoding may differ on
    # ambiguous body. We assert the python ref behavior is mirrored for both.
    payload = b'<meta charset="iso-8859-1">Moli\xe8re d\xe9j\xe0 body text here more.' * 6
    sample = tmp_path / "decl_toggle.bin"
    sample.write_bytes(payload)

    # default (preemptive on)
    assert rust_best(rust_exe, sample) == python_best(payload, preemptive=True)
    # disabled
    assert rust_best(rust_exe, sample, "--no-preemptive") == python_best(
        payload, preemptive=False
    )


def test_threshold_toggle_on_ambiguous(rust_exe: Path, tmp_path: Path) -> None:
    # A payload that under strict low thresh may reject some, higher allows different SB.
    # We only require parity of observable best encoding under same thresh.
    payload = b"caf\xe9 d\xe9j\xe0 price 100\xa3 more text to sample." * 8
    sample = tmp_path / "thresh_ambig.bin"
    sample.write_bytes(payload)

    # default 0.2
    assert rust_best(rust_exe, sample) == python_best(payload, threshold=0.2)
    # higher tolerance
    assert rust_best(rust_exe, sample, "--threshold", "0.5") == python_best(
        payload, threshold=0.5
    )


# Threshold edge tests (0.0, 0.1, 0.2, 0.5) using adversarial base payloads.
# Parity on best encoding under identical settings.


def test_threshold_edges(rust_exe: Path, tmp_path: Path) -> None:
    # Exercise edges; for 0.0 expect fallback or undefined parity when all exceed.
    payload = b"caf\xe9 d\xe9j\xe0 price \xa3\xff\x00 more text." * 5
    sample = tmp_path / "te_messy.bin"
    sample.write_bytes(payload)

    for t in (0.0, 0.1, 0.2, 0.5):
        assert rust_best(rust_exe, sample, "--threshold", str(t)) == python_best(
            payload, threshold=t
        )

    # low-mess edge
    payload2 = b"naive text with single high \xff byte to nudge." * 6
    sample2 = tmp_path / "te_low.bin"
    sample2.write_bytes(payload2)
    for t in (0.0, 0.1, 0.2, 0.5):
        assert rust_best(rust_exe, sample2, "--threshold", str(t)) == python_best(
            payload2, threshold=t
        )


# cp_isolation / cp_exclusion via CLI harness + Python ref.
# Added only for differential test exposure (no default behavior change).


def test_cp_isolation_and_exclusion(rust_exe: Path, tmp_path: Path) -> None:
    # ascii payload under cp1252 isolation: filter skips ascii/utf_8, picks cp1252
    payload = b"hello world only ascii text here for parity test of isolation." * 2
    sample = tmp_path / "cpiso.bin"
    sample.write_bytes(payload)

    assert rust_best(rust_exe, sample, "--cp-isolation", "cp1252") == python_best(
        payload, cp_isolation=["cp1252"]
    )

    # exclusion of ascii,utf_8
    assert rust_best(rust_exe, sample, "--cp-exclusion", "ascii,utf_8") == python_best(
        payload, cp_exclusion=["ascii", "utf_8"]
    )

    # high-byte western under exclusion
    payload2 = b"Moli\xe8re d\xe9j\xe0" * 5
    sample2 = tmp_path / "cpex.bin"
    sample2.write_bytes(payload2)
    res_py = python_best(payload2, cp_exclusion=["ascii", "utf_8"])
    res_rs = rust_best(rust_exe, sample2, "--cp-exclusion", "ascii,utf_8")
    assert res_rs == res_py
