# cJSON C → Rust API-surface audit

This is a mechanically generated inventory of the vendored public header,
not a recollection of the currently ported slice. The source of truth is
`examples/cjson/cJSON.h`; classifications are deliberately exhaustive and
fail closed when that header changes.

## Reproduce and enforce

```bash
uv run python examples/cjson/tools/api_surface_audit.py --check
uv run python examples/cjson/tools/api_surface_audit.py --write
```

The complete C-oracle/Rust-port evidence chain is
`examples/cjson/tools/check_port.sh --full`; its quick mode omits only
Miri and reports unavailable C/ASan or nightly-Miri tooling as explicit skips.

`--check` parses the header, validates that every parsed entry has exactly
one classification, and compares this file to the generated result. The
pytest audit test also copies and corrupts the header to prove that the
enumeration is input-derived. Every excluded function maps to a named C
C-oracle refusal trace; the test executes each trace, asserts that its named
operation was reached, and runs those traces under ASan when available.

## Quantified boundary

The header currently has **78 functions** and **23
public constants/macros/limits/types**. Of the functions, **68 are covered**, **6 are blocked by the exclusive `Box` representation**, and **4 are
excluded for process-global C allocator/error state rather than ownership**.
There are **no silently deferred function entries**. The separate structural
boundary is `cJSON.prev` plus the `cJSON_IsReference` and
`cJSON_StringIsConst` flags: all require non-owning aliases/identity beyond
the acyclic owned tree. The C traces are evidence for the boundary, not just
a list of names.

## Functions (from `CJSON_PUBLIC` declarations)

