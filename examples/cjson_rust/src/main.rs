//! CLI mirroring the C golden runner default mode: read JSON from stdin, parse,
//! print unformatted (or `__PARSE_ERROR__`), and drop the tree.

use std::io::{self, Read, Write};

use cjson_rust::{parse, print_unformatted};

fn main() {
    let mut input = Vec::new();
    io::stdin().read_to_end(&mut input).expect("read stdin");

    let stdout = io::stdout();
    let mut w = stdout.lock();
    match parse(&input) {
        Some(root) => {
            let out = print_unformatted(&root);
            w.write_all(&out).expect("write");
            w.write_all(b"\n").expect("write");
            // `root` drops here, exercising the recursive ownership free.
        }
        None => {
            w.write_all(b"__PARSE_ERROR__\n").expect("write");
        }
    }
}
