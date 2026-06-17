//! Tiny CLI so `cargo run` is a meaningful end-to-end stage in port_eval.
//! With two file arguments it prints the diff ops of their contents; with no
//! arguments it is a no-op (exit 0).

use std::process::ExitCode;

use diff_match_patch_rust::{DiffMatchPatch, DELETE, EQUAL, INSERT};

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.len() < 2 {
        return ExitCode::SUCCESS;
    }
    let text1 = match std::fs::read_to_string(&args[0]) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("cannot read {}: {}", args[0], e);
            return ExitCode::from(1);
        }
    };
    let text2 = match std::fs::read_to_string(&args[1]) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("cannot read {}: {}", args[1], e);
            return ExitCode::from(1);
        }
    };
    let dmp = DiffMatchPatch::new();
    for (op, text) in dmp.diff_main(&text1, &text2, true) {
        let tag = match op {
            DELETE => "-",
            INSERT => "+",
            EQUAL => "=",
            _ => "?",
        };
        println!("{} {:?}", tag, text);
    }
    ExitCode::SUCCESS
}
