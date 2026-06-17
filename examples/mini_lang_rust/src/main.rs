//! CLI entry point. Mirrors examples/mini_lang/main.py: read source, run, print.
//! Exit 0 on success, 1 on any MiniLangError (message to stderr).

use std::io::Read;
use std::process::ExitCode;

use mini_lang_rust::run_source;

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().collect();
    let source = if args.len() > 1 {
        std::fs::read_to_string(&args[1]).unwrap_or_default()
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
