# charset-normalizer context packs

This directory contains the saved GraphRAG/context-pack handoff snapshot used for
the scoped `charset-normalizer` Python→Rust stress-test.

The packs are kept beside the Rust port instead of at repository root so they
remain clearly tied to this example. They are useful as review material and as a
portable memory bundle for local agents, but the source of truth is still the
vendored Python reference plus a graph regenerated from it.

To refresh from the repo root when a local graph is available:

```bash
uv run python scripts/context_pack.py "api:from_bytes" --graph byog_charset_normalizer --full-text
```

`byog_charset_normalizer/` is intentionally ignored by Git like the other BYOG
snapshot directories. Keep it locally when you want fast graph queries without
re-indexing; regenerate it when extractor behavior changes.
