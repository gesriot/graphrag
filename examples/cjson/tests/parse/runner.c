/* Golden runner for the cJSON C->Rust ownership-slice port.
 *
 * Exercises parse -> inspect -> print -> delete over JSON read from stdin.
 * argv[1] selects the oracle:
 *   "unformatted" (default) -> cJSON_PrintUnformatted
 *   "formatted"             -> cJSON_Print
 *   "inspect"               -> a canonical tree descriptor built ONLY from the
 *                              public getter API (GetArraySize/GetArrayItem/
 *                              GetObjectItem is exercised via key walking, the
 *                              Is* predicates, valuestring/valueint/valuedouble).
 *
 * On parse failure the runner prints `__PARSE_ERROR__`. Every mode parses and
 * then cJSON_Delete()s the tree, so compiling this under -fsanitize=address and
 * running the corpus checks the free/ownership path (no leak/double-free).
 *
 * Number fidelity note: the inspect descriptor emits each number as its integer
 * value plus the raw IEEE-754 bits of valuedouble, so parse fidelity is checked
 * independently of print. Non-integer *printing* (`%1.15g`/`%1.17g`) is covered
 * by the separate `golden_float_print.json` corpus (same runner modes).
 *
 * Build: cc -I <cjson dir> -o runner runner.c cJSON.c
 *
 * Mutation traces:
 *   "mutation" -> stdin is a fixed scenario name. The runner executes that
 *                   sequence against cJSON's builder/mutation API and emits a
 *                   canonical JSON trace. Fixed scenarios deliberately avoid
 *                   adding a second, unverified script parser to the oracle:
 *                   every ownership transfer is visible in a named C sequence
 *                   and is exercised by the same ASan build as parse cases.
 */
#include <stdint.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "cJSON.h"

static char out[1 << 20];
static size_t outlen = 0;

static void emit(char c) {
    if (outlen < sizeof(out) - 1)
        out[outlen++] = c;
}

static void emit_raw(const char* s) {
    for (; *s; s++)
        emit(*s);
}

static void emit_json_str(const char* s) {
    emit('"');
    for (; s && *s; s++) {
        unsigned char c = (unsigned char)*s;
        switch (c) {
            case '"': emit_raw("\\\""); break;
            case '\\': emit_raw("\\\\"); break;
            case '\n': emit_raw("\\n"); break;
            case '\r': emit_raw("\\r"); break;
            case '\t': emit_raw("\\t"); break;
            default:
                if (c < 0x20) {
                    char buf[8];
                    snprintf(buf, sizeof(buf), "\\u%04x", c);
                    emit_raw(buf);
                } else {
                    emit((char)c);
                }
        }
    }
    emit('"');
}

