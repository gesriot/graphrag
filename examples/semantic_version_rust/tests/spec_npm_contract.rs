//! Golden contract verifier for the Rust NpmSpec port (v2b).
//!
//! Loads examples/semantic_version/tests/spec_npm/golden_*.json and asserts the
//! Rust NpmSpec reproduces every case: match / invalid / select / filter.
//!
//! Run with: cargo test --test spec_npm_contract

use std::fs;
use std::path::PathBuf;

use semantic_version_rust::NpmSpec;
use serde_json::Value;

fn npm_golden_dir() -> PathBuf {
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    p.pop(); // -> examples
    p.push("semantic_version");
    p.push("tests");
    p.push("spec_npm");
    p
}

fn str_list(v: &Value) -> Vec<String> {
    v.as_array()
        .unwrap()
        .iter()
        .map(|x| x.as_str().unwrap().to_string())
        .collect()
}

fn run_case(c: &Value) {
    match c["op"].as_str().unwrap() {
        "match" => {
            let spec = NpmSpec::new(c["spec"].as_str().unwrap()).expect("valid npm spec");
            let got = spec.matches(c["version"].as_str().unwrap());
            assert_eq!(
                got,
                c["result"].as_bool().unwrap(),
                "match {} {}",
                c["spec"],
                c["version"]
            );
        }
        "invalid" => {
            let err =
                NpmSpec::new(c["spec"].as_str().unwrap()).expect_err("npm spec must be invalid");
            assert_eq!(err, c["error"].as_str().unwrap());
        }
        "select" => {
            let spec = NpmSpec::new(c["spec"].as_str().unwrap()).unwrap();
            let versions = str_list(&c["versions"]);
            let refs: Vec<&str> = versions.iter().map(|s| s.as_str()).collect();
            let got = spec.select(&refs);
            let got_value = match got {
                Some(s) => Value::String(s),
                None => Value::Null,
            };
            assert_eq!(got_value, c["selected"], "select {}", c["spec"]);
        }
        "filter" => {
            let spec = NpmSpec::new(c["spec"].as_str().unwrap()).unwrap();
            let versions = str_list(&c["versions"]);
            let refs: Vec<&str> = versions.iter().map(|s| s.as_str()).collect();
            assert_eq!(
                spec.filter(&refs),
                str_list(&c["matched"]),
                "filter {}",
                c["spec"]
            );
        }
        other => panic!("unknown op {other}"),
    }
}

#[test]
fn npm_contract_all_cases() {
    let dir = npm_golden_dir();
    let mut files: Vec<PathBuf> = fs::read_dir(&dir)
        .unwrap_or_else(|_| panic!("cannot read npm golden dir {:?}", dir))
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
        total >= 40,
        "expected >= 40 NpmSpec golden cases, got {}",
        total
    );
}
