"""AST node value types. Deliberately a small closed set -> Rust enum + match."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Number:
    value: float


@dataclass
class Var:
    name: str


@dataclass
class UnaryOp:
    op: str  # "-"
    operand: "Expr"


@dataclass
class BinOp:
    op: str  # "+" "-" "*" "/"
    left: "Expr"
    right: "Expr"


# An expression is one of the node types above.
Expr = "Number | Var | UnaryOp | BinOp"


@dataclass
class LetStmt:
    name: str
    expr: "Expr"


@dataclass
class ExprStmt:
    expr: "Expr"


# A statement is a binding or a bare expression.
Stmt = "LetStmt | ExprStmt"
