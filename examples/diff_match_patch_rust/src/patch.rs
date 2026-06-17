//! Patch (v3) scope. Mirrors the patch_* methods + patch_obj in
//! examples/diff_match_patch/diff_match_patch.py.
//!
//! Critical boundary: patch offsets/lengths are counted in Unicode code points
//! (`Vec<char>`), while the percent codec operates on UTF-8 bytes. The two are
//! never mixed. `patch_splitMax` keeps `Match_MaxBits = 32` even though the Bitap
//! match uses arbitrary-precision integers.

use std::collections::VecDeque;

use crate::diff::{
    diff_levenshtein, diff_text1, diff_text2, diff_x_index, Diff, DiffMatchPatch, DELETE, EQUAL,
    INSERT,
};

#[derive(Debug, Clone)]
pub struct Patch {
    pub diffs: Vec<Diff>,
    pub start1: i64,
    pub start2: i64,
    pub length1: i64,
    pub length2: i64,
}

impl Patch {
    fn new() -> Self {
        Patch {
            diffs: Vec::new(),
            start1: 0,
            start2: 0,
            length1: 0,
            length2: 0,
        }
    }

    /// GNU-diff style serialization (mirrors patch_obj.__str__).
    pub fn to_text(&self) -> String {
        let coords1 = if self.length1 == 0 {
            format!("{},0", self.start1)
        } else if self.length1 == 1 {
            format!("{}", self.start1 + 1)
        } else {
            format!("{},{}", self.start1 + 1, self.length1)
        };
        let coords2 = if self.length2 == 0 {
            format!("{},0", self.start2)
        } else if self.length2 == 1 {
            format!("{}", self.start2 + 1)
        } else {
            format!("{},{}", self.start2 + 1, self.length2)
        };
        let mut out = format!("@@ -{} +{} @@\n", coords1, coords2);
        for (op, data) in &self.diffs {
            let sign = match *op {
                INSERT => '+',
                DELETE => '-',
                _ => ' ',
            };
            out.push(sign);
            out.push_str(&quote(data));
            out.push('\n');
        }
        out
    }
}

// ---- percent codec (urllib.parse.quote/unquote compatible) ----

/// Mirrors `urllib.parse.quote(data.encode("utf-8"), "!~*'();/?:@&=+$,# ")`.
/// Always-safe = unreserved (ALNUM + `_.-~`); plus the explicit safe set (incl. space).
fn quote(s: &str) -> String {
    const SAFE: &[u8] = b"!~*'();/?:@&=+$,# ";
    let mut out = String::new();
    for &b in s.as_bytes() {
        let unreserved = b.is_ascii_alphanumeric() || matches!(b, b'_' | b'.' | b'-' | b'~');
        if unreserved || SAFE.contains(&b) {
            out.push(b as char);
        } else {
            out.push('%');
            out.push_str(&format!("{:02X}", b));
        }
    }
    out
}

/// Mirrors `urllib.parse.unquote(s, errors="replace")`: decode %XX runs to bytes
/// (malformed escapes kept literal), then UTF-8 decode lossily.
fn unquote(s: &str) -> String {
    let segs: Vec<&str> = s.split('%').collect();
    let mut bytes: Vec<u8> = Vec::new();
    bytes.extend_from_slice(segs[0].as_bytes());
    for seg in &segs[1..] {
        let sb = seg.as_bytes();
        if sb.len() >= 2 && sb[0].is_ascii_hexdigit() && sb[1].is_ascii_hexdigit() {
            let hi = (sb[0] as char).to_digit(16).unwrap();
            let lo = (sb[1] as char).to_digit(16).unwrap();
            bytes.push((hi * 16 + lo) as u8);
            bytes.extend_from_slice(&sb[2..]);
        } else {
            bytes.push(b'%');
            bytes.extend_from_slice(sb);
        }
    }
    String::from_utf8_lossy(&bytes).into_owned()
}

// ---- char-slice search ----

fn find_first(hay: &[char], needle: &[char]) -> i64 {
    if needle.len() > hay.len() {
        return -1;
    }
    (0..=hay.len() - needle.len())
        .find(|&i| &hay[i..i + needle.len()] == needle)
        .map(|i| i as i64)
        .unwrap_or(-1)
}