| Header entry | Rust status / required representation change | Category |
| --- | --- | --- |
| `cJSON_Version` | Rust `version` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_InitHooks` | C refusal trace: `custom_hooks`. C installs process-global malloc/free callbacks. Closing it needs an allocator parameter threaded through every allocation or an explicitly unsafe/global allocator policy; it is not blocked by tree ownership. | Deliberately excluded – global/process state |
| `cJSON_Parse` | `parse`; owned byte slice input. | Covered |
| `cJSON_ParseWithLength` | `parse`; a Rust slice already carries its exact length. | Covered |
| `cJSON_ParseWithOpts` | `parse_with_opts`; returns an owned tree plus parse-end/error offset. | Covered |
| `cJSON_ParseWithLengthOpts` | `parse_with_opts`; the Rust slice supplies `buffer_length`. | Covered |
| `cJSON_Print` | `print_formatted` returns owned bytes. | Covered |
| `cJSON_PrintUnformatted` | `print_unformatted` returns owned bytes. | Covered |
| `cJSON_PrintBuffered` | `print_buffered`; prebuffer is a capacity hint. | Covered |
| `cJSON_PrintPreallocated` | `print_preallocated` writes a caller-owned mutable buffer and reports fit. | Covered |
| `cJSON_Delete` | ordinary `drop(Box<CJson>)`; `delete` is also supplied for C-shaped migration code. | Covered |
| `cJSON_GetArraySize` | Rust `get_array_size` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_GetArrayItem` | Rust `get_array_item` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_GetObjectItem` | Rust `get_object_item` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_GetObjectItemCaseSensitive` | Rust `get_object_item_case_sensitive` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_HasObjectItem` | Rust `has_object_item` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_GetErrorPtr` | C refusal trace: `error_pointer`. C exposes a process-global borrowed pointer to the most recent parse error. Rust returns an error offset from `parse_with_opts`; reproducing the global pointer requires thread/global mutable error state and borrowed input lifetime. | Deliberately excluded – global/process state |
| `cJSON_GetStringValue` | Rust `get_string_value` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_GetNumberValue` | Rust `get_number_value` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_IsInvalid` | Rust `is_invalid` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_IsFalse` | Rust `is_false` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_IsTrue` | Rust `is_true` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_IsBool` | Rust `is_bool` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_IsNull` | Rust `is_null` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_IsNumber` | Rust `is_number` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_IsString` | Rust `is_string` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_IsArray` | Rust `is_array` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_IsObject` | Rust `is_object` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_IsRaw` | Rust `is_raw` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_CreateNull` | Rust `create_null` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_CreateTrue` | Rust `create_true` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_CreateFalse` | Rust `create_false` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_CreateBool` | Rust `create_bool` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_CreateNumber` | Rust `create_number` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_CreateString` | Rust `create_string` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_CreateRaw` | Rust `create_raw` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_CreateArray` | Rust `create_array` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_CreateObject` | Rust `create_object` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_CreateStringReference` | C refusal trace: `string_reference`. A reference node borrows caller string storage and observes later writes. Closing it needs a lifetime-parameterized/borrowed string field or shared reference-counted storage instead of the current owned `Vec<u8>`. Compiler attempt: `examples/cjson_rust/compiler_rejections/create_string_reference.rs`; rustc rejects it with `error[E0502]: cannot borrow `source` as mutable because it is also borrowed as immutable`. Use shared mutable string storage such as Rc<RefCell<Vec<u8>>>, or make this behavior explicitly unsafe. That changes every string parser, printer, getter, setter, duplicate, and comparison path from owned bytes to shared borrows/runtime checks. A lifetime-only Cow rewrite would also alter every CJson signature yet still cannot run the C trace. | Deliberately excluded – ownership blocked |
| `cJSON_CreateObjectReference` | C refusal trace: `object_reference`. The returned object aliases another tree's child chain and must not free it. Closing it needs shared/borrowed child ownership (`Rc`/`Arc` plus interior mutability, or a lifetime-carrying graph), not exclusive `Box` links. Compiler attempt: `examples/cjson_rust/compiler_rejections/create_object_reference.rs`; rustc rejects it with `error[E0502]: cannot borrow `source` as mutable because it is also borrowed as immutable`. Use Rc<RefCell<CJson>> nodes, or an arena plus runtime borrow discipline, for child/sibling links. Every getter, iterator, mutation API, parser, printer, duplicate/compare path, and the iterative Drop strategy would move from Box traversal to shared handles and runtime borrow/error handling. | Deliberately excluded – ownership blocked |
| `cJSON_CreateArrayReference` | C refusal trace: `array_reference`. The returned array aliases another tree's child chain and must not free it. Closing it needs shared/borrowed child ownership (`Rc`/`Arc` plus interior mutability, or a lifetime-carrying graph), not exclusive `Box` links. Compiler attempt: `examples/cjson_rust/compiler_rejections/create_array_reference.rs`; rustc rejects it with `error[E0502]: cannot borrow `source` as mutable because it is also borrowed as immutable`. Use Rc<RefCell<CJson>> nodes, or an arena plus runtime borrow discipline, for child/sibling links. Every getter, iterator, mutation API, parser, printer, duplicate/compare path, and the iterative Drop strategy would move from Box traversal to shared handles and runtime borrow/error handling. | Deliberately excluded – ownership blocked |
| `cJSON_CreateIntArray` | Rust `create_int_array` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_CreateFloatArray` | Rust `create_float_array` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_CreateDoubleArray` | Rust `create_double_array` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_CreateStringArray` | Rust `create_string_array` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_AddItemToArray` | Rust `add_item_to_array` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_AddItemToObject` | Rust `add_item_to_object` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_AddItemToObjectCS` | C refusal trace: `object_const_key`. C stores the caller key pointer and marks `cJSON_StringIsConst`; later key writes are observable. Closing it needs borrowed/shared key storage and its lifetime in the node instead of every Rust key being owned bytes. Compiler attempt: `examples/cjson_rust/compiler_rejections/add_item_to_object_cs.rs`; rustc rejects it with `error[E0502]: cannot borrow `key` as mutable because it is also borrowed as immutable`. Use shared mutable key storage such as Rc<RefCell<Vec<u8>>>, or an explicitly unsafe borrowed-key API. That changes parsing, printing, key lookup, duplicate/compare, and every object builder from owned bytes to shared borrows/runtime checks. A lifetime/Cow conversion changes all existing signatures but still cannot reproduce caller-side mutation. | Deliberately excluded – ownership blocked |
| `cJSON_AddItemReferenceToArray` | C refusal trace: `array_item_reference`. The destination receives a non-owning alias of an already owned item. Closing it needs shared nodes or a borrowed graph rather than one owning `Box` parent per node. Compiler attempt: `examples/cjson_rust/compiler_rejections/add_item_reference_to_array.rs`; rustc rejects it with `error[E0502]: cannot borrow `source` as mutable because it is also borrowed as immutable`. Use Rc<RefCell<CJson>> child nodes or an arena/handle graph. Because one child chain can then be reached from multiple parents, all existing Box-transfer signatures, traversal, mutation, parse/print, and the custom recursive/iterative Drop ownership model must be redesigned. | Deliberately excluded – ownership blocked |
| `cJSON_AddItemReferenceToObject` | C refusal trace: `object_item_reference`. The destination receives a non-owning alias of an already owned item. Closing it needs shared nodes or a borrowed graph rather than one owning `Box` parent per node. Compiler attempt: `examples/cjson_rust/compiler_rejections/add_item_reference_to_object.rs`; rustc rejects it with `error[E0502]: cannot borrow `source` as mutable because it is also borrowed as immutable`. Use Rc<RefCell<CJson>> child nodes or an arena/handle graph. Because one child chain can then be reached from multiple parents, all existing Box-transfer signatures, traversal, mutation, parse/print, and the custom recursive/iterative Drop ownership model must be redesigned. | Deliberately excluded – ownership blocked |
| `cJSON_DetachItemViaPointer` | `detach_item_via_pointer`; the target is a `*const CJson` identity token, compared and never dereferenced, so no `unsafe`/arena/interior mutability is needed over the exclusive `Box` tree. | Covered |
| `cJSON_DetachItemFromArray` | Rust `detach_item_from_array` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_DeleteItemFromArray` | Rust `delete_item_from_array` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_DetachItemFromObject` | Rust `detach_item_from_object` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_DetachItemFromObjectCaseSensitive` | Rust `detach_item_from_object_case_sensitive` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_DeleteItemFromObject` | Rust `delete_item_from_object` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_DeleteItemFromObjectCaseSensitive` | Rust `delete_item_from_object_case_sensitive` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_InsertItemInArray` | Rust `insert_item_in_array` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_ReplaceItemViaPointer` | `replace_item_via_pointer`; same node-address identity, and a miss returns the untouched replacement box instead of leaking it. | Covered |
| `cJSON_ReplaceItemInArray` | Rust `replace_item_in_array` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_ReplaceItemInObject` | Rust `replace_item_in_object` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_ReplaceItemInObjectCaseSensitive` | Rust `replace_item_in_object_case_sensitive` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_Duplicate` | Rust `duplicate` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_Compare` | Rust `compare` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_Minify` | Rust `minify` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_AddNullToObject` | Rust `add_null_to_object` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_AddTrueToObject` | Rust `add_true_to_object` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_AddFalseToObject` | Rust `add_false_to_object` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_AddBoolToObject` | Rust `add_bool_to_object` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_AddNumberToObject` | Rust `add_number_to_object` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_AddStringToObject` | Rust `add_string_to_object` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_AddRawToObject` | Rust `add_raw_to_object` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_AddObjectToObject` | Rust `add_object_to_object` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_AddArrayToObject` | Rust `add_array_to_object` equivalent (typed ownership/borrowing adaptation). | Covered |
| `cJSON_SetNumberHelper` | `set_number_value`; typed function, not macro helper leakage. | Covered |
| `cJSON_SetValuestring` | `set_value_string`; owned replacement bytes with an `Option` failure result. | Covered |
| `cJSON_malloc` | C refusal trace: `custom_hooks`. This is the public face of C's mutable global hook allocator. Rust allocation is owned by `Box`/`Vec`; exposing raw alloc/free pairs would require unsafe ownership transfer and the hook policy above, not a tree representation change. | Deliberately excluded – global/process state |
| `cJSON_free` | C refusal trace: `custom_hooks`. This is the public face of C's mutable global hook allocator. Rust allocation is owned by `Box`/`Vec`; exposing raw alloc/free pairs would require unsafe ownership transfer and the hook policy above, not a tree representation change. | Deliberately excluded – global/process state |

