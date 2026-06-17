//! Recursive-descent parser: tokens -> statements. Mirrors examples/mini_lang/parser.py.

use crate::ast_nodes::{Expr, Stmt};
use crate::errors::{pyrepr, MiniLangError};
use crate::tokens::{Token, TokenKind};

pub struct Parser {
    tokens: Vec<Token>,
    i: usize,
}

impl Parser {
    pub fn new(tokens: Vec<Token>) -> Self {
        Parser { tokens, i: 0 }
    }

    fn peek(&self) -> &Token {
        &self.tokens[self.i]
    }

    fn advance(&mut self) -> Token {
        let tok = self.tokens[self.i].clone();
        self.i += 1;
        tok
    }

    fn expect(&mut self, kind: TokenKind) -> Result<Token, MiniLangError> {
        let tok = self.peek();
        if tok.kind != kind {
            return Err(MiniLangError::Parse(format!(
                "expected {}, got {} {}",
                kind.name(),
                tok.kind.name(),
                pyrepr(&tok.text),
            )));
        }
        Ok(self.advance())
    }

    pub fn parse_program(&mut self) -> Result<Vec<Stmt>, MiniLangError> {
        let mut stmts = Vec::new();
        while self.peek().kind != TokenKind::Eof {
            if self.peek().kind == TokenKind::Semicolon {
                self.advance();
                continue;
            }
            stmts.push(self.statement()?);
            let nxt = self.peek().kind;
            if nxt != TokenKind::Semicolon && nxt != TokenKind::Eof {
                let tok = self.peek();
                return Err(MiniLangError::Parse(format!(
                    "unexpected token {} after statement",
                    pyrepr(&tok.text)
                )));
            }
        }
        Ok(stmts)
    }

    fn statement(&mut self) -> Result<Stmt, MiniLangError> {
        if self.peek().kind == TokenKind::Let {
            self.advance();
            let name = self.expect(TokenKind::Ident)?.text;
            self.expect(TokenKind::Equals)?;
            return Ok(Stmt::Let(name, self.expr()?));
        }
        Ok(Stmt::Expr(self.expr()?))
    }

    fn expr(&mut self) -> Result<Expr, MiniLangError> {
        let mut node = self.term()?;
        while matches!(self.peek().kind, TokenKind::Plus | TokenKind::Minus) {
            let op = self.advance().text.chars().next().unwrap();
            node = Expr::Binary(op, Box::new(node), Box::new(self.term()?));
        }
        Ok(node)
    }

    fn term(&mut self) -> Result<Expr, MiniLangError> {
        let mut node = self.factor()?;
        while matches!(self.peek().kind, TokenKind::Star | TokenKind::Slash) {
            let op = self.advance().text.chars().next().unwrap();
            node = Expr::Binary(op, Box::new(node), Box::new(self.factor()?));
        }
        Ok(node)
    }

    fn factor(&mut self) -> Result<Expr, MiniLangError> {
        if self.peek().kind == TokenKind::Minus {
            self.advance();
            return Ok(Expr::Unary(Box::new(self.factor()?)));
        }
        self.primary()
    }

    fn primary(&mut self) -> Result<Expr, MiniLangError> {
        let tok = self.peek().clone();
        match tok.kind {
            TokenKind::Number => {
                self.advance();
                Ok(Expr::Number(tok.text.parse::<f64>().unwrap()))
            }
            TokenKind::Ident => {
                self.advance();
                Ok(Expr::Var(tok.text))
            }
            TokenKind::LParen => {
                self.advance();
                let node = self.expr()?;
                self.expect(TokenKind::RParen)?;
                Ok(node)
            }
            _ => {
                if tok.text.is_empty() {
                    Err(MiniLangError::Parse("unexpected end of input".to_string()))
                } else {
                    Err(MiniLangError::Parse(format!(
                        "unexpected token {}",
                        pyrepr(&tok.text)
                    )))
                }
            }
        }
    }
}

pub fn parse(tokens: Vec<Token>) -> Result<Vec<Stmt>, MiniLangError> {
    Parser::new(tokens).parse_program()
}
