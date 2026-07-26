//! Structure-preserving Rust port of the cJSON ownership slice
//! (parse -> inspect -> print -> delete) in default config.
//!
//! This deliberately mirrors cJSON's C node layout rather than using an
//! idiomatic Rust enum, because the point of this milestone is to carry C-style
//! tree ownership across the port:
//! - a node is a struct with a type tag, `child`/`next` links, and the
//!   `valuestring`/`valueint`/`valuedouble`/`string` fields;
//! - the child/sibling chain is a `Box`-owned singly linked list (no raw
//!   pointers, no `prev`);
//! - `Drop` mirrors `cJSON_Delete`: it iterates the `next` chain (so a long
//!   sibling list does not recurse) while each node's `child` drops recursively.
//!
//! Scope: parse + the public getter API + unformatted/formatted printing over a
//! bounded corpus (objects/arrays/strings/escapes/integers/bool/null/nesting)
//! plus a float-printing fidelity suite matching cJSON's `%1.15g`/`%1.17g`
//! number printer (see `print_number` and `golden_float_print.json`). The
//! owned builder/mutation subset is also ported: constructors, typed arrays,
//! add, detach, delete, replace, and node-address identity mutation. Reference
//! constructors/reference adds and the const-key alias remain outside this
//! `Box`-owned representation: checked compiler candidates prove that a safe
//! borrow cannot reproduce cJSON's later-source-mutation trace. See
//! `examples/cjson/API_SURFACE_AUDIT.md` for the exact candidates and the
//! representation changes that would close them.

// jsmn-style bit-flag type tags (cJSON.h).
pub const CJSON_INVALID: i32 = 0;
pub const CJSON_FALSE: i32 = 1 << 0;
pub const CJSON_TRUE: i32 = 1 << 1;
pub const CJSON_NULL: i32 = 1 << 2;
pub const CJSON_NUMBER: i32 = 1 << 3;
pub const CJSON_STRING: i32 = 1 << 4;
pub const CJSON_ARRAY: i32 = 1 << 5;
pub const CJSON_OBJECT: i32 = 1 << 6;
pub const CJSON_RAW: i32 = 1 << 7;

/// Vendored `cJSON.h` version metadata.
pub const VERSION_MAJOR: u8 = 1;
pub const VERSION_MINOR: u8 = 7;
pub const VERSION_PATCH: u8 = 19;
pub const VERSION: &str = "1.7.19";

/// Matches `CJSON_NESTING_LIMIT` in the vendored default header.
pub const NESTING_LIMIT: usize = 1000;

/// Mirror of the C `cJSON` node. Strings are kept as bytes (cJSON's `char*`),
/// since decoded values and keys may hold arbitrary UTF-8.
pub struct CJson {
    pub child: Option<Box<CJson>>,
    pub next: Option<Box<CJson>>,
    pub type_: i32,
    pub valuestring: Option<Vec<u8>>,
    pub valueint: i32,
    pub valuedouble: f64,
    pub string: Option<Vec<u8>>,
}

/// Safe borrowed traversal equivalent to `cJSON_ArrayForEach`.
pub struct Children<'a> {
    next: Option<&'a CJson>,
}

impl<'a> Iterator for Children<'a> {
    type Item = &'a CJson;

    fn next(&mut self) -> Option<Self::Item> {
        let current = self.next?;
        self.next = current.next.as_deref();
        Some(current)
    }
}

/// `cJSON_ArrayForEach` without exposing raw sibling pointers.
pub fn children(item: &CJson) -> Children<'_> {
    Children {
        next: item.child.as_deref(),
    }
}

/// `cJSON_Version`.
pub fn version() -> &'static str {
    VERSION
}

impl CJson {
    fn new() -> Self {
        CJson {
            child: None,
            next: None,
            type_: CJSON_INVALID,
            valuestring: None,
            valueint: 0,
            valuedouble: 0.0,
            string: None,
        }
    }
}

impl Drop for CJson {
    fn drop(&mut self) {
        // Mirror cJSON_Delete: iterate the sibling (`next`) chain instead of
        // recursing through it, so freeing a long array/object does not blow the
        // stack. Each node's `child` still drops recursively as the node falls
        // out of scope, matching cJSON's recursive child delete.
        let mut next = self.next.take();
        while let Some(mut node) = next {
            next = node.next.take();
        }
    }
}

/* ---------- getter API (ported 1:1 over the struct) ---------- */

pub fn is_null(item: &CJson) -> bool {
    (item.type_ & 0xff) == CJSON_NULL
}
pub fn is_invalid(item: &CJson) -> bool {
    (item.type_ & 0xff) == CJSON_INVALID
}
pub fn is_false(item: &CJson) -> bool {
    (item.type_ & 0xff) == CJSON_FALSE
}
pub fn is_bool(item: &CJson) -> bool {
    (item.type_ & (CJSON_TRUE | CJSON_FALSE)) != 0
}
pub fn is_true(item: &CJson) -> bool {
    (item.type_ & 0xff) == CJSON_TRUE
}
pub fn is_number(item: &CJson) -> bool {
    (item.type_ & 0xff) == CJSON_NUMBER
}
pub fn is_string(item: &CJson) -> bool {
    (item.type_ & 0xff) == CJSON_STRING
}
pub fn is_array(item: &CJson) -> bool {
    (item.type_ & 0xff) == CJSON_ARRAY
}
pub fn is_object(item: &CJson) -> bool {
    (item.type_ & 0xff) == CJSON_OBJECT
}
pub fn is_raw(item: &CJson) -> bool {
    (item.type_ & 0xff) == CJSON_RAW
}

/// `cJSON_GetArraySize`: number of children (works for arrays and objects).
pub fn get_array_size(item: &CJson) -> i32 {
    let mut n = 0i32;
    let mut cur = item.child.as_deref();
    while let Some(node) = cur {
        n += 1;
        cur = node.next.as_deref();
    }
    n
}

/// `cJSON_GetArrayItem`: the child at `index`, or None.
pub fn get_array_item(item: &CJson, index: i32) -> Option<&CJson> {
    if index < 0 {
        return None;
    }
    let mut remaining = index;
    let mut cur = item.child.as_deref();
    while let Some(node) = cur {
        if remaining == 0 {
            return Some(node);
        }
        remaining -= 1;
        cur = node.next.as_deref();
    }
    None
}

/// `cJSON_GetObjectItem`: case-insensitive lookup by key.
pub fn get_object_item<'a>(item: &'a CJson, name: &[u8]) -> Option<&'a CJson> {
    let mut cur = item.child.as_deref();
    while let Some(node) = cur {
        if let Some(key) = node.string.as_deref() {
            if key.eq_ignore_ascii_case(name) {
                return Some(node);
            }
        }
        cur = node.next.as_deref();
    }
    None
}

