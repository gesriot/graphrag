//! Property tests over the cJSON ownership-slice port (parse → print → drop).
//!
//! Phase 5 requires every invariant labelled as one of:
//! - **deterministic** — follows from the code / cJSON semantics by construction
//! - **inferred** — expected from the port design but not a formal proof
//! - **human-approved** — a deliberate scope choice of this slice
//!
//! Keep case counts low so the suite stays fast under both `cargo test` and Miri.
//!
//! Run: `cargo test --test ownership_props`
//! Miri (ownership only; float FFI excluded via `cfg(miri)` in `print_number`):
//!   `cargo +nightly miri test --test ownership_props`

use cjson_rust::{
    add_bool_to_object, add_item_to_array, add_number_to_object, children, compare, create_number,
    create_object, delete_item_from_array, detach_item_from_array, detach_item_via_pointer,
    duplicate, get_array_item, get_array_size, get_object_item_case_sensitive,
    insert_item_in_array, is_array, is_bool, is_null, is_number, is_object, is_string, is_true,
    minify, parse, parse_with_opts, print_unformatted, replace_item_in_array,
    replace_item_via_pointer, set_bool_value, set_int_value, set_number_value, CJson, CJSON_ARRAY,
    CJSON_FALSE, CJSON_NULL, CJSON_NUMBER, CJSON_OBJECT, CJSON_STRING, CJSON_TRUE,
};
use proptest::prelude::*;
use proptest::test_runner::{Config, TestRunner};

/// Shared runner: fixed case count for speed; proptest prints the seed on failure.
fn runner() -> TestRunner {
    // Normal builds: 32 cases. Under Miri each case is much slower, so 16.
    #[cfg(miri)]
    let cases = 16;
    #[cfg(not(miri))]
    let cases = 32;
    TestRunner::new(Config {
        cases,
        failure_persistence: None,
        ..Config::default()
    })
}

/// Generate compact JSON text for the ownership / structure path.
///
/// Biased toward containers and small leaves so trees stay shallow enough for
/// Miri and the suite stays sub-second. Depth/width caps are explicit.
fn arb_json(max_depth: u32) -> impl Strategy<Value = String> {
    arb_value(max_depth).prop_map(|v| v.to_string())
}

fn arb_value(max_depth: u32) -> BoxedStrategy<serde_json::Value> {
    use serde_json::Value;
    let leaf = prop_oneof![
        Just(Value::Null),
        any::<bool>().prop_map(Value::Bool),
        // Keep integers in i32 range so the port's valueint path is exercised
        // without depending on float-print FFI under Miri.
        (-1000i32..1000).prop_map(|n| Value::Number(n.into())),
        "[a-zA-Z0-9_ ]{0,12}".prop_map(Value::String),
    ]
    .boxed();

    leaf.prop_recursive(
        max_depth, // depth
        48,        // max nodes overall (soft)
        6,         // max items per collection
        |inner| {
            prop_oneof![
                prop::collection::vec(inner.clone(), 0..5).prop_map(Value::Array),
                prop::collection::btree_map("[a-z]{1,4}", inner, 0..4)
                    .prop_map(|m| { Value::Object(m.into_iter().collect()) }),
            ]
            .boxed()
        },
    )
    .boxed()
}

// ---------------------------------------------------------------------------
// Properties
// ---------------------------------------------------------------------------

/// **deterministic** — a successful parse produces a node whose low type tag is
/// one of the public cJSON kinds; invalid is only the pre-parse state.
#[test]
fn prop_parsed_root_has_public_type_tag() {
    let mut r = runner();
    r.run(&arb_json(3), |js| {
        if let Some(root) = parse(js.as_bytes()) {
            let t = root.type_ & 0xff;
            prop_assert!(
                matches!(
                    t,
                    CJSON_FALSE
                        | CJSON_TRUE
                        | CJSON_NULL
                        | CJSON_NUMBER
                        | CJSON_STRING
                        | CJSON_ARRAY
                        | CJSON_OBJECT
                ),
                "unexpected type tag {t} for {js:?}"
            );
        }
        Ok(())
    })
    .unwrap();
}

/// **deterministic** — `Is*` predicates are mutually exclusive for a single
/// primary kind (bool is the union of true/false; number/string/array/object/
/// null are pairwise exclusive).
#[test]
fn prop_is_predicates_partition_primary_kind() {
    let mut r = runner();
    r.run(&arb_json(3), |js| {
        if let Some(root) = parse(js.as_bytes()) {
            check_partition(&root)?;
        }
        Ok(())
    })
    .unwrap();
}

