//! AST node types. A small closed set -> idiomatic Rust enum + match.

#[derive(Debug, Clone, PartialEq)]
pub enum Expr {
    Number(f64),
    Var(String),
    Unary(Box<Expr>),                   // unary minus
    Binary(char, Box<Expr>, Box<Expr>), // op in {'+','-','*','/'}
}

#[derive(Debug, Clone, PartialEq)]
pub enum Stmt {
    Let(String, Expr),
    Expr(Expr),
}
