//! Golden contract test for the charset-normalizer core port.
//!
//! This MUST pass with the exact same results as the Python reference
//! on the frozen sample set (see golden_detection.json).
//!
//! Generated from Python before any Rust was written (see PROVENANCE.md gate #2).
//! The Python reference was verified to match 18/18 cases.

use std::fs;
use std::path::Path;

use serde::Deserialize;
use serde_json;

use charset_normalizer_rust::from_bytes;

#[derive(Debug, Deserialize)]
#[allow(dead_code)]
struct GoldenCase {
    name: String,
    size: usize,
    encoding: String,
    language: Option<String>,
    chaos: f64,
    coherence: f64,
    bom: bool,
    #[serde(default)]
    normalized_len: Option<usize>,
    #[serde(default)]
    normalized_head: Option<String>,
}

#[derive(Debug, Deserialize)]
struct GoldenFile {
    version: u32,
    cases: Vec<GoldenCase>,
}

fn load_golden() -> GoldenFile {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/golden_detection.json");
    let data =
        fs::read_to_string(path).expect("golden_detection.json must exist next to this test");
    serde_json::from_str(&data).expect("valid golden json")
}

#[test]
fn golden_detection_matches_python_reference() {
    let golden = load_golden();
    assert_eq!(golden.version, 1);
    let data_dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/data");

    let mut passed = 0;
    let total = golden.cases.len();

    for case in &golden.cases {
        let bytes = fs::read(data_dir.join(&case.name))
            .unwrap_or_else(|_| panic!("missing sample {}", case.name));

        assert_eq!(bytes.len(), case.size, "size mismatch for {}", case.name);

        let results = from_bytes(&bytes);
        let best = results.best();

        if let Some(best) = best {
            let lang_match = best.language.as_deref() == case.language.as_deref();
            let chaos_match = (best.chaos - case.chaos).abs() < 1e-5;
            let coh_match = (best.coherence - case.coherence).abs() < 1e-5;

            if best.encoding == case.encoding
                && lang_match
                && chaos_match
                && coh_match
                && best.bom == case.bom
            {
                passed += 1;
            } else {
                eprintln!(
                    "MISMATCH {}: got encoding={} lang={:?} chaos={} coh={} bom={}",
                    case.name, best.encoding, best.language, best.chaos, best.coherence, best.bom
                );
            }
        } else {
            eprintln!("NO RESULT for {}", case.name);
        }
    }

    assert_eq!(
        passed, total,
        "Golden contract failed: {}/{} cases matched. Implement the real from_bytes using context packs.",
        passed, total
    );
}

#[test]
fn at_least_some_samples_are_text() {
    // Sanity: the current stub returns nothing; real impl must return at least one match for these
    let data = include_bytes!("data/sample-english.bom.txt");
    let res = from_bytes(data);
    // This will fail until the port is done. That's intentional for the contract gate.
    assert!(
        res.best().is_some(),
        "Real implementation must detect at least one CharsetMatch for this UTF-8-with-BOM sample"
    );
}

#[test]
fn off_golden_mutation_still_plausible_not_fallback() {
    // +1 ASCII byte to golden sample; must not fall to utf_8/English/0.5 chaos
    let data_dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/data");
    let mut data = fs::read(data_dir.join("sample-french-1.txt")).expect("sample");
    data.push(b'!');
    let res = from_bytes(&data);
    let best = res.best().expect("must return a result");
    // py ref on this mutation yields cp1252/French/chaos~0.1675/coh 0.8378 (not utf8/English/0.5)
    assert_eq!(best.encoding, "cp1252");
    assert_eq!(best.language.as_deref(), Some("French"));
    assert!((best.chaos - 0.1675).abs() < 0.01);
    assert!((best.coherence - 0.8378).abs() < 0.01);
}
