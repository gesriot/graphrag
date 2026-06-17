//! Tree-walking evaluator. Mirrors examples/mini_lang/eval.py.

use std::collections::HashMap;

use crate::ast_nodes::{Expr, Stmt};
use crate::errors::MiniLangError;

/// Stable, matches Python `format_number`: integers without a fractional part,
/// otherwise a trimmed fixed-precision decimal.
pub fn format_number(x: f64) -> String {
    if x == x.trunc() && x.abs() < 1e15 {
        return format!("{}", x as i64);
    }
    let s = format!("{:.10}", x);
    s.trim_end_matches('0').trim_end_matches('.').to_string()
}

pub struct Interpreter {
    env: HashMap<String, f64>,
}

impl Interpreter {
    pub fn new() -> Self {
        Interpreter {
            env: HashMap::new(),
        }
    }

    pub fn run(&mut self, stmts: &[Stmt]) -> Result<Vec<String>, MiniLangError> {
        let mut out = Vec::new();
        for stmt in stmts {
            match stmt {
                Stmt::Let(name, expr) => {
                    let v = self.eval_expr(expr)?;
                    self.env.insert(name.clone(), v);
                }
                Stmt::Expr(expr) => {
                    let v = self.eval_expr(expr)?;
                    out.push(format_number(v));
                }
            }
        }
        Ok(out)
    }

    fn eval_expr(&self, node: &Expr) -> Result<f64, MiniLangError> {
        match node {
            Expr::Number(v) => Ok(*v),
            Expr::Var(name) => self.env.get(name).copied().ok_or_else(|| {
                MiniLangError::UndefinedVariable(format!("variable '{}' is not defined", name))
            }),
            Expr::Unary(operand) => Ok(-self.eval_expr(operand)?),
            Expr::Binary(op, left, right) => self.eval_binop(*op, left, right),
        }
    }

    fn eval_binop(&self, op: char, left: &Expr, right: &Expr) -> Result<f64, MiniLangError> {
        let l = self.eval_expr(left)?;
        let r = self.eval_expr(right)?;
        match op {
            '+' => Ok(l + r),
            '-' => Ok(l - r),
            '*' => Ok(l * r),
            '/' => {
                if r == 0.0 {
                    Err(MiniLangError::DivisionByZero(
                        "division by zero".to_string(),
                    ))
                } else {
                    Ok(l / r)
                }
            }
            _ => unreachable!("parser only emits + - * /"),
        }
    }
}

impl Default for Interpreter {
    fn default() -> Self {
        Self::new()
    }
}

pub fn run(stmts: &[Stmt]) -> Result<Vec<String>, MiniLangError> {
    Interpreter::new().run(stmts)
}
