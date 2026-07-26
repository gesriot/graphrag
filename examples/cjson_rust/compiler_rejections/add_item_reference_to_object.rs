// cJSON API: cJSON_AddItemReferenceToObject
// expected-error: error[E0502]: cannot borrow `source` as mutable because it is also borrowed as immutable
//
// Candidate: an object member owns its copied key but borrows the referenced
// item. The C trace mutates the source after the add and expects the object's
// reference member to observe the new value; safe borrowing rejects that trace.

struct CJson<'a> {
    value: i32,
    member: Option<(&'static [u8], &'a CJson<'a>)>,
}

fn add_item_reference_to_object<'a>(
    object: &mut CJson<'a>,
    key: &'static [u8],
    item: &'a CJson<'a>,
) {
    object.member = Some((key, item));
}

fn set_number(item: &mut CJson<'_>, value: i32) {
    item.value = value;
}

#[allow(dead_code)]
fn c_oracle_mutation_trace() {
    let mut source = CJson {
        value: 1,
        member: None,
    };
    let mut object = CJson {
        value: 0,
        member: None,
    };
    add_item_reference_to_object(&mut object, b"value", &source);
    set_number(&mut source, 2);
    assert_eq!(object.member.map(|(_, item)| item.value), Some(2));
}
