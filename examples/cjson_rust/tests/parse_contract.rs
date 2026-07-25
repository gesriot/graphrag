//! Golden contract verifier for the Rust cJSON ownership-slice port.
//! Reads examples/cjson/tests/parse/golden_*.json (ground truth = C cJSON) and
//! asserts the Rust port reproduces the unformatted/inspect/formatted oracles
//! and the parse-error outcomes exactly.
//! Run with: cargo test --test parse_contract

use std::fs;
use std::path::PathBuf;

use cjson_rust::{
    add_item_to_array, add_item_to_object, create_array, create_bool, create_double_array,
    create_false, create_float_array, create_int_array, create_null, create_number, create_object,
    create_raw, create_string, create_string_array, create_true, delete_item_from_array,
    delete_item_from_object, delete_item_from_object_case_sensitive, detach_item_from_array,
    detach_item_from_object, detach_item_from_object_case_sensitive, inspect, parse,
    print_formatted, print_unformatted, replace_item_in_array, replace_item_in_object,
    replace_item_in_object_case_sensitive, CJson,
};
use serde_json::{json, Value};

fn golden_dir() -> PathBuf {
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    p.pop();
    p.push("cjson");
    p.push("tests");
    p.push("parse");
    p
}

fn trace_step(steps: &mut Vec<Value>, step: &str, tree: &CJson) {
    let tree: Value = serde_json::from_slice(&inspect(tree))
        .unwrap_or_else(|err| panic!("inspect trace must be JSON: {err}"));
    steps.push(json!({"step": step, "tree": tree}));
}