/// `cJSON_GetObjectItemCaseSensitive`.
pub fn get_object_item_case_sensitive<'a>(item: &'a CJson, name: &[u8]) -> Option<&'a CJson> {
    let mut cur = item.child.as_deref();
    while let Some(node) = cur {
        if node.string.as_deref().is_some_and(|key| key == name) {
            return Some(node);
        }
        cur = node.next.as_deref();
    }
    None
}

/// `cJSON_HasObjectItem` (ASCII case-insensitive, matching cJSON).
pub fn has_object_item(item: &CJson, name: &[u8]) -> bool {
    get_object_item(item, name).is_some()
}

pub fn get_string_value(item: &CJson) -> Option<&[u8]> {
    if is_string(item) {
        item.valuestring.as_deref()
    } else {
        None
    }
}

/// `cJSON_GetNumberValue`: returns the stored number, or NaN for a node that is
/// not a number.
pub fn get_number_value(item: &CJson) -> f64 {
    if is_number(item) {
        item.valuedouble
    } else {
        f64::NAN
    }
}

/* ---------- owned builder / mutation API ---------- */

fn new_with_type(type_: i32) -> Box<CJson> {
    let mut item = CJson::new();
    item.type_ = type_;
    Box::new(item)
}

fn set_number(item: &mut CJson, number: f64) {
    item.type_ = CJSON_NUMBER;
    item.valuedouble = number;
    item.valueint = if number >= i32::MAX as f64 {
        i32::MAX
    } else if number <= i32::MIN as f64 {
        i32::MIN
    } else {
        number as i32
    };
}

fn set_number_fields(item: &mut CJson, number: f64) -> f64 {
    item.valuedouble = number;
    item.valueint = if number >= i32::MAX as f64 {
        i32::MAX
    } else if number <= i32::MIN as f64 {
        i32::MIN
    } else {
        number as i32
    };
    number
}

/// Typed replacement for `cJSON_SetIntValue`.
pub fn set_int_value(item: &mut CJson, number: i32) -> i32 {
    item.valueint = number;
    item.valuedouble = number as f64;
    number
}

/// Typed replacement for `cJSON_SetNumberHelper` / `cJSON_SetNumberValue`.
/// Unlike `create_number`, this preserves the node's existing type bits.
pub fn set_number_value(item: &mut CJson, number: f64) -> f64 {
    set_number_fields(item, number)
}

/// Typed replacement for `cJSON_SetBoolValue`.
pub fn set_bool_value(item: &mut CJson, value: bool) -> i32 {
    if !is_bool(item) {
        return CJSON_INVALID;
    }
    item.type_ =
        (item.type_ & !(CJSON_FALSE | CJSON_TRUE)) | if value { CJSON_TRUE } else { CJSON_FALSE };
    item.type_
}

/// Typed replacement for `cJSON_SetValuestring`.
///
/// Reference strings are not representable in this port, so a string node with
/// no owned bytes models the C failure case. Successful updates return the
/// owned replacement bytes.
pub fn set_value_string<'a>(item: &'a mut CJson, value: impl AsRef<[u8]>) -> Option<&'a [u8]> {
    if !is_string(item) || item.valuestring.is_none() {
        return None;
    }
    item.valuestring = Some(value.as_ref().to_vec());
    item.valuestring.as_deref()
}

/// C-shaped migration helper for `cJSON_Delete`; normal Rust callers should
/// simply let the `Box<CJson>` drop at scope end.
pub fn delete(item: Box<CJson>) {
    drop(item);
}

/// `cJSON_CreateNull`.
pub fn create_null() -> Box<CJson> {
    new_with_type(CJSON_NULL)
}

/// `cJSON_CreateTrue`.
pub fn create_true() -> Box<CJson> {
    new_with_type(CJSON_TRUE)
}

/// `cJSON_CreateFalse`.
pub fn create_false() -> Box<CJson> {
    new_with_type(CJSON_FALSE)
}

/// `cJSON_CreateBool`.
pub fn create_bool(value: bool) -> Box<CJson> {
    if value {
        create_true()
    } else {
        create_false()
    }
}

/// `cJSON_CreateNumber`.
pub fn create_number(number: f64) -> Box<CJson> {
    let mut item = CJson::new();
    set_number(&mut item, number);
    Box::new(item)
}

/// `cJSON_CreateString`. The bytes are copied into the returned, owned node.
pub fn create_string(value: impl AsRef<[u8]>) -> Box<CJson> {
    let mut item = new_with_type(CJSON_STRING);
    item.valuestring = Some(value.as_ref().to_vec());
    item
}

/// `cJSON_CreateRaw`. The bytes are copied into the returned, owned node.
pub fn create_raw(value: impl AsRef<[u8]>) -> Box<CJson> {
    let mut item = new_with_type(CJSON_RAW);
    item.valuestring = Some(value.as_ref().to_vec());
    item
}

/// `cJSON_CreateArray`.
pub fn create_array() -> Box<CJson> {
    new_with_type(CJSON_ARRAY)
}

/// `cJSON_CreateObject`.
pub fn create_object() -> Box<CJson> {
    new_with_type(CJSON_OBJECT)
}

fn append_child(parent: &mut CJson, item: Box<CJson>) -> &mut CJson {
    let mut link = &mut parent.child;
    loop {
        match link {
            Some(node) => link = &mut node.next,
            slot @ None => {
                *slot = Some(item);
                return slot
                    .as_deref_mut()
                    .expect("invariant: assigning an appended child fills its link");
            }
        }
    }
}

/// `cJSON_AddItemToArray`.
///
/// `item` is consumed: its `Box` now belongs to `array` and will be dropped
/// with that tree unless a later detach transfers it back to the caller.
pub fn add_item_to_array(array: &mut CJson, item: Box<CJson>) {
    let _ = append_child(array, item);
}

/// `cJSON_AddItemToObject`.
///
/// The key is copied, as cJSON duplicates `string`; `item` is consumed by the
/// object on return.
fn add_item_to_object_impl(
    object: &mut CJson,
    key: impl AsRef<[u8]>,
    mut item: Box<CJson>,
) -> &mut CJson {
    item.string = Some(key.as_ref().to_vec());
    append_child(object, item)
}

pub fn add_item_to_object(object: &mut CJson, key: impl AsRef<[u8]>, item: Box<CJson>) {
    let _ = add_item_to_object_impl(object, key, item);
}

/// `cJSON_AddNullToObject`.
pub fn add_null_to_object(object: &mut CJson, key: impl AsRef<[u8]>) -> &mut CJson {
    add_item_to_object_impl(object, key, create_null())
}