/* Recursive descriptor over the PUBLIC cJSON getter API. */
static void describe(const cJSON* item) {
    if (item == NULL) {
        emit_raw("{\"t\":\"missing\"}");
        return;
    }
    if (cJSON_IsNull(item)) {
        emit_raw("{\"t\":\"null\"}");
    } else if (cJSON_IsBool(item)) {
        emit_raw("{\"t\":\"bool\",\"v\":");
        emit_raw(cJSON_IsTrue(item) ? "true" : "false");
        emit('}');
    } else if (cJSON_IsNumber(item)) {
        double d = cJSON_GetNumberValue(item);
        uint64_t bits;
        memcpy(&bits, &d, sizeof(bits));
        char buf[64];
        snprintf(buf, sizeof(buf), "{\"t\":\"num\",\"i\":%d,\"bits\":%llu}",
                 item->valueint, (unsigned long long)bits);
        emit_raw(buf);
    } else if (cJSON_IsString(item)) {
        emit_raw("{\"t\":\"str\",\"v\":");
        emit_json_str(cJSON_GetStringValue(item));
        emit('}');
    } else if (cJSON_IsRaw(item)) {
        emit_raw("{\"t\":\"raw\",\"v\":");
        /* cJSON_GetStringValue intentionally only accepts cJSON_String;
         * `valuestring` is public and is the raw payload for cJSON_Raw. */
        emit_json_str(item->valuestring);
        emit('}');
    } else if (cJSON_IsArray(item)) {
        int n = cJSON_GetArraySize(item);
        char buf[32];
        snprintf(buf, sizeof(buf), "{\"t\":\"arr\",\"n\":%d,\"items\":[", n);
        emit_raw(buf);
        for (int i = 0; i < n; i++) {
            if (i)
                emit(',');
            describe(cJSON_GetArrayItem(item, i));
        }
        emit_raw("]}");
    } else if (cJSON_IsObject(item)) {
        int n = cJSON_GetArraySize(item);
        char buf[32];
        snprintf(buf, sizeof(buf), "{\"t\":\"obj\",\"n\":%d,\"members\":[", n);
        emit_raw(buf);
        for (int i = 0; i < n; i++) {
            if (i)
                emit(',');
            const cJSON* child = cJSON_GetArrayItem(item, i);
            emit_raw("{\"k\":");
            emit_json_str(child ? child->string : "");
            emit_raw(",\"v\":");
            describe(child);
            emit('}');
        }
        emit_raw("]}");
    } else {
        emit_raw("{\"t\":\"invalid\"}");
    }
}

/* ---------- mutation trace oracle ---------- */

static int trace_first_step = 1;

static void trace_begin(const char *scenario) {
    outlen = 0;
    trace_first_step = 1;
    emit_raw("{\"scenario\":");
    emit_json_str(scenario);
    emit_raw(",\"steps\":[");
}

static void trace_step(const char *step, const cJSON *tree) {
    if (!trace_first_step) {
        emit(',');
    }
    trace_first_step = 0;
    emit_raw("{\"step\":");
    emit_json_str(step);
    emit_raw(",\"tree\":");
    describe(tree);
    emit('}');
}

static void trace_result_step(const char *step, cJSON_bool ok, const cJSON *tree) {
    if (!trace_first_step) {
        emit(',');
    }
    trace_first_step = 0;
    emit_raw("{\"step\":");
    emit_json_str(step);
    emit_raw(",\"ok\":");
    emit_raw(ok ? "true" : "false");
    emit_raw(",\"tree\":");
    describe(tree);
    emit('}');
}

static void trace_compare_step(const char *step, cJSON_bool equal) {
    if (!trace_first_step) {
        emit(',');
    }
    trace_first_step = 0;
    emit_raw("{\"step\":");
    emit_json_str(step);
    emit_raw(",\"equal\":");
    emit_raw(equal ? "true" : "false");
    emit('}');
}

static void trace_accessor_step(const char *step, const cJSON *item) {
    double number = cJSON_GetNumberValue(item);
    char number_buffer[64];
    char *string = cJSON_GetStringValue(item);

    if (!trace_first_step) {
        emit(',');
    }
    trace_first_step = 0;
    emit_raw("{\"step\":");
    emit_json_str(step);
    emit_raw(",\"number\":");
    if (isnan(number)) {
        emit_raw("null");
    } else {
        snprintf(number_buffer, sizeof(number_buffer), "%1.17g", number);
        emit_raw(number_buffer);
    }
    emit_raw(",\"number_is_nan\":");
    emit_raw(isnan(number) ? "true" : "false");
    emit_raw(",\"string\":");
    if (string == NULL) {
        emit_raw("null");
    } else {
        emit_json_str(string);
    }
    emit('}');
}

static void trace_end(void) {
    emit_raw("]}");
}

