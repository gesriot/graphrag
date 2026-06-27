//! Parity probe helper binary for exhaustive CD + codec tests.
//! Invoked by pytest harness in tests/test_codec_cd_parity.py
//! Does NOT modify the production CLI (main.rs) or core behavior.

use charset_normalizer_rust::{cd, CharsetMatch};
use std::env;
use std::process::ExitCode;

fn from_hex(s: &str) -> Result<Vec<u8>, String> {
    if s.len() % 2 != 0 {
        return Err("odd hex length".to_string());
    }
    let mut out = Vec::with_capacity(s.len() / 2);
    let bytes = s.as_bytes();
    for i in (0..bytes.len()).step_by(2) {
        let h = (hex_val(bytes[i])? << 4) | hex_val(bytes[i + 1])?;
        out.push(h);
    }
    Ok(out)
}

fn hex_val(c: u8) -> Result<u8, String> {
    match c {
        b'0'..=b'9' => Ok(c - b'0'),
        b'a'..=b'f' => Ok(10 + c - b'a'),
        b'A'..=b'F' => Ok(10 + c - b'A'),
        _ => Err(format!("bad hex digit {}", c as char)),
    }
}

fn to_hex(data: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut s = String::with_capacity(data.len() * 2);
    for &b in data {
        s.push(HEX[(b >> 4) as usize] as char);
        s.push(HEX[(b & 0xf) as usize] as char);
    }
    s
}

fn emit_json_langs(langs: &[String]) {
    print!("[");
    for (i, l) in langs.iter().enumerate() {
        if i > 0 {
            print!(",");
        }
        // langs are ascii letters/spaces/dash, safe to inline
        print!("\"{}\"", l);
    }
    println!("]");
}

fn main() -> ExitCode {
    let args: Vec<String> = env::args().collect();
    if args.len() < 2 {
        eprintln!("parity_probe: need subcommand");
        return ExitCode::from(2);
    }
    let cmd = args[1].as_str();

    match cmd {
        "cd-langs" => {
            if args.len() < 3 {
                eprintln!("usage: parity_probe cd-langs <iana>");
                return ExitCode::from(2);
            }
            let name = &args[2];
            let langs = cd::encoding_languages(name);
            emit_json_langs(&langs);
        }
        "mb-langs" => {
            if args.len() < 3 {
                eprintln!("usage: parity_probe mb-langs <iana>");
                return ExitCode::from(2);
            }
            let name = &args[2];
            let langs = cd::mb_encoding_languages(name);
            emit_json_langs(&langs);
        }
        "probe-bytes" => {
            if args.len() < 3 {
                eprintln!("usage: parity_probe probe-bytes <iana>");
                return ExitCode::from(2);
            }
            let enc = &args[2];
            print!("{{");
            let mut first = true;
            for b in 0u8..=255u8 {
                if !first {
                    print!(",");
                }
                first = false;
                let m = CharsetMatch {
                    encoding: enc.clone(),
                    language: None,
                    language_ratios: vec![],
                    chaos: 0.0,
                    coherence: 0.0,
                    bom: false,
                    raw: vec![b],
                    preemptive_declaration: None,
                    submatches: vec![],
                };
                let key = format!("{:02x}", b);
                if let Some(d) = m.decoded() {
                    // For SB probes we expect 0 or 1 scalar; use its codepoint
                    let cp = d.chars().next().map(|c| c as u32).unwrap_or(0);
                    print!("\"{}\":{{\"ok\":true,\"cp\":{}}}", key, cp);
                } else {
                    print!("\"{}\":{{\"ok\":false}}", key);
                }
            }
            println!("}}");
        }
        "strict-decode" => {
            if args.len() < 4 {
                eprintln!("usage: parity_probe strict-decode <iana> <hexpayload>");
                return ExitCode::from(2);
            }
            let enc = &args[2];
            let payload = match from_hex(&args[3]) {
                Ok(v) => v,
                Err(e) => {
                    eprintln!("bad hex: {}", e);
                    return ExitCode::from(2);
                }
            };
            let m = CharsetMatch {
                encoding: enc.clone(),
                language: None,
                language_ratios: vec![],
                chaos: 0.0,
                coherence: 0.0,
                bom: false,
                raw: payload,
                preemptive_declaration: None,
                submatches: vec![],
            };
            match m.decoded() {
                Some(d) => {
                    let bytes = d.into_bytes();
                    print!("OK:{}", to_hex(&bytes));
                }
                None => {
                    print!("ERR");
                }
            }
        }
        "strict-encode" => {
            // strict-encode <target_enc> <utf8_hex_of_text>
            // Uses utf_8 as source raw for .decoded() to obtain the unicode text,
            // then .output(target) encodes it. This tests the exposed encode path.
            if args.len() < 4 {
                eprintln!("usage: parity_probe strict-encode <iana> <utf8_hex_text>");
                return ExitCode::from(2);
            }
            let target = &args[2];
            let text_bytes = match from_hex(&args[3]) {
                Ok(v) => v,
                Err(e) => {
                    eprintln!("bad hex: {}", e);
                    return ExitCode::from(2);
                }
            };
            let text = match String::from_utf8(text_bytes) {
                Ok(s) => s,
                Err(_) => {
                    eprintln!("utf8 text required for encode probe");
                    return ExitCode::from(2);
                }
            };
            let m = CharsetMatch {
                encoding: "utf_8".to_string(),
                language: None,
                language_ratios: vec![],
                chaos: 0.0,
                coherence: 0.0,
                bom: false,
                raw: text.as_bytes().to_vec(),
                preemptive_declaration: None,
                submatches: vec![],
            };
            match m.output(target) {
                Some(b) => {
                    print!("OK:{}", to_hex(&b));
                }
                None => {
                    print!("ERR");
                }
            }
        }
        other => {
            eprintln!("unknown subcommand: {}", other);
            return ExitCode::from(2);
        }
    }
    ExitCode::SUCCESS
}