/// `cJSON_AddTrueToObject`.
pub fn add_true_to_object(object: &mut CJson, key: impl AsRef<[u8]>) -> &mut CJson {
    add_item_to_object_impl(object, key, create_true())
}

/// `cJSON_AddFalseToObject`.
pub fn add_false_to_object(object: &mut CJson, key: impl AsRef<[u8]>) -> &mut CJson {
    add_item_to_object_impl(object, key, create_false())
}

/// `cJSON_AddBoolToObject`.
pub fn add_bool_to_object(object: &mut CJson, key: impl AsRef<[u8]>, value: bool) -> &mut CJson {
    add_item_to_object_impl(object, key, create_bool(value))
}

/// `cJSON_AddNumberToObject`.
pub fn add_number_to_object(object: &mut CJson, key: impl AsRef<[u8]>, value: f64) -> &mut CJson {
    add_item_to_object_impl(object, key, create_number(value))
}

/// `cJSON_AddStringToObject`.
pub fn add_string_to_object(
    object: &mut CJson,
    key: impl AsRef<[u8]>,
    value: impl AsRef<[u8]>,
) -> &mut CJson {
    add_item_to_object_impl(object, key, create_string(value))
}

/// `cJSON_AddRawToObject`.
pub fn add_raw_to_object(
    object: &mut CJson,
    key: impl AsRef<[u8]>,
    value: impl AsRef<[u8]>,
) -> &mut CJson {
    add_item_to_object_impl(object, key, create_raw(value))
}

/// `cJSON_AddObjectToObject`.
pub fn add_object_to_object(object: &mut CJson, key: impl AsRef<[u8]>) -> &mut CJson {
    add_item_to_object_impl(object, key, create_object())
}

/// `cJSON_AddArrayToObject`.
pub fn add_array_to_object(object: &mut CJson, key: impl AsRef<[u8]>) -> &mut CJson {
    add_item_to_object_impl(object, key, create_array())
}

/// `cJSON_InsertItemInArray`.
///
/// `newitem` is consumed on success. As captured by the C oracle, an index at
/// or past the current end appends; a negative index fails and returns the box
/// to the caller.
pub fn insert_item_in_array(
    array: &mut CJson,
    index: i32,
    mut newitem: Box<CJson>,
) -> Result<(), Box<CJson>> {
    if index < 0 {
        return Err(newitem);
    }
    let mut link = &mut array.child;
    for _ in 0..index {
        let Some(node) = link else {
            break;
        };
        link = &mut node.next;
    }
    newitem.next = link.take();
    *link = Some(newitem);
    Ok(())
}

fn child_slot_at(parent: &mut CJson, index: i32) -> Option<&mut Option<Box<CJson>>> {
    if index < 0 {
        return None;
    }
    let mut link = &mut parent.child;
    for _ in 0..index {
        link = &mut link.as_mut()?.next;
    }
    if link.is_some() {
        Some(link)
    } else {
        None
    }
}

fn object_child_slot<'a>(
    object: &'a mut CJson,
    key: &[u8],
    case_sensitive: bool,
) -> Option<&'a mut Option<Box<CJson>>> {
    let mut link = &mut object.child;
    loop {
        let matches = link
            .as_ref()
            .and_then(|node| node.string.as_deref())
            .is_some_and(|candidate| {
                if case_sensitive {
                    candidate == key
                } else {
                    candidate.eq_ignore_ascii_case(key)
                }
            });
        if matches {
            return Some(link);
        }
        link = &mut link.as_mut()?.next;
    }
}

/// Locate a child by node address rather than by index or key.
///
/// The target is a `*const CJson` used purely as an identity token: it is
/// compared, never dereferenced, so any pointer value (including a dangling
/// one) is sound and no `unsafe` is required. The caller obtains it from a
/// shared borrow that ends before the mutable borrow starts, which is why this
/// works over the exclusive-`Box` tree without handles or interior mutability.
fn child_slot_by_address(
    parent: &mut CJson,
    target: *const CJson,
) -> Option<&mut Option<Box<CJson>>> {
    let mut link = &mut parent.child;
    loop {
        let matches = link
            .as_deref()
            .is_some_and(|node| std::ptr::eq(node as *const CJson, target));
        if matches {
            return Some(link);
        }
        link = &mut link.as_mut()?.next;
    }
}

fn detach_from_slot(slot: &mut Option<Box<CJson>>) -> Option<Box<CJson>> {
    let mut detached = slot.take()?;
    *slot = detached.next.take();
    Some(detached)
}

/// `cJSON_DetachItemFromArray`. The returned box is caller-owned again.
pub fn detach_item_from_array(array: &mut CJson, index: i32) -> Option<Box<CJson>> {
    detach_from_slot(child_slot_at(array, index)?)
}

/// `cJSON_DetachItemFromObject` (ASCII case-insensitive key matching). The
/// returned box is caller-owned again.
pub fn detach_item_from_object(object: &mut CJson, key: impl AsRef<[u8]>) -> Option<Box<CJson>> {
    detach_from_slot(object_child_slot(object, key.as_ref(), false)?)
}

/// `cJSON_DetachItemFromObjectCaseSensitive`. The returned box is caller-owned
/// again.
pub fn detach_item_from_object_case_sensitive(
    object: &mut CJson,
    key: impl AsRef<[u8]>,
) -> Option<Box<CJson>> {
    detach_from_slot(object_child_slot(object, key.as_ref(), true)?)
}

/// `cJSON_DetachItemViaPointer`. The returned box is caller-owned again, and it
/// is the same allocation the caller identified, as in C.
pub fn detach_item_via_pointer(parent: &mut CJson, target: *const CJson) -> Option<Box<CJson>> {
    detach_from_slot(child_slot_by_address(parent, target)?)
}

/// `cJSON_DeleteItemFromArray`. A successful detach is immediately dropped.
pub fn delete_item_from_array(array: &mut CJson, index: i32) -> bool {
    detach_item_from_array(array, index).is_some()
}

/// `cJSON_DeleteItemFromObject` (ASCII case-insensitive key matching).
pub fn delete_item_from_object(object: &mut CJson, key: impl AsRef<[u8]>) -> bool {
    detach_item_from_object(object, key).is_some()
}

/// `cJSON_DeleteItemFromObjectCaseSensitive`.
pub fn delete_item_from_object_case_sensitive(object: &mut CJson, key: impl AsRef<[u8]>) -> bool {
    detach_item_from_object_case_sensitive(object, key).is_some()
}

