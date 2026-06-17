//! Golden contract verifier for the Rust patch (v3) port of diff-match-patch.
//!
//! Loads examples/diff_match_patch/tests/patch/golden_*.json and asserts the Rust
//! port reproduces every case: make / apply / roundtrip / invalid.
//!
//! Run with: cargo test --test patch_contract

use std::fs;
use std::path::PathBuf;

use diff_match_patch_rust::DiffMatchPatch;
use serde_json::Value;

fn patch_golden_dir() -> PathBuf {
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    p.pop(); // -> examples
    p.push("diff_match_patch");
    p.push("tests");
    p.push("patch");
    p
}

fn run_case(c: &Value) {
    let dmp = DiffMatchPatch::new();
    match c["op"].as_str().unwrap() {
        "make" => {
            let patches =
                dmp.patch_make(c["text1"].as_str().unwrap(), c["text2"].as_str().unwrap());
            assert_eq!(
                dmp.patch_to_text(&patches),
                c["patch_text"].as_str().unwrap()
            );
        }
        "apply" => {
            let patches = dmp
                .patch_from_text(c["patch_text"].as_str().unwrap())
                .unwrap();
            let before = dmp.patch_to_text(&patches);
            let (new_text, results) = dmp.patch_apply(&patches, c["source"].as_str().unwrap());
            assert_eq!(new_text, c["result"].as_str().unwrap(), "apply result");
            let want: Vec<bool> = c["results"]
                .as_array()
                .unwrap()
                .iter()
                .map(|b| b.as_bool().unwrap())
                .collect();
            assert_eq!(results, want, "apply results");
            // patch_apply must not mutate its input patches.
            assert_eq!(
                dmp.patch_to_text(&patches),
                before,
                "patches mutated by apply"
            );
        }
        "roundtrip" => {
            let patches = dmp
                .patch_from_text(c["patch_text"].as_str().unwrap())
                .unwrap();
            assert_eq!(dmp.patch_to_text(&patches), c["expected"].as_str().unwrap());
        }
        "invalid" => {
            let err = dmp
                .patch_from_text(c["patch_text"].as_str().unwrap())
                .unwrap_err();
            assert_eq!(err, c["error"].as_str().unwrap());
        }
        other => panic!("unknown op {other}"),
    }
}

#[test]
fn patch_contract_all_cases() {
    let dir = patch_golden_dir();
    let mut files: Vec<PathBuf> = fs::read_dir(&dir)
        .unwrap_or_else(|_| panic!("cannot read patch golden dir {:?}", dir))
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| {
            p.file_name()
                .and_then(|n| n.to_str())
                .map(|n| n.starts_with("golden_") && n.ends_with(".json"))
                .unwrap_or(false)
        })
        .collect();
    files.sort();
    assert!(!files.is_empty(), "no golden_*.json in {:?}", dir);

    let mut total = 0usize;
    for path in &files {
        let content = fs::read_to_string(path).unwrap_or_else(|_| panic!("cannot read {:?}", path));
        let data: Value = serde_json::from_str(&content).unwrap();
        for case in data["cases"].as_array().unwrap() {
            run_case(case);
            total += 1;
        }
    }
    assert!(
        total >= 20,
        "expected >= 20 patch golden cases, got {}",
        total
    );
}
