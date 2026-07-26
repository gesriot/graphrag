"""Temporary C oracle used by the cJSON API-surface audit tests.

The source stays in the test harness rather than the indexed golden runner so
the published `byog_cjson` snapshot remains a library + golden-runner graph.
Each execution still links the vendored `cJSON.c` and is run under ASan by the
normal C contract test when the toolchain supports it.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


ORACLE_SOURCE = r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "cJSON.h"

static int allocation_count = 0;
static int free_count = 0;
static void *tracked_malloc(size_t size) { allocation_count++; return malloc(size); }
static void tracked_free(void *pointer) { free_count++; free(pointer); }
static void print_preallocated_trace(const char *name, cJSON_bool ok, const unsigned char *buffer, size_t length);

static void print_safe_trace(void) {
    cJSON *root = cJSON_CreateObject();
    cJSON *number;
    cJSON *integer;
    cJSON *boolean;
    cJSON *string;
    char *printed;
    char *buffered;
    char preallocated[512];
    char too_small[5] = {0xAA, 0xAA, 0xAA, 0xAA, 0xAA};
    const char prefix[] = "{\"p\":1} trailing";
    const char terminated[] = "{\"p\":1}";
    const char *end = NULL;
    cJSON *parsed;
    cJSON *false_item;
    cJSON invalid = {0};
    char minified[] = " { /* note */ \"a b\" : 1 // tail\n } ";
    int child_count = 0;
    cJSON *child = NULL;

    cJSON_AddNullToObject(root, "null");
    cJSON_AddTrueToObject(root, "true");
    cJSON_AddFalseToObject(root, "false");
    cJSON_AddBoolToObject(root, "bool", 0);
    cJSON_AddNumberToObject(root, "number", 1.5);
    cJSON_AddNumberToObject(root, "int", 0);
    cJSON_AddStringToObject(root, "string", "before");
    cJSON_AddRawToObject(root, "raw", "null");
    cJSON_AddObjectToObject(root, "object");
    cJSON_AddArrayToObject(root, "array");

    number = cJSON_GetObjectItemCaseSensitive(root, "number");
    integer = cJSON_GetObjectItemCaseSensitive(root, "int");
    boolean = cJSON_GetObjectItemCaseSensitive(root, "bool");
    string = cJSON_GetObjectItemCaseSensitive(root, "string");

    printf("version=%s\n", cJSON_Version());
    cJSON_ArrayForEach(child, root) { child_count++; }
    printf("children=%d\n", child_count);
    printf("case_sensitive=%d\n", cJSON_GetObjectItemCaseSensitive(root, "string") != NULL);
    printf("case_insensitive=%d\n", cJSON_GetObjectItem(root, "STRING") != NULL);
    printf("has_number=%d\n", cJSON_HasObjectItem(root, "NuMbEr"));
    printf("is_invalid=%d\n", cJSON_IsInvalid(&invalid));
    false_item = cJSON_CreateFalse();
    printf("is_false=%d\n", cJSON_IsFalse(false_item));
    cJSON_Delete(false_item);

    printf("set_number=%g\n", cJSON_SetNumberHelper(number, -3.5));
    printf("set_int=%d\n", cJSON_SetIntValue(integer, -4));
    printf("set_bool=%d\n", cJSON_SetBoolValue(boolean, 1));
    printf("set_string=%d\n", cJSON_SetValuestring(string, "after") != NULL);

    printed = cJSON_PrintUnformatted(root);
    buffered = cJSON_PrintBuffered(root, 4, 0);
    printf("printed=%s\n", printed);
    printf("buffered=%s\n", buffered);
    printf("preallocated=%d:%s\n", cJSON_PrintPreallocated(root, preallocated, (int)sizeof(preallocated), 0), preallocated);
    print_preallocated_trace("preallocated_small", cJSON_PrintPreallocated(root, too_small, (int)sizeof(too_small), 0), (unsigned char *)too_small, sizeof(too_small));

    parsed = cJSON_ParseWithLengthOpts(prefix, sizeof(prefix) - 1, &end, 0);
    printf("parse_prefix=%d:%td\n", parsed != NULL, end - prefix);
    cJSON_Delete(parsed);
    parsed = cJSON_ParseWithLengthOpts(prefix, sizeof(prefix) - 1, &end, 1);
    printf("parse_prefix_required=%d:%td\n", parsed != NULL, end - prefix);
    cJSON_Delete(parsed);
    parsed = cJSON_ParseWithLengthOpts(terminated, sizeof(terminated), &end, 1);
    printf("parse_nul_required=%d:%td\n", parsed != NULL, end - terminated);
    cJSON_Delete(parsed);

    cJSON_Minify(minified);
    printf("minify=%s\n", minified);

    /* Node-address identity over an owned tree. Rust reaches this with a
       *const CJson that is compared but never dereferenced, so it belongs in
       the byte-compared safe trace rather than in the refusal set. */
    {
        cJSON *vector = cJSON_CreateArray();
        cJSON *middle;
        cJSON *detached;
        char *vector_printed;
        cJSON_AddItemToArray(vector, cJSON_CreateNumber(1));
        cJSON_AddItemToArray(vector, cJSON_CreateNumber(2));
        cJSON_AddItemToArray(vector, cJSON_CreateNumber(3));
        middle = cJSON_GetArrayItem(vector, 1);
        detached = cJSON_DetachItemViaPointer(vector, middle);
        printf("detach_via_pointer_identity=%d\n", detached == middle);
        vector_printed = cJSON_PrintUnformatted(vector);
        printf("detach_via_pointer_rest=%s\n", vector_printed);
        cJSON_free(vector_printed);
        vector_printed = cJSON_PrintUnformatted(detached);
        printf("detach_via_pointer_item=%s\n", vector_printed);
        cJSON_free(vector_printed);
        cJSON_Delete(detached);
        middle = cJSON_GetArrayItem(vector, 1);
        printf("replace_via_pointer=%d\n",
               cJSON_ReplaceItemViaPointer(vector, middle, cJSON_CreateNumber(9)));
        vector_printed = cJSON_PrintUnformatted(vector);
        printf("replace_via_pointer_result=%s\n", vector_printed);
        cJSON_free(vector_printed);
        cJSON_Delete(vector);
    }

    cJSON_free(printed);
    cJSON_free(buffered);
    cJSON_Delete(root);
}

static void refusal_header(const char *operation, const char *kind) {
    printf("operation=%s\noperations=%s\nreached=1\nkind=%s\n", operation, operation, kind);
}

static void print_and_free_observed(cJSON *item) {
    char *printed = cJSON_PrintUnformatted(item);
    printf("observed=%s\n", printed == NULL ? "__PRINT_ERROR__" : printed);
    cJSON_free(printed);
}

static void print_preallocated_trace(const char *name, cJSON_bool ok, const unsigned char *buffer, size_t length) {
    size_t index;
    printf("%s=%d:", name, ok);
    for (index = 0; index < length; index++) {
        printf("%02x", buffer[index]);
    }
    printf("\n");
}

static void refusal_string_reference(void) {
    char text[] = "before";
    cJSON *reference = cJSON_CreateStringReference(text);
    strcpy(text, "after");
    refusal_header("cJSON_CreateStringReference", "borrowed_string");
    print_and_free_observed(reference);
    cJSON_Delete(reference);
}

static void refusal_object_reference(void) {
    cJSON *source = cJSON_CreateObject();
    cJSON *reference;
    cJSON_AddNumberToObject(source, "value", 1);
    reference = cJSON_CreateObjectReference(source->child);
    cJSON_SetNumberHelper(source->child, 2);
    refusal_header("cJSON_CreateObjectReference", "borrowed_child_chain");
    print_and_free_observed(reference);
    cJSON_Delete(reference);
    cJSON_Delete(source);
}

static void refusal_array_reference(void) {
    cJSON *source = cJSON_CreateArray();
    cJSON *reference;
    cJSON_AddItemToArray(source, cJSON_CreateNumber(1));
    reference = cJSON_CreateArrayReference(source->child);
    cJSON_SetNumberHelper(source->child, 2);
    refusal_header("cJSON_CreateArrayReference", "borrowed_child_chain");
    print_and_free_observed(reference);
    cJSON_Delete(reference);
    cJSON_Delete(source);
}

static void refusal_const_key(void) {
    char key[] = "first";
    cJSON *object = cJSON_CreateObject();
    cJSON_AddItemToObjectCS(object, key, cJSON_CreateNumber(1));
    strcpy(key, "after");
    refusal_header("cJSON_AddItemToObjectCS", "borrowed_key");
    print_and_free_observed(object);
    cJSON_Delete(object);
}

static void refusal_array_item_reference(void) {
    cJSON *source = cJSON_CreateArray();
    cJSON *array = cJSON_CreateArray();
    cJSON_AddItemToArray(source, cJSON_CreateNumber(1));
    cJSON_AddItemReferenceToArray(array, source);
    cJSON_SetNumberHelper(source->child, 2);
    refusal_header("cJSON_AddItemReferenceToArray", "borrowed_item");
    print_and_free_observed(array);
    cJSON_Delete(array);
    cJSON_Delete(source);
}

static void refusal_object_item_reference(void) {
    cJSON *source = cJSON_CreateArray();
    cJSON *object = cJSON_CreateObject();
    cJSON_AddItemToArray(source, cJSON_CreateNumber(1));
    cJSON_AddItemReferenceToObject(object, "value", source);
    cJSON_SetNumberHelper(source->child, 2);
    refusal_header("cJSON_AddItemReferenceToObject", "borrowed_item");
    print_and_free_observed(object);
    cJSON_Delete(object);
    cJSON_Delete(source);
}

static void refusal_error_pointer(void) {
    const char broken[] = "{";
    cJSON *item = cJSON_Parse(broken);
    const char *error = cJSON_GetErrorPtr();
    refusal_header("cJSON_GetErrorPtr", "global_error_pointer");
    printf("error_offset=%td\n", error == NULL ? -1 : error - broken);
    cJSON_Delete(item);
}

static void refusal_custom_hooks(void) {
    cJSON_Hooks hooks = {tracked_malloc, tracked_free};
    void *memory;
    cJSON *item;
    allocation_count = 0;
    free_count = 0;
    cJSON_InitHooks(&hooks);
    memory = cJSON_malloc(4);
    cJSON_free(memory);
    item = cJSON_CreateNumber(1);
    cJSON_Delete(item);
    refusal_header("cJSON_InitHooks", "global_allocator");
    printf("operations=cJSON_InitHooks,cJSON_malloc,cJSON_free\n");
    printf("allocations=%d\nfrees=%d\n", allocation_count, free_count);
    cJSON_InitHooks(NULL);
}

static int run_refusal(const char *scenario) {
    if (strcmp(scenario, "string_reference") == 0) { refusal_string_reference(); return 0; }
    if (strcmp(scenario, "object_reference") == 0) { refusal_object_reference(); return 0; }
    if (strcmp(scenario, "array_reference") == 0) { refusal_array_reference(); return 0; }
    if (strcmp(scenario, "object_const_key") == 0) { refusal_const_key(); return 0; }
    if (strcmp(scenario, "array_item_reference") == 0) { refusal_array_item_reference(); return 0; }
    if (strcmp(scenario, "object_item_reference") == 0) { refusal_object_item_reference(); return 0; }
    if (strcmp(scenario, "error_pointer") == 0) { refusal_error_pointer(); return 0; }
    if (strcmp(scenario, "custom_hooks") == 0) { refusal_custom_hooks(); return 0; }
    return 2;
}

int main(int argc, char **argv) {
    if (argc < 2) { return 2; }
    if (strcmp(argv[1], "safe") == 0) { print_safe_trace(); return 0; }
    if (strcmp(argv[1], "refusal") == 0 && argc == 3) { return run_refusal(argv[2]); }
    return 2;
}
'''


def compile_oracle(cc: str, cjson_dir: Path, output: Path, extra: list[str]) -> bool:
    source = output.with_suffix(".c")
    source.write_text(ORACLE_SOURCE)
    result = subprocess.run(
        [cc, "-I", str(cjson_dir), *extra, "-o", str(output), str(source), str(cjson_dir / "cJSON.c")],
        capture_output=True,
    )
    return result.returncode == 0


def run_oracle(binary: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run([str(binary), *args], capture_output=True)