fn replace_at_slot(slot: &mut Option<Box<CJson>>, mut replacement: Box<CJson>) {
    let mut old = slot
        .take()
        .expect("replace slot was checked to contain an item");
    replacement.next = old.next.take();
    *slot = Some(replacement);
    // `old` drops here, mirroring cJSON_ReplaceItemViaPointer's cJSON_Delete.
}

/// `cJSON_ReplaceItemInArray`.
///
/// A C `false` result leaves `newitem` caller-owned. Rust makes that ownership
/// explicit: the `Err` value is the untouched replacement box.
pub fn replace_item_in_array(
    array: &mut CJson,
    index: i32,
    newitem: Box<CJson>,
) -> Result<(), Box<CJson>> {
    let Some(slot) = child_slot_at(array, index) else {
        return Err(newitem);
    };
    replace_at_slot(slot, newitem);
    Ok(())
}

/// `cJSON_ReplaceItemViaPointer`. Identity is the node address, as in C; on a
/// miss the replacement box is returned untouched instead of leaking.
pub fn replace_item_via_pointer(
    parent: &mut CJson,
    target: *const CJson,
    newitem: Box<CJson>,
) -> Result<(), Box<CJson>> {
    let Some(slot) = child_slot_by_address(parent, target) else {
        return Err(newitem);
    };
    replace_at_slot(slot, newitem);
    Ok(())
}

fn replace_item_in_object_impl(
    object: &mut CJson,
    key: &[u8],
    mut newitem: Box<CJson>,
    case_sensitive: bool,
) -> Result<(), Box<CJson>> {
    let Some(slot) = object_child_slot(object, key, case_sensitive) else {
        return Err(newitem);
    };
    // cJSON copies the supplied lookup key into the replacement before linking.
    newitem.string = Some(key.to_vec());
    replace_at_slot(slot, newitem);
    Ok(())
}

/// `cJSON_ReplaceItemInObject` (ASCII case-insensitive key matching).
pub fn replace_item_in_object(
    object: &mut CJson,
    key: impl AsRef<[u8]>,
    newitem: Box<CJson>,
) -> Result<(), Box<CJson>> {
    replace_item_in_object_impl(object, key.as_ref(), newitem, false)
}

/// `cJSON_ReplaceItemInObjectCaseSensitive`.
pub fn replace_item_in_object_case_sensitive(
    object: &mut CJson,
    key: impl AsRef<[u8]>,
    newitem: Box<CJson>,
) -> Result<(), Box<CJson>> {
    replace_item_in_object_impl(object, key.as_ref(), newitem, true)
}

fn duplicate_child_chain(first: Option<&CJson>) -> Option<Box<CJson>> {
    let mut copy = None;
    let mut tail = &mut copy;
    let mut current = first;
    while let Some(item) = current {
        *tail = Some(duplicate_item(item, true));
        if let Some(inserted) = tail {
            tail = &mut inserted.next;
        }
        current = item.next.as_deref();
    }
    copy
}

fn duplicate_item(item: &CJson, recurse: bool) -> Box<CJson> {
    Box::new(CJson {
        child: if recurse {
            duplicate_child_chain(item.child.as_deref())
        } else {
            None
        },
        // cJSON_Duplicate always returns an unlinked root, even when the source
        // happens to be a member of a sibling chain.
        next: None,
        type_: item.type_,
        valuestring: item.valuestring.clone(),
        valueint: item.valueint,
        valuedouble: item.valuedouble,
        string: item.string.clone(),
    })
}

/// `cJSON_Duplicate`.
///
/// The returned tree owns cloned fields and children. With `recurse == false`,
/// it copies only the supplied node (never that node's sibling chain).
pub fn duplicate(item: &CJson, recurse: bool) -> Box<CJson> {
    duplicate_item(item, recurse)
}

fn object_item_by_key<'a>(
    object: &'a CJson,
    key: &[u8],
    case_sensitive: bool,
) -> Option<&'a CJson> {
    let mut current = object.child.as_deref();
    while let Some(item) = current {
        if let Some(candidate) = item.string.as_deref() {
            let matches = if case_sensitive {
                candidate == key
            } else {
                candidate.eq_ignore_ascii_case(key)
            };
            if matches {
                return Some(item);
            }
        }
        current = item.next.as_deref();
    }
    None
}

fn compare_sibling_chains(
    mut left: Option<&CJson>,
    mut right: Option<&CJson>,
    case_sensitive: bool,
) -> bool {
    loop {
        match (left, right) {
            (None, None) => return true,
            (Some(a), Some(b)) if compare(a, b, case_sensitive) => {
                left = a.next.as_deref();
                right = b.next.as_deref();
            }
            _ => return false,
        }
    }
}

fn compare_objects(left: &CJson, right: &CJson, case_sensitive: bool) -> bool {
    let mut current = left.child.as_deref();
    while let Some(item) = current {
        let Some(key) = item.string.as_deref() else {
            return false;
        };
        let Some(other) = object_item_by_key(right, key, case_sensitive) else {
            return false;
        };
        if !compare(item, other, case_sensitive) {
            return false;
        }
        current = item.next.as_deref();
    }

    // cJSON compares both directions, so a right-hand extra key cannot match
    // merely because every left-hand key was found.
    current = right.child.as_deref();
    while let Some(item) = current {
        let Some(key) = item.string.as_deref() else {
            return false;
        };
        if object_item_by_key(left, key, case_sensitive).is_none() {
            return false;
        }
        current = item.next.as_deref();
    }
    true
}

/// `cJSON_Compare` for non-null Rust nodes.
///
/// Arrays compare in order. Objects compare by key, independently of member
/// order; `case_sensitive` controls ASCII key matching as in cJSON.
pub fn compare(left: &CJson, right: &CJson, case_sensitive: bool) -> bool {
    let left_type = left.type_ & 0xff;
    if left_type != (right.type_ & 0xff) {
        return false;
    }
    match left_type {
        CJSON_FALSE | CJSON_TRUE | CJSON_NULL => true,
        CJSON_NUMBER => compare_double(left.valuedouble, right.valuedouble),
        CJSON_STRING | CJSON_RAW => left.valuestring == right.valuestring,
        CJSON_ARRAY => compare_sibling_chains(
            left.child.as_deref(),
            right.child.as_deref(),
            case_sensitive,
        ),
        CJSON_OBJECT => compare_objects(left, right, case_sensitive),
        _ => false,
    }
}

/// `cJSON_CreateIntArray`.
pub fn create_int_array(numbers: &[i32]) -> Box<CJson> {
    let mut array = create_array();
    for &number in numbers {
        add_item_to_array(&mut array, create_number(number as f64));
    }
    array
}