fn check_partition(item: &CJson) -> Result<(), TestCaseError> {
    let flags = [
        is_null(item),
        is_bool(item),
        is_number(item),
        is_string(item),
        is_array(item),
        is_object(item),
    ];
    let n = flags.iter().filter(|&&b| b).count();
    prop_assert!(
        n == 1,
        "expected exactly one primary Is* kind, got {n} (type={})",
        item.type_ & 0xff
    );
    if is_bool(item) {
        let t = item.type_ & 0xff;
        prop_assert_eq!(is_true(item), t == CJSON_TRUE);
        prop_assert!(t == CJSON_TRUE || t == CJSON_FALSE);
    }
    // Recurse into children (structure walk must not assume cycles — none exist).
    let mut cur = item.child.as_deref();
    while let Some(node) = cur {
        check_partition(node)?;
        cur = node.next.as_deref();
    }
    Ok(())
}

/// **deterministic** — `GetArraySize` equals the length of the sibling chain
/// under `child` (cJSON uses the same chain for arrays and objects).
#[test]
fn prop_array_size_matches_sibling_chain() {
    let mut r = runner();
    r.run(&arb_json(3), |js| {
        if let Some(root) = parse(js.as_bytes()) {
            check_sizes(&root)?;
        }
        Ok(())
    })
    .unwrap();
}

fn check_sizes(item: &CJson) -> Result<(), TestCaseError> {
    if is_array(item) || is_object(item) {
        let mut n = 0i32;
        let mut cur = item.child.as_deref();
        while let Some(node) = cur {
            n += 1;
            cur = node.next.as_deref();
        }
        prop_assert_eq!(get_array_size(item), n);
    }
    let mut cur = item.child.as_deref();
    while let Some(node) = cur {
        check_sizes(node)?;
        cur = node.next.as_deref();
    }
    Ok(())
}

/// **inferred** — parse then unformatted-print yields UTF-8, and for inputs that
/// parse successfully a second parse of the printed bytes also succeeds
/// (structural round-trip). Byte-identity with the *original* input is **not**
/// claimed (key order, number formatting, whitespace all may differ).
#[test]
fn prop_print_is_utf8_and_reparsable() {
    let mut r = runner();
    r.run(&arb_json(3), |js| {
        if let Some(root) = parse(js.as_bytes()) {
            let printed = print_unformatted(&root);
            prop_assert!(
                std::str::from_utf8(&printed).is_ok(),
                "print_unformatted produced non-UTF-8 for {js:?}"
            );
            let again = parse(&printed);
            prop_assert!(
                again.is_some(),
                "re-parse of printed output failed: {} (from {js:?})",
                String::from_utf8_lossy(&printed)
            );
        }
        Ok(())
    })
    .unwrap();
}

/// **deterministic** — dropping a successfully parsed tree is safe for arbitrary
/// shapes (Miri-checked when run under `cargo miri test`). This is the ownership
/// claim: `Drop` walks `next` iteratively and `child` recursively with no raw
/// pointers. The property is that parse+print+drop of random JSON does not
/// panic (and under Miri, does not UB).
#[test]
fn prop_parse_print_drop_no_panic() {
    let mut r = runner();
    r.run(&arb_json(4), |js| {
        if let Some(root) = parse(js.as_bytes()) {
            let _printed = print_unformatted(&root);
            drop(root);
        }
        Ok(())
    })
    .unwrap();
}

