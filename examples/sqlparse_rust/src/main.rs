//! Tiny CLI so `cargo run` is a meaningful end-to-end stage in port_eval:
//! reads SQL from stdin and prints one split statement per line.

use std::io::Read;

fn main() {
    let mut sql = String::new();
    let _ = std::io::stdin().read_to_string(&mut sql);
    for stmt in sqlparse_rust::split::split(&sql, false) {
        println!("{}", stmt);
    }
}