static void mutation_array_ownership(void) {
    cJSON *root = cJSON_CreateArray();
    cJSON *detached;

    trace_begin("array_ownership");
    trace_step("create_array", root);
    cJSON_AddItemToArray(root, cJSON_CreateNumber(1));
    trace_step("add_1", root);
    cJSON_AddItemToArray(root, cJSON_CreateString("two"));
    trace_step("add_two", root);
    cJSON_AddItemToArray(root, cJSON_CreateNumber(3));
    trace_step("add_3", root);

    detached = cJSON_DetachItemFromArray(root, 1);
    trace_step("detach_returns_caller_owned", detached);
    trace_step("after_detach", root);
    cJSON_Delete(detached);
    trace_step("after_caller_deletes_detached", root);

    cJSON_DeleteItemFromArray(root, 1);
    trace_step("after_delete_index_1", root);
    cJSON_AddItemToArray(root, cJSON_CreateNumber(4));
    trace_step("add_4", root);
    cJSON_ReplaceItemInArray(root, 0, cJSON_CreateNumber(9));
    trace_step("after_replace_index_0", root);
    trace_end();

    printf("%.*s\n", (int)outlen, out);
    cJSON_Delete(root);
}

static void mutation_object_ownership(void) {
    cJSON *root = cJSON_CreateObject();
    cJSON *detached;

    trace_begin("object_ownership");
    trace_step("create_object", root);
    cJSON_AddItemToObject(root, "Alpha", cJSON_CreateNumber(1));
    trace_step("add_Alpha", root);
    cJSON_AddItemToObject(root, "alpha", cJSON_CreateNumber(2));
    trace_step("add_alpha", root);

    detached = cJSON_DetachItemFromObject(root, "ALPHA");
    trace_step("detach_case_insensitive_returns_caller_owned", detached);
    trace_step("after_case_insensitive_detach", root);
    cJSON_Delete(detached);

    detached = cJSON_DetachItemFromObjectCaseSensitive(root, "alpha");
    trace_step("detach_case_sensitive_returns_caller_owned", detached);
    trace_step("after_case_sensitive_detach", root);
    cJSON_Delete(detached);

    cJSON_AddItemToObject(root, "Gone", cJSON_CreateTrue());
    cJSON_DeleteItemFromObject(root, "gone");
    trace_step("after_delete_case_insensitive", root);
    cJSON_AddItemToObject(root, "Exact", cJSON_CreateFalse());
    cJSON_DeleteItemFromObjectCaseSensitive(root, "Exact");
    trace_step("after_delete_case_sensitive", root);

    cJSON_AddItemToObject(root, "Name", cJSON_CreateString("old"));
    cJSON_ReplaceItemInObject(root, "name", cJSON_CreateString("new"));
    trace_step("after_replace_case_insensitive", root);
    cJSON_ReplaceItemInObjectCaseSensitive(root, "name", cJSON_CreateBool(0));
    trace_step("after_replace_case_sensitive", root);
    trace_end();

    printf("%.*s\n", (int)outlen, out);
    cJSON_Delete(root);
}

static void mutation_constructors(void) {
    cJSON *root = cJSON_CreateArray();

    trace_begin("constructors");
    trace_step("create_array", root);
    cJSON_AddItemToArray(root, cJSON_CreateNull());
    trace_step("create_null", root);
    cJSON_AddItemToArray(root, cJSON_CreateTrue());
    trace_step("create_true", root);
    cJSON_AddItemToArray(root, cJSON_CreateFalse());
    trace_step("create_false", root);
    cJSON_AddItemToArray(root, cJSON_CreateBool(1));
    trace_step("create_bool_true", root);
    cJSON_AddItemToArray(root, cJSON_CreateBool(0));
    trace_step("create_bool_false", root);
    cJSON_AddItemToArray(root, cJSON_CreateNumber(-12.5));
    trace_step("create_number", root);
    cJSON_AddItemToArray(root, cJSON_CreateString("owned"));
    trace_step("create_string", root);
    cJSON_AddItemToArray(root, cJSON_CreateRaw("{\"raw\":true}"));
    trace_step("create_raw", root);
    cJSON_AddItemToArray(root, cJSON_CreateArray());
    trace_step("create_nested_array", root);
    cJSON_AddItemToArray(root, cJSON_CreateObject());
    trace_step("create_nested_object", root);
    trace_end();

    printf("%.*s\n", (int)outlen, out);
    cJSON_Delete(root);
}

