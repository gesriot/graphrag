//! Differential lexer-parity gate (Stage 2). Mirrors the Python `lexer.tokenize`
//! token stream exactly, token-by-token (token_type_path, value), before the
//! StatementSplitter is built on top.
//!
//! Run with: cargo test --test lex_contract

use std::fs;
use std::path::PathBuf;

use serde_json::Value;
use sqlparse_rust::lexer::tokenize;

fn lex_golden_dir() -> PathBuf {
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    p.pop(); // -> examples
    p.push("sqlparse");
    p.push("tests");
    p.push("lex");
    p
}

#[test]
fn lex_contract_all_cases() {
    let mut files: Vec<PathBuf> = fs::read_dir(lex_golden_dir())
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
            let sql = case["sql"].as_str().unwrap();
            let got = tokenize(sql);
            let want = case["tokens"].as_array().unwrap();
            // Compare token-by-token: (token_type_path, value).
            let got_json: Vec<Value> = got
                .iter()
                .map(|(tt, val)| {
                    Value::Array(vec![
                        Value::Array(tt.iter().map(|s| Value::from(*s)).collect()),
                        Value::from(val.clone()),
                    ])
                })
                .collect();
            assert_eq!(
                &Value::Array(got_json),
                &case["tokens"],
                "lexer mismatch for {:?}",
                sql
            );
            total += want.len();
        }
    }
    assert!(total >= 340, "expected >= 340 lexed tokens, got {}", total);
}
