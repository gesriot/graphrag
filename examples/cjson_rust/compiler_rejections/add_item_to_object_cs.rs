// cJSON API: cJSON_AddItemToObjectCS
// expected-error: error[E0502]: cannot borrow `key` as mutable because it is also borrowed as immutable
//
// Smallest lifetime/Cow candidate: the object stores a borrowed key. The C
// oracle overwrites the caller key after insertion, then prints the object and
// observes the new key. Safe Rust prevents that overwrite while the object
// retains its borrow.

use std::borrow::Cow;

struct CJson<'a> {
    key: Option<Cow<'a, [u8]>>,
}

fn add_item_to_object_cs<'a>(object: &mut CJson<'a>, key: &'a [u8]) {
    object.key = Some(Cow::Borrowed(key));
}

#[allow(dead_code)]
fn c_oracle_mutation_trace() {
    let mut key = *b"first";
    let mut object = CJson { key: None };
    add_item_to_object_cs(&mut object, &key);
    key.copy_from_slice(b"after");
    assert_eq!(object.key.as_deref(), Some(&b"after"[..]));
}
