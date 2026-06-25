//! Golden contract verifier for the Rust jsmn port (first C→Rust port).
//! Reads examples/jsmn/tests/parse/golden_*.json (ground truth = C jsmn) and
//! asserts the Rust port reproduces (result, tokens) exactly.
//! Run with: cargo test --test jsmn_contract

use std::fs;
use std::path::PathBuf;

use jsmn_rust::parse_json;
use serde_json::Value;

fn golden_dir() -> PathBuf {
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    p.pop();
    p.push("jsmn");
    p.push("tests");
    p.push("parse");
    p
}

#[test]
fn jsmn_contract_all_cases() {
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
            let js = case["json"].as_str().unwrap();
            let cap = case["cap"].as_i64().unwrap() as i32;
            let (r, tokens) = parse_json(js.as_bytes(), cap);
            assert_eq!(
                r as i64,
                case["result"].as_i64().unwrap(),
                "result for {:?}",
                js
            );
            let got: Vec<Value> = tokens
                .iter()
                .map(|t| {
                    serde_json::json!({"type": t.ttype, "start": t.start, "end": t.end, "size": t.size})
                })
                .collect();
            assert_eq!(&Value::Array(got), &case["tokens"], "tokens for {:?}", js);
            total += 1;
        }
    }
    assert!(
        total >= 15,
        "expected >= 15 jsmn golden cases, got {}",
        total
    );
}