/// Rust counterpart to the fixed C runner scenarios in `golden_mutation.json`.
/// The scenario sequence is intentionally spelled out here: the C output is
/// golden ground truth, while this only exercises the public safe API.
fn mutation_trace(scenario: &str) -> Value {
    let mut steps = Vec::new();
    match scenario {
        "array_ownership" => {
            let mut root = create_array();
            trace_step(&mut steps, "create_array", &root);
            add_item_to_array(&mut root, create_number(1.0));
            trace_step(&mut steps, "add_1", &root);
            add_item_to_array(&mut root, create_string("two"));
            trace_step(&mut steps, "add_two", &root);
            add_item_to_array(&mut root, create_number(3.0));
            trace_step(&mut steps, "add_3", &root);

            let detached = detach_item_from_array(&mut root, 1)
                .unwrap_or_else(|| panic!("array scenario must detach index 1"));
            trace_step(&mut steps, "detach_returns_caller_owned", &detached);
            trace_step(&mut steps, "after_detach", &root);
            drop(detached);
            trace_step(&mut steps, "after_caller_deletes_detached", &root);

            assert!(delete_item_from_array(&mut root, 1));
            trace_step(&mut steps, "after_delete_index_1", &root);
            add_item_to_array(&mut root, create_number(4.0));
            trace_step(&mut steps, "add_4", &root);
            assert!(replace_item_in_array(&mut root, 0, create_number(9.0)).is_ok());
            trace_step(&mut steps, "after_replace_index_0", &root);
        }
        "object_ownership" => {
            let mut root = create_object();
            trace_step(&mut steps, "create_object", &root);
            add_item_to_object(&mut root, "Alpha", create_number(1.0));
            trace_step(&mut steps, "add_Alpha", &root);
            add_item_to_object(&mut root, "alpha", create_number(2.0));
            trace_step(&mut steps, "add_alpha", &root);

            let detached = detach_item_from_object(&mut root, "ALPHA")
                .unwrap_or_else(|| panic!("object scenario must detach Alpha"));
            trace_step(
                &mut steps,
                "detach_case_insensitive_returns_caller_owned",
                &detached,
            );
            trace_step(&mut steps, "after_case_insensitive_detach", &root);
            drop(detached);

            let detached = detach_item_from_object_case_sensitive(&mut root, "alpha")
                .unwrap_or_else(|| panic!("object scenario must detach exact alpha"));
            trace_step(
                &mut steps,
                "detach_case_sensitive_returns_caller_owned",
                &detached,
            );
            trace_step(&mut steps, "after_case_sensitive_detach", &root);
            drop(detached);

            add_item_to_object(&mut root, "Gone", create_true());
            assert!(delete_item_from_object(&mut root, "gone"));
            trace_step(&mut steps, "after_delete_case_insensitive", &root);
            add_item_to_object(&mut root, "Exact", create_false());
            assert!(delete_item_from_object_case_sensitive(&mut root, "Exact"));
            trace_step(&mut steps, "after_delete_case_sensitive", &root);

            add_item_to_object(&mut root, "Name", create_string("old"));
            assert!(replace_item_in_object(&mut root, "name", create_string("new")).is_ok());
            trace_step(&mut steps, "after_replace_case_insensitive", &root);
            assert!(
                replace_item_in_object_case_sensitive(&mut root, "name", create_bool(false))
                    .is_ok()
            );
            trace_step(&mut steps, "after_replace_case_sensitive", &root);
        }
        "constructors" => {
            let mut root = create_array();
            trace_step(&mut steps, "create_array", &root);
            add_item_to_array(&mut root, create_null());
            trace_step(&mut steps, "create_null", &root);
            add_item_to_array(&mut root, create_true());
            trace_step(&mut steps, "create_true", &root);
            add_item_to_array(&mut root, create_false());
            trace_step(&mut steps, "create_false", &root);
            add_item_to_array(&mut root, create_bool(true));
            trace_step(&mut steps, "create_bool_true", &root);
            add_item_to_array(&mut root, create_bool(false));
            trace_step(&mut steps, "create_bool_false", &root);
            add_item_to_array(&mut root, create_number(-12.5));
            trace_step(&mut steps, "create_number", &root);
            add_item_to_array(&mut root, create_string("owned"));
            trace_step(&mut steps, "create_string", &root);
            add_item_to_array(&mut root, create_raw("{\"raw\":true}"));
            trace_step(&mut steps, "create_raw", &root);
            add_item_to_array(&mut root, create_array());
            trace_step(&mut steps, "create_nested_array", &root);
            add_item_to_array(&mut root, create_object());
            trace_step(&mut steps, "create_nested_object", &root);
        }
        "typed_arrays" => {
            let mut root = create_array();
            trace_step(&mut steps, "create_array", &root);
            add_item_to_array(&mut root, create_int_array(&[-2, 0, 7]));
            trace_step(&mut steps, "create_int_array", &root);
            add_item_to_array(&mut root, create_float_array(&[1.5, -0.25]));
            trace_step(&mut steps, "create_float_array", &root);
            add_item_to_array(&mut root, create_double_array(&[0.1, 3.141592653589793]));
            trace_step(&mut steps, "create_double_array", &root);
            add_item_to_array(&mut root, create_string_array(["red", "blue"]));
            trace_step(&mut steps, "create_string_array", &root);
        }
        unknown => panic!("unknown C mutation scenario {unknown:?}"),
    }
    json!({"scenario": scenario, "steps": steps})
}

// Under Miri the float-print golden cannot match: libc snprintf is cfg'd out and
// the Display stand-in is not C-byte-faithful. Ownership is covered by
// `ownership_props` under Miri instead.
#[test]
#[cfg_attr(
    miri,
    ignore = "float-print golden needs libc; run ownership_props under Miri"
)]
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
            if let Some(scenario) = case.get("scenario").and_then(|v| v.as_str()) {
                assert_eq!(
                    mutation_trace(scenario),
                    case["trace"],
                    "mutation trace for {desc:?}"
                );
                total += 1;
                continue;
            }
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
    // 22 ownership-slice cases + bounded float printing + mutation traces.
    assert!(
        total >= 54,
        "expected >= 54 cjson golden cases (ownership + float print + mutation), got {total}"
    );
}