static void mutation_typed_arrays(void) {
    const int ints[] = {-2, 0, 7};
    const float floats[] = {1.5f, -0.25f};
    const double doubles[] = {0.1, 3.141592653589793};
    const char *strings[] = {"red", "blue"};
    cJSON *root = cJSON_CreateArray();

    trace_begin("typed_arrays");
    trace_step("create_array", root);
    cJSON_AddItemToArray(root, cJSON_CreateIntArray(ints, 3));
    trace_step("create_int_array", root);
    cJSON_AddItemToArray(root, cJSON_CreateFloatArray(floats, 2));
    trace_step("create_float_array", root);
    cJSON_AddItemToArray(root, cJSON_CreateDoubleArray(doubles, 2));
    trace_step("create_double_array", root);
    cJSON_AddItemToArray(root, cJSON_CreateStringArray(strings, 2));
    trace_step("create_string_array", root);
    trace_end();

    printf("%.*s\n", (int)outlen, out);
    cJSON_Delete(root);
}

static void mutation_insert_positions(void) {
    cJSON *root = cJSON_CreateArray();
    cJSON *past_end;
    cJSON *negative;
    cJSON_bool inserted;

    trace_begin("insert_positions");
    cJSON_AddItemToArray(root, cJSON_CreateNumber(1));
    cJSON_AddItemToArray(root, cJSON_CreateNumber(3));
    trace_step("initial", root);

    inserted = cJSON_InsertItemInArray(root, 0, cJSON_CreateNumber(0));
    trace_result_step("insert_at_0", inserted, root);
    inserted = cJSON_InsertItemInArray(root, 2, cJSON_CreateNumber(2));
    trace_result_step("insert_in_middle", inserted, root);
    inserted = cJSON_InsertItemInArray(root, 4, cJSON_CreateNumber(4));
    trace_result_step("insert_at_end", inserted, root);

    past_end = cJSON_CreateNumber(99);
    inserted = cJSON_InsertItemInArray(root, 99, past_end);
    trace_result_step("insert_past_end", inserted, root);
    if (!inserted) {
        cJSON_Delete(past_end);
    }

    negative = cJSON_CreateNumber(-1);
    inserted = cJSON_InsertItemInArray(root, -1, negative);
    trace_result_step("insert_negative_index", inserted, root);
    if (!inserted) {
        cJSON_Delete(negative);
    }
    trace_end();

    printf("%.*s\n", (int)outlen, out);
    cJSON_Delete(root);
}

