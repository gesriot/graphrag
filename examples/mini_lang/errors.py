"""Error types. Each carries a stable ``label`` so golden contracts can match
``"<label>: <message>"`` identically across the Python source and the Rust port."""

from __future__ import annotations


class MiniLangError(Exception):
    label = "Error"

    def formatted(self) -> str:
        return f"{self.label}: {self}"


class LexError(MiniLangError):
    label = "LexError"


class ParseError(MiniLangError):
    label = "ParseError"


class UndefinedVariable(MiniLangError):
    label = "UndefinedVariable"


class DivisionByZero(MiniLangError):
    label = "DivisionByZero"
