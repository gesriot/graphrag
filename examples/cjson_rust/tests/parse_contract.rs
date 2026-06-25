//! Golden contract verifier for the Rust cJSON ownership-slice port.
//! Reads examples/cjson/tests/parse/golden_*.json (ground truth = C cJSON) and
//! asserts the Rust port reproduces the unformatted/inspect/formatted oracles
//! and the parse-error outcomes exactly.
//! Run with: cargo test --test parse_contract

use std::fs;
use std::path::PathBuf;

use cjson_rust::{inspect, parse, print_formatted, print_unformatted};
use serde_json::Value;

fn golden_dir() -> PathBuf {
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    p.pop();
    p.push("cjson");
    p.push("tests");
    p.push("parse");
    p
}

#[test]
fn cjson_contract_all_cases() {
    let mut files: Vec<PathBuf> = fs::read_dir(golden_dir())
        .unwrap()
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| {
            p.file_name()
                .and_then(|n| n.to_str())
                .map(|n| n.starts_with("golden_") && n.ends_with(".json"))
                .unwrap_or(false)
        })
        .collect();
    files.sort();
    assert!(!files.is_empty());

    let mut total = 0usize;
    for path in &files {
        let data: Value = serde_json::from_str(&fs::read_to_string(path).unwrap()).unwrap();
        for case in data["cases"].as_array().unwrap() {
            let desc = case["desc"].as_str().unwrap_or("");
            let json = case["json"].as_str().unwrap();
            let parsed = parse(json.as_bytes());

            if !case["parse_ok"].as_bool().unwrap() {
                assert!(parsed.is_none(), "expected parse error for {desc:?}");
                assert_eq!(case["unformatted"].as_str().unwrap(), "__PARSE_ERROR__");
                total += 1;
                continue;
            }

            let root = parsed.unwrap_or_else(|| panic!("expected parse ok for {desc:?}"));

            let unf = String::from_utf8(print_unformatted(&root)).unwrap();
            assert_eq!(
                unf,
                case["unformatted"].as_str().unwrap(),
                "unformatted for {desc:?}"
            );

            let got_inspect: Value = serde_json::from_slice(&inspect(&root)).unwrap();
            assert_eq!(&got_inspect, &case["inspect"], "inspect for {desc:?}");

            if let Some(fmt) = case.get("formatted").and_then(|v| v.as_str()) {
                let got = String::from_utf8(print_formatted(&root)).unwrap();
                assert_eq!(got, fmt, "formatted for {desc:?}");
            }
            total += 1;
        }
    }
    assert!(
        total >= 22,
        "expected >= 22 cjson golden cases, got {total}"
    );
}