/// **deterministic** — add and insert transfer each incoming `Box` into the
/// sibling chain; detach removes exactly one node and returns it with no sibling;
/// delete drops in place; and replace drops the old node while preserving the
/// rest of the chain. This is the safe-Rust ownership counterpart to the
/// mutation C oracle, and Miri checks it without raw pointers.
#[test]
fn prop_mutation_transfers_and_releases_ownership() {
    let mut r = runner();
    r.run(&prop::collection::vec(-1000i32..1000, 3..20), |numbers| {
        let mut root = cjson_rust::create_array();
        for &number in &numbers {
            add_item_to_array(&mut root, create_number(number as f64));
        }
        prop_assert_eq!(get_array_size(&root), numbers.len() as i32);

        prop_assert!(insert_item_in_array(&mut root, 0, create_number(-9999.0)).is_ok());
        prop_assert_eq!(get_array_size(&root), numbers.len() as i32 + 1);

        let index = (numbers.len() / 2) as i32;
        let detached = detach_item_from_array(&mut root, index)
            .ok_or_else(|| TestCaseError::fail("index selected from array must detach"))?;
        prop_assert!(detached.next.is_none(), "detached node retained a sibling");
        prop_assert_eq!(get_array_size(&root), numbers.len() as i32);
        drop(detached);

        prop_assert!(delete_item_from_array(&mut root, 0));
        prop_assert_eq!(get_array_size(&root), numbers.len() as i32 - 1);
        prop_assert!(replace_item_in_array(&mut root, 0, create_number(42.0)).is_ok());
        prop_assert_eq!(get_array_size(&root), numbers.len() as i32 - 1);
        drop(root);

        // The non-aliasing API closure is also safe under Miri: object helper
        // constructors own their inserted boxes, setters mutate only their
        // unique node, the iterator borrows without raw links, and minify
        // compacts one uniquely owned byte buffer.
        let mut object = create_object();
        add_number_to_object(&mut object, "number", 1.0);
        add_bool_to_object(&mut object, "bool", false);
        prop_assert_eq!(children(&object).count(), 2);
        let number = object
            .child
            .as_deref_mut()
            .ok_or_else(|| TestCaseError::fail("object helper must insert number"))?;
        prop_assert_eq!(set_number_value(number, 2.5), 2.5);
        let boolean = number
            .next
            .as_deref_mut()
            .ok_or_else(|| TestCaseError::fail("object helper must insert bool"))?;
        prop_assert_ne!(set_bool_value(boolean, true), 0);
        prop_assert_eq!(set_int_value(number, -4), -4);
        prop_assert!(get_object_item_case_sensitive(&object, b"number").is_some());
        let (_, end) = parse_with_opts(b"{\"x\":1}\0", true)
            .map_err(|offset| TestCaseError::fail(format!("NUL parse failed at {offset}")))?;
        prop_assert_eq!(end, 7);
        let mut compact = b" { /*x*/ \"a b\" : 1 } ".to_vec();
        minify(&mut compact);
        prop_assert_eq!(compact, b"{\"a b\":1}");
        drop(object);
        Ok(())
    })
    .unwrap();
}

/// **deterministic** — node-address identity does not weaken ownership. The
/// target is a `*const CJson` compared and never dereferenced, so detaching by
/// address returns the same allocation the caller named, with no sibling
/// attached, and replacing by address drops exactly the old node. Miri checks
/// that taking the address under a shared borrow and mutating afterwards leaves
/// no invalidated tag behind.
#[test]
fn prop_via_pointer_identity_preserves_ownership() {
    let mut r = runner();
    r.run(&prop::collection::vec(-1000i32..1000, 2..12), |numbers| {
        let mut root = cjson_rust::create_array();
        for &number in &numbers {
            add_item_to_array(&mut root, create_number(number as f64));
        }
        let index = (numbers.len() / 2) as i32;
        let target: *const CJson = get_array_item(&root, index)
            .ok_or_else(|| TestCaseError::fail("index selected from array must exist"))?;

        let detached = detach_item_via_pointer(&mut root, target)
            .ok_or_else(|| TestCaseError::fail("address of a current child must detach"))?;
        prop_assert!(
            std::ptr::eq(&*detached as *const CJson, target),
            "detach must return the identified allocation"
        );
        prop_assert!(detached.next.is_none(), "detached node retained a sibling");
        prop_assert_eq!(get_array_size(&root), numbers.len() as i32 - 1);
        drop(detached);

        let survivor: *const CJson = get_array_item(&root, 0)
            .ok_or_else(|| TestCaseError::fail("array keeps at least one child"))?;
        prop_assert!(replace_item_via_pointer(&mut root, survivor, create_number(42.0)).is_ok());
        prop_assert_eq!(get_array_size(&root), numbers.len() as i32 - 1);

        // A miss must hand the replacement back rather than leak or unlink.
        let orphan = create_number(7.0);
        let stale: *const CJson = &*orphan;
        prop_assert!(replace_item_via_pointer(&mut root, stale, create_number(1.0)).is_err());
        prop_assert_eq!(get_array_size(&root), numbers.len() as i32 - 1);
        drop(orphan);
        drop(root);
        Ok(())
    })
    .unwrap();
}