fn find_last(hay: &[char], needle: &[char]) -> i64 {
    if needle.len() > hay.len() {
        return -1;
    }
    (0..=hay.len() - needle.len())
        .rev()
        .find(|&i| &hay[i..i + needle.len()] == needle)
        .map(|i| i as i64)
        .unwrap_or(-1)
}

// ---- patch header parsing ----

fn split_coords(p: &str) -> Option<(i64, String)> {
    if let Some((num, len)) = p.split_once(',') {
        if num.is_empty() || !num.bytes().all(|b| b.is_ascii_digit()) {
            return None;
        }
        if !len.bytes().all(|b| b.is_ascii_digit()) {
            return None;
        }
        Some((num.parse().ok()?, len.to_string()))
    } else {
        if p.is_empty() || !p.bytes().all(|b| b.is_ascii_digit()) {
            return None;
        }
        Some((p.parse().ok()?, String::new()))
    }
}

fn parse_header(line: &str) -> Option<(i64, String, i64, String)> {
    let inner = line.strip_prefix("@@ -")?.strip_suffix(" @@")?;
    let (p1, p2) = inner.split_once(" +")?;
    let (s1, l1) = split_coords(p1)?;
    let (s2, l2) = split_coords(p2)?;
    Some((s1, l1, s2, l2))
}

impl DiffMatchPatch {
    pub fn patch_to_text(&self, patches: &[Patch]) -> String {
        patches.iter().map(|p| p.to_text()).collect()
    }

    pub fn patch_make(&self, text1: &str, text2: &str) -> Vec<Patch> {
        let mut diffs = self.diff_main(text1, text2, true);
        if diffs.len() > 2 {
            self.diff_cleanup_semantic(&mut diffs);
            self.diff_cleanup_efficiency(&mut diffs);
        }
        if diffs.is_empty() {
            return Vec::new();
        }
        let margin = self.patch_margin;
        let mut patches: Vec<Patch> = Vec::new();
        let mut patch = Patch::new();
        let mut char_count1 = 0i64;
        let mut char_count2 = 0i64;
        let mut prepatch: Vec<char> = text1.chars().collect();
        let mut postpatch: Vec<char> = text1.chars().collect();
        for x in 0..diffs.len() {
            let (diff_type, diff_text) = diffs[x].clone();
            let dchars: Vec<char> = diff_text.chars().collect();
            let dlen = dchars.len() as i64;
            if patch.diffs.is_empty() && diff_type != EQUAL {
                patch.start1 = char_count1;
                patch.start2 = char_count2;
            }
            if diff_type == INSERT {
                patch.diffs.push(diffs[x].clone());
                patch.length2 += dlen;
                let cc2 = char_count2 as usize;
                let mut np = postpatch[..cc2].to_vec();
                np.extend(&dchars);
                np.extend_from_slice(&postpatch[cc2..]);
                postpatch = np;
            } else if diff_type == DELETE {
                patch.length1 += dlen;
                patch.diffs.push(diffs[x].clone());
                let cc2 = char_count2 as usize;
                let mut np = postpatch[..cc2].to_vec();
                np.extend_from_slice(&postpatch[cc2 + dlen as usize..]);
                postpatch = np;
            } else if dlen <= 2 * margin && !patch.diffs.is_empty() && x + 1 != diffs.len() {
                patch.diffs.push(diffs[x].clone());
                patch.length1 += dlen;
                patch.length2 += dlen;
            }
            if diff_type == EQUAL && dlen >= 2 * margin && !patch.diffs.is_empty() {
                self.patch_add_context(&mut patch, &prepatch);
                patches.push(patch);
                patch = Patch::new();
                prepatch = postpatch.clone();
                char_count1 = char_count2;
            }
            if diff_type != INSERT {
                char_count1 += dlen;
            }
            if diff_type != DELETE {
                char_count2 += dlen;
            }
        }
        if !patch.diffs.is_empty() {
            self.patch_add_context(&mut patch, &prepatch);
            patches.push(patch);
        }
        patches
    }

