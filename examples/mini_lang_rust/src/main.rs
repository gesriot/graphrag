//! CLI entry point. Mirrors examples/mini_lang/main.py: read source, run, print.
//! Exit 0 on success, 1 on any MiniLangError (message to stderr).
//!
//! Behavioral contract scope: the golden_*.json cases pin `run_source` (language
//! semantics) only. CLI file I/O is NOT in the golden contract, so the exact
//! error *text* for a missing file is not required to match Python's traceback.
//! The *outcome* is kept faithful, though: like Python's uncaught open() error, a
//! missing/unreadable file fails with exit 1 rather than silently running empty.

use std::io::Read;
use std::process::ExitCode;

use mini_lang_rust::run_source;

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().collect();
    let source = if args.len() > 1 {
        match std::fs::read_to_string(&args[1]) {
            Ok(s) => s,
            Err(e) => {
                eprintln!("cannot read {}: {}", args[1], e);
                return ExitCode::from(1);
            }
        }
    } else {
        let mut buf = String::new();
        let _ = std::io::stdin().read_to_string(&mut buf);
        buf
    };

    let (outputs, error) = run_source(&source);
    for line in &outputs {
        println!("{}", line);
    }
    match error {
        Some(msg) => {
            eprintln!("{}", msg);
            ExitCode::from(1)
        }
        None => ExitCode::SUCCESS,
    }
}