/// `cJSON_CreateFloatArray`.
pub fn create_float_array(numbers: &[f32]) -> Box<CJson> {
    let mut array = create_array();
    for &number in numbers {
        add_item_to_array(&mut array, create_number(number as f64));
    }
    array
}

/// `cJSON_CreateDoubleArray`.
pub fn create_double_array(numbers: &[f64]) -> Box<CJson> {
    let mut array = create_array();
    for &number in numbers {
        add_item_to_array(&mut array, create_number(number));
    }
    array
}

/// `cJSON_CreateStringArray`.
pub fn create_string_array<I, S>(strings: I) -> Box<CJson>
where
    I: IntoIterator<Item = S>,
    S: AsRef<[u8]>,
{
    let mut array = create_array();
    for string in strings {
        add_item_to_array(&mut array, create_string(string));
    }
    array
}

/* ---------- parse ---------- */

struct ParseBuffer<'a> {
    content: &'a [u8],
    length: usize,
    offset: usize,
    depth: usize,
}

impl<'a> ParseBuffer<'a> {
    // All index math is wrapping to mirror cJSON's `size_t` arithmetic: a few
    // malformed-input paths transiently set offset to SIZE_MAX before stepping
    // it back, exactly as the C does.
    fn can_read(&self, size: usize) -> bool {
        self.offset.wrapping_add(size) <= self.length
    }
    fn can_access(&self, index: usize) -> bool {
        self.offset.wrapping_add(index) < self.length
    }
    fn byte(&self, index: usize) -> u8 {
        self.content[self.offset.wrapping_add(index)]
    }
    fn dec(&mut self) {
        self.offset = self.offset.wrapping_sub(1);
    }
    fn inc(&mut self) {
        self.offset = self.offset.wrapping_add(1);
    }

    fn skip_whitespace(&mut self) {
        while self.can_access(0) && self.byte(0) <= 32 {
            self.inc();
        }
        if self.offset == self.length {
            self.dec();
        }
    }

    fn skip_utf8_bom(&mut self) {
        if self.offset == 0 && self.can_access(4) && self.content[0..3] == [0xEF, 0xBB, 0xBF] {
            self.offset += 3;
        }
    }

    fn parse_value(&mut self, item: &mut CJson) -> bool {
        if self.can_read(4) && &self.content[self.offset..self.offset + 4] == b"null" {
            item.type_ = CJSON_NULL;
            self.offset += 4;
            return true;
        }
        if self.can_read(5) && &self.content[self.offset..self.offset + 5] == b"false" {
            item.type_ = CJSON_FALSE;
            self.offset += 5;
            return true;
        }
        if self.can_read(4) && &self.content[self.offset..self.offset + 4] == b"true" {
            item.type_ = CJSON_TRUE;
            item.valueint = 1;
            self.offset += 4;
            return true;
        }
        if self.can_access(0) && self.byte(0) == b'"' {
            return self.parse_string(item);
        }
        if self.can_access(0) && (self.byte(0) == b'-' || self.byte(0).is_ascii_digit()) {
            return self.parse_number(item);
        }
        if self.can_access(0) && self.byte(0) == b'[' {
            return self.parse_array(item);
        }
        if self.can_access(0) && self.byte(0) == b'{' {
            return self.parse_object(item);
        }
        false
    }

    fn parse_number(&mut self, item: &mut CJson) -> bool {
        let start = self.offset;
        let mut end = self.offset;
        while end < self.length {
            match self.content[end] {
                b'0'..=b'9' | b'+' | b'-' | b'e' | b'E' | b'.' => end += 1,
                _ => break,
            }
        }
        let text = match std::str::from_utf8(&self.content[start..end]) {
            Ok(t) => t,
            Err(_) => return false,
        };
        let number: f64 = match text.parse() {
            Ok(n) => n,
            Err(_) => return false,
        };
        item.valuedouble = number;
        if number >= i32::MAX as f64 {
            item.valueint = i32::MAX;
        } else if number <= i32::MIN as f64 {
            item.valueint = i32::MIN;
        } else {
            item.valueint = number as i32;
        }
        item.type_ = CJSON_NUMBER;
        self.offset = end;
        true
    }

    fn parse_string(&mut self, item: &mut CJson) -> bool {
        if !self.can_access(0) || self.byte(0) != b'"' {
            return false;
        }
        // Find the closing quote, honoring escapes.
        let mut e = self.offset + 1;
        while e < self.length && self.content[e] != b'"' {
            if self.content[e] == b'\\' {
                if e + 1 >= self.length {
                    return false;
                }
                e += 1;
            }
            e += 1;
        }
        if e >= self.length || self.content[e] != b'"' {
            return false;
        }
        let end = e; // index of the closing quote

        let mut out: Vec<u8> = Vec::new();
        let mut i = self.offset + 1;
        while i < end {
            if self.content[i] != b'\\' {
                out.push(self.content[i]);
                i += 1;
            } else {
                match self.content[i + 1] {
                    b'b' => {
                        out.push(0x08);
                        i += 2;
                    }
                    b'f' => {
                        out.push(0x0c);
                        i += 2;
                    }
                    b'n' => {
                        out.push(b'\n');
                        i += 2;
                    }
                    b'r' => {
                        out.push(b'\r');
                        i += 2;
                    }
                    b't' => {
                        out.push(b'\t');
                        i += 2;
                    }
                    b'"' | b'\\' | b'/' => {
                        out.push(self.content[i + 1]);
                        i += 2;
                    }
                    b'u' => {
                        let seq = utf16_literal_to_utf8(self.content, i, end, &mut out);
                        if seq == 0 {
                            return false;
                        }
                        i += seq;
                    }
                    _ => return false,
                }
            }
        }

        item.type_ = CJSON_STRING;
        item.valuestring = Some(out);
        self.offset = end + 1;
        true
    }

    fn parse_array(&mut self, item: &mut CJson) -> bool {
        if self.depth >= NESTING_LIMIT {
            return false;
        }
        self.depth += 1;
        // caller verified '['
        self.inc();
        self.skip_whitespace();
        if self.can_access(0) && self.byte(0) == b']' {
            self.depth -= 1;
            item.type_ = CJSON_ARRAY;
            self.inc();
            return true;
        }
        if !self.can_access(0) {
            self.dec();
            self.depth -= 1;
            return false;
        }
        self.dec(); // step back before first element
        let mut children: Vec<CJson> = Vec::new();
        loop {
            let mut new_item = CJson::new();
            self.inc();
            self.skip_whitespace();
            if !self.parse_value(&mut new_item) {
                self.depth -= 1;
                return false; // children drop here, freeing what was parsed
            }
            children.push(new_item);
            self.skip_whitespace();
            if !(self.can_access(0) && self.byte(0) == b',') {
                break;
            }
        }
        if !self.can_access(0) || self.byte(0) != b']' {
            self.depth -= 1;
            return false;
        }
        self.depth -= 1;
        item.type_ = CJSON_ARRAY;
        item.child = link(children);
        self.inc();
        true
    }

