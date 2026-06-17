"""Tree-walking evaluator. Runs statements against a variable environment and
collects the formatted output of each bare-expression statement."""

from __future__ import annotations

from typing import Dict, List

from ast_nodes import BinOp, ExprStmt, LetStmt, Number, UnaryOp, Var
from errors import DivisionByZero, MiniLangError, UndefinedVariable


def format_number(x: float) -> str:
    """Stable, Rust-portable formatting: integers without a fractional part,
    otherwise a trimmed fixed-precision decimal."""
    if x == int(x) and abs(x) < 1e15:
        return str(int(x))
    return f"{x:.10f}".rstrip("0").rstrip(".")


class Interpreter:
    def __init__(self) -> None:
        self.env: Dict[str, float] = {}

    def run(self, stmts) -> List[str]:
        out: List[str] = []
        for stmt in stmts:
            if isinstance(stmt, LetStmt):
                self.env[stmt.name] = self.eval_expr(stmt.expr)
            elif isinstance(stmt, ExprStmt):
                out.append(format_number(self.eval_expr(stmt.expr)))
            else:  # pragma: no cover - closed statement set
                raise MiniLangError(f"unknown statement {stmt!r}")
        return out

    def eval_expr(self, node) -> float:
        if isinstance(node, Number):
            return node.value
        if isinstance(node, Var):
            if node.name not in self.env:
                raise UndefinedVariable(f"variable {node.name!r} is not defined")
            return self.env[node.name]
        if isinstance(node, UnaryOp):
            return -self.eval_expr(node.operand)
        if isinstance(node, BinOp):
            return self.eval_binop(node)
        raise MiniLangError(f"unknown expression {node!r}")  # pragma: no cover

    def eval_binop(self, node: BinOp) -> float:
        left = self.eval_expr(node.left)
        right = self.eval_expr(node.right)
        if node.op == "+":
            return left + right
        if node.op == "-":
            return left - right
        if node.op == "*":
            return left * right
        if node.op == "/":
            if right == 0:
                raise DivisionByZero("division by zero")
            return left / right
        raise MiniLangError(f"unknown operator {node.op!r}")  # pragma: no cover


def run(stmts) -> List[str]:
    return Interpreter().run(stmts)
