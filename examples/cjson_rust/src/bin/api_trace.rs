//! Deterministic trace for the cJSON API-surface C-oracle test.
//!
//! `examples/cjson/tests/test_cjson_api_audit.py` compiles the vendored C
//! implementation and compares its `safe` trace to this binary byte-for-byte.

use cjson_rust::{
    add_array_to_object, add_bool_to_object, add_false_to_object, add_item_to_array,
    add_null_to_object, add_number_to_object, add_object_to_object, add_raw_to_object,
    add_string_to_object, add_true_to_object, children, detach_item_via_pointer, get_array_item,
    get_object_item, get_object_item_case_sensitive, has_object_item, is_false, is_invalid, minify,
    parse_with_opts, print_buffered, print_preallocated, print_unformatted,
    replace_item_via_pointer, set_bool_value, set_int_value, set_number_value, set_value_string,
    version, CJson, CJSON_INVALID,
};

fn c_string(buffer: &[u8]) -> &str {
    let end = buffer
        .iter()
        .position(|byte| *byte == 0)
        .unwrap_or(buffer.len());
    std::str::from_utf8(&buffer[..end]).expect("invariant: API trace only renders ASCII JSON bytes")
}

fn json_text(buffer: &[u8]) -> &str {
    std::str::from_utf8(buffer).expect("invariant: API trace only renders ASCII JSON bytes")
}

fn hex(buffer: &[u8]) -> String {
    let mut rendered = String::with_capacity(buffer.len() * 2);
    for byte in buffer {
        use std::fmt::Write;
        write!(&mut rendered, "{byte:02x}")
            .expect("invariant: writing hexadecimal bytes to a String cannot fail");
    }
    rendered
}

