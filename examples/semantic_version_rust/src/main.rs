//! Tiny CLI: read version strings (one per line from args or stdin), print the
//! normalized form, or the parse error to stderr. Exists so `cargo run` is a
//! meaningful end-to-end stage in port_eval.

use std::io::Read;
use std::process::ExitCode;

use semantic_version_rust::Version;

fn report(input: &str) -> bool {
    match Version::parse(input) {
        Ok(v) => {
            println!("{}", v);
            true
        }
        Err(msg) => {
            eprintln!("{}", msg);
            false
        }
    }
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mut ok = true;
    if args.is_empty() {
        let mut buf = String::new();
        let _ = std::io::stdin().read_to_string(&mut buf);
        for line in buf.lines().filter(|l| !l.trim().is_empty()) {
            ok &= report(line.trim());
        }
    } else {
        for a in &args {
            ok &= report(a);
        }
    }
    if ok {
        ExitCode::SUCCESS
    } else {
        ExitCode::from(1)
    }
}
