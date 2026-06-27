use std::env;
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::process::ExitCode;

const PROG: &str = "normalizer";
const UPSTREAM_VERSION: &str =
    "Charset-Normalizer 3.4.7 - Python 3.14.4 - Unicode 16.0.0 - SpeedUp OFF";

const HELP: &str = "\
usage: normalizer [-h] [-v] [-a] [-n] [-m] [-r] [-f] [-i] [-t THRESHOLD]
                  [--version]
                  files [files ...]

The Real First Universal Charset Detector. Discover originating encoding used
on text file. Normalize text to unicode.

positional arguments:
  files                 File(s) to be analysed

options:
  -h, --help            show this help message and exit
  -v, --verbose         Display complementary information about file if any.
                        Stdout will contain logs about the detection process.
  -a, --with-alternative
                        Output complementary possibilities if any. Top-level
                        JSON WILL be a list.
  -n, --normalize       Permit to normalize input file. If not set, program
                        does not write anything.
  -m, --minimal         Only output the charset detected to STDOUT. Disabling
                        JSON output.
  -r, --replace         Replace file when trying to normalize it instead of
                        creating a new one.
  -f, --force           Replace file without asking if you are sure, use this
                        flag with caution.
  -i, --no-preemptive   Disable looking at a charset declaration to hint the
                        detector.
  -t, --threshold THRESHOLD
                        Define a custom maximum amount of noise allowed in
                        decoded content. 0. <= noise <= 1.
  --version             Show version information and exit.
";

#[derive(Debug, Clone)]
struct InputItem {
    arg: String,
    display_name: String,
}

