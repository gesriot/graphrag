// cJSON API: cJSON_CreateStringReference
// expected-error: error[E0502]: cannot borrow `source` as mutable because it is also borrowed as immutable
//
// Smallest lifetime/Cow candidate: storing a borrowed string type-checks, but
// the C oracle then mutates the caller buffer while the reference node remains
// live. Safe Rust rejects precisely that observable aliasing trace.

use std::borrow::Cow;

struct CJson<'a> {
    valuestring: Cow<'a, [u8]>,
}

fn create_string_reference<'a>(value: &'a [u8]) -> CJson<'a> {
    CJson {
        valuestring: Cow::Borrowed(value),
    }
}

#[allow(dead_code)]
fn c_oracle_mutation_trace() {
    let mut source = *b"first";
    let reference = create_string_reference(&source);
    source.copy_from_slice(b"after");
    assert_eq!(reference.valuestring.as_ref(), b"after");
}
