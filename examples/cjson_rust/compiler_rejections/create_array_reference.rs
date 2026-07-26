// cJSON API: cJSON_CreateArrayReference
// expected-error: error[E0502]: cannot borrow `source` as mutable because it is also borrowed as immutable
//
// Candidate: a reference-array node borrows the source child chain. As with the
// object constructor, the C trace mutates the source after the alias is stored
// and expects the alias to print the new child value.

struct CJson<'a> {
    value: i32,
    child: Option<&'a CJson<'a>>,
}

fn create_array_reference<'a>(child: &'a CJson<'a>) -> CJson<'a> {
    CJson {
        value: 0,
        child: Some(child),
    }
}

fn set_number(item: &mut CJson<'_>, value: i32) {
    item.value = value;
}

#[allow(dead_code)]
fn c_oracle_mutation_trace() {
    let mut source = CJson {
        value: 1,
        child: None,
    };
    let reference = create_array_reference(&source);
    set_number(&mut source, 2);
    assert_eq!(reference.child.map(|child| child.value), Some(2));
}
