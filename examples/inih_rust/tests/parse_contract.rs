//! Golden contract verifier for the Rust inih port (first inih C→Rust port).
//! Reads examples/inih/tests/parse/golden_*.json (ground truth = C inih) and
//! asserts the Rust port reproduces (result, callback events) exactly.
//! Run with: cargo test --test parse_contract

use std::cell::RefCell;
use std::fs;
use std::path::PathBuf;

use inih_rust::parse_string;
use serde_json::{json, Value};

fn golden_dir() -> PathBuf {
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    p.pop();
    p.push("inih");
    p.push("tests");
    p.push("parse");
    p
}

#[test]
fn inih_contract_all_cases() {
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
            let ini = case["ini"].as_str().unwrap();
            let desc = case["desc"].as_str().unwrap_or("");

            let events = RefCell::new(Vec::<Value>::new());
            let result = parse_string(ini.as_bytes(), |section, name, value| {
                events.borrow_mut().push(json!({
                    "section": String::from_utf8_lossy(section),
                    "name": String::from_utf8_lossy(name),
                    "value": String::from_utf8_lossy(value),
                }));
                true
            });

            assert_eq!(
                result as i64,
                case["result"].as_i64().unwrap(),
                "result for {desc:?}"
            );
            assert_eq!(
                &Value::Array(events.into_inner()),
                &case["events"],
                "events for {desc:?}"
            );
            total += 1;
        }
    }
    assert!(total >= 21, "expected >= 21 inih golden cases, got {total}");
}
