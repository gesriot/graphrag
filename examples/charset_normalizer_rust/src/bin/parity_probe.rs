//! Parity probe helper binary for exhaustive CD + codec tests.
//! Invoked by pytest harness in tests/test_codec_cd_parity.py
//! Does NOT modify the production CLI (main.rs) or core behavior.

use charset_normalizer_rust::{
    cd, from_bytes, is_binary_path_with_options, is_binary_reader, utils, CharsetMatch,
    CliDetectionResult, FromBytesOptions, VERSION, VERSION_STRING,
};
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

fn parse_scalar(value: &str) -> Result<char, String> {
    let code_point = u32::from_str_radix(value, 16)
        .map_err(|error| format!("invalid scalar hex '{}': {}", value, error))?;
    char::from_u32(code_point).ok_or_else(|| format!("invalid Unicode scalar U+{code_point:04X}"))
}

fn output_match(source_encoding: &str, payload: Vec<u8>) -> CharsetMatch {
    CharsetMatch {
        encoding: source_encoding.to_string(),
        language: None,
        language_ratios: vec![],
        chaos: 0.0,
        coherence: 0.0,
        bom: false,
        raw: payload,
        preemptive_declaration: None,
        submatches: vec![],
    }
}

fn emit_bool(value: bool) {
    print!("{}", u8::from(value));
}

fn emit_char_helpers(character: char) {
    let range = utils::unicode_range(character).unwrap_or("-");
    print!("{range}");
    for value in [
        utils::is_accentuated(character),
        utils::is_latin(character),
        utils::is_punctuation(character),
        utils::is_symbol(character),
        utils::is_emoticon(character),
        utils::is_separator(character),
        utils::is_case_variable(character),
        utils::is_cjk(character),
        utils::is_hiragana(character),
        utils::is_katakana(character),
        utils::is_hangul(character),
        utils::is_thai(character),
        utils::is_arabic(character),
        utils::is_arabic_isolated_form(character),
        utils::is_cjk_uncommon(character),
        utils::is_unprintable(character),
    ] {
        print!("\t");
        emit_bool(value);
    }
    match utils::remove_accent(character) {
        Ok(value) => println!("\t{:x}", value as u32),
        Err(_) => println!("\tERR"),
    }
}

fn emit_json_langs(langs: &[String]) {
    print!("[");
    for (i, l) in langs.iter().enumerate() {
        if i > 0 {
            print!(",");
        }
        print!("\"{}\"", json_escape_ascii(l));
    }
    println!("]");
}