## Compiler-backed ownership attempts

For each remaining ownership exclusion, the checked-in candidate below is
compiled directly with rustc --edition=2021 --crate-type=lib. The cJSON
pytest audit asserts every source still fails and that the JSON diagnostic's
primary span falls inside its c_oracle_mutation_trace function. Each candidate
reaches the same later source mutation that the named C oracle refusal trace
observes; construction itself type-checks.
These are proofs about safe shared borrows over the current owned-tree API,
not claims that no Rust representation could express the operation.

| Header entry | attempted safe representation | actual compiler diagnostic | representation that would close the C behavior and cost |
| --- | --- | --- | --- |
| `cJSON_CreateStringReference` | `examples/cjson_rust/compiler_rejections/create_string_reference.rs` – Change valuestring from owned bytes to Cow with a borrowed source. Construction type-checks; the C-trace mutation is the rejected line. | `error[E0502]: cannot borrow `source` as mutable because it is also borrowed as immutable` | Use shared mutable string storage such as Rc<RefCell<Vec<u8>>>, or make this behavior explicitly unsafe. That changes every string parser, printer, getter, setter, duplicate, and comparison path from owned bytes to shared borrows/runtime checks. A lifetime-only Cow rewrite would also alter every CJson signature yet still cannot run the C trace. |
| `cJSON_CreateObjectReference` | `examples/cjson_rust/compiler_rejections/create_object_reference.rs` – Replace the owned child link for this node with a borrowed CJson child and return a reference object. Construction type-checks; mutating the source child as the C trace does is rejected. | `error[E0502]: cannot borrow `source` as mutable because it is also borrowed as immutable` | Use Rc<RefCell<CJson>> nodes, or an arena plus runtime borrow discipline, for child/sibling links. Every getter, iterator, mutation API, parser, printer, duplicate/compare path, and the iterative Drop strategy would move from Box traversal to shared handles and runtime borrow/error handling. |
| `cJSON_CreateArrayReference` | `examples/cjson_rust/compiler_rejections/create_array_reference.rs` – Replace the owned child link for this node with a borrowed CJson child and return a reference array. Construction type-checks; mutating the source child as the C trace does is rejected. | `error[E0502]: cannot borrow `source` as mutable because it is also borrowed as immutable` | Use Rc<RefCell<CJson>> nodes, or an arena plus runtime borrow discipline, for child/sibling links. Every getter, iterator, mutation API, parser, printer, duplicate/compare path, and the iterative Drop strategy would move from Box traversal to shared handles and runtime borrow/error handling. |
| `cJSON_AddItemToObjectCS` | `examples/cjson_rust/compiler_rejections/add_item_to_object_cs.rs` – Change the object key to Cow with a borrowed key. Insertion type-checks; the C-trace overwrite of the caller key is rejected. | `error[E0502]: cannot borrow `key` as mutable because it is also borrowed as immutable` | Use shared mutable key storage such as Rc<RefCell<Vec<u8>>>, or an explicitly unsafe borrowed-key API. That changes parsing, printing, key lookup, duplicate/compare, and every object builder from owned bytes to shared borrows/runtime checks. A lifetime/Cow conversion changes all existing signatures but still cannot reproduce caller-side mutation. |
| `cJSON_AddItemReferenceToArray` | `examples/cjson_rust/compiler_rejections/add_item_reference_to_array.rs` – Extend the current child slot to an owned or borrowed alternative. Adding the borrowed item type-checks; the C-trace source mutation is rejected while the destination keeps that borrow. | `error[E0502]: cannot borrow `source` as mutable because it is also borrowed as immutable` | Use Rc<RefCell<CJson>> child nodes or an arena/handle graph. Because one child chain can then be reached from multiple parents, all existing Box-transfer signatures, traversal, mutation, parse/print, and the custom recursive/iterative Drop ownership model must be redesigned. |
| `cJSON_AddItemReferenceToObject` | `examples/cjson_rust/compiler_rejections/add_item_reference_to_object.rs` – Keep an owned/copied object key but make the member item a borrowed CJson. Adding the member type-checks; the C-trace source mutation is rejected while the object keeps that borrow. | `error[E0502]: cannot borrow `source` as mutable because it is also borrowed as immutable` | Use Rc<RefCell<CJson>> child nodes or an arena/handle graph. Because one child chain can then be reached from multiple parents, all existing Box-transfer signatures, traversal, mutation, parse/print, and the custom recursive/iterative Drop ownership model must be redesigned. |

