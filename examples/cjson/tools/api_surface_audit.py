#!/usr/bin/env python3
"""Generate and verify the cJSON public-surface audit from ``cJSON.h``.

The classifications are deliberately explicit.  A header addition, deletion, or
rename is an audit failure until its classification is reviewed; ``--write``
then renders the mechanically enumerated table once the manifest is updated.

Run from the repository root:

    uv run python examples/cjson/tools/api_surface_audit.py --check
    uv run python examples/cjson/tools/api_surface_audit.py --write
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_HEADER = ROOT / "examples/cjson/cJSON.h"
DEFAULT_AUDIT = ROOT / "examples/cjson/API_SURFACE_AUDIT.md"

FUNCTION_RE = re.compile(
    r"CJSON_PUBLIC\s*\([^;]*?\)\s*(cJSON_[A-Za-z0-9_]+)\s*\(", re.DOTALL
)
MACRO_RE = re.compile(r"^\s*#define\s+((?:cJSON_|CJSON_)[A-Za-z0-9_]+)", re.MULTILINE)
TYPE_RE = re.compile(
    r"typedef\s+struct\s+(cJSON(?:_Hooks)?)\s*\{.*?\}\s*(cJSON(?:_Hooks)?)\s*;"
    r"|typedef\s+int\s+(cJSON_bool)\s*;",
    re.DOTALL,
)


# This is an exact manifest, rather than a permissive default.  The check at
# the bottom of the module makes an upstream header change fail until a human
# classifies the new declaration.
COVERED_FUNCTIONS = frozenset(
    """
    cJSON_Version
    cJSON_Parse cJSON_ParseWithLength cJSON_ParseWithOpts cJSON_ParseWithLengthOpts
    cJSON_Print cJSON_PrintUnformatted cJSON_PrintBuffered cJSON_PrintPreallocated
    cJSON_Delete
    cJSON_GetArraySize cJSON_GetArrayItem cJSON_GetObjectItem
    cJSON_GetObjectItemCaseSensitive cJSON_HasObjectItem
    cJSON_GetStringValue cJSON_GetNumberValue
    cJSON_IsInvalid cJSON_IsFalse cJSON_IsTrue cJSON_IsBool cJSON_IsNull
    cJSON_IsNumber cJSON_IsString cJSON_IsArray cJSON_IsObject cJSON_IsRaw
    cJSON_CreateNull cJSON_CreateTrue cJSON_CreateFalse cJSON_CreateBool
    cJSON_CreateNumber cJSON_CreateString cJSON_CreateRaw cJSON_CreateArray
    cJSON_CreateObject
    cJSON_CreateIntArray cJSON_CreateFloatArray cJSON_CreateDoubleArray
    cJSON_CreateStringArray
    cJSON_AddItemToArray cJSON_AddItemToObject
    cJSON_DetachItemFromArray cJSON_DeleteItemFromArray
    cJSON_DetachItemFromObject cJSON_DetachItemFromObjectCaseSensitive
    cJSON_DeleteItemFromObject cJSON_DeleteItemFromObjectCaseSensitive
    cJSON_InsertItemInArray cJSON_ReplaceItemInArray
    cJSON_ReplaceItemInObject cJSON_ReplaceItemInObjectCaseSensitive
    cJSON_DetachItemViaPointer cJSON_ReplaceItemViaPointer
    cJSON_Duplicate cJSON_Compare cJSON_Minify
    cJSON_AddNullToObject cJSON_AddTrueToObject cJSON_AddFalseToObject
    cJSON_AddBoolToObject cJSON_AddNumberToObject cJSON_AddStringToObject
    cJSON_AddRawToObject cJSON_AddObjectToObject cJSON_AddArrayToObject
    cJSON_SetNumberHelper cJSON_SetValuestring
    """.split()
)

OWNERSHIP_BLOCKED = {
    "cJSON_CreateStringReference": (
        "A reference node borrows caller string storage and observes later writes. "
        "Closing it needs a lifetime-parameterized/borrowed string field or shared "
        "reference-counted storage instead of the current owned `Vec<u8>`.",
        "string_reference",
    ),
    "cJSON_CreateObjectReference": (
        "The returned object aliases another tree's child chain and must not free it. "
        "Closing it needs shared/borrowed child ownership (`Rc`/`Arc` plus interior "
        "mutability, or a lifetime-carrying graph), not exclusive `Box` links.",
        "object_reference",
    ),
    "cJSON_CreateArrayReference": (
        "The returned array aliases another tree's child chain and must not free it. "
        "Closing it needs shared/borrowed child ownership (`Rc`/`Arc` plus interior "
        "mutability, or a lifetime-carrying graph), not exclusive `Box` links.",
        "array_reference",
    ),
    "cJSON_AddItemToObjectCS": (
        "C stores the caller key pointer and marks `cJSON_StringIsConst`; later key "
        "writes are observable. Closing it needs borrowed/shared key storage and its "
        "lifetime in the node instead of every Rust key being owned bytes.",
        "object_const_key",
    ),
    "cJSON_AddItemReferenceToArray": (
        "The destination receives a non-owning alias of an already owned item. "
        "Closing it needs shared nodes or a borrowed graph rather than one owning `Box` "
        "parent per node.",
        "array_item_reference",
    ),
    "cJSON_AddItemReferenceToObject": (
        "The destination receives a non-owning alias of an already owned item. "
        "Closing it needs shared nodes or a borrowed graph rather than one owning `Box` "
        "parent per node.",
        "object_item_reference",
    ),
}

# A C trace establishes that cJSON exhibits the behavior. These minimal,
# intentionally failing Rust programs establish the different fact needed for
# this port: the smallest safe-borrow implementation cannot reproduce that
# trace. The pytest audit compiles every file and checks this exact diagnostic,
# so a comment or a stale source file cannot be mistaken for evidence.
BACKTICK = "`"

COMPILER_REJECTIONS = {
    "cJSON_CreateStringReference": {
        "snippet": "examples/cjson_rust/compiler_rejections/create_string_reference.rs",
        "diagnostic": (
            f"error[E0502]: cannot borrow {BACKTICK}source{BACKTICK} as mutable because "
            "it is also borrowed as immutable"
        ),
        "attempt": (
            "Change valuestring from owned bytes to Cow with a borrowed source. "
            "Construction type-checks; the C-trace mutation is the rejected line."
        ),
        "closure": (
            "Use shared mutable string storage such as Rc<RefCell<Vec<u8>>>, or make this "
            "behavior explicitly unsafe. That changes every string parser, printer, "
            "getter, setter, duplicate, and comparison path from owned bytes to shared "
            "borrows/runtime checks. A lifetime-only Cow rewrite would also alter every "
            "CJson signature yet still cannot run the C trace."
        ),
    },
    "cJSON_CreateObjectReference": {
        "snippet": "examples/cjson_rust/compiler_rejections/create_object_reference.rs",
        "diagnostic": (
            f"error[E0502]: cannot borrow {BACKTICK}source{BACKTICK} as mutable because "
            "it is also borrowed as immutable"
        ),
        "attempt": (
            "Replace the owned child link for this node with a borrowed CJson child and "
            "return a reference object. Construction type-checks; mutating the source "
            "child as the C trace does is rejected."
        ),
        "closure": (
            "Use Rc<RefCell<CJson>> nodes, or an arena plus runtime borrow discipline, "
            "for child/sibling links. Every getter, iterator, mutation API, parser, "
            "printer, duplicate/compare path, and the iterative Drop strategy would move "
            "from Box traversal to shared handles and runtime borrow/error handling."
        ),
    },
    "cJSON_CreateArrayReference": {
        "snippet": "examples/cjson_rust/compiler_rejections/create_array_reference.rs",
        "diagnostic": (
            f"error[E0502]: cannot borrow {BACKTICK}source{BACKTICK} as mutable because "
            "it is also borrowed as immutable"
        ),
        "attempt": (
            "Replace the owned child link for this node with a borrowed CJson child and "
            "return a reference array. Construction type-checks; mutating the source "
            "child as the C trace does is rejected."
        ),
        "closure": (
            "Use Rc<RefCell<CJson>> nodes, or an arena plus runtime borrow discipline, "
            "for child/sibling links. Every getter, iterator, mutation API, parser, "
            "printer, duplicate/compare path, and the iterative Drop strategy would move "
            "from Box traversal to shared handles and runtime borrow/error handling."
        ),
    },
    "cJSON_AddItemToObjectCS": {
        "snippet": "examples/cjson_rust/compiler_rejections/add_item_to_object_cs.rs",
        "diagnostic": (
            f"error[E0502]: cannot borrow {BACKTICK}key{BACKTICK} as mutable because it "
            "is also borrowed as immutable"
        ),
        "attempt": (
            "Change the object key to Cow with a borrowed key. Insertion type-checks; "
            "the C-trace overwrite of the caller key is rejected."
        ),
        "closure": (
            "Use shared mutable key storage such as Rc<RefCell<Vec<u8>>>, or an "
            "explicitly unsafe borrowed-key API. "
            "That changes parsing, printing, key lookup, duplicate/compare, and every "
            "object builder from owned bytes to shared borrows/runtime checks. A "
            "lifetime/Cow conversion changes all existing signatures but still cannot "
            "reproduce caller-side mutation."
        ),
    },
    "cJSON_AddItemReferenceToArray": {
        "snippet": "examples/cjson_rust/compiler_rejections/add_item_reference_to_array.rs",
        "diagnostic": (
            f"error[E0502]: cannot borrow {BACKTICK}source{BACKTICK} as mutable because "
            "it is also borrowed as immutable"
        ),
        "attempt": (
            "Extend the current child slot to an owned or borrowed alternative. Adding "
            "the borrowed item type-checks; the C-trace source mutation is rejected "
            "while the destination keeps that borrow."
        ),
        "closure": (
            "Use Rc<RefCell<CJson>> child nodes or an arena/handle graph. Because one "
            "child chain can "
            "then be reached from multiple parents, all existing Box-transfer signatures, "
            "traversal, mutation, parse/print, and the custom recursive/iterative Drop "
            "ownership model must be redesigned."
        ),
    },
    "cJSON_AddItemReferenceToObject": {
        "snippet": "examples/cjson_rust/compiler_rejections/add_item_reference_to_object.rs",
        "diagnostic": (
            f"error[E0502]: cannot borrow {BACKTICK}source{BACKTICK} as mutable because "
            "it is also borrowed as immutable"
        ),
        "attempt": (
            "Keep an owned/copied object key but make the member item a borrowed CJson. "
            "Adding the member type-checks; the C-trace source mutation is rejected while "
            "the object keeps that borrow."
        ),
        "closure": (
            "Use Rc<RefCell<CJson>> child nodes or an arena/handle graph. Because one "
            "child chain can "
            "then be reached from multiple parents, all existing Box-transfer signatures, "
            "traversal, mutation, parse/print, and the custom recursive/iterative Drop "
            "ownership model must be redesigned."
        ),
    },
}

GLOBAL_STATE_EXCLUDED = {
    "cJSON_InitHooks": (
        "C installs process-global malloc/free callbacks. Closing it needs an allocator "
        "parameter threaded through every allocation or an explicitly unsafe/global "
        "allocator policy; it is not blocked by tree ownership.",
        "custom_hooks",
    ),
    "cJSON_GetErrorPtr": (
        "C exposes a process-global borrowed pointer to the most recent parse error. "
        "Rust returns an error offset from `parse_with_opts`; reproducing the global "
        "pointer requires thread/global mutable error state and borrowed input lifetime.",
        "error_pointer",
    ),
    "cJSON_malloc": (
        "This is the public face of C's mutable global hook allocator. Rust allocation is "
        "owned by `Box`/`Vec`; exposing raw alloc/free pairs would require unsafe ownership "
        "transfer and the hook policy above, not a tree representation change.",
        "custom_hooks",
    ),
    "cJSON_free": (
        "This is the public face of C's mutable global hook allocator. Rust allocation is "
        "owned by `Box`/`Vec`; exposing raw alloc/free pairs would require unsafe ownership "
        "transfer and the hook policy above, not a tree representation change.",
        "custom_hooks",
    ),
}

EXCLUDED_FUNCTIONS = frozenset(OWNERSHIP_BLOCKED) | frozenset(GLOBAL_STATE_EXCLUDED)

DATA_MANIFEST = {
    "CJSON_VERSION_MAJOR": ("Covered", "`VERSION_MAJOR`; typed Rust version metadata."),
    "CJSON_VERSION_MINOR": ("Covered", "`VERSION_MINOR`; typed Rust version metadata."),
    "CJSON_VERSION_PATCH": ("Covered", "`VERSION_PATCH`; typed Rust version metadata."),
    "CJSON_NESTING_LIMIT": ("Covered", "`NESTING_LIMIT` is 1000 and drives the parser."),
    "CJSON_CIRCULAR_LIMIT": (
        "Not applicable",
        "The exclusive `Box` tree is acyclic by construction, so it has no C circular-reference traversal to bound.",
    ),
    "cJSON_Invalid": ("Covered", "`CJSON_INVALID`."),
    "cJSON_False": ("Covered", "`CJSON_FALSE`."),
    "cJSON_True": ("Covered", "`CJSON_TRUE`."),
    "cJSON_NULL": ("Covered", "`CJSON_NULL`."),
    "cJSON_Number": ("Covered", "`CJSON_NUMBER`."),
    "cJSON_String": ("Covered", "`CJSON_STRING`."),
    "cJSON_Array": ("Covered", "`CJSON_ARRAY`."),
    "cJSON_Object": ("Covered", "`CJSON_OBJECT`."),
    "cJSON_Raw": ("Covered", "`CJSON_RAW`."),
    "cJSON_IsReference": (
        "Deliberately excluded",
        "Marks the non-owning aliases refused above; closing it requires shared/borrowed node or string storage.",
    ),
    "cJSON_StringIsConst": (
        "Deliberately excluded",
        "Marks a borrowed object-key pointer; closing it requires lifetime-carrying/shared key storage.",
    ),
    "cJSON_SetIntValue": ("Covered", "`set_int_value`, a typed function rather than a C macro."),
    "cJSON_SetNumberValue": ("Covered", "`set_number_value`, a typed function rather than a C macro."),
    "cJSON_SetBoolValue": ("Covered", "`set_bool_value`, a typed function rather than a C macro."),
    "cJSON_ArrayForEach": ("Covered", "`children`, a safe borrowed iterator rather than a pointer-walking macro."),
    "cJSON": (
        "Covered",
        "`CJson` preserves tag/value/key/child/next layout. `prev` is intentionally absent: a non-owning back-link would require handles or shared/borrowed nodes.",
    ),
    "cJSON_Hooks": (
        "Deliberately excluded",
        "Its function pointers feed process-global allocation state; see `cJSON_InitHooks`.",
    ),
    "cJSON_bool": ("Covered", "Rust `bool` replaces the C integer boolean."),
}


def parse_header(path: Path) -> tuple[list[str], list[str]]:
    text = path.read_text()
    functions = FUNCTION_RE.findall(text)
    macros = [
        name
        for name in MACRO_RE.findall(text)
        if (name.startswith("cJSON_") and name != "cJSON__h")
        or name
        in {
            "CJSON_VERSION_MAJOR",
            "CJSON_VERSION_MINOR",
            "CJSON_VERSION_PATCH",
            "CJSON_NESTING_LIMIT",
            "CJSON_CIRCULAR_LIMIT",
        }
    ]
    # The two public limits have #ifndef guards, so retain them even if the
    # precise macro formatting changes around their definitions.
    for name in ("CJSON_NESTING_LIMIT", "CJSON_CIRCULAR_LIMIT"):
        if name not in macros and re.search(rf"#define\s+{name}\b", text):
            macros.append(name)
    types: list[str] = []
    for struct_one, struct_two, scalar in TYPE_RE.findall(text):
        if scalar:
            types.append(scalar)
        elif struct_two:
            types.append(struct_two)
        elif struct_one:
            types.append(struct_one)
    entries = functions + macros + types
    if len(entries) != len(set(entries)):
        raise ValueError("header parser found duplicate public API entries")
    return functions, macros + types


def snake(name: str) -> str:
    body = name.removeprefix("cJSON_")
    return re.sub(r"(?<!^)([A-Z])", r"_\1", body).lower()


def covered_status(name: str) -> str:
    special = {
        "cJSON_Parse": "`parse`; owned byte slice input.",
        "cJSON_ParseWithLength": "`parse`; a Rust slice already carries its exact length.",
        "cJSON_ParseWithOpts": "`parse_with_opts`; returns an owned tree plus parse-end/error offset.",
        "cJSON_ParseWithLengthOpts": "`parse_with_opts`; the Rust slice supplies `buffer_length`.",
        "cJSON_Print": "`print_formatted` returns owned bytes.",
        "cJSON_PrintUnformatted": "`print_unformatted` returns owned bytes.",
        "cJSON_PrintBuffered": "`print_buffered`; prebuffer is a capacity hint.",
        "cJSON_PrintPreallocated": "`print_preallocated` writes a caller-owned mutable buffer and reports fit.",
        "cJSON_Delete": "ordinary `drop(Box<CJson>)`; `delete` is also supplied for C-shaped migration code.",
        "cJSON_SetNumberHelper": "`set_number_value`; typed function, not macro helper leakage.",
        "cJSON_SetValuestring": "`set_value_string`; owned replacement bytes with an `Option` failure result.",
        "cJSON_DetachItemViaPointer": (
            "`detach_item_via_pointer`; the target is a `*const CJson` identity token, "
            "compared and never dereferenced, so no `unsafe`/arena/interior mutability "
            "is needed over the exclusive `Box` tree."
        ),
        "cJSON_ReplaceItemViaPointer": (
            "`replace_item_via_pointer`; same node-address identity, and a miss returns "
            "the untouched replacement box instead of leaking it."
        ),
    }
    if name in special:
        return special[name]
    return f"Rust `{snake(name)}` equivalent (typed ownership/borrowing adaptation)."


def validate_manifest(functions: list[str], data: list[str]) -> None:
    expected_functions = COVERED_FUNCTIONS | EXCLUDED_FUNCTIONS
    found_functions = frozenset(functions)
    if found_functions != expected_functions:
        missing = sorted(found_functions - expected_functions)
        stale = sorted(expected_functions - found_functions)
        raise ValueError(
            "function manifest no longer matches cJSON.h; "
            f"unclassified={missing or '-'} stale={stale or '-'}"
        )
    found_data = frozenset(data)
    expected_data = frozenset(DATA_MANIFEST)
    if found_data != expected_data:
        missing = sorted(found_data - expected_data)
        stale = sorted(expected_data - found_data)
        raise ValueError(
            "data manifest no longer matches cJSON.h; "
            f"unclassified={missing or '-'} stale={stale or '-'}"
        )
    rejected = frozenset(COMPILER_REJECTIONS)
    blocked = frozenset(OWNERSHIP_BLOCKED)
    if rejected != blocked:
        missing = sorted(blocked - rejected)
        stale = sorted(rejected - blocked)
        raise ValueError(
            "compiler-rejection manifest no longer matches ownership exclusions; "
            f"missing={missing or '-'} stale={stale or '-'}"
        )
    for function, proof in COMPILER_REJECTIONS.items():
        snippet = ROOT / proof["snippet"]
        if not snippet.is_file():
            raise ValueError(f"compiler-rejection snippet missing for {function}: {snippet}")
        if proof["diagnostic"] not in snippet.read_text():
            raise ValueError(
                f"compiler-rejection snippet does not record the expected diagnostic for "
                f"{function}"
            )


def render(header: Path) -> str:
    functions, data = parse_header(header)
    validate_manifest(functions, data)
    function_rows: list[str] = []
    for name in functions:
        if name in OWNERSHIP_BLOCKED:
            reason, scenario = OWNERSHIP_BLOCKED[name]
            proof = COMPILER_REJECTIONS[name]
            status = (
                f"C refusal trace: {BACKTICK}{scenario}{BACKTICK}. {reason} "
                f"Compiler attempt: {BACKTICK}{proof['snippet']}{BACKTICK}; rustc "
                f"rejects it with {BACKTICK}{proof['diagnostic']}{BACKTICK}. "
                f"{proof['closure']}"
            )
            category = "Deliberately excluded — ownership blocked"
        elif name in GLOBAL_STATE_EXCLUDED:
            reason, scenario = GLOBAL_STATE_EXCLUDED[name]
            status = f"C refusal trace: `{scenario}`. {reason}"
            category = "Deliberately excluded — global/process state"
        else:
            status = covered_status(name)
            category = "Covered"
        function_rows.append(f"| `{name}` | {status} | {category} |")

    data_rows = []
    for name in data:
        category, status = DATA_MANIFEST[name]
        data_rows.append(f"| `{name}` | {status} | {category} |")

    compiler_rows = []
    for name in OWNERSHIP_BLOCKED:
        proof = COMPILER_REJECTIONS[name]
        compiler_rows.append(
            f"| {BACKTICK}{name}{BACKTICK} | {BACKTICK}{proof['snippet']}{BACKTICK} — "
            f"{proof['attempt']} | {BACKTICK}{proof['diagnostic']}{BACKTICK} | "
            f"{proof['closure']} |"
        )

    return "\n".join(
        [
            "# cJSON C → Rust API-surface audit",
            "",
            "This is a mechanically generated inventory of the vendored public header,",
            "not a recollection of the currently ported slice. The source of truth is",
            "`examples/cjson/cJSON.h`; classifications are deliberately exhaustive and",
            "fail closed when that header changes.",
            "",
            "## Reproduce and enforce",
            "",
            "```bash",
            "uv run python examples/cjson/tools/api_surface_audit.py --check",
            "uv run python examples/cjson/tools/api_surface_audit.py --write",
            "```",
            "",
            "`--check` parses the header, validates that every parsed entry has exactly",
            "one classification, and compares this file to the generated result. The",
            "pytest audit test also copies and corrupts the header to prove that the",
            "enumeration is input-derived. Every excluded function maps to a named C",
            "C-oracle refusal trace; the test executes each trace, asserts that its named",
            "operation was reached, and runs those traces under ASan when available.",
            "",
            "## Quantified boundary",
            "",
            f"The header currently has **{len(functions)} functions** and **{len(data)}",
            f"public constants/macros/limits/types**. Of the functions, "
            f"**{len(COVERED_FUNCTIONS)} are covered**, "
            f"**{len(OWNERSHIP_BLOCKED)} are blocked by the exclusive `Box` representation**, "
            f"and **{len(GLOBAL_STATE_EXCLUDED)} are",
            "excluded for process-global C allocator/error state rather than ownership**.",
            "There are **no silently deferred function entries**. The separate structural",
            "boundary is `cJSON.prev` plus the `cJSON_IsReference` and",
            "`cJSON_StringIsConst` flags: all require non-owning aliases/identity beyond",
            "the acyclic owned tree. The C traces are evidence for the boundary, not just",
            "a list of names.",
            "",
            "## Functions (from `CJSON_PUBLIC` declarations)",
            "",
            "| Header entry | Rust status / required representation change | Category |",
            "| --- | --- | --- |",
            *function_rows,
            "",
            "## Compiler-backed ownership attempts",
            "",
            "For each remaining ownership exclusion, the checked-in candidate below is",
            "compiled directly with rustc --edition=2021 --crate-type=lib. The cJSON",
            "pytest audit asserts every source still fails and contains the exact error",
            "shown here. Each candidate reaches the same later source mutation that the",
            "named C oracle refusal trace observes; construction itself type-checks.",
            "These are proofs about safe shared borrows over the current owned-tree API,",
            "not claims that no Rust representation could express the operation.",
            "",
            "| Header entry | attempted safe representation | actual compiler diagnostic | representation that would close the C behavior and cost |",
            "| --- | --- | --- | --- |",
            *compiler_rows,
            "",
            "## Public constants, macros, limits, and types",
            "",
            "The generator intentionally excludes ABI-configuration macros such as",
            "`CJSON_PUBLIC`/`CJSON_CDECL`: they control C compilation and symbol export,",
            "not the cJSON runtime API. Version, type, setter, traversal, and limit",
            "definitions are included below.",
            "",
            "| Header entry | Rust status / required representation change | Category |",
            "| --- | --- | --- |",
            *data_rows,
            "",
            "## Closing scope statement",
            "",
            "The safe owned-tree port covers every header function that does not require",
            "a non-owning alias or mutable process-global allocation/error state. The",
            f"{len(OWNERSHIP_BLOCKED)} remaining ownership entries have executable C",
            "counterexamples and checked-in compiler attempts. Their ordinary borrowed",
            "storage candidates construct successfully but reject the C trace's later",
            "source mutation with E0502. This proves the boundary of safe borrows over",
            "the present owned tree; it does not assert that a shared-node/shared-byte",
            "representation is impossible. The table above names the concrete replacement",
            "and the signatures, traversal, parser/printer, and Drop work it would cost.",
            f"The {len(GLOBAL_STATE_EXCLUDED)} global-state entries need an allocator/error",
            "policy redesign instead. No other public function remains merely unimplemented.",
            "",
            "Node-address identity is *not* part of that boundary, contrary to an earlier",
            "reading of it. `cJSON_DetachItemViaPointer` and `cJSON_ReplaceItemViaPointer`",
            "are covered: the target is a `*const CJson` used purely as an identity token,",
            "compared and never dereferenced, obtained from a shared borrow that ends",
            "before the mutable borrow begins. That needs no `unsafe`, no handle arena and",
            "no interior mutability, and both appear in the byte-compared safe C/Rust",
            "trace. The rule this illustrates: `&CJson` cannot coexist with `&mut parent`,",
            "but an address can.",
            "",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--header", type=Path, default=DEFAULT_HEADER)
    parser.add_argument("--output", type=Path, default=DEFAULT_AUDIT)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true")
    action.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    try:
        generated = render(args.header)
    except (OSError, ValueError) as error:
        print(f"cJSON API audit: {error}", file=sys.stderr)
        return 1
    if args.write:
        args.output.write_text(generated)
        print(f"wrote {args.output.relative_to(ROOT)}")
        return 0
    if not args.output.exists() or args.output.read_text() != generated:
        print("cJSON API audit is stale; run with --write", file=sys.stderr)
        return 1
    print(f"cJSON API audit: {len(parse_header(args.header)[0])} functions, current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