/// **deterministic** — a recursive duplicate owns cloned children and strings,
/// so it compares equal initially, diverges after the source is mutated, and
/// remains printable after that source is dropped. A separately dropped copy
/// leaves its source intact, proving ownership works independently both ways.
#[test]
fn prop_recursive_duplicate_is_independent_and_comparable() {
    let mut r = runner();
    r.run(&prop::collection::vec(-1000i32..1000, 1..16), |numbers| {
        let mut source = cjson_rust::create_array();
        for &number in &numbers {
            add_item_to_array(&mut source, create_number(number as f64));
        }
        let source_before = print_unformatted(&source);

        let dropped_copy = duplicate(&source, true);
        drop(dropped_copy);
        prop_assert_eq!(print_unformatted(&source), source_before);

        let shallow = duplicate(&source, false);
        prop_assert_eq!(get_array_size(&shallow), 0);
        let deep = duplicate(&source, true);
        prop_assert!(compare(&source, &deep, true));
        let deep_before = print_unformatted(&deep);

        let changed_value = if numbers[0] == 42 { 43.0 } else { 42.0 };
        prop_assert!(replace_item_in_array(&mut source, 0, create_number(changed_value)).is_ok());
        prop_assert!(!compare(&source, &deep, true));
        prop_assert_eq!(print_unformatted(&deep), deep_before.clone());
        drop(source);
        prop_assert_eq!(print_unformatted(&deep), deep_before);
        drop(shallow);
        drop(deep);
        Ok(())
    })
    .unwrap();
}

/// **human-approved** — nesting beyond cJSON's default limit (1000) is rejected
/// rather than overflowing the stack. We only probe a few depths near the
/// boundary (not 1000 recursive prop cases) for speed.
#[test]
fn prop_deep_nesting_rejected_past_limit() {
    // Build arrays nested D deep: "[[[[…]]]]".
    // Port increments depth on each container enter and rejects when
    // `depth >= NESTING_LIMIT` (1000) *before* entering the next container, so
    // 1000 nested arrays still parse and 1001 is the first rejection.
    // Under Miri, skip near-limit depths (999–1002): building/parsing ~1000
    // nested containers dominates Miri wall time and only re-checks the same
    // depth counter. Boundary rejection is asserted on normal `cargo test`.
    #[cfg(miri)]
    let depths: &[usize] = &[0, 1, 10, 40];
    #[cfg(not(miri))]
    let depths: &[usize] = &[0, 1, 10, 100, 999, 1000, 1001, 1002];

    for &depth in depths {
        let mut s = String::new();
        for _ in 0..depth {
            s.push('[');
        }
        s.push('1');
        for _ in 0..depth {
            s.push(']');
        }
        let parsed = parse(s.as_bytes());
        if depth <= 1000 {
            assert!(parsed.is_some(), "expected parse ok at depth {depth}");
        } else {
            assert!(parsed.is_none(), "expected parse reject at depth {depth}");
        }
    }
}

/// **inferred** — a wide sibling list (long array) still drops cleanly; this is
/// the `Drop` design point (iterate `next`, do not recurse on siblings).
#[test]
fn prop_wide_array_drop_no_panic() {
    // Enough siblings to matter if Drop were recursive on `next`.
    // Under Miri keep it smaller — the ownership shape is the same.
    #[cfg(miri)]
    let width = 40usize;
    #[cfg(not(miri))]
    let width = 200usize;

    let mut s = String::from('[');
    for i in 0..width {
        if i > 0 {
            s.push(',');
        }
        s.push('0');
    }
    s.push(']');
    let root = parse(s.as_bytes()).expect("wide array parses");
    assert_eq!(get_array_size(&root), width as i32);
    let _ = print_unformatted(&root);
    drop(root);
}

/// **deterministic** — garbage / truncated inputs either parse cleanly or return
/// `None`; they must not panic (partial trees are dropped via ordinary ownership).
#[test]
fn prop_garbage_input_no_panic() {
    let mut r = runner();
    let strat = prop_oneof![
        Just(String::new()),
        Just("{".into()),
        Just("[".into()),
        Just("{\"a\":".into()),
        Just("\"unterminated".into()),
        Just("null null".into()),
        "[\\x00-\\x7f]{0,40}".prop_map(|s| s),
    ];
    r.run(&strat, |js| {
        let _ = parse(js.as_bytes());
        Ok(())
    })
    .unwrap();
}