## Public constants, macros, limits, and types

The generator intentionally excludes ABI-configuration macros such as
`CJSON_PUBLIC`/`CJSON_CDECL`: they control C compilation and symbol export,
not the cJSON runtime API. Version, type, setter, traversal, and limit
definitions are included below.

| Header entry | Rust status / required representation change | Category |
| --- | --- | --- |
| `CJSON_VERSION_MAJOR` | `VERSION_MAJOR`; typed Rust version metadata. | Covered |
| `CJSON_VERSION_MINOR` | `VERSION_MINOR`; typed Rust version metadata. | Covered |
| `CJSON_VERSION_PATCH` | `VERSION_PATCH`; typed Rust version metadata. | Covered |
| `cJSON_Invalid` | `CJSON_INVALID`. | Covered |
| `cJSON_False` | `CJSON_FALSE`. | Covered |
| `cJSON_True` | `CJSON_TRUE`. | Covered |
| `cJSON_NULL` | `CJSON_NULL`. | Covered |
| `cJSON_Number` | `CJSON_NUMBER`. | Covered |
| `cJSON_String` | `CJSON_STRING`. | Covered |
| `cJSON_Array` | `CJSON_ARRAY`. | Covered |
| `cJSON_Object` | `CJSON_OBJECT`. | Covered |
| `cJSON_Raw` | `CJSON_RAW`. | Covered |
| `cJSON_IsReference` | Marks the non-owning aliases refused above; closing it requires shared/borrowed node or string storage. | Deliberately excluded |
| `cJSON_StringIsConst` | Marks a borrowed object-key pointer; closing it requires lifetime-carrying/shared key storage. | Deliberately excluded |
| `CJSON_NESTING_LIMIT` | `NESTING_LIMIT` is 1000 and drives the parser. | Covered |
| `CJSON_CIRCULAR_LIMIT` | The exclusive `Box` tree is acyclic by construction, so it has no C circular-reference traversal to bound. | Not applicable |
| `cJSON_SetIntValue` | `set_int_value`, a typed function rather than a C macro. | Covered |
| `cJSON_SetNumberValue` | `set_number_value`, a typed function rather than a C macro. | Covered |
| `cJSON_SetBoolValue` | `set_bool_value`, a typed function rather than a C macro. | Covered |
| `cJSON_ArrayForEach` | `children`, a safe borrowed iterator rather than a pointer-walking macro. | Covered |
| `cJSON` | `CJson` preserves tag/value/key/child/next layout. `prev` is intentionally absent: a non-owning back-link would require handles or shared/borrowed nodes. | Covered |
| `cJSON_Hooks` | Its function pointers feed process-global allocation state; see `cJSON_InitHooks`. | Deliberately excluded |
| `cJSON_bool` | Rust `bool` replaces the C integer boolean. | Covered |

## Closing scope statement

The safe owned-tree port covers every header function that does not require
a non-owning alias or mutable process-global allocation/error state. The
6 remaining ownership entries have executable C
counterexamples and checked-in compiler attempts. Their ordinary borrowed
storage candidates construct successfully but reject the C trace's later
source mutation with E0502. This proves the boundary of safe borrows over
the present owned tree; it does not assert that a shared-node/shared-byte
representation is impossible. The table above names the concrete replacement
and the signatures, traversal, parser/printer, and Drop work it would cost.
The 4 global-state entries need an allocator/error
policy redesign instead. No other public function remains merely unimplemented.

Node-address identity is *not* part of that boundary, contrary to an earlier
reading of it. `cJSON_DetachItemViaPointer` and `cJSON_ReplaceItemViaPointer`
are covered: the target is a `*const CJson` used purely as an identity token,
compared and never dereferenced, obtained from a shared borrow that ends
before the mutable borrow begins. That needs no `unsafe`, no handle arena and
no interior mutability, and both appear in the byte-compared safe C/Rust
trace. The rule this illustrates: `&CJson` cannot coexist with `&mut parent`,
but an address can.