    fn patch_add_context(&self, patch: &mut Patch, text: &[char]) {
        if text.is_empty() {
            return;
        }
        let s2 = patch.start2 as usize;
        let l1 = patch.length1 as usize;
        let mut pattern: Vec<char> = text[s2..s2 + l1].to_vec();
        let mut padding = 0i64;
        while find_first(text, &pattern) != find_last(text, &pattern)
            && (self.match_max_bits == 0
                || (pattern.len() as i64) < self.match_max_bits - 2 * self.patch_margin)
        {
            padding += self.patch_margin;
            let lo = (patch.start2 - padding).max(0) as usize;
            let hi = ((patch.start2 + patch.length1 + padding) as usize).min(text.len());
            pattern = text[lo..hi].to_vec();
        }
        padding += self.patch_margin;
        let pre_lo = (patch.start2 - padding).max(0) as usize;
        let prefix: Vec<char> = text[pre_lo..s2].to_vec();
        if !prefix.is_empty() {
            patch.diffs.insert(0, (EQUAL, prefix.iter().collect()));
        }
        let suf_lo = (patch.start2 + patch.length1) as usize;
        let suf_hi = ((patch.start2 + patch.length1 + padding) as usize).min(text.len());
        let suffix: Vec<char> = text[suf_lo..suf_hi].to_vec();
        if !suffix.is_empty() {
            patch.diffs.push((EQUAL, suffix.iter().collect()));
        }
        let plen = prefix.len() as i64;
        let slen = suffix.len() as i64;
        patch.start1 -= plen;
        patch.start2 -= plen;
        patch.length1 += plen + slen;
        patch.length2 += plen + slen;
    }

    fn patch_deep_copy(&self, patches: &[Patch]) -> Vec<Patch> {
        patches.to_vec()
    }

    pub fn patch_apply(&self, patches: &[Patch], text: &str) -> (String, Vec<bool>) {
        if patches.is_empty() {
            return (text.to_string(), Vec::new());
        }
        let mut patches = self.patch_deep_copy(patches);
        let null_padding = self.patch_add_padding(&mut patches);
        let mut tchars: Vec<char> = null_padding
            .chars()
            .chain(text.chars())
            .chain(null_padding.chars())
            .collect();
        self.patch_split_max(&mut patches);

        let max_bits = self.match_max_bits;
        let mut delta = 0i64;
        let mut results: Vec<bool> = Vec::new();
        for patch in &patches {
            let expected_loc = patch.start2 + delta;
            let text1 = diff_text1(&patch.diffs);
            let text1_chars: Vec<char> = text1.chars().collect();
            let t1len = text1_chars.len() as i64;
            let mut end_loc = -1i64;
            let start_loc;
            if t1len > max_bits {
                let cur: String = tchars.iter().collect();
                let head: String = text1_chars[..max_bits as usize].iter().collect();
                let mut sl = self.match_main(&cur, &head, expected_loc);
                if sl != -1 {
                    let tail: String = text1_chars[(t1len - max_bits) as usize..].iter().collect();
                    end_loc = self.match_main(&cur, &tail, expected_loc + t1len - max_bits);
                    if end_loc == -1 || sl >= end_loc {
                        sl = -1;
                    }
                }
                start_loc = sl;
            } else {
                let cur: String = tchars.iter().collect();
                start_loc = self.match_main(&cur, &text1, expected_loc);
            }
            if start_loc == -1 {
                results.push(false);
                delta -= patch.length2 - patch.length1;
                continue;
            }
            results.push(true);
            delta = start_loc - expected_loc;
            let text2_chars: Vec<char> = if end_loc == -1 {
                let hi = ((start_loc + t1len) as usize).min(tchars.len());
                tchars[start_loc as usize..hi].to_vec()
            } else {
                let hi = ((end_loc + max_bits) as usize).min(tchars.len());
                tchars[start_loc as usize..hi].to_vec()
            };
            if text1_chars == text2_chars {
                let repl: Vec<char> = diff_text2(&patch.diffs).chars().collect();
                let mut nt = tchars[..start_loc as usize].to_vec();
                nt.extend(&repl);
                nt.extend_from_slice(&tchars[(start_loc + t1len) as usize..]);
                tchars = nt;
            } else {
                let text2: String = text2_chars.iter().collect();
                let mut diffs = self.diff_main(&text1, &text2, false);
                if t1len > max_bits
                    && diff_levenshtein(&diffs) as f64 / t1len as f64 > self.patch_delete_threshold
                {
                    *results.last_mut().unwrap() = false;
                } else {
                    self.diff_cleanup_semantic_lossless(&mut diffs);
                    let mut index1 = 0i64;
                    for (op, data) in &patch.diffs {
                        let dlen = data.chars().count() as i64;
                        let index2 = if *op != EQUAL {
                            diff_x_index(&diffs, index1)
                        } else {
                            0
                        };
                        if *op == INSERT {
                            let pos = (start_loc + index2) as usize;
                            let dchars: Vec<char> = data.chars().collect();
                            let mut nt = tchars[..pos].to_vec();
                            nt.extend(&dchars);
                            nt.extend_from_slice(&tchars[pos..]);
                            tchars = nt;
                        } else if *op == DELETE {
                            let pos = (start_loc + index2) as usize;
                            let endpos = (start_loc + diff_x_index(&diffs, index1 + dlen)) as usize;
                            let mut nt = tchars[..pos].to_vec();
                            nt.extend_from_slice(&tchars[endpos..]);
                            tchars = nt;
                        }
                        if *op != DELETE {
                            index1 += dlen;
                        }
                    }
                }
            }
        }
        let pad = null_padding.chars().count();
        let result: String = tchars[pad..tchars.len() - pad].iter().collect();
        (result, results)
    }