#[derive(Debug, Clone)]
enum JsonValue {
    Null,
    Bool(bool),
    Number(String),
    String(String),
    Array(Vec<JsonValue>),
    Object(Vec<(&'static str, JsonValue)>),
}

fn main() -> ExitCode {
    let args: Vec<String> = env::args().collect();

    let mut i = 1usize;
    let mut minimal = false;
    let mut with_alt = false;
    let mut threshold: Option<f64> = None;
    let mut no_preemptive = false;
    let mut verbose = false;
    let mut normalize = false;
    let mut replace = false;
    let mut force = false;
    let mut cp_isolation: Vec<String> = vec![];
    let mut cp_exclusion: Vec<String> = vec![];
    let mut paths: Vec<String> = vec![];

    while i < args.len() {
        let a = &args[i];
        if a == "-h" || a == "--help" {
            print!("{HELP}");
            return ExitCode::SUCCESS;
        } else if a == "--version" {
            println!("{UPSTREAM_VERSION}");
            return ExitCode::SUCCESS;
        } else if a == "-v" || a == "--verbose" {
            verbose = true;
        } else if a == "-i" || a == "--no-preemptive" {
            no_preemptive = true;
        } else if a == "-m" || a == "--minimal" {
            minimal = true;
        } else if a == "-n" || a == "--normalize" {
            normalize = true;
        } else if a == "-r" || a == "--replace" {
            replace = true;
        } else if a == "-f" || a == "--force" {
            force = true;
        } else if a == "-a" || a == "--with-alternative" {
            with_alt = true;
        } else if a == "-t" || a == "--threshold" {
            i += 1;
            if i >= args.len() {
                return argparse_error("argument -t/--threshold: expected one argument");
            }
            match args[i].parse::<f64>() {
                Ok(v) => threshold = Some(v),
                Err(_) => {
                    return argparse_error(&format!(
                        "argument -t/--threshold: invalid float value: '{}'",
                        args[i]
                    ));
                }
            }
        } else if let Some(val) = a.strip_prefix("--threshold=") {
            match val.parse::<f64>() {
                Ok(v) => threshold = Some(v),
                Err(_) => {
                    return argparse_error(&format!(
                        "argument -t/--threshold: invalid float value: '{}'",
                        val
                    ));
                }
            }
        } else if a == "--cp-isolation" || a == "--cp_isolation" {
            i += 1;
            if i >= args.len() {
                return argparse_error("argument --cp-isolation: expected one argument");
            }
            for part in args[i].split(',') {
                let p = part.trim();
                if !p.is_empty() {
                    cp_isolation.push(p.to_string());
                }
            }
        } else if let Some(val) = a
            .strip_prefix("--cp-isolation=")
            .or_else(|| a.strip_prefix("--cp_isolation="))
        {
            for part in val.split(',') {
                let p = part.trim();
                if !p.is_empty() {
                    cp_isolation.push(p.to_string());
                }
            }
        } else if a == "--cp-exclusion" || a == "--cp_exclusion" {
            i += 1;
            if i >= args.len() {
                return argparse_error("argument --cp-exclusion: expected one argument");
            }
            for part in args[i].split(',') {
                let p = part.trim();
                if !p.is_empty() {
                    cp_exclusion.push(p.to_string());
                }
            }
        } else if let Some(val) = a
            .strip_prefix("--cp-exclusion=")
            .or_else(|| a.strip_prefix("--cp_exclusion="))
        {
            for part in val.split(',') {
                let p = part.trim();
                if !p.is_empty() {
                    cp_exclusion.push(p.to_string());
                }
            }
        } else if a == "--" {
            paths.extend(args.iter().skip(i + 1).cloned());
            break;
        } else if a.starts_with('-') && a != "-" {
            return argparse_error(&format!("unrecognized arguments: {a}"));
        } else {
            paths.push(a.clone());
        }
        i += 1;
    }

    if paths.is_empty() {
        return argparse_error("the following arguments are required: files");
    }

    for path in &paths {
        if path == "-" {
            continue;
        }
        if let Err(e) = std::fs::File::open(path) {
            return argparse_error(&format!(
                "argument files: can't open '{}': {}",
                path,
                python_io_error(path, &e)
            ));
        }
    }

    if replace && !normalize {
        eprintln!("Use --replace in addition of --normalize only.");
        return ExitCode::from(1);
    }
    if force && !replace {
        eprintln!("Use --force in addition of --replace only.");
        return ExitCode::from(1);
    }
    if let Some(t) = threshold {
        if t < 0.0 || t > 1.0 {
            eprintln!("--threshold VALUE should be between 0. AND 1.");
            return ExitCode::from(1);
        }
    }

    let inputs: Vec<InputItem> = paths
        .iter()
        .map(|path| InputItem {
            arg: path.clone(),
            display_name: if path == "-" {
                "<stdin>".to_string()
            } else {
                path.clone()
            },
        })
        .collect();

    if normalize {
        let mut detected = Vec::new();
        for item in &inputs {
            let mut normalized_written = false;
            let data = match read_input(item) {
                Ok(data) => data,
                Err(e) => {
                    eprintln!("error: {}: {}", item.arg, e);
                    return ExitCode::from(1);
                }
            };
            let (ms, traces) = if verbose {
                detect_bytes_with_trace(
                    &data,
                    threshold,
                    no_preemptive,
                    &cp_isolation,
                    &cp_exclusion,
                    true,
                )
            } else {
                (
                    detect_bytes(
                        &data,
                        threshold,
                        no_preemptive,
                        &cp_isolation,
                        &cp_exclusion,
                    ),
                    vec![],
                )
            };
            if verbose {
                emit_traces(&traces);
            }
            match ms.best() {
                Some(best) => match best.output_utf8() {
                    Some(out) => {
                        if best.encoding.starts_with("utf") {
                            eprintln!(
                                "\"{}\" file does not need to be normalized, as it already came from unicode.",
                                item.display_name
                            );
                        } else if !replace {
                            let out_path =
                                normalized_path(&item.display_name, &best.encoding, false);
                            if let Err(e) = std::fs::write(&out_path, &out) {
                                eprintln!("error: {}: {}", out_path, e);
                                return ExitCode::from(2);
                            }
                            normalized_written = true;
                        } else {
                            if !force
                                && !query_yes_no(
                                    &format!(
                                        "Are you sure to normalize \"{}\" by replacing it ?",
                                        item.display_name
                                    ),
                                    false,
                                )
                            {
                                detected.push((item.clone(), ms, false));
                                continue;
                            }
                            let out_path =
                                normalized_path(&item.display_name, &best.encoding, true);
                            if let Err(e) = std::fs::write(&out_path, &out) {
                                eprintln!("error: {}: {}", out_path, e);
                                return ExitCode::from(2);
                            }
                            normalized_written = true;
                        }
                    }
                    None => {
                        eprintln!("error: no best match");
                        return ExitCode::from(1);
                    }
                },
                None => {
                    eprintln!("error: no best match");
                    return ExitCode::from(1);
                }
            }
            detected.push((item.clone(), ms, normalized_written));
        }
        emit_detection_output(&detected, minimal, with_alt, replace);
        return ExitCode::SUCCESS;
    }

    if minimal {
        for item in &inputs {
            let data = match read_input(item) {
                Ok(data) => data,
                Err(e) => {
                    eprintln!("error: {}: {}", item.arg, e);
                    return ExitCode::from(1);
                }
            };
            let (ms, traces) = if verbose {
                detect_bytes_with_trace(
                    &data,
                    threshold,
                    no_preemptive,
                    &cp_isolation,
                    &cp_exclusion,
                    true,
                )
            } else {
                (
                    detect_bytes(
                        &data,
                        threshold,
                        no_preemptive,
                        &cp_isolation,
                        &cp_exclusion,
                    ),
                    vec![],
                )
            };
            if verbose {
                emit_traces(&traces);
            }
            if with_alt {
                if ms.results.is_empty() {
                    println!("undefined");
                } else {
                    let encs: Vec<&str> = ms.results.iter().map(|m| m.encoding.as_str()).collect();
                    println!("{}", encs.join(", "));
                }
            } else if let Some(m) = ms.best() {
                println!("{}", m.encoding);
            } else {
                println!("undefined");
            }
        }
        return ExitCode::SUCCESS;
    }

    let mut rows = Vec::new();
    for item in &inputs {
        let data = match read_input(item) {
            Ok(data) => data,
            Err(e) => {
                eprintln!("error: {}: {}", item.arg, e);
                return ExitCode::from(1);
            }
        };
        let (ms, traces) = if verbose {
            detect_bytes_with_trace(
                &data,
                threshold,
                no_preemptive,
                &cp_isolation,
                &cp_exclusion,
                true,
            )
        } else {
            (
                detect_bytes(
                    &data,
                    threshold,
                    no_preemptive,
                    &cp_isolation,
                    &cp_exclusion,
                ),
                vec![],
            )
        };
        if verbose {
            emit_traces(&traces);
        }
        let cands: Vec<_> = if with_alt {
            ms.results.iter().collect()
        } else {
            ms.best().map(|b| vec![b]).unwrap_or_default()
        };
        if cands.is_empty() {
            rows.push(make_json_row(item, None, true, None));
        } else {
            for (idx, m) in cands.iter().enumerate() {
                rows.push(make_json_row(item, Some(m), idx == 0, None));
            }
        }
    }

    if rows.len() == 1 {
        println!("{}", render_json(&rows[0], 0));
    } else {
        println!("{}", render_json(&JsonValue::Array(rows), 0));
    }
    ExitCode::SUCCESS
}

fn detect_bytes(
    data: &[u8],
    threshold: Option<f64>,
    no_preemptive: bool,
    cp_isolation: &[String],
    cp_exclusion: &[String],
) -> charset_normalizer_rust::CharsetMatches {
    detect_bytes_with_trace(
        data,
        threshold,
        no_preemptive,
        cp_isolation,
        cp_exclusion,
        false,
    )
    .0
}

fn detect_bytes_with_trace(
    data: &[u8],
    threshold: Option<f64>,
    no_preemptive: bool,
    cp_isolation: &[String],
    cp_exclusion: &[String],
    explain: bool,
) -> (charset_normalizer_rust::CharsetMatches, Vec<String>) {
    let mut options = charset_normalizer_rust::FromBytesOptions::default();
    if let Some(threshold) = threshold {
        options.threshold = threshold;
    }
    options.preemptive_behaviour = !no_preemptive;
    options.cp_isolation = cp_isolation.to_vec();
    options.cp_exclusion = cp_exclusion.to_vec();
    options.explain = explain;
    charset_normalizer_rust::from_bytes_with_options_and_trace(data, options)
}

fn emit_traces(traces: &[String]) {
    // Emit on stderr to match Python reference actual behavior (StreamHandler defaults to stderr).
    // Fixed timestamp for test comparison (see normalize_log_lines in tests/cli.rs; we do not
    // claim byte-exact verbose parity when using the helper).
    for msg in traces {
        let level = if msg.starts_with("Encoding detection:") {
            "DEBUG"
        } else {
            "Level 5"
        };
        eprintln!("2026-06-27 00:00:00,000 | {} | {}", level, msg);
    }
}

fn read_input(item: &InputItem) -> io::Result<Vec<u8>> {
    if item.arg == "-" {
        let mut buf = Vec::new();
        io::stdin().read_to_end(&mut buf)?;
        Ok(buf)
    } else {
        std::fs::read(&item.arg)
    }
}

fn query_yes_no(question: &str, default_yes: bool) -> bool {
    let prompt = if default_yes { " [Y/n] " } else { " [y/N] " };

    loop {
        print!("{}{}", question, prompt);
        let _ = std::io::stdout().flush();

        let mut line = String::new();
        match std::io::stdin().read_line(&mut line) {
            Ok(0) => return default_yes,
            Ok(_) => {
                let answer = line.trim().to_ascii_lowercase();
                if answer.is_empty() {
                    return default_yes;
                }
                if answer == "y" || answer == "yes" {
                    return true;
                }
                if answer == "n" || answer == "no" {
                    return false;
                }
                println!("Please respond with 'y' or 'n'.");
            }
            Err(_) => return default_yes,
        }
    }
}

fn emit_detection_output(
    items: &[(InputItem, charset_normalizer_rust::CharsetMatches, bool)],
    minimal: bool,
    with_alt: bool,
    replace: bool,
) {
    if minimal {
        for (_item, ms, _normalized) in items {
            if with_alt {
                if ms.results.is_empty() {
                    println!("undefined");
                } else {
                    let encs: Vec<&str> = ms.results.iter().map(|m| m.encoding.as_str()).collect();
                    println!("{}", encs.join(", "));
                }
            } else if let Some(m) = ms.best() {
                println!("{}", m.encoding);
            } else {
                println!("undefined");
            }
        }
        return;
    }

    let mut rows = Vec::new();
    for (item, ms, normalized) in items {
        let cands: Vec<_> = if with_alt {
            ms.results.iter().collect()
        } else {
            ms.best().map(|b| vec![b]).unwrap_or_default()
        };
        if cands.is_empty() {
            rows.push(make_json_row(item, None, true, None));
        } else {
            for (idx, m) in cands.iter().enumerate() {
                let unicode_path = if idx == 0 && *normalized {
                    Some(normalized_path(&item.display_name, &m.encoding, replace))
                } else {
                    None
                };
                rows.push(make_json_row(
                    item,
                    Some(m),
                    idx == 0,
                    unicode_path.as_deref(),
                ));
            }
        }
    }

    if rows.len() == 1 {
        println!("{}", render_json(&rows[0], 0));
    } else {
        println!("{}", render_json(&JsonValue::Array(rows), 0));
    }
}

fn make_json_row(
    item: &InputItem,
    match_: Option<&charset_normalizer_rust::CharsetMatch>,
    is_pref: bool,
    unicode_path: Option<&str>,
) -> JsonValue {
    let path = abs_path(&item.display_name);
    JsonValue::Object(vec![
        ("path", JsonValue::String(path)),
        (
            "encoding",
            match match_ {
                Some(m) => JsonValue::String(m.encoding.clone()),
                None => JsonValue::Null,
            },
        ),
        (
            "encoding_aliases",
            match match_ {
                Some(m) => json_string_array(m.encoding_aliases()),
                None => JsonValue::Array(Vec::new()),
            },
        ),
        (
            "alternative_encodings",
            match match_ {
                Some(m) => {
                    let alternatives: Vec<String> = m
                        .could_be_from_charset()
                        .into_iter()
                        .filter(|encoding| encoding != &m.encoding)
                        .collect();
                    json_string_array(alternatives)
                }
                None => JsonValue::Array(Vec::new()),
            },
        ),
        (
            "language",
            JsonValue::String(
                match_
                    .and_then(|m| m.language.as_deref())
                    .unwrap_or("Unknown")
                    .to_string(),
            ),
        ),
        (
            "alphabets",
            match match_ {
                Some(m) => json_string_array(m.alphabets()),
                None => JsonValue::Array(Vec::new()),
            },
        ),
        (
            "has_sig_or_bom",
            JsonValue::Bool(match_.map(|m| m.bom).unwrap_or(false)),
        ),
        (
            "chaos",
            JsonValue::Number(py_float(match_.map(|m| m.percent_chaos()).unwrap_or(1.0))),
        ),
        (
            "coherence",
            JsonValue::Number(py_float(
                match_.map(|m| m.percent_coherence()).unwrap_or(0.0),
            )),
        ),
        (
            "unicode_path",
            match unicode_path {
                Some(path) => JsonValue::String(path.to_string()),
                None => JsonValue::Null,
            },
        ),
        ("is_preferred", JsonValue::Bool(is_pref)),
    ])
}

fn json_string_array(values: Vec<String>) -> JsonValue {
    JsonValue::Array(values.into_iter().map(JsonValue::String).collect())
}

fn render_json(value: &JsonValue, indent: usize) -> String {
    match value {
        JsonValue::Null => "null".to_string(),
        JsonValue::Bool(v) => v.to_string(),
        JsonValue::Number(v) => v.clone(),
        JsonValue::String(v) => json_str(v),
        JsonValue::Array(values) => {
            if values.is_empty() {
                return "[]".to_string();
            }
            let child_indent = indent + 4;
            let mut out = String::from("[\n");
            for (idx, item) in values.iter().enumerate() {
                if idx > 0 {
                    out.push_str(",\n");
                }
                out.push_str(&" ".repeat(child_indent));
                out.push_str(&render_json(item, child_indent));
            }
            out.push('\n');
            out.push_str(&" ".repeat(indent));
            out.push(']');
            out
        }
        JsonValue::Object(fields) => {
            if fields.is_empty() {
                return "{}".to_string();
            }
            let child_indent = indent + 4;
            let mut out = String::from("{\n");
            for (idx, (key, item)) in fields.iter().enumerate() {
                if idx > 0 {
                    out.push_str(",\n");
                }
                out.push_str(&" ".repeat(child_indent));
                out.push_str(&json_str(key));
                out.push_str(": ");
                out.push_str(&render_json(item, child_indent));
            }
            out.push('\n');
            out.push_str(&" ".repeat(indent));
            out.push('}');
            out
        }
    }
}

fn json_str(s: &str) -> String {
    let mut out = String::from("\"");
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            ch if (ch as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", ch as u32)),
            ch if (ch as u32) <= 0x7f => out.push(ch),
            ch if (ch as u32) <= 0xffff => out.push_str(&format!("\\u{:04x}", ch as u32)),
            ch => {
                let value = ch as u32 - 0x1_0000;
                let high = 0xd800 + ((value >> 10) & 0x3ff);
                let low = 0xdc00 + (value & 0x3ff);
                out.push_str(&format!("\\u{high:04x}\\u{low:04x}"));
            }
        }
    }
    out.push('"');
    out
}

fn py_float(value: f64) -> String {
    if value.is_finite() && value.fract() == 0.0 {
        format!("{value:.1}")
    } else {
        value.to_string()
    }
}

fn normalized_path(input_path: &str, encoding: &str, replace: bool) -> String {
    let real_path = real_path(input_path);
    if replace {
        return real_path.to_string_lossy().into_owned();
    }

    let file_name = real_path
        .file_name()
        .and_then(|s| s.to_str())
        .unwrap_or(input_path);
    let mut parts: Vec<String> = file_name.split('.').map(str::to_string).collect();
    let insert_at = parts.len().saturating_sub(1);
    parts.insert(insert_at, encoding.to_string());
    let new_name = parts.join(".");

    if let Some(dir) = real_path.parent() {
        if !dir.as_os_str().is_empty() {
            let mut out = dir.to_path_buf();
            out.push(new_name);
            return out.to_string_lossy().into_owned();
        }
    }
    new_name
}

fn abs_path(input_path: &str) -> String {
    absolutize(Path::new(input_path))
        .to_string_lossy()
        .into_owned()
}

fn real_path(input_path: &str) -> PathBuf {
    std::fs::canonicalize(input_path).unwrap_or_else(|_| absolutize(Path::new(input_path)))
}

fn absolutize(path: &Path) -> PathBuf {
    if path.is_absolute() {
        path.to_path_buf()
    } else {
        env::current_dir()
            .unwrap_or_else(|_| PathBuf::from("."))
            .join(path)
    }
}

fn print_usage_stderr() {
    eprintln!("usage: normalizer [-h] [-v] [-a] [-n] [-m] [-r] [-f] [-i] [-t THRESHOLD]");
    eprintln!("                  [--version]");
    eprintln!("                  files [files ...]");
}

fn argparse_error(message: &str) -> ExitCode {
    print_usage_stderr();
    eprintln!("{PROG}: error: {message}");
    ExitCode::from(2)
}

fn python_io_error(path: &str, error: &io::Error) -> String {
    match error.raw_os_error() {
        Some(code) => format!("[Errno {code}] {}: '{path}'", python_error_message(error)),
        None => error.to_string(),
    }
}

fn python_error_message(error: &io::Error) -> String {
    match error.kind() {
        io::ErrorKind::NotFound => "No such file or directory".to_string(),
        io::ErrorKind::PermissionDenied => "Permission denied".to_string(),
        _ => error.to_string(),
    }
}