    fn parse_object(&mut self, item: &mut CJson) -> bool {
        if self.depth >= NESTING_LIMIT {
            return false;
        }
        self.depth += 1;
        if !self.can_access(0) || self.byte(0) != b'{' {
            self.depth -= 1;
            return false;
        }
        self.inc();
        self.skip_whitespace();
        if self.can_access(0) && self.byte(0) == b'}' {
            self.depth -= 1;
            item.type_ = CJSON_OBJECT;
            self.inc();
            return true;
        }
        if !self.can_access(0) {
            self.dec();
            self.depth -= 1;
            return false;
        }
        self.dec(); // step back before first element
        let mut children: Vec<CJson> = Vec::new();
        loop {
            let mut new_item = CJson::new();
            if !self.can_access(1) {
                self.depth -= 1;
                return false;
            }
            self.inc();
            self.skip_whitespace();
            if !self.parse_string(&mut new_item) {
                self.depth -= 1;
                return false;
            }
            self.skip_whitespace();
            // swap valuestring -> string (we parsed the key)
            new_item.string = new_item.valuestring.take();
            if !self.can_access(0) || self.byte(0) != b':' {
                self.depth -= 1;
                return false;
            }
            self.inc();
            self.skip_whitespace();
            if !self.parse_value(&mut new_item) {
                self.depth -= 1;
                return false;
            }
            children.push(new_item);
            self.skip_whitespace();
            if !(self.can_access(0) && self.byte(0) == b',') {
                break;
            }
        }
        if !self.can_access(0) || self.byte(0) != b'}' {
            self.depth -= 1;
            return false;
        }
        self.depth -= 1;
        item.type_ = CJSON_OBJECT;
        item.child = link(children);
        self.inc();
        true
    }
}

/// Chain a vector of children into a `Box`-owned `next` list, preserving order.
fn link(mut children: Vec<CJson>) -> Option<Box<CJson>> {
    let mut head: Option<Box<CJson>> = None;
    while let Some(mut node) = children.pop() {
        node.next = head;
        head = Some(Box::new(node));
    }
    head
}

fn parse_hex4(input: &[u8]) -> u32 {
    let mut h: u32 = 0;
    for (i, &c) in input.iter().take(4).enumerate() {
        h += match c {
            b'0'..=b'9' => (c - b'0') as u32,
            b'A'..=b'F' => 10 + (c - b'A') as u32,
            b'a'..=b'f' => 10 + (c - b'a') as u32,
            _ => return 0,
        };
        if i < 3 {
            h <<= 4;
        }
    }
    h
}

/// Port of `utf16_literal_to_utf8`. `idx` points at the backslash of `\uXXXX`,
/// `end` is the closing-quote index. Returns input bytes consumed (6 or 12), or
/// 0 on failure.
fn utf16_literal_to_utf8(c: &[u8], idx: usize, end: usize, out: &mut Vec<u8>) -> usize {
    if end < idx + 6 {
        return 0;
    }
    let first = parse_hex4(&c[idx + 2..]);
    if (0xDC00..=0xDFFF).contains(&first) {
        return 0;
    }
    let (codepoint, seq_len) = if (0xD800..=0xDBFF).contains(&first) {
        let second = idx + 6;
        if end < second + 6 {
            return 0;
        }
        if c[second] != b'\\' || c[second + 1] != b'u' {
            return 0;
        }
        let second_code = parse_hex4(&c[second + 2..]);
        if !(0xDC00..=0xDFFF).contains(&second_code) {
            return 0;
        }
        (
            0x10000 + (((first & 0x3FF) << 10) | (second_code & 0x3FF)),
            12,
        )
    } else {
        (first, 6)
    };
    match char::from_u32(codepoint) {
        Some(ch) => {
            let mut buf = [0u8; 4];
            out.extend_from_slice(ch.encode_utf8(&mut buf).as_bytes());
            seq_len
        }
        None => 0,
    }
}

/// Typed equivalent of `cJSON_ParseWithLengthOpts` / `cJSON_ParseWithOpts`.
///
/// The returned offset is the next byte after the parsed value (or the trailing
/// NUL after whitespace when `require_null_terminated` is true). A Rust slice
/// supplies C's explicit length; callers that request C's NUL-termination rule
/// must include that terminating `0` byte in the slice.
pub fn parse_with_opts(
    input: &[u8],
    require_null_terminated: bool,
) -> Result<(Box<CJson>, usize), usize> {
    if input.is_empty() {
        return Err(0);
    }
    let mut buffer = ParseBuffer {
        content: input,
        length: input.len(),
        offset: 0,
        depth: 0,
    };
    let mut item = Box::new(CJson::new());
    buffer.skip_utf8_bom();
    buffer.skip_whitespace();
    if !buffer.parse_value(&mut item) {
        return Err(buffer.offset.min(input.len().saturating_sub(1)));
    }

    if require_null_terminated {
        while buffer.offset < buffer.length
            && buffer.content[buffer.offset] != 0
            && buffer.content[buffer.offset] <= 32
        {
            buffer.offset += 1;
        }
        if buffer.offset >= buffer.length || buffer.content[buffer.offset] != 0 {
            return Err(buffer.offset.min(input.len().saturating_sub(1)));
        }
    }
    Ok((item, buffer.offset))
}

/// `cJSON_ParseWithLength` (default opts: not requiring null termination).
pub fn parse(input: &[u8]) -> Option<Box<CJson>> {
    parse_with_opts(input, false).ok().map(|(item, _)| item)
}

/* ---------- print ---------- */

/// Relative comparison matching cJSON's `compare_double`. It is used by both
/// the libc print fallback and the public structural comparison API, so it must
/// remain available under Miri as well.
fn compare_double(a: f64, b: f64) -> bool {
    let max_val = a.abs().max(b.abs());
    (a - b).abs() <= max_val * f64::EPSILON
}

