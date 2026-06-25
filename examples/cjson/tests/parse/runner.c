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
 * exactly without depending on float *printing* (deferred to a later sub-stage).
 *
 * Build: cc -I <cjson dir> -o runner runner.c cJSON.c
 */
#include <stdint.h>
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

int main(int argc, char** argv) {
    const char* mode = argc > 1 ? argv[1] : "unformatted";
    static char input[1 << 20];
    size_t n = fread(input, 1, sizeof(input) - 1, stdin);
    input[n] = '\0';

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
