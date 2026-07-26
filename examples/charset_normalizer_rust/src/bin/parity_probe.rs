//! Parity probe helper binary for exhaustive CD + codec tests.
//! Invoked by pytest harness in tests/test_codec_cd_parity.py
//! Does NOT modify the production CLI (main.rs) or core behavior.

use charset_normalizer_rust::{cd, from_bytes, CharsetMatch};
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

/// Emit a deliberately simple, tab-delimited detection observation for the
/// seeded Python-vs-Rust differential harness.  Hex avoids JSON escaping and
/// preserves an exact decoded UTF-8 payload without adding a runtime-only
/// serialization dependency to this test helper.
fn detect_file(path: &str) -> Result<(), String> {
    let payload = std::fs::read(path)
        .map_err(|error| format!("unable to read differential payload '{}': {}", path, error))?;
    let matches = from_bytes(&payload);
    let candidate_encodings = matches
        .results
        .iter()
        .map(|candidate| candidate.encoding.as_str())
        .collect::<Vec<_>>()
        .join("|");

    match matches.best() {
        Some(best) => {
            let decoded_hex = match best.decoded() {
                Some(decoded) => to_hex(decoded.as_bytes()),
                // A detector result without a strict decoded form is itself a
                // useful observation. The harness records it as a mismatch.
                None => "-".to_string(),
            };
            println!(
                "BEST\t{}\t{}\t{:.17}\t{:.17}\t{}\t{}\t{}\t{}",
                best.encoding,
                match best.language.as_deref() {
                    Some(language) => language,
                    None => "Unknown",
                },
                best.chaos,
                best.coherence,
                u8::from(best.bom),
                matches.len(),
                candidate_encodings,
                decoded_hex
            );
        }
        None => println!("NONE\t{}\t{}", matches.len(), candidate_encodings),
    }

    Ok(())
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
        "detect-file" => {
            if args.len() < 3 {
                eprintln!("usage: parity_probe detect-file <payload-path>");
                return ExitCode::from(2);
            }
            if let Err(error) = detect_file(&args[2]) {
                eprintln!("parity_probe: {}", error);
                return ExitCode::from(2);
            }
        }
        other => {
            eprintln!("unknown subcommand: {}", other);
            return ExitCode::from(2);
        }
    }
    ExitCode::SUCCESS
}
