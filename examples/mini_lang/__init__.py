"""Mini expression language (lexer -> parser -> AST -> eval) for GraphRAG experiments.

Pure Python, fully deterministic, no external runtime deps. Designed as the second
Python->Rust porting target: tokenizer/parser/AST/eval map cleanly onto Rust
enum + match, with sharp golden contracts (source -> stdout / error).
"""