static void mutation_duplicate_compare(void) {
    cJSON *source = cJSON_CreateObject();
    cJSON *items = cJSON_CreateArray();
    cJSON *shallow;
    cJSON *deep;
    cJSON *left;
    cJSON *right;
    cJSON *ordered_left;
    cJSON *ordered_right;

    cJSON_AddItemToObject(source, "Name", cJSON_CreateString("source"));
    cJSON_AddItemToArray(items, cJSON_CreateNumber(1));
    cJSON_AddItemToArray(items, cJSON_CreateString("two"));
    cJSON_AddItemToObject(source, "Items", items);

    trace_begin("duplicate_compare");
    trace_step("source", source);
    shallow = cJSON_Duplicate(source, 0);
    trace_step("duplicate_non_recursive", shallow);
    deep = cJSON_Duplicate(source, 1);
    trace_step("duplicate_recursive", deep);
    trace_compare_step(
        "deep_equals_source_case_sensitive",
        cJSON_Compare(source, deep, 1)
    );

    cJSON_ReplaceItemInObjectCaseSensitive(source, "Name", cJSON_CreateString("changed"));
    trace_step("source_after_replace", source);
    trace_step("deep_after_source_replace", deep);
    trace_compare_step(
        "deep_differs_after_source_replace",
        cJSON_Compare(source, deep, 1)
    );

    left = cJSON_CreateObject();
    right = cJSON_CreateObject();
    cJSON_AddItemToObject(left, "Key", cJSON_CreateNumber(7));
    cJSON_AddItemToObject(right, "key", cJSON_CreateNumber(7));
    trace_step("case_left", left);
    trace_step("case_right", right);
    trace_compare_step(
        "keys_equal_case_insensitive",
        cJSON_Compare(left, right, 0)
    );
    trace_compare_step(
        "keys_differ_case_sensitive",
        cJSON_Compare(left, right, 1)
    );

    ordered_left = cJSON_CreateObject();
    ordered_right = cJSON_CreateObject();
    cJSON_AddItemToObject(ordered_left, "one", cJSON_CreateNumber(1));
    cJSON_AddItemToObject(ordered_left, "two", cJSON_CreateNumber(2));
    cJSON_AddItemToObject(ordered_right, "two", cJSON_CreateNumber(2));
    cJSON_AddItemToObject(ordered_right, "one", cJSON_CreateNumber(1));
    trace_step("ordered_left", ordered_left);
    trace_step("ordered_right", ordered_right);
    trace_compare_step(
        "object_member_order_ignored",
        cJSON_Compare(ordered_left, ordered_right, 1)
    );

    cJSON_Delete(source);
    trace_step("deep_after_source_delete", deep);
    trace_end();

    printf("%.*s\n", (int)outlen, out);
    cJSON_Delete(shallow);
    cJSON_Delete(deep);
    cJSON_Delete(left);
    cJSON_Delete(right);
    cJSON_Delete(ordered_left);
    cJSON_Delete(ordered_right);
}

static void mutation_value_accessors(void) {
    cJSON *number = cJSON_CreateNumber(-12.5);
    cJSON *string = cJSON_CreateString("text");

    trace_begin("value_accessors");
    trace_accessor_step("number_item", number);
    trace_accessor_step("string_item", string);
    trace_end();

    printf("%.*s\n", (int)outlen, out);
    cJSON_Delete(number);
    cJSON_Delete(string);
}

static int run_mutation(const char *scenario) {
    if (strcmp(scenario, "array_ownership") == 0) {
        mutation_array_ownership();
    } else if (strcmp(scenario, "object_ownership") == 0) {
        mutation_object_ownership();
    } else if (strcmp(scenario, "constructors") == 0) {
        mutation_constructors();
    } else if (strcmp(scenario, "typed_arrays") == 0) {
        mutation_typed_arrays();
    } else if (strcmp(scenario, "insert_positions") == 0) {
        mutation_insert_positions();
    } else if (strcmp(scenario, "duplicate_compare") == 0) {
        mutation_duplicate_compare();
    } else if (strcmp(scenario, "value_accessors") == 0) {
        mutation_value_accessors();
    } else {
        printf("__UNKNOWN_MUTATION_SCENARIO__\n");
        return 1;
    }
    return 0;
}

int main(int argc, char** argv) {
    const char* mode = argc > 1 ? argv[1] : "unformatted";
    static char input[1 << 20];
    size_t n = fread(input, 1, sizeof(input) - 1, stdin);
    input[n] = '\0';

    if (strcmp(mode, "mutation") == 0) {
        return run_mutation(input);
    }

    cJSON* root = cJSON_ParseWithLength(input, n);
    if (root == NULL) {
        printf("__PARSE_ERROR__\n");
        cJSON_Delete(root); /* delete(NULL) is a no-op, exercises the guard */
        return 0;
    }

    if (strcmp(mode, "inspect") == 0) {
        describe(root);
        printf("%.*s\n", (int)outlen, out);
    } else {
        char* printed = (strcmp(mode, "formatted") == 0) ? cJSON_Print(root)
                                                          : cJSON_PrintUnformatted(root);
        if (printed == NULL) {
            printf("__PRINT_ERROR__\n");
        } else {
            printf("%s\n", printed);
            free(printed);
        }
    }

    cJSON_Delete(root);
    return 0;
}
