//! Tiny CLI so `cargo run` is a meaningful end-to-end stage in port_eval:
//! reads JSON from stdin and prints (result, tokens) like the C golden runner.
use std::io::Read;

fn main() {
    let mut buf = Vec::new();
    let _ = std::io::stdin().read_to_end(&mut buf);
    let (r, tokens) = jsmn_rust::parse_json(&buf, 256);
    println!("result={}", r);
    for t in tokens {
        println!(
            "type={} start={} end={} size={}",
            t.ttype, t.start, t.end, t.size
        );
    }
}
