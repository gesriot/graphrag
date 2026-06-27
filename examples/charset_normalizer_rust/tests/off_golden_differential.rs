//! Off-golden differential test matrix.
//! Derived from vendored Python reference on deterministic mutations of data samples.
//! Each asserts concrete encoding/language/chaos/coherence/bom (no weak "some result").

use std::fs;
use std::path::Path;

use charset_normalizer_rust::{
    from_bytes, from_bytes_with_options, from_bytes_with_options_and_trace, FromBytesOptions,
};

fn assert_best(p: &[u8], enc: &str, lang: Option<&str>, chaos: f64, coh: f64, bom: bool) {
    let m = from_bytes(p);
    let best = m.best().expect("must return best");
    assert_eq!(best.encoding, enc);
    assert_eq!(best.language.as_deref(), lang);
    assert!(
        (best.chaos - chaos).abs() < 1e-4,
        "chaos got {} want {}",
        best.chaos,
        chaos
    );
    assert!(
        (best.coherence - coh).abs() < 1e-4,
        "coh got {} want {}",
        best.coherence,
        coh
    );
    assert_eq!(best.bom, bom);
}

#[test]
fn off_golden_append_ascii_punct_cp1252_french() {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/data");
    let mut d = fs::read(dir.join("sample-french-1.txt")).unwrap();
    d.extend_from_slice(b"?!.");
    assert_best(&d, "cp1252", Some("French"), 0.1675, 0.8378, false);
}

#[test]
fn off_golden_prepend_ascii_header_utf8_russian() {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/data");
    let ru = fs::read(dir.join("sample-russian-2.txt")).unwrap();
    let mut d = b"From: a@b.com\r\n\r\n".to_vec();
    d.extend_from_slice(&ru);
    assert_best(&d, "utf_8", Some("Russian"), 0.002, 0.5625, false);
}

#[test]
fn off_golden_truncate_large_utf8_prefix_polish() {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/data");
    let pl = fs::read(dir.join("sample-polish.txt")).unwrap();
    let d = pl[..2800].to_vec();
    assert_best(&d, "utf_8", Some("Polish"), 0.005, 0.5748, false);
}

#[test]
fn off_golden_concat_same_lang_utf8_russians() {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/data");
    let ru2 = fs::read(dir.join("sample-russian-2.txt")).unwrap();
    let ru3 = fs::read(dir.join("sample-russian-3.txt")).unwrap();
    let mut d = ru2;
    d.extend_from_slice(b"\n");
    d.extend_from_slice(&ru3[..600]);
    assert_best(&d, "utf_8", Some("Russian"), 0.0, 0.6016, false);
}

#[test]
fn off_golden_empty() {
    assert_best(b"", "utf_8", Some("Unknown"), 0.0, 0.0, false);
}

#[test]
fn off_golden_pure_ascii_literal() {
    assert_best(
        b"Hello world 123!",
        "ascii",
        Some("English"),
        0.0,
        0.0,
        false,
    );
}

#[test]
fn off_golden_append_to_bom_sample_keeps_bom() {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/data");
    let mut d = fs::read(dir.join("sample-english.bom.txt")).unwrap();
    d.extend_from_slice(b"!!");
    assert_best(&d, "utf_8", Some("English"), 0.0, 0.8182, true);
}

#[test]
fn off_golden_append_ascii_to_utf8_french() {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/data");
    let mut d = fs::read(dir.join("sample-french.txt")).unwrap();
    d.extend_from_slice(b" -- end.");
    assert_best(&d, "utf_8", Some("French"), 0.001, 0.8197, false);
}

// Large payload lazy decoding behavior tests (synthetic just over TOO_BIG_SEQUENCE).
// These exercise prefix-probe (500k), on-demand strict chunk decode for !MB,
// final tail [50k:] lookup, lazy logs on explain, and detection parity for large inputs.
// Use small-overhead repeating payloads; 10M+ is required to hit is_too_large in both py/rust.

#[test]
fn large_payload_mostly_utf8_detects_consistently() {
    const BIG: usize = 10_000_100;
    let mut data = Vec::with_capacity(BIG);
    // mix ascii + some multibyte utf8 so not pure ascii
    let sample = b"na\xc3\xafve caf\xc3\xa9 UTF8 large-payload test. ";
    while data.len() < BIG {
        data.extend_from_slice(sample);
    }
    data.truncate(BIG);
    let m = from_bytes(&data);
    let best = m.best().expect("must have best for large utf8-ish");
    assert_eq!(best.encoding, "utf_8");
}

#[test]
fn large_payload_trace_emits_lazy_and_prefix_paths() {
    const BIG: usize = 10_000_100;
    let data = vec![b'x'; BIG];
    let opts = FromBytesOptions {
        explain: true,
        ..Default::default()
    };
    let (_, traces) = from_bytes_with_options_and_trace(&data, opts);
    assert!(
        traces
            .iter()
            .any(|t| t.contains("Using lazy str decoding because the payload is quite large")),
        "missing top-level lazy log"
    );
}

#[test]
fn large_payload_ascii_md_chunk_reject_via_isolation() {
    const BIG: usize = 10_000_100;
    let mut data = vec![b'a'; BIG];
    // corrupt after 500k (inside the 512-byte chunk window at ~2M stride offset) so probe ok, md chunk decode fails strict
    if BIG > 2_000_030 {
        data[2_000_030] = 0xff;
    }
    let opts = FromBytesOptions {
        explain: true,
        enable_fallback: false,
        cp_isolation: vec!["ascii".to_string()],
        ..Default::default()
    };
    let (m, traces) = from_bytes_with_options_and_trace(&data, opts);
    assert!(
        m.best().is_none(),
        "ascii must be rejected by lazy MD chunk decode for this large input"
    );
    assert!(
        traces
            .iter()
            .any(|t| t.contains("LazyStr Loading: After MD chunk decode")),
        "missing LazyStr MD chunk log"
    );
}

#[test]
fn large_payload_ascii_final_lookup_reject_via_isolation() {
    const BIG: usize = 10_000_100;
    let mut data = vec![b'a'; BIG];
    // corrupt after 500k prefix, not on md stride sample (~2M), so final lookup rejects
    if BIG > 700_000 {
        data[700_000] = 0xff;
    }
    let opts = FromBytesOptions {
        explain: true,
        enable_fallback: false,
        cp_isolation: vec!["ascii".to_string()],
        ..Default::default()
    };
    let (m, traces) = from_bytes_with_options_and_trace(&data, opts);
    assert!(
        m.best().is_none(),
        "ascii must be rejected by final lookup for large"
    );
    assert!(
        traces
            .iter()
            .any(|t| t.contains("LazyStr Loading: After final lookup")),
        "missing LazyStr final lookup log"
    );
}

#[test]
fn large_non_priority_sb_candidate_succeeds_via_lazy_path() {
    const BIG: usize = 10_000_100;
    let mut data = vec![b'a'; BIG];
    data[1234] = b'z';
    // non-prio sb; exercises lazy chunking + final without upfront full decode of whole
    let opts = FromBytesOptions {
        enable_fallback: false,
        cp_isolation: vec!["cp1252".to_string()],
        ..Default::default()
    };
    let m = from_bytes_with_options(&data, opts);
    assert!(
        m.best().is_some(),
        "non-prio sb large should still produce result via lazy"
    );
}