fn json_escape_ascii(value: &str) -> String {
    let mut escaped = String::new();
    for character in value.chars() {
        match character {
            '"' => escaped.push_str("\\\""),
            '\\' => escaped.push_str("\\\\"),
            '\n' => escaped.push_str("\\n"),
            '\r' => escaped.push_str("\\r"),
            '\t' => escaped.push_str("\\t"),
            character if character <= '\u{1f}' => {
                escaped.push_str(&format!("\\u{:04x}", character as u32));
            }
            character if character <= '\u{7f}' => escaped.push(character),
            character if character <= '\u{ffff}' => {
                escaped.push_str(&format!("\\u{:04x}", character as u32));
            }
            character => {
                let scalar = character as u32 - 0x1_0000;
                escaped.push_str(&format!(
                    "\\u{:04x}\\u{:04x}",
                    0xd800 + (scalar >> 10),
                    0xdc00 + (scalar & 0x3ff)
                ));
            }
        }
    }
    escaped
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

/// Emit the strict HZ mappings supplied by the port's current codec backend.
///
/// The output is intentionally a compact tab-delimited observation protocol for
/// the Python-oracle parity test and the re-runnable characterization script:
/// `E <scalar> <bytes>` for successful non-ASCII encodes and `D <pair>
/// <UTF-8-bytes>` for successful HZ shifted-pair decodes. Keeping the full
/// enumeration in this one process makes a table comparison practical without
/// making the production codec expose its implementation detail.
fn emit_hz_codec_map() {
    for scalar in 0x80..=0x10ffff {
        let Some(character) = char::from_u32(scalar) else {
            continue;
        };
        let text = character.to_string();
        if let Some(bytes) = output_match("utf_8", text.into_bytes()).output_strict("hz") {
            println!("E\t{scalar:x}\t{}", to_hex(&bytes));
        }
    }

    for lead in 0x21u8..=0x7e {
        for trail in 0x21u8..=0x7e {
            let payload = [b'~', b'{', lead, trail, b'~', b'}'];
            if let Some(decoded) = output_match("hz", payload.to_vec()).decoded() {
                println!("D\t{lead:02x}{trail:02x}\t{}", to_hex(decoded.as_bytes()));
            }
        }
    }
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
            match m.output_strict(target) {
                Some(b) => {
                    print!("OK:{}", to_hex(&b));
                }
                None => {
                    print!("ERR");
                }
            }
        }
        "hz-codec-map" => emit_hz_codec_map(),
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
        "version" => {
            println!("{}\t{}", VERSION_STRING, VERSION.join("."));
        }
        "is-binary-reader" => {
            if args.len() < 3 {
                eprintln!("usage: parity_probe is-binary-reader <payload-path>");
                return ExitCode::from(2);
            }
            let file = match std::fs::File::open(&args[2]) {
                Ok(file) => file,
                Err(error) => {
                    eprintln!("parity_probe: unable to open '{}': {}", args[2], error);
                    return ExitCode::from(2);
                }
            };
            match is_binary_reader(file) {
                Ok(value) => println!("{}", u8::from(value)),
                Err(error) => {
                    eprintln!("parity_probe: unable to classify '{}': {}", args[2], error);
                    return ExitCode::from(2);
                }
            }
        }
        "is-binary-path-options" => {
            if args.len() < 3 {
                eprintln!("usage: parity_probe is-binary-path-options <payload-path>");
                return ExitCode::from(2);
            }
            match is_binary_path_with_options(&args[2], FromBytesOptions::default()) {
                Ok(value) => println!("{}", u8::from(value)),
                Err(error) => {
                    eprintln!("parity_probe: unable to classify '{}': {}", args[2], error);
                    return ExitCode::from(2);
                }
            }
        }
        "byte-order-mark" => {
            if args.len() < 3 {
                eprintln!("usage: parity_probe byte-order-mark <hexpayload>");
                return ExitCode::from(2);
            }
            let payload = match from_hex(&args[2]) {
                Ok(payload) => payload,
                Err(error) => {
                    eprintln!("bad hex: {}", error);
                    return ExitCode::from(2);
                }
            };
            match from_bytes(&payload).best() {
                Some(best) => println!(
                    "{}\t{}",
                    u8::from(best.bom),
                    u8::from(best.byte_order_mark())
                ),
                None => println!("NONE"),
            }
        }
        "api-output" => {
            if args.len() < 5 {
                eprintln!("usage: parity_probe api-output <source-encoding> <hexpayload> <target-encoding>");
                return ExitCode::from(2);
            }
            let payload = match from_hex(&args[3]) {
                Ok(payload) => payload,
                Err(error) => {
                    eprintln!("bad hex: {}", error);
                    return ExitCode::from(2);
                }
            };
            match output_match(&args[2], payload).output(&args[4]) {
                Some(bytes) => println!("OK:{}", to_hex(&bytes)),
                None => println!("ERR"),
            }
        }
        "api-output-default" => {
            if args.len() < 4 {
                eprintln!("usage: parity_probe api-output-default <source-encoding> <hexpayload>");
                return ExitCode::from(2);
            }
            let payload = match from_hex(&args[3]) {
                Ok(payload) => payload,
                Err(error) => {
                    eprintln!("bad hex: {}", error);
                    return ExitCode::from(2);
                }
            };
            match output_match(&args[2], payload).output_default() {
                Some(bytes) => println!("OK:{}", to_hex(&bytes)),
                None => println!("ERR"),
            }
        }
        "api-submatch" => {
            if args.len() < 6 {
                eprintln!("usage: parity_probe api-submatch <left-encoding> <left-hex> <right-encoding> <right-hex>");
                return ExitCode::from(2);
            }
            let left = match from_hex(&args[3]) {
                Ok(payload) => payload,
                Err(error) => {
                    eprintln!("bad left hex: {}", error);
                    return ExitCode::from(2);
                }
            };
            let right = match from_hex(&args[5]) {
                Ok(payload) => payload,
                Err(error) => {
                    eprintln!("bad right hex: {}", error);
                    return ExitCode::from(2);
                }
            };
            let mut parent = output_match(&args[2], left);
            let child = output_match(&args[4], right);
            match parent.add_submatch(child) {
                Ok(()) => println!("OK\t{}", parent.submatch().len()),
                Err(error) => println!("ERR\t{:?}", error),
            }
        }
        "cli-result-fixture" => {
            let result = CliDetectionResult::new(
                "input/é.txt".to_string(),
                Some("cp1252".to_string()),
                vec!["windows_1252".to_string()],
                vec!["latin_1".to_string(), "iso8859_15".to_string()],
                "French".to_string(),
                vec!["Basic Latin".to_string(), "Latin-1 Supplement".to_string()],
                true,
                0.125,
                1.0,
                Some("output/😀.utf8".to_string()),
                false,
            );
            println!("{}", result.to_json());
        }
        "helper-char" => {
            if args.len() < 3 {
                eprintln!("usage: parity_probe helper-char <scalar-hex>");
                return ExitCode::from(2);
            }
            match parse_scalar(&args[2]) {
                Ok(character) => emit_char_helpers(character),
                Err(error) => {
                    eprintln!("parity_probe: {}", error);
                    return ExitCode::from(2);
                }
            }
        }
        "helper-iana" => {
            if args.len() < 4 {
                eprintln!("usage: parity_probe helper-iana <name> <strict-0-or-1>");
                return ExitCode::from(2);
            }
            let strict = args[3] == "1";
            match utils::iana_name(&args[2], strict) {
                Ok(name) => println!("OK:{name}"),
                Err(_) => println!("ERR"),
            }
        }
        "helper-multibyte" => {
            if args.len() < 3 {
                eprintln!("usage: parity_probe helper-multibyte <name>");
                return ExitCode::from(2);
            }
            println!("{}", u8::from(utils::is_multi_byte_encoding(&args[2])));
        }
        "helper-bom" => {
            if args.len() < 3 {
                eprintln!("usage: parity_probe helper-bom <hexpayload>");
                return ExitCode::from(2);
            }
            let payload = match from_hex(&args[2]) {
                Ok(payload) => payload,
                Err(error) => {
                    eprintln!("bad hex: {}", error);
                    return ExitCode::from(2);
                }
            };
            let (encoding, mark) = utils::identify_sig_or_bom(&payload);
            let mark = if mark.is_empty() {
                "-".to_string()
            } else {
                to_hex(&mark)
            };
            println!("{}\t{}", encoding.unwrap_or_else(|| "-".to_string()), mark);
        }
        "helper-strip" => {
            if args.len() < 3 {
                eprintln!("usage: parity_probe helper-strip <encoding>");
                return ExitCode::from(2);
            }
            println!("{}", u8::from(utils::should_strip_sig_or_bom(&args[2])));
        }
        "helper-secondary" => {
            if args.len() < 3 {
                eprintln!("usage: parity_probe helper-secondary <unicode-range>");
                return ExitCode::from(2);
            }
            println!("{}", u8::from(utils::is_unicode_range_secondary(&args[2])));
        }
        "helper-cp" => {
            if args.len() < 4 {
                eprintln!("usage: parity_probe helper-cp <left-encoding> <right-encoding>");
                return ExitCode::from(2);
            }
            println!(
                "{:.17}\t{}",
                utils::cp_similarity(&args[2], &args[3]),
                u8::from(utils::is_cp_similar(&args[2], &args[3]))
            );
        }
        "helper-specified" => {
            if args.len() < 4 {
                eprintln!("usage: parity_probe helper-specified <hexpayload> <search-zone>");
                return ExitCode::from(2);
            }
            let payload = match from_hex(&args[2]) {
                Ok(payload) => payload,
                Err(error) => {
                    eprintln!("bad hex: {}", error);
                    return ExitCode::from(2);
                }
            };
            let search_zone = match args[3].parse::<usize>() {
                Ok(value) => value,
                Err(error) => {
                    eprintln!("invalid search zone: {}", error);
                    return ExitCode::from(2);
                }
            };
            println!(
                "{}",
                utils::any_specified_encoding(&payload, search_zone)
                    .unwrap_or_else(|| "-".to_string())
            );
        }
        "helper-chunks" => {
            if args.len() < 9 {
                eprintln!("usage: parity_probe helper-chunks <encoding> <hexpayload> <start> <end> <size> <multibyte-0-or-1> <decoded-utf8-hex-or-->");
                return ExitCode::from(2);
            }
            let payload = match from_hex(&args[3]) {
                Ok(payload) => payload,
                Err(error) => {
                    eprintln!("bad hex: {}", error);
                    return ExitCode::from(2);
                }
            };
            let start = match args[4].parse::<usize>() {
                Ok(value) => value,
                Err(error) => {
                    eprintln!("invalid start: {}", error);
                    return ExitCode::from(2);
                }
            };
            let end = match args[5].parse::<usize>() {
                Ok(value) => value,
                Err(error) => {
                    eprintln!("invalid end: {}", error);
                    return ExitCode::from(2);
                }
            };
            let size = match args[6].parse::<usize>() {
                Ok(value) => value,
                Err(error) => {
                    eprintln!("invalid size: {}", error);
                    return ExitCode::from(2);
                }
            };
            let decoded = if args[8] == "-" {
                None
            } else {
                match from_hex(&args[8])
                    .and_then(|bytes| String::from_utf8(bytes).map_err(|error| error.to_string()))
                {
                    Ok(value) => Some(value),
                    Err(error) => {
                        eprintln!("invalid decoded UTF-8: {}", error);
                        return ExitCode::from(2);
                    }
                }
            };
            let chunks = utils::cut_sequence_chunks(
                &payload,
                &args[2],
                start..end,
                size,
                false,
                true,
                &[],
                args[7] == "1",
                decoded.as_deref(),
            );
            println!(
                "{}",
                chunks
                    .iter()
                    .map(|chunk| to_hex(chunk.as_bytes()))
                    .collect::<Vec<_>>()
                    .join("|")
            );
        }
        "cd-encoding-range" => {
            if args.len() < 3 {
                eprintln!("usage: parity_probe cd-encoding-range <iana>");
                return ExitCode::from(2);
            }
            emit_json_langs(&cd::encoding_unicode_range(&args[2]));
        }
        "cd-range-languages" => {
            if args.len() < 3 {
                eprintln!("usage: parity_probe cd-range-languages <unicode-range>");
                return ExitCode::from(2);
            }
            emit_json_langs(&cd::unicode_range_languages(&args[2]));
        }
        "cd-target-features" => {
            if args.len() < 3 {
                eprintln!("usage: parity_probe cd-target-features <language>");
                return ExitCode::from(2);
            }
            let (accentuated, pure_latin) = cd::get_target_features(&args[2]);
            println!("{}\t{}", u8::from(accentuated), u8::from(pure_latin));
        }
        "cd-filter-alt" => {
            let result = cd::filter_alt_coherence_matches(vec![
                ("English".to_string(), 0.2),
                ("English—".to_string(), 0.8),
                ("French".to_string(), 0.5),
            ]);
            println!(
                "{}",
                result
                    .iter()
                    .map(|(language, score)| format!("{}:{score:.17}", language))
                    .collect::<Vec<_>>()
                    .join("|")
            );
        }
        other => {
            eprintln!("unknown subcommand: {}", other);
            return ExitCode::from(2);
        }
    }
    ExitCode::SUCCESS
}
