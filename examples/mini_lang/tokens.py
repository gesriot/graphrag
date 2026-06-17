"""Token kinds and the Token value type."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TokenKind(Enum):
    NUMBER = "NUMBER"
    IDENT = "IDENT"
    LET = "LET"
    PLUS = "PLUS"
    MINUS = "MINUS"
    STAR = "STAR"
    SLASH = "SLASH"
    LPAREN = "LPAREN"
    RPAREN = "RPAREN"
    EQUALS = "EQUALS"
    SEMICOLON = "SEMICOLON"
    EOF = "EOF"


@dataclass
class Token:
    kind: TokenKind
    text: str
    pos: int
