//! CLI mirroring the C golden runner: read INI from stdin, print the parse
//! result and the ordered callback events as JSON.

use std::cell::RefCell;
use std::io::Read;

use inih_rust::parse_string;

fn json_escape(s: &[u8], out: &mut String) {
    out.push('"');
    for &c in s {
        match c {
            b'"' => out.push_str("\\\""),
            b'\\' => out.push_str("\\\\"),
            b'\n' => out.push_str("\\n"),
            b'\r' => out.push_str("\\r"),
            b'\t' => out.push_str("\\t"),
            0x00..=0x1f => out.push_str(&format!("\\u{:04x}", c)),
            _ => out.push(c as char),
        }
    }
    out.push('"');
}

fn main() {
    let mut input = Vec::new();
    std::io::stdin()
        .read_to_end(&mut input)
        .expect("read stdin");

    let events = RefCell::new(String::new());
    let first = RefCell::new(true);
    let result = parse_string(&input, |section, name, value| {
        let mut ev = events.borrow_mut();
        if !*first.borrow() {
            ev.push(',');
        }
        *first.borrow_mut() = false;
        ev.push_str("{\"section\":");
        json_escape(section, &mut ev);
        ev.push_str(",\"name\":");
        json_escape(name, &mut ev);
        ev.push_str(",\"value\":");
        json_escape(value, &mut ev);
        ev.push('}');
        true
    });

    println!("{{\"result\":{},\"events\":[{}]}}", result, events.borrow());
}