/// Format `d` with libc `snprintf` using a C printf conversion (`%1.15g` or
/// `%1.17g`). Returns the number of bytes written into `buf` (excluding NUL),
/// or `None` if snprintf failed / truncated past the cJSON 26-byte temp buffer.
///
/// Why libc rather than `format!("{d}")` or a pure-Rust dtoa: cJSON's oracle is
/// the platform C library's printf. Rust's `Display` for `f64` is a different
/// algorithm and produces different bytes (e.g. pi, 1/3, min-normal). Calling
/// the same snprintf the C code calls is the faithful match, not a divergence.
///
/// Under Miri (`cfg(miri)`) this is not compiled: Miri cannot execute foreign
/// `snprintf`/`sscanf`. The non-Miri build is unchanged — no feature flag, no
/// runtime branch — so the 52 golden cases stay byte-identical to the C oracle.
#[cfg(not(miri))]
fn c_sprintf_g(d: f64, precision: u8, buf: &mut [u8; 26]) -> Option<usize> {
    use std::os::raw::{c_char, c_double, c_int};

    extern "C" {
        fn snprintf(s: *mut c_char, n: usize, format: *const c_char, ...) -> c_int;
    }

    // Formats are compile-time constants with a trailing NUL for C.
    let fmt: &[u8] = match precision {
        15 => b"%1.15g\0",
        17 => b"%1.17g\0",
        _ => return None,
    };
    // Zero the buffer so a failed write cannot leave stale non-NUL bytes.
    *buf = [0u8; 26];
    let n = unsafe {
        snprintf(
            buf.as_mut_ptr() as *mut c_char,
            buf.len(),
            fmt.as_ptr() as *const c_char,
            d as c_double,
        )
    };
    if n < 0 || (n as usize) > buf.len() - 1 {
        return None;
    }
    Some(n as usize)
}

/// Re-parse a C-printed number buffer with libc `sscanf("%lg")`, matching
/// cJSON's recovery check before it decides to fall back to 17 digits.
#[cfg(not(miri))]
fn c_sscanf_lg(buf: &[u8]) -> Option<f64> {
    use std::os::raw::{c_char, c_double, c_int};

    extern "C" {
        fn sscanf(s: *const c_char, format: *const c_char, ...) -> c_int;
    }

    // buf is a snprintf result: digits and a trailing NUL within 26 bytes.
    let mut test: c_double = 0.0;
    let scanned = unsafe {
        sscanf(
            buf.as_ptr() as *const c_char,
            b"%lg\0".as_ptr() as *const c_char,
            &mut test as *mut c_double,
        )
    };
    if scanned != 1 {
        return None;
    }
    Some(test as f64)
}

fn print_number(item: &CJson, out: &mut Vec<u8>) {
    let d = item.valuedouble;
    // NaN / Inf → JSON null (cJSON print_number).
    if d.is_nan() || d.is_infinite() {
        out.extend_from_slice(b"null");
        return;
    }
    // Exact integer values take the `%d` path (incl. -0.0, since -0.0 == 0.0).
    if d == item.valueint as f64 {
        out.extend_from_slice(item.valueint.to_string().as_bytes());
        return;
    }

    // Non-integer path: platform libc under normal builds; pure-Rust Display
    // under Miri only (see cfg below). Normal builds keep C byte-parity.
    print_number_non_integer(d, out);
}

/// cJSON's two-step `%1.15g` → `%1.17g` printer via libc (golden-faithful).
#[cfg(not(miri))]
fn print_number_non_integer(d: f64, out: &mut Vec<u8>) {
    // 26 bytes is the buffer cJSON.c itself uses; %1.17g of a finite double never
    // fills it. A failure here is a bug in this file, not a runtime condition --
    // returning silently would emit a number-less, invalid JSON document.
    let mut number_buffer = [0u8; 26];
    let mut length = c_sprintf_g(d, 15, &mut number_buffer)
        .expect("26-byte buffer holds %1.15g of a finite double, as in cJSON.c");
    let recovered = c_sscanf_lg(&number_buffer).filter(|&t| compare_double(t, d));
    if recovered.is_none() {
        length = c_sprintf_g(d, 17, &mut number_buffer)
            .expect("26-byte buffer holds %1.17g of a finite double, as in cJSON.c");
    }

    // cJSON replaces a locale decimal point with '.'; default build has no
    // ENABLE_LOCALES, so snprintf already emits '.'. Copy bytes as-is.
    out.extend_from_slice(&number_buffer[..length]);
}

/// Miri-only stand-in for the non-integer branch. Miri cannot execute libc
/// `snprintf`/`sscanf`, so we use Rust `Display` solely so ownership tests can
/// run under Miri. This path is **not** golden-faithful and is never compiled
/// into the normal (non-Miri) binary — float-print fidelity remains a libc check.
#[cfg(miri)]
fn print_number_non_integer(d: f64, out: &mut Vec<u8>) {
    out.extend_from_slice(format!("{d}").as_bytes());
}

fn print_string_ptr(s: Option<&[u8]>, out: &mut Vec<u8>) {
    let input = match s {
        None => {
            out.extend_from_slice(b"\"\"");
            return;
        }
        Some(b) => b,
    };
    out.push(b'"');
    for &c in input {
        if c > 31 && c != b'"' && c != b'\\' {
            out.push(c);
        } else {
            out.push(b'\\');
            match c {
                b'\\' => out.push(b'\\'),
                b'"' => out.push(b'"'),
                0x08 => out.push(b'b'),
                0x0c => out.push(b'f'),
                b'\n' => out.push(b'n'),
                b'\r' => out.push(b'r'),
                b'\t' => out.push(b't'),
                _ => out.extend_from_slice(format!("u{c:04x}").as_bytes()),
            }
        }
    }
    out.push(b'"');
}

fn print_value(item: &CJson, out: &mut Vec<u8>, format: bool, depth: usize) {
    match item.type_ & 0xff {
        CJSON_NULL => out.extend_from_slice(b"null"),
        CJSON_FALSE => out.extend_from_slice(b"false"),
        CJSON_TRUE => out.extend_from_slice(b"true"),
        CJSON_NUMBER => print_number(item, out),
        CJSON_RAW => {
            if let Some(raw) = item.valuestring.as_deref() {
                out.extend_from_slice(raw);
            }
        }
        CJSON_STRING => print_string_ptr(item.valuestring.as_deref(), out),
        CJSON_ARRAY => print_array(item, out, format, depth),
        CJSON_OBJECT => print_object(item, out, format, depth),
        _ => {}
    }
}

fn print_array(item: &CJson, out: &mut Vec<u8>, format: bool, depth: usize) {
    out.push(b'[');
    let mut cur = item.child.as_deref();
    while let Some(node) = cur {
        print_value(node, out, format, depth + 1);
        if node.next.is_some() {
            out.push(b',');
            if format {
                out.push(b' ');
            }
        }
        cur = node.next.as_deref();
    }
    out.push(b']');
}

