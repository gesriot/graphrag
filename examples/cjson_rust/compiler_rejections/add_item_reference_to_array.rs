// cJSON API: cJSON_AddItemReferenceToArray
// expected-error: error[E0502]: cannot borrow `source` as mutable because it is also borrowed as immutable
//
// Candidate: extend the current owned child slot with a borrowed alternative.
// The insertion itself type-checks; the exact C trace's later source mutation
// does not while the destination array holds the alias.

enum Child<'a> {
    Owned(Option<Box<CJson<'a>>>),
    Borrowed(&'a CJson<'a>),
}

struct CJson<'a> {
    value: i32,
    child: Child<'a>,
}

fn add_item_reference_to_array<'a>(array: &mut CJson<'a>, item: &'a CJson<'a>) {
    array.child = Child::Borrowed(item);
}

fn set_number(item: &mut CJson<'_>, value: i32) {
    item.value = value;
}

#[allow(dead_code)]
fn c_oracle_mutation_trace() {
    let mut source = CJson {
        value: 1,
        child: Child::Owned(None),
    };
    let mut array = CJson {
        value: 0,
        child: Child::Owned(None),
    };
    add_item_reference_to_array(&mut array, &source);
    set_number(&mut source, 2);
    match array.child {
        Child::Borrowed(item) => assert_eq!(item.value, 2),
        Child::Owned(_) => unreachable!(),
    }
}
