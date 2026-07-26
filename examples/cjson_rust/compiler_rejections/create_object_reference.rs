// cJSON API: cJSON_CreateObjectReference
// expected-error: error[E0502]: cannot borrow `source` as mutable because it is also borrowed as immutable
//
// Candidate: a reference-object node borrows the source child chain. The C
// oracle mutates that source after creating the reference and observes the
// change through the reference; safe Rust rejects the mutation while the child
// borrow stored in the returned node is live.

struct CJson<'a> {
    value: i32,
    child: Option<&'a CJson<'a>>,
}

fn create_object_reference<'a>(child: &'a CJson<'a>) -> CJson<'a> {
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
    let reference = create_object_reference(&source);
    set_number(&mut source, 2);
    assert_eq!(reference.child.map(|child| child.value), Some(2));
}
