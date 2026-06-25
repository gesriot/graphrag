/* Dedicated golden runner for the vendored jsmn.h (default mode: non-strict,
 * no JSMN_PARENT_LINKS). Reads JSON from stdin; argv[1] = token capacity
 * (negative => NULL count-only). Prints {"result":N,"tokens":[{type,start,end,size}]}. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "jsmn.h"

int main(int argc, char **argv) {
  int cap = (argc > 1) ? atoi(argv[1]) : 0;
  char *buf = NULL;
  size_t len = 0, capb = 0;
  int c;
  while ((c = getchar()) != EOF) {
    if (len + 1 >= capb) { capb = capb ? capb * 2 : 256; buf = realloc(buf, capb); }
    buf[len++] = (char)c;
  }
  if (buf == NULL) { buf = malloc(1); }
  jsmn_parser p;
  jsmn_init(&p);
  int r;
  jsmntok_t *toks = NULL;
  if (cap < 0) {
    r = jsmn_parse(&p, buf, len, NULL, 0);
  } else {
    toks = calloc((size_t)(cap > 0 ? cap : 1), sizeof(jsmntok_t));
    r = jsmn_parse(&p, buf, len, toks, (unsigned int)cap);
  }
  printf("{\"result\":%d,\"tokens\":[", r);
  int n = (cap >= 0 && r > 0) ? r : 0;
  for (int i = 0; i < n; i++) {
    printf("%s{\"type\":%d,\"start\":%d,\"end\":%d,\"size\":%d}",
           i ? "," : "", toks[i].type, toks[i].start, toks[i].end, toks[i].size);
  }
  printf("]}\n");
  return 0;
}
