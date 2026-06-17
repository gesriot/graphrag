"""Hand-written lexer: source text -> list of tokens."""

from __future__ import annotations

from typing import List

from errors import LexError
from tokens import Token, TokenKind

_SINGLE = {
    "+": TokenKind.PLUS,
    "-": TokenKind.MINUS,
    "*": TokenKind.STAR,
    "/": TokenKind.SLASH,
    "(": TokenKind.LPAREN,
    ")": TokenKind.RPAREN,
    "=": TokenKind.EQUALS,
    ";": TokenKind.SEMICOLON,
}


def _is_ident_start(c: str) -> bool:
    return c.isalpha() or c == "_"


def _is_ident_part(c: str) -> bool:
    return c.isalnum() or c == "_"


def tokenize(source: str) -> List[Token]:
    """Turn ``source`` into tokens, ending with an EOF token.

    Raises ``LexError`` on any character that cannot start a token.
    """
    tokens: List[Token] = []
    i = 0
    n = len(source)
    while i < n:
        c = source[i]
        if c in " \t\r":
            i += 1
            continue
        if c == "\n":
            # Newlines separate statements, just like ';'.
            tokens.append(Token(TokenKind.SEMICOLON, "\n", i))
            i += 1
            continue
        if c in _SINGLE:
            tokens.append(Token(_SINGLE[c], c, i))
            i += 1
            continue
        if c.isdigit() or c == ".":
            start = i
            seen_dot = False
            while i < n and (source[i].isdigit() or source[i] == "."):
                if source[i] == ".":
                    if seen_dot:
                        raise LexError(f"malformed number at position {start}")
                    seen_dot = True
                i += 1
            text = source[start:i]
            if text == ".":
                raise LexError(f"malformed number at position {start}")
            tokens.append(Token(TokenKind.NUMBER, text, start))
            continue
        if _is_ident_start(c):
            start = i
            while i < n and _is_ident_part(source[i]):
                i += 1
            text = source[start:i]
            kind = TokenKind.LET if text == "let" else TokenKind.IDENT
            tokens.append(Token(kind, text, start))
            continue
        raise LexError(f"unknown token {c!r} at position {i}")

    tokens.append(Token(TokenKind.EOF, "", n))
    return tokens
