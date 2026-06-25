/* Golden runner for the inih C->Rust port (default config).
 *
 * Reads INI text from stdin and prints the ground-truth contract as JSON:
 *   {"result": <code>, "events": [{"section":..,"name":..,"value":..}, ...]}
 * where `result` is inih's return (0, or the first-error line number) and
 * `events` is the ordered sequence of handler callbacks.
 *
 * argv[1] selects the input path being exercised:
 *   "string" (default) -> ini_parse_string_length  (string reader)
 *   "file"             -> ini_parse_file            (fgets over a tmpfile)
 * Both must agree; the Python contract test asserts string<->file parity.
 *
 * Build: cc -I <inih dir> -o runner runner.c ini.c
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "ini.h"

static char ev[1 << 16];
static size_t evlen = 0;
static int ev_first = 1;

static void emit(char c) {
    if (evlen < sizeof(ev) - 1)
        ev[evlen++] = c;
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

static int handler(void* user, const char* section, const char* name,
                   const char* value) {
    (void)user;
    if (!ev_first)
        emit(',');
    ev_first = 0;
    emit('{');
    emit_raw("\"section\":");
    emit_json_str(section);
    emit_raw(",\"name\":");
    emit_json_str(name);
    emit_raw(",\"value\":");
    emit_json_str(value);
    emit('}');
    return 1; /* success: keep parsing so `result` reflects inih's own errors */
}

int main(int argc, char** argv) {
    const char* mode = argc > 1 ? argv[1] : "string";
    static char input[1 << 16];
    size_t n = fread(input, 1, sizeof(input) - 1, stdin);
    input[n] = '\0';

    int result;
    if (strcmp(mode, "file") == 0) {
        FILE* f = tmpfile();
        if (!f) {
            fprintf(stderr, "tmpfile failed\n");
            return 2;
        }
        fwrite(input, 1, n, f);
        rewind(f);
        result = ini_parse_file(f, handler, NULL);
        fclose(f);
    } else {
        result = ini_parse_string_length(input, n, handler, NULL);
    }

    printf("{\"result\":%d,\"events\":[%.*s]}\n", result, (int)evlen, ev);
    return 0;
}