    fn patch_add_padding(&self, patches: &mut [Patch]) -> String {
        let plen = self.patch_margin;
        let null_padding: String = (1..=plen as u32)
            .map(|x| char::from_u32(x).unwrap())
            .collect();
        let pad_chars: Vec<char> = null_padding.chars().collect();
        for p in patches.iter_mut() {
            p.start1 += plen;
            p.start2 += plen;
        }
        // Pad the start of the first patch.
        {
            let patch = &mut patches[0];
            if patch.diffs.is_empty() || patch.diffs[0].0 != EQUAL {
                patch.diffs.insert(0, (EQUAL, null_padding.clone()));
                patch.start1 -= plen;
                patch.start2 -= plen;
                patch.length1 += plen;
                patch.length2 += plen;
            } else {
                let first_len = patch.diffs[0].1.chars().count() as i64;
                if plen > first_len {
                    let extra = plen - first_len;
                    let tail: String = pad_chars[first_len as usize..].iter().collect();
                    patch.diffs[0].1 = format!("{}{}", tail, patch.diffs[0].1);
                    patch.start1 -= extra;
                    patch.start2 -= extra;
                    patch.length1 += extra;
                    patch.length2 += extra;
                }
            }
        }
        // Pad the end of the last patch.
        {
            let last = patches.len() - 1;
            let patch = &mut patches[last];
            if patch.diffs.is_empty() || patch.diffs.last().unwrap().0 != EQUAL {
                patch.diffs.push((EQUAL, null_padding.clone()));
                patch.length1 += plen;
                patch.length2 += plen;
            } else {
                let li = patch.diffs.len() - 1;
                let last_len = patch.diffs[li].1.chars().count() as i64;
                if plen > last_len {
                    let extra = plen - last_len;
                    let head: String = pad_chars[..extra as usize].iter().collect();
                    patch.diffs[li].1 = format!("{}{}", patch.diffs[li].1, head);
                    patch.length1 += extra;
                    patch.length2 += extra;
                }
            }
        }
        null_padding
    }