fn main() {
    let mut root = cjson_rust::create_object();
    add_null_to_object(&mut root, "null");
    add_true_to_object(&mut root, "true");
    add_false_to_object(&mut root, "false");
    add_bool_to_object(&mut root, "bool", false);
    add_number_to_object(&mut root, "number", 1.5);
    add_number_to_object(&mut root, "int", 0.0);
    add_string_to_object(&mut root, "string", "before");
    add_raw_to_object(&mut root, "raw", "null");
    add_object_to_object(&mut root, "object");
    add_array_to_object(&mut root, "array");

    let number = get_object_item_case_sensitive(&root, b"number")
        .expect("invariant: add_number_to_object inserted number key");
    // Borrow `root` mutably only after the immutable lookup is no longer used.
    let _ = number;
    let number = root
        .child
        .as_deref_mut()
        .and_then(|first| {
            let mut cursor = Some(first);
            while let Some(node) = cursor {
                if node.string.as_deref() == Some(b"number") {
                    return Some(node);
                }
                cursor = node.next.as_deref_mut();
            }
            None
        })
        .expect("invariant: add_number_to_object inserted mutable number key");
    let set_number = set_number_value(number, -3.5);

    let int = root
        .child
        .as_deref_mut()
        .and_then(|first| {
            let mut cursor = Some(first);
            while let Some(node) = cursor {
                if node.string.as_deref() == Some(b"int") {
                    return Some(node);
                }
                cursor = node.next.as_deref_mut();
            }
            None
        })
        .expect("invariant: add_number_to_object inserted mutable int key");
    let set_int = set_int_value(int, -4);

    let bool_item = root
        .child
        .as_deref_mut()
        .and_then(|first| {
            let mut cursor = Some(first);
            while let Some(node) = cursor {
                if node.string.as_deref() == Some(b"bool") {
                    return Some(node);
                }
                cursor = node.next.as_deref_mut();
            }
            None
        })
        .expect("invariant: add_bool_to_object inserted mutable bool key");
    let set_bool = set_bool_value(bool_item, true);

    let string = root
        .child
        .as_deref_mut()
        .and_then(|first| {
            let mut cursor = Some(first);
            while let Some(node) = cursor {
                if node.string.as_deref() == Some(b"string") {
                    return Some(node);
                }
                cursor = node.next.as_deref_mut();
            }
            None
        })
        .expect("invariant: add_string_to_object inserted mutable string key");
    let string_ok = set_value_string(string, "after").is_some();

    let case_sensitive = get_object_item_case_sensitive(&root, b"string").is_some();
    let case_insensitive = get_object_item(&root, b"STRING").is_some();
    let has_number = has_object_item(&root, b"NuMbEr");
    let printed = print_unformatted(&root);
    let buffered = print_buffered(&root, 4, false);
    let mut preallocated = [0xAAu8; 512];
    let preallocated_ok = print_preallocated(&root, &mut preallocated, false);
    let mut too_small = [0xAAu8; 5];
    let too_small_ok = print_preallocated(&root, &mut too_small, false);

    let prefix = b"{\"p\":1} trailing";
    let prefix_result = parse_with_opts(prefix, false);
    let prefix_required = parse_with_opts(prefix, true);
    let nul_result = parse_with_opts(b"{\"p\":1}\0", true);
    let mut minified = b" { /* note */ \"a b\" : 1 // tail\n } ".to_vec();
    minify(&mut minified);

    let invalid = CJson {
        child: None,
        next: None,
        type_: CJSON_INVALID,
        valuestring: None,
        valueint: 0,
        valuedouble: 0.0,
        string: None,
    };
    let false_item = cjson_rust::create_false();

    println!("version={}", version());
    println!("children={}", children(&root).count());
    println!("case_sensitive={}", u8::from(case_sensitive));
    println!("case_insensitive={}", u8::from(case_insensitive));
    println!("has_number={}", u8::from(has_number));
    println!("is_invalid={}", u8::from(is_invalid(&invalid)));
    println!("is_false={}", u8::from(is_false(&false_item)));
    println!("set_number={set_number}");
    println!("set_int={set_int}");
    println!("set_bool={set_bool}");
    println!("set_string={}", u8::from(string_ok));
    println!(
        "printed={}",
        std::str::from_utf8(&printed).expect("invariant: API trace prints ASCII JSON")
    );
    println!(
        "buffered={}",
        std::str::from_utf8(&buffered).expect("invariant: API trace prints ASCII JSON")
    );
    println!(
        "preallocated={}:{}",
        u8::from(preallocated_ok),
        c_string(&preallocated)
    );
    println!(
        "preallocated_small={}:{}",
        u8::from(too_small_ok),
        hex(&too_small)
    );
    println!(
        "parse_prefix={}",
        match prefix_result {
            Ok((_, end)) => format!("1:{end}"),
            Err(end) => format!("0:{end}"),
        }
    );
    println!(
        "parse_prefix_required={}",
        match prefix_required {
            Ok((_, end)) => format!("1:{end}"),
            Err(end) => format!("0:{end}"),
        }
    );
    println!(
        "parse_nul_required={}",
        match nul_result {
            Ok((_, end)) => format!("1:{end}"),
            Err(end) => format!("0:{end}"),
        }
    );
    println!(
        "minify={}",
        std::str::from_utf8(&minified).expect("invariant: API trace minifies ASCII bytes")
    );

    // Node-address identity: the target is a raw pointer taken from a shared
    // borrow that ends before the mutable borrow, compared but never
    // dereferenced. No `unsafe`, no handle arena, no interior mutability.
    let mut vector = cjson_rust::create_array();
    add_item_to_array(&mut vector, cjson_rust::create_number(1.0));
    add_item_to_array(&mut vector, cjson_rust::create_number(2.0));
    add_item_to_array(&mut vector, cjson_rust::create_number(3.0));
    let middle: *const CJson = get_array_item(&vector, 1).expect("invariant: index 1 was added");
    let detached = detach_item_via_pointer(&mut vector, middle)
        .expect("invariant: the pointer names a current child");
    println!(
        "detach_via_pointer_identity={}",
        u8::from(std::ptr::eq(&*detached as *const CJson, middle))
    );
    println!(
        "detach_via_pointer_rest={}",
        json_text(&print_unformatted(&vector))
    );
    println!(
        "detach_via_pointer_item={}",
        json_text(&print_unformatted(&detached))
    );
    drop(detached);
    let middle: *const CJson = get_array_item(&vector, 1).expect("invariant: index 1 remains");
    let replaced = replace_item_via_pointer(&mut vector, middle, cjson_rust::create_number(9.0));
    println!("replace_via_pointer={}", u8::from(replaced.is_ok()));
    println!(
        "replace_via_pointer_result={}",
        json_text(&print_unformatted(&vector))
    );
}
