//! Hidden Rust golden contract for the humanize number-formatting ablation.
//! Injected by `scripts/ablation.py eval` into a filled-in kit (crate `arm`),
//! with `number_golden_dir()` patched to the absolute golden dir. Reads
//! examples/humanize/tests/number/golden_*.json (ground truth = Python oracle)
//! and asserts `arm::<func>(args) == result` for every case.
//!
//! Not compiled by the repo build; both arms exclude it (raw kit skips tests/,
//! graph kit only gets context packs), so the golden stays hidden.

use std::fs;
use std::path::PathBuf;

use serde_json::Value;

fn number_golden_dir() -> PathBuf {
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    p.pop();
    p.push("humanize");
    p.push("tests");
    p.push("number");
    p
}

#[test]
fn humanize_number_contract() {
    let mut files: Vec<PathBuf> = fs::read_dir(number_golden_dir())
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
    assert!(!files.is_empty(), "no golden files found");

    let mut total = 0usize;
    for path in &files {
        let data: Value = serde_json::from_str(&fs::read_to_string(path).unwrap()).unwrap();
        for c in data["cases"].as_array().unwrap() {
            let func = c["func"].as_str().unwrap();
            let args = c["args"].as_array().unwrap();
            let want = c["result"].as_str().unwrap();
            let got: String = match func {
                "intcomma" => {
                    let v = args[0].as_f64().unwrap();
                    let nd = args
                        .get(1)
                        .and_then(|x| if x.is_null() { None } else { x.as_i64() });
                    arm::intcomma(v, nd)
                }
                "intword" => {
                    let v = args[0].as_f64().unwrap();
                    let fmt = args.get(1).and_then(|x| x.as_str()).unwrap_or("%.1f");
                    arm::intword(v, fmt)
                }
                "apnumber" => arm::apnumber(args[0].as_i64().unwrap()),
                "ordinal" => {
                    let v = args[0].as_i64().unwrap();
                    let g = args.get(1).and_then(|x| x.as_str()).unwrap_or("male");
                    arm::ordinal(v, g)
                }
                "fractional" => arm::fractional(args[0].as_f64().unwrap()),
                "scientific" => {
                    let v = args[0].as_f64().unwrap();
                    let p = args.get(1).and_then(|x| x.as_i64()).unwrap_or(2);
                    arm::scientific(v, p)
                }
                other => panic!("unknown func {other}"),
            };
            assert_eq!(got, want, "{func}({args:?})");
            total += 1;
        }
    }
    assert!(total >= 55, "expected >= 55 cases, got {total}");
}