    fn patch_split_max(&self, patches: &mut Vec<Patch>) {
        let patch_size = self.match_max_bits;
        if patch_size == 0 {
            return;
        }
        let margin = self.patch_margin;
        let mut result: Vec<Patch> = Vec::new();
        for bigpatch in std::mem::take(patches) {
            if bigpatch.length1 <= patch_size {
                result.push(bigpatch);
                continue;
            }
            let mut start1 = bigpatch.start1;
            let mut start2 = bigpatch.start2;
            let mut precontext: Vec<char> = Vec::new();
            let mut bdiffs: VecDeque<Diff> = bigpatch.diffs.into_iter().collect();
            while !bdiffs.is_empty() {
                let mut patch = Patch::new();
                let mut empty = true;
                patch.start1 = start1 - precontext.len() as i64;
                patch.start2 = start2 - precontext.len() as i64;
                if !precontext.is_empty() {
                    patch.length1 = precontext.len() as i64;
                    patch.length2 = precontext.len() as i64;
                    patch.diffs.push((EQUAL, precontext.iter().collect()));
                }
                while !bdiffs.is_empty() && patch.length1 < patch_size - margin {
                    let (diff_type, diff_text) = bdiffs[0].clone();
                    let dchars: Vec<char> = diff_text.chars().collect();
                    let dlen = dchars.len() as i64;
                    if diff_type == INSERT {
                        patch.length2 += dlen;
                        start2 += dlen;
                        patch.diffs.push(bdiffs.pop_front().unwrap());
                        empty = false;
                    } else if diff_type == DELETE
                        && patch.diffs.len() == 1
                        && patch.diffs[0].0 == EQUAL
                        && dlen > 2 * patch_size
                    {
                        patch.length1 += dlen;
                        start1 += dlen;
                        empty = false;
                        patch.diffs.push((diff_type, diff_text));
                        bdiffs.pop_front();
                    } else {
                        let take =
                            ((patch_size - patch.length1 - margin) as usize).min(dchars.len());
                        let sub: Vec<char> = dchars[..take].to_vec();
                        let sub_len = sub.len() as i64;
                        patch.length1 += sub_len;
                        start1 += sub_len;
                        if diff_type == EQUAL {
                            patch.length2 += sub_len;
                            start2 += sub_len;
                        } else {
                            empty = false;
                        }
                        patch.diffs.push((diff_type, sub.iter().collect()));
                        if sub_len == dlen {
                            bdiffs.pop_front();
                        } else {
                            let rest: String = dchars[take..].iter().collect();
                            bdiffs[0] = (diff_type, rest);
                        }
                    }
                }
                let pc_full: Vec<char> = diff_text2(&patch.diffs).chars().collect();
                precontext = if pc_full.len() as i64 > margin {
                    pc_full[pc_full.len() - margin as usize..].to_vec()
                } else {
                    pc_full
                };
                let remaining: Vec<Diff> = bdiffs.iter().cloned().collect();
                let pc1: Vec<char> = diff_text1(&remaining).chars().collect();
                let postcontext: Vec<char> = if pc1.len() as i64 > margin {
                    pc1[..margin as usize].to_vec()
                } else {
                    pc1
                };
                if !postcontext.is_empty() {
                    let post_len = postcontext.len() as i64;
                    patch.length1 += post_len;
                    patch.length2 += post_len;
                    if !patch.diffs.is_empty() && patch.diffs.last().unwrap().0 == EQUAL {
                        let li = patch.diffs.len() - 1;
                        patch.diffs[li].1 = format!(
                            "{}{}",
                            patch.diffs[li].1,
                            postcontext.iter().collect::<String>()
                        );
                    } else {
                        patch.diffs.push((EQUAL, postcontext.iter().collect()));
                    }
                }
                if !empty {
                    result.push(patch);
                }
            }
        }
        *patches = result;
    }

    pub fn patch_from_text(&self, textline: &str) -> Result<Vec<Patch>, String> {
        let mut patches: Vec<Patch> = Vec::new();
        if textline.is_empty() {
            return Ok(patches);
        }
        let mut lines: VecDeque<&str> = textline.split('\n').collect();
        while !lines.is_empty() {
            let header = lines[0];
            let (s1, l1, s2, l2) =
                parse_header(header).ok_or_else(|| format!("Invalid patch string: {}", header))?;
            let mut patch = Patch::new();
            patch.start1 = s1;
            if l1.is_empty() {
                patch.start1 -= 1;
                patch.length1 = 1;
            } else if l1 == "0" {
                patch.length1 = 0;
            } else {
                patch.start1 -= 1;
                patch.length1 = l1.parse().unwrap();
            }
            patch.start2 = s2;
            if l2.is_empty() {
                patch.start2 -= 1;
                patch.length2 = 1;
            } else if l2 == "0" {
                patch.length2 = 0;
            } else {
                patch.start2 -= 1;
                patch.length2 = l2.parse().unwrap();
            }
            lines.pop_front();

            while let Some(&l) = lines.front() {
                let sign = l.chars().next();
                let body = match sign {
                    Some(c) => &l[c.len_utf8()..],
                    None => "",
                };
                let line = unquote(body);
                match sign {
                    Some('+') => patch.diffs.push((INSERT, line)),
                    Some('-') => patch.diffs.push((DELETE, line)),
                    Some(' ') => patch.diffs.push((EQUAL, line)),
                    Some('@') => break,
                    None => {}
                    Some(c) => return Err(format!("Invalid patch mode: '{}'\n{}", c, line)),
                }
                lines.pop_front();
            }
            patches.push(patch);
        }
        Ok(patches)
    }
}