fn print_object(item: &CJson, out: &mut Vec<u8>, format: bool, depth: usize) {
    let d = depth + 1;
    out.push(b'{');
    if format {
        out.push(b'\n');
    }
    let mut cur = item.child.as_deref();
    while let Some(node) = cur {
        if format {
            for _ in 0..d {
                out.push(b'\t');
            }
        }
        print_string_ptr(node.string.as_deref(), out);
        out.push(b':');
        if format {
            out.push(b'\t');
        }
        print_value(node, out, format, d);
        if node.next.is_some() {
            out.push(b',');
        }
        if format {
            out.push(b'\n');
        }
        cur = node.next.as_deref();
    }
    if format {
        for _ in 0..(d - 1) {
            out.push(b'\t');
        }
    }
    out.push(b'}');
}

/// `cJSON_PrintUnformatted`.
pub fn print_unformatted(item: &CJson) -> Vec<u8> {
    print_buffered(item, 0, false)
}

/// `cJSON_Print` (formatted).
pub fn print_formatted(item: &CJson) -> Vec<u8> {
    print_buffered(item, 0, true)
}

/// `cJSON_PrintBuffered`. Rust exposes the prebuffer as a non-negative
/// capacity hint and returns owned bytes, avoiding C's caller-allocator rule.
pub fn print_buffered(item: &CJson, prebuffer: usize, formatted: bool) -> Vec<u8> {
    let mut out = Vec::with_capacity(prebuffer);
    print_value(item, &mut out, formatted, 0);
    out
}

/// `cJSON_PrintPreallocated`.
///
/// On success it writes the NUL-terminated result and leaves bytes after that
/// terminator untouched. On failure cJSON may already have written a prefix;
/// its preallocated path reserves four bytes of safety slack, so this typed
/// adaptation copies that same bounded prefix without adding a terminator.
pub fn print_preallocated(item: &CJson, buffer: &mut [u8], formatted: bool) -> bool {
    let rendered = print_buffered(item, buffer.len(), formatted);
    let Some(required) = rendered.len().checked_add(1) else {
        return false;
    };
    if required > buffer.len() {
        let prefix = rendered.len().min(buffer.len().saturating_sub(4));
        buffer[..prefix].copy_from_slice(&rendered[..prefix]);
        return false;
    }
    buffer[..rendered.len()].copy_from_slice(&rendered);
    buffer[rendered.len()] = 0;
    true
}

/// `cJSON_Minify` with a Rust-owned byte buffer. Whitespace and C/JSON-style
/// comments outside strings are removed; string bytes and escapes are retained.
pub fn minify(json: &mut Vec<u8>) {
    let mut read = 0usize;
    let mut write = 0usize;
    let mut in_string = false;
    let mut escaped = false;
    while read < json.len() {
        let byte = json[read];
        if in_string {
            json[write] = byte;
            write += 1;
            read += 1;
            if escaped {
                escaped = false;
            } else if byte == b'\\' {
                escaped = true;
            } else if byte == b'"' {
                in_string = false;
            }
            continue;
        }
        if byte <= b' ' {
            read += 1;
            continue;
        }
        if byte == b'/' && read + 1 < json.len() {
            match json[read + 1] {
                b'/' => {
                    read += 2;
                    while read < json.len() && json[read] != b'\n' && json[read] != b'\r' {
                        read += 1;
                    }
                    continue;
                }
                b'*' => {
                    read += 2;
                    while read + 1 < json.len() && !(json[read] == b'*' && json[read + 1] == b'/') {
                        read += 1;
                    }
                    if read + 1 < json.len() {
                        read += 2;
                    }
                    continue;
                }
                _ => {}
            }
        }
        json[write] = byte;
        write += 1;
        read += 1;
        if byte == b'"' {
            in_string = true;
        }
    }
    json.truncate(write);
}

/* ---------- inspect (canonical descriptor via the getter API) ---------- */

fn json_escape(s: &[u8], out: &mut Vec<u8>) {
    out.push(b'"');
    for &c in s {
        match c {
            b'"' => out.extend_from_slice(b"\\\""),
            b'\\' => out.extend_from_slice(b"\\\\"),
            b'\n' => out.extend_from_slice(b"\\n"),
            b'\r' => out.extend_from_slice(b"\\r"),
            b'\t' => out.extend_from_slice(b"\\t"),
            0..=0x1f => out.extend_from_slice(format!("\\u{c:04x}").as_bytes()),
            _ => out.push(c),
        }
    }
    out.push(b'"');
}

fn describe(item: &CJson, out: &mut Vec<u8>) {
    if is_null(item) {
        out.extend_from_slice(b"{\"t\":\"null\"}");
    } else if is_bool(item) {
        out.extend_from_slice(b"{\"t\":\"bool\",\"v\":");
        out.extend_from_slice(if is_true(item) { b"true" } else { b"false" });
        out.push(b'}');
    } else if is_number(item) {
        out.extend_from_slice(
            format!(
                "{{\"t\":\"num\",\"i\":{},\"bits\":{}}}",
                item.valueint,
                item.valuedouble.to_bits()
            )
            .as_bytes(),
        );
    } else if is_string(item) {
        out.extend_from_slice(b"{\"t\":\"str\",\"v\":");
        json_escape(get_string_value(item).unwrap_or(b""), out);
        out.push(b'}');
    } else if is_raw(item) {
        out.extend_from_slice(b"{\"t\":\"raw\",\"v\":");
        json_escape(item.valuestring.as_deref().unwrap_or(b""), out);
        out.push(b'}');
    } else if is_array(item) {
        let n = get_array_size(item);
        out.extend_from_slice(format!("{{\"t\":\"arr\",\"n\":{n},\"items\":[").as_bytes());
        for i in 0..n {
            if i > 0 {
                out.push(b',');
            }
            describe(get_array_item(item, i).expect("index < size"), out);
        }
        out.extend_from_slice(b"]}");
    } else if is_object(item) {
        let n = get_array_size(item);
        out.extend_from_slice(format!("{{\"t\":\"obj\",\"n\":{n},\"members\":[").as_bytes());
        for i in 0..n {
            if i > 0 {
                out.push(b',');
            }
            let child = get_array_item(item, i).expect("index < size");
            out.extend_from_slice(b"{\"k\":");
            json_escape(child.string.as_deref().unwrap_or(b""), out);
            out.extend_from_slice(b",\"v\":");
            describe(child, out);
            out.push(b'}');
        }
        out.extend_from_slice(b"]}");
    } else {
        out.extend_from_slice(b"{\"t\":\"invalid\"}");
    }
}

/// Canonical tree descriptor built from the public getter API, matching the C
/// golden runner's `inspect` oracle byte-for-byte (numbers carry valueint plus
/// the IEEE-754 bits of valuedouble).
pub fn inspect(item: &CJson) -> Vec<u8> {
    let mut out = Vec::new();
    describe(item, &mut out);
    out
}
