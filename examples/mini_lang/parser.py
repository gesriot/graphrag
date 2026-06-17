"""Recursive-descent parser: tokens -> list of statements.

Grammar (precedence climbing by hand):

    program := (statement (SEP)*)*            SEP = ';' or newline
    statement := 'let' IDENT '=' expr | expr
    expr   := term (('+' | '-') term)*
    term   := factor (('*' | '/') factor)*
    factor := '-' factor | primary
    primary:= NUMBER | IDENT | '(' expr ')'
"""

from __future__ import annotations

from typing import List

from ast_nodes import BinOp, ExprStmt, LetStmt, Number, Stmt, UnaryOp, Var
from errors import ParseError
from tokens import Token, TokenKind


class Parser:
    def __init__(self, tokens: List[Token]) -> None:
        self.tokens = tokens
        self.i = 0

    def _peek(self) -> Token:
        return self.tokens[self.i]

    def _advance(self) -> Token:
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def _expect(self, kind: TokenKind) -> Token:
        tok = self._peek()
        if tok.kind is not kind:
            raise ParseError(f"expected {kind.value}, got {tok.kind.value} {tok.text!r}")
        return self._advance()

    def parse_program(self) -> List[Stmt]:
        stmts: List[Stmt] = []
        while self._peek().kind is not TokenKind.EOF:
            if self._peek().kind is TokenKind.SEMICOLON:
                self._advance()
                continue
            stmts.append(self._statement())
            nxt = self._peek().kind
            if nxt not in (TokenKind.SEMICOLON, TokenKind.EOF):
                tok = self._peek()
                raise ParseError(f"unexpected token {tok.text!r} after statement")
        return stmts

    def _statement(self) -> Stmt:
        if self._peek().kind is TokenKind.LET:
            self._advance()
            name = self._expect(TokenKind.IDENT).text
            self._expect(TokenKind.EQUALS)
            return LetStmt(name, self._expr())
        return ExprStmt(self._expr())

    def _expr(self):
        node = self._term()
        while self._peek().kind in (TokenKind.PLUS, TokenKind.MINUS):
            op = self._advance().text
            node = BinOp(op, node, self._term())
        return node

    def _term(self):
        node = self._factor()
        while self._peek().kind in (TokenKind.STAR, TokenKind.SLASH):
            op = self._advance().text
            node = BinOp(op, node, self._factor())
        return node

    def _factor(self):
        if self._peek().kind is TokenKind.MINUS:
            self._advance()
            return UnaryOp("-", self._factor())
        return self._primary()

    def _primary(self):
        tok = self._peek()
        if tok.kind is TokenKind.NUMBER:
            self._advance()
            return Number(float(tok.text))
        if tok.kind is TokenKind.IDENT:
            self._advance()
            return Var(tok.text)
        if tok.kind is TokenKind.LPAREN:
            self._advance()
            node = self._expr()
            self._expect(TokenKind.RPAREN)
            return node
        raise ParseError(f"unexpected token {tok.text!r}" if tok.text else "unexpected end of input")


def parse(tokens: List[Token]) -> List[Stmt]:
    return Parser(tokens).parse_program()
