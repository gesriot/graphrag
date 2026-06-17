//! Structure-preserving port of the diff (v1) scope of diff-match-patch.
//! Mirrors examples/diff_match_patch/diff_match_patch.py.
//!
//! v1 supports `Diff_Timeout <= 0` semantics only: `diff_halfMatch` returns None
//! and there is no deadline/bailout (so the Myers bisect always runs to
//! completion). All slicing is done on `Vec<char>` (Unicode code points), exactly
//! like Python's str indexing; `String`s are built only at diff-op boundaries.

pub const DELETE: i32 = -1;
pub const EQUAL: i32 = 0;
pub const INSERT: i32 = 1;

pub type Diff = (i32, String);
type CDiff = Vec<(i32, Vec<char>)>;

// ---- substring search on char slices (Python str.find) ----

fn find(hay: &[char], needle: &[char]) -> Option<usize> {
    if needle.is_empty() {
        return Some(0);
    }
    if needle.len() > hay.len() {
        return None;
    }
    (0..=hay.len() - needle.len()).find(|&i| &hay[i..i + needle.len()] == needle)
}

fn find_from(hay: &[char], needle: &[char], start: usize) -> Option<usize> {
    if start > hay.len() {
        return None;
    }
    find(&hay[start..], needle).map(|i| i + start)
}

fn common_prefix(t1: &[char], t2: &[char]) -> usize {
    let n = t1.len().min(t2.len());
    let mut i = 0;
    while i < n && t1[i] == t2[i] {
        i += 1;
    }
    i
}

fn common_suffix(t1: &[char], t2: &[char]) -> usize {
    let n = t1.len().min(t2.len());
    let mut i = 0;
    while i < n && t1[t1.len() - 1 - i] == t2[t2.len() - 1 - i] {
        i += 1;
    }
    i
}

fn common_overlap(t1: &[char], t2: &[char]) -> usize {
    let l1 = t1.len();
    let l2 = t2.len();
    if l1 == 0 || l2 == 0 {
        return 0;
    }
    let t1 = if l1 > l2 { &t1[l1 - l2..] } else { t1 };
    let t2 = if l2 > l1 { &t2[..l1] } else { t2 };
    let text_length = l1.min(l2);
    if t1 == t2 {
        return text_length;
    }
    let mut best = 0usize;
    let mut length = 1usize;
    loop {
        let pattern = &t1[t1.len() - length..];
        match find(t2, pattern) {
            None => return best,
            Some(found) => {
                length += found;
                if found == 0 || t1[t1.len() - length..] == t2[..length] {
                    best = length;
                    length += 1;
                }
            }
        }
    }
}

// ---- core diff ----

fn diff_main_chars(t1: &[char], t2: &[char], checklines: bool) -> CDiff {
    if t1 == t2 {
        if t1.is_empty() {
            return vec![];
        }
        return vec![(EQUAL, t1.to_vec())];
    }
    let pl = common_prefix(t1, t2);
    let commonprefix = &t1[..pl];
    let t1 = &t1[pl..];
    let t2 = &t2[pl..];
    let sl = common_suffix(t1, t2);
    let (commonsuffix, t1, t2): (&[char], &[char], &[char]) = if sl == 0 {
        (&[], t1, t2)
    } else {
        (
            &t1[t1.len() - sl..],
            &t1[..t1.len() - sl],
            &t2[..t2.len() - sl],
        )
    };
    let mut diffs = diff_compute_chars(t1, t2, checklines);
    if !commonprefix.is_empty() {
        diffs.insert(0, (EQUAL, commonprefix.to_vec()));
    }
    if !commonsuffix.is_empty() {
        diffs.push((EQUAL, commonsuffix.to_vec()));
    }
    cleanup_merge_chars(&mut diffs);
    diffs
}

fn diff_compute_chars(t1: &[char], t2: &[char], checklines: bool) -> CDiff {
    if t1.is_empty() {
        return vec![(INSERT, t2.to_vec())];
    }
    if t2.is_empty() {
        return vec![(DELETE, t1.to_vec())];
    }
    let (longtext, shorttext) = if t1.len() > t2.len() {
        (t1, t2)
    } else {
        (t2, t1)
    };
    if let Some(i) = find(longtext, shorttext) {
        let mut diffs = vec![
            (INSERT, longtext[..i].to_vec()),
            (EQUAL, shorttext.to_vec()),
            (INSERT, longtext[i + shorttext.len()..].to_vec()),
        ];
        if t1.len() > t2.len() {
            diffs[0].0 = DELETE;
            diffs[2].0 = DELETE;
        }
        return diffs;
    }
    if shorttext.len() == 1 {
        return vec![(DELETE, t1.to_vec()), (INSERT, t2.to_vec())];
    }
    // half-match is skipped: v1 uses Diff_Timeout <= 0, where it returns None.
    if checklines && t1.len() > 100 && t2.len() > 100 {
        return diff_line_mode_chars(t1, t2);
    }
    diff_bisect_chars(t1, t2)
}

fn diff_bisect_chars(t1: &[char], t2: &[char]) -> CDiff {
    let n1 = t1.len() as i64;
    let n2 = t2.len() as i64;
    let max_d = (n1 + n2 + 1) / 2;
    let v_offset = max_d;
    let v_length = (2 * max_d) as usize;
    let mut v1 = vec![-1i64; v_length];
    let mut v2 = vec![-1i64; v_length];
    v1[(v_offset + 1) as usize] = 0;
    v2[(v_offset + 1) as usize] = 0;
    let delta = n1 - n2;
    let front = delta % 2 != 0;
    let (mut k1start, mut k1end, mut k2start, mut k2end) = (0i64, 0i64, 0i64, 0i64);
    for d in 0..max_d {
        let mut k1 = -d + k1start;
        while k1 <= d - k1end {
            let k1_offset = (v_offset + k1) as usize;
            let mut x1 = if k1 == -d || (k1 != d && v1[k1_offset - 1] < v1[k1_offset + 1]) {
                v1[k1_offset + 1]
            } else {
                v1[k1_offset - 1] + 1
            };
            let mut y1 = x1 - k1;
            while x1 < n1 && y1 < n2 && t1[x1 as usize] == t2[y1 as usize] {
                x1 += 1;
                y1 += 1;
            }
            v1[k1_offset] = x1;
            if x1 > n1 {
                k1end += 2;
            } else if y1 > n2 {
                k1start += 2;
            } else if front {
                let k2_offset = v_offset + delta - k1;
                if k2_offset >= 0 && (k2_offset as usize) < v_length && v2[k2_offset as usize] != -1
                {
                    let x2 = n1 - v2[k2_offset as usize];
                    if x1 >= x2 {
                        return diff_bisect_split_chars(t1, t2, x1 as usize, y1 as usize);
                    }
                }
            }
            k1 += 2;
        }
        let mut k2 = -d + k2start;
        while k2 <= d - k2end {
            let k2_offset = (v_offset + k2) as usize;
            let mut x2 = if k2 == -d || (k2 != d && v2[k2_offset - 1] < v2[k2_offset + 1]) {
                v2[k2_offset + 1]
            } else {
                v2[k2_offset - 1] + 1
            };
            let mut y2 = x2 - k2;
            while x2 < n1 && y2 < n2 && t1[(n1 - x2 - 1) as usize] == t2[(n2 - y2 - 1) as usize] {
                x2 += 1;
                y2 += 1;
            }
            v2[k2_offset] = x2;
            if x2 > n1 {
                k2end += 2;
            } else if y2 > n2 {
                k2start += 2;
            } else if !front {
                let k1_offset = v_offset + delta - k2;
                if k1_offset >= 0 && (k1_offset as usize) < v_length && v1[k1_offset as usize] != -1
                {
                    let x1 = v1[k1_offset as usize];
                    let y1 = v_offset + x1 - k1_offset;
                    let x2m = n1 - x2;
                    if x1 >= x2m {
                        return diff_bisect_split_chars(t1, t2, x1 as usize, y1 as usize);
                    }
                }
            }
            k2 += 2;
        }
    }
    vec![(DELETE, t1.to_vec()), (INSERT, t2.to_vec())]
}

fn diff_bisect_split_chars(t1: &[char], t2: &[char], x: usize, y: usize) -> CDiff {
    let mut a = diff_main_chars(&t1[..x], &t2[..y], false);
    let b = diff_main_chars(&t1[x..], &t2[y..], false);
    a.extend(b);
    a
}

fn diff_line_mode_chars(t1: &[char], t2: &[char]) -> CDiff {
    let (e1, e2, line_array) = diff_lines_to_chars(t1, t2);
    let mut diffs = diff_main_chars(&e1, &e2, false);
    diff_chars_to_lines(&mut diffs, &line_array);
    cleanup_semantic_chars(&mut diffs);

    diffs.push((EQUAL, vec![]));
    let mut pointer = 0usize;
    let mut count_delete = 0usize;
    let mut count_insert = 0usize;
    let mut text_delete: Vec<char> = Vec::new();
    let mut text_insert: Vec<char> = Vec::new();
    while pointer < diffs.len() {
        match diffs[pointer].0 {
            INSERT => {
                count_insert += 1;
                text_insert.extend(diffs[pointer].1.iter());
            }
            DELETE => {
                count_delete += 1;
                text_delete.extend(diffs[pointer].1.iter());
            }
            _ => {
                if count_delete >= 1 && count_insert >= 1 {
                    let sub = diff_main_chars(&text_delete, &text_insert, false);
                    let start = pointer - count_delete - count_insert;
                    let sub_len = sub.len();
                    diffs.splice(start..pointer, sub);
                    pointer = start + sub_len;
                }
                count_insert = 0;
                count_delete = 0;
                text_delete.clear();
                text_insert.clear();
            }
        }
        pointer += 1;
    }
    diffs.pop();
    diffs
}

const SURROGATE_START: usize = 0xD800;
const SURROGATE_END: usize = 0xE000;
const MAX_LINE_INDEX: usize = 0x10FFFF - (SURROGATE_END - SURROGATE_START);

fn line_index_to_char(index: usize) -> char {
    let scalar = if index >= SURROGATE_START {
        index + (SURROGATE_END - SURROGATE_START)
    } else {
        index
    };
    char::from_u32(scalar as u32).expect("line index exceeds Unicode scalar capacity")
}

fn char_to_line_index(value: char) -> usize {
    let scalar = value as usize;
    if scalar >= SURROGATE_END {
        scalar - (SURROGATE_END - SURROGATE_START)
    } else {
        scalar
    }
}

fn diff_lines_to_chars(t1: &[char], t2: &[char]) -> (Vec<char>, Vec<char>, Vec<Vec<char>>) {
    use std::collections::HashMap;
    let mut line_array: Vec<Vec<char>> = vec![vec![]];
    let mut line_hash: HashMap<Vec<char>, usize> = HashMap::new();

    fn munge(
        text: &[char],
        line_array: &mut Vec<Vec<char>>,
        line_hash: &mut std::collections::HashMap<Vec<char>, usize>,
        max_lines: usize,
    ) -> Vec<char> {
        let mut chars: Vec<char> = Vec::new();
        let mut line_start = 0usize;
        let mut line_end: i64 = -1;
        while line_end < text.len() as i64 - 1 {
            let found = find_from(text, &['\n'], line_start);
            let le = match found {
                Some(i) => i,
                None => text.len() - 1,
            };
            let mut line: Vec<char> = text[line_start..=le].to_vec();
            let mut le = le;
            if let Some(&idx) = line_hash.get(&line) {
                chars.push(line_index_to_char(idx));
            } else {
                if line_array.len() == max_lines {
                    line = text[line_start..].to_vec();
                    le = text.len();
                }
                line_array.push(line.clone());
                line_hash.insert(line, line_array.len() - 1);
                chars.push(line_index_to_char(line_array.len() - 1));
            }
            line_start = le + 1;
            line_end = le as i64;
        }
        chars
    }

    let chars1 = munge(t1, &mut line_array, &mut line_hash, 666666);
    let chars2 = munge(t2, &mut line_array, &mut line_hash, MAX_LINE_INDEX);
    (chars1, chars2, line_array)
}

fn diff_chars_to_lines(diffs: &mut CDiff, line_array: &[Vec<char>]) {
    for d in diffs.iter_mut() {
        let mut text: Vec<char> = Vec::new();
        for c in &d.1 {
            text.extend(line_array[char_to_line_index(*c)].iter());
        }
        d.1 = text;
    }
}

// ---- cleanups ----

fn cleanup_merge_chars(diffs: &mut CDiff) {
    diffs.push((EQUAL, vec![]));
    let mut pointer = 0usize;
    let mut count_delete = 0usize;
    let mut count_insert = 0usize;
    let mut text_delete: Vec<char> = Vec::new();
    let mut text_insert: Vec<char> = Vec::new();
    while pointer < diffs.len() {
        match diffs[pointer].0 {
            INSERT => {
                count_insert += 1;
                text_insert.extend(diffs[pointer].1.iter());
                pointer += 1;
            }
            DELETE => {
                count_delete += 1;
                text_delete.extend(diffs[pointer].1.iter());
                pointer += 1;
            }
            _ => {
                if count_delete + count_insert > 1 {
                    if count_delete != 0 && count_insert != 0 {
                        let cl = common_prefix(&text_insert, &text_delete);
                        if cl != 0 {
                            let x = pointer as i64 - count_delete as i64 - count_insert as i64 - 1;
                            if x >= 0 && diffs[x as usize].0 == EQUAL {
                                let pref: Vec<char> = text_insert[..cl].to_vec();
                                diffs[x as usize].1.extend(pref);
                            } else {
                                diffs.insert(0, (EQUAL, text_insert[..cl].to_vec()));
                                pointer += 1;
                            }
                            text_insert = text_insert[cl..].to_vec();
                            text_delete = text_delete[cl..].to_vec();
                        }
                        let cl = common_suffix(&text_insert, &text_delete);
                        if cl != 0 {
                            let suff: Vec<char> = text_insert[text_insert.len() - cl..].to_vec();
                            let mut newtext = suff;
                            newtext.extend(diffs[pointer].1.iter());
                            diffs[pointer].1 = newtext;
                            text_insert = text_insert[..text_insert.len() - cl].to_vec();
                            text_delete = text_delete[..text_delete.len() - cl].to_vec();
                        }
                    }
                    let mut new_ops: CDiff = Vec::new();
                    if !text_delete.is_empty() {
                        new_ops.push((DELETE, text_delete.clone()));
                    }
                    if !text_insert.is_empty() {
                        new_ops.push((INSERT, text_insert.clone()));
                    }
                    pointer -= count_delete + count_insert;
                    let nlen = new_ops.len();
                    diffs.splice(pointer..pointer + count_delete + count_insert, new_ops);
                    pointer += nlen + 1;
                } else if pointer != 0 && diffs[pointer - 1].0 == EQUAL {
                    let cur: Vec<char> = diffs[pointer].1.clone();
                    diffs[pointer - 1].1.extend(cur);
                    diffs.remove(pointer);
                } else {
                    pointer += 1;
                }
                count_insert = 0;
                count_delete = 0;
                text_delete.clear();
                text_insert.clear();
            }
        }
    }
    if diffs.last().map(|d| d.1.is_empty()).unwrap_or(false) {
        diffs.pop();
    }

    // Second pass: shift single edits surrounded by equalities.
    let mut changes = false;
    let mut pointer = 1usize;
    while pointer + 1 < diffs.len() {
        if diffs[pointer - 1].0 == EQUAL && diffs[pointer + 1].0 == EQUAL {
            let prev = diffs[pointer - 1].1.clone();
            let cur = diffs[pointer].1.clone();
            let next = diffs[pointer + 1].1.clone();
            if cur.ends_with(&prev) {
                // Shift the edit over the previous equality (endswith("") is true,
                // so an empty previous equality is simply dropped).
                if !prev.is_empty() {
                    let mut shifted = prev.clone();
                    shifted.extend(cur[..cur.len() - prev.len()].iter());
                    diffs[pointer].1 = shifted;
                    let mut nn = prev.clone();
                    nn.extend(next.iter());
                    diffs[pointer + 1].1 = nn;
                }
                diffs.remove(pointer - 1);
                changes = true;
            } else if cur.starts_with(&next) {
                // Shift the edit over the next equality.
                let mut np = prev.clone();
                np.extend(next.iter());
                diffs[pointer - 1].1 = np;
                let mut shifted: Vec<char> = cur[next.len()..].to_vec();
                shifted.extend(next.iter());
                diffs[pointer].1 = shifted;
                diffs.remove(pointer + 1);
                changes = true;
            }
        }
        pointer += 1;
    }
    if changes {
        cleanup_merge_chars(diffs);
    }
}

fn cleanup_semantic_lossless_chars(diffs: &mut CDiff) {
    let mut pointer = 1usize;
    while pointer + 1 < diffs.len() {
        if diffs[pointer - 1].0 == EQUAL && diffs[pointer + 1].0 == EQUAL {
            let mut equality1 = diffs[pointer - 1].1.clone();
            let mut edit = diffs[pointer].1.clone();
            let mut equality2 = diffs[pointer + 1].1.clone();

            let common_offset = common_suffix(&equality1, &edit);
            if common_offset != 0 {
                let common_string: Vec<char> = edit[edit.len() - common_offset..].to_vec();
                equality1 = equality1[..equality1.len() - common_offset].to_vec();
                let mut new_edit = common_string.clone();
                new_edit.extend(edit[..edit.len() - common_offset].iter());
                edit = new_edit;
                let mut new_eq2 = common_string;
                new_eq2.extend(equality2.iter());
                equality2 = new_eq2;
            }

            let mut best_equality1 = equality1.clone();
            let mut best_edit = edit.clone();
            let mut best_equality2 = equality2.clone();
            let mut best_score =
                semantic_score(&equality1, &edit) + semantic_score(&edit, &equality2);
            while !edit.is_empty() && !equality2.is_empty() && edit[0] == equality2[0] {
                equality1.push(edit[0]);
                let mut new_edit: Vec<char> = edit[1..].to_vec();
                new_edit.push(equality2[0]);
                edit = new_edit;
                equality2 = equality2[1..].to_vec();
                let score = semantic_score(&equality1, &edit) + semantic_score(&edit, &equality2);
                if score >= best_score {
                    best_score = score;
                    best_equality1 = equality1.clone();
                    best_edit = edit.clone();
                    best_equality2 = equality2.clone();
                }
            }

            if diffs[pointer - 1].1 != best_equality1 {
                if !best_equality1.is_empty() {
                    diffs[pointer - 1].1 = best_equality1;
                } else {
                    diffs.remove(pointer - 1);
                    pointer -= 1;
                }
                diffs[pointer].1 = best_edit;
                if !best_equality2.is_empty() {
                    diffs[pointer + 1].1 = best_equality2;
                } else {
                    diffs.remove(pointer + 1);
                    pointer = pointer.saturating_sub(1);
                }
            }
        }
        pointer += 1;
    }
}

fn semantic_score(one: &[char], two: &[char]) -> i32 {
    if one.is_empty() || two.is_empty() {
        return 6;
    }
    let char1 = one[one.len() - 1];
    let char2 = two[0];
    let non_alnum1 = !char1.is_alphanumeric();
    let non_alnum2 = !char2.is_alphanumeric();
    let ws1 = non_alnum1 && char1.is_whitespace();
    let ws2 = non_alnum2 && char2.is_whitespace();
    let lb1 = ws1 && (char1 == '\r' || char1 == '\n');
    let lb2 = ws2 && (char2 == '\r' || char2 == '\n');
    let blank1 = lb1 && blanklineend(one);
    let blank2 = lb2 && blanklinestart(two);

    if blank1 || blank2 {
        5
    } else if lb1 || lb2 {
        4
    } else if non_alnum1 && !ws1 && ws2 {
        3
    } else if ws1 || ws2 {
        2
    } else if non_alnum1 || non_alnum2 {
        1
    } else {
        0
    }
}

// BLANKLINEEND = r"\n\r?\n$" ; BLANKLINESTART = r"^\r?\n\r?\n"
fn blanklineend(s: &[char]) -> bool {
    let n = s.len();
    if n >= 2 && s[n - 1] == '\n' && s[n - 2] == '\n' {
        return true;
    }
    n >= 3 && s[n - 1] == '\n' && s[n - 2] == '\r' && s[n - 3] == '\n'
}

fn blanklinestart(s: &[char]) -> bool {
    // ^\r?\n\r?\n
    let v: String = s.iter().take(4).collect();
    v.starts_with("\n\n")
        || v.starts_with("\r\n\n")
        || v.starts_with("\n\r\n")
        || v.starts_with("\r\n\r\n")
}

fn cleanup_semantic_chars(diffs: &mut CDiff) {
    let mut changes = false;
    let mut equalities: Vec<usize> = Vec::new();
    let mut last_equality: Option<Vec<char>> = None;
    let mut pointer: i64 = 0;
    let (mut li1, mut ld1, mut li2, mut ld2) = (0usize, 0usize, 0usize, 0usize);
    while (pointer as usize) < diffs.len() {
        let p = pointer as usize;
        if diffs[p].0 == EQUAL {
            equalities.push(p);
            li1 = li2;
            li2 = 0;
            ld1 = ld2;
            ld2 = 0;
            last_equality = Some(diffs[p].1.clone());
        } else {
            if diffs[p].0 == INSERT {
                li2 += diffs[p].1.len();
            } else {
                ld2 += diffs[p].1.len();
            }
            let le_len = last_equality.as_ref().map(|e| e.len()).unwrap_or(0);
            if last_equality.is_some() && le_len <= li1.max(ld1) && le_len <= li2.max(ld2) {
                let le = last_equality.clone().unwrap();
                let eq_idx = *equalities.last().unwrap();
                diffs.insert(eq_idx, (DELETE, le));
                diffs[eq_idx + 1].0 = INSERT;
                equalities.pop();
                if !equalities.is_empty() {
                    equalities.pop();
                }
                pointer = if let Some(&e) = equalities.last() {
                    e as i64
                } else {
                    -1
                };
                li1 = 0;
                ld1 = 0;
                li2 = 0;
                ld2 = 0;
                last_equality = None;
                changes = true;
            }
        }
        pointer += 1;
    }

    if changes {
        cleanup_merge_chars(diffs);
    }
    cleanup_semantic_lossless_chars(diffs);

    // Overlaps between deletions and insertions.
    let mut pointer = 1usize;
    while pointer < diffs.len() {
        if diffs[pointer - 1].0 == DELETE && diffs[pointer].0 == INSERT {
            let deletion = diffs[pointer - 1].1.clone();
            let insertion = diffs[pointer].1.clone();
            let ol1 = common_overlap(&deletion, &insertion);
            let ol2 = common_overlap(&insertion, &deletion);
            if ol1 >= ol2 {
                if ol1 as f64 >= deletion.len() as f64 / 2.0
                    || ol1 as f64 >= insertion.len() as f64 / 2.0
                {
                    diffs.insert(pointer, (EQUAL, insertion[..ol1].to_vec()));
                    diffs[pointer - 1].1 = deletion[..deletion.len() - ol1].to_vec();
                    diffs[pointer + 1].1 = insertion[ol1..].to_vec();
                    pointer += 1;
                }
            } else if ol2 as f64 >= deletion.len() as f64 / 2.0
                || ol2 as f64 >= insertion.len() as f64 / 2.0
            {
                diffs.insert(pointer, (EQUAL, deletion[..ol2].to_vec()));
                diffs[pointer - 1].0 = INSERT;
                diffs[pointer - 1].1 = insertion[..insertion.len() - ol2].to_vec();
                diffs[pointer + 1].0 = DELETE;
                diffs[pointer + 1].1 = deletion[ol2..].to_vec();
                pointer += 1;
            }
            pointer += 1;
        }
        pointer += 1;
    }
}

fn cleanup_efficiency_chars(diffs: &mut CDiff, edit_cost: usize) {
    let mut changes = false;
    let mut equalities: Vec<usize> = Vec::new();
    let mut last_equality: Option<Vec<char>> = None;
    let mut pointer: i64 = 0;
    let (mut pre_ins, mut pre_del, mut post_ins, mut post_del) = (false, false, false, false);
    while (pointer as usize) < diffs.len() {
        let p = pointer as usize;
        if diffs[p].0 == EQUAL {
            if diffs[p].1.len() < edit_cost && (post_ins || post_del) {
                equalities.push(p);
                pre_ins = post_ins;
                pre_del = post_del;
                last_equality = Some(diffs[p].1.clone());
            } else {
                equalities.clear();
                last_equality = None;
            }
            post_ins = false;
            post_del = false;
        } else {
            if diffs[p].0 == DELETE {
                post_del = true;
            } else {
                post_ins = true;
            }
            let sum = pre_ins as i32 + pre_del as i32 + post_ins as i32 + post_del as i32;
            let le_len = last_equality.as_ref().map(|e| e.len()).unwrap_or(0);
            if last_equality.is_some()
                && ((pre_ins && pre_del && post_ins && post_del)
                    || ((le_len * 2 < edit_cost) && sum == 3))
            {
                let le = last_equality.clone().unwrap();
                let eq_idx = *equalities.last().unwrap();
                diffs.insert(eq_idx, (DELETE, le));
                diffs[eq_idx + 1].0 = INSERT;
                equalities.pop();
                last_equality = None;
                if pre_ins && pre_del {
                    post_ins = true;
                    post_del = true;
                    equalities.clear();
                } else {
                    if !equalities.is_empty() {
                        equalities.pop();
                    }
                    pointer = if let Some(&e) = equalities.last() {
                        e as i64
                    } else {
                        -1
                    };
                    post_ins = false;
                    post_del = false;
                }
                changes = true;
            }
        }
        pointer += 1;
    }
    if changes {
        cleanup_merge_chars(diffs);
    }
}

// ---- conversions + public API ----

fn to_cdiff(diffs: &[Diff]) -> CDiff {
    diffs
        .iter()
        .map(|(o, t)| (*o, t.chars().collect()))
        .collect()
}

fn to_diff(c: CDiff) -> Vec<Diff> {
    c.into_iter()
        .map(|(o, t)| (o, t.into_iter().collect()))
        .collect()
}

#[derive(Debug, Clone)]
pub struct DiffMatchPatch {
    pub diff_timeout: f64,
    pub diff_edit_cost: usize,
    pub match_threshold: f64,
    pub match_distance: i64,
    pub patch_delete_threshold: f64,
    pub patch_margin: i64,
    pub match_max_bits: i64,
}

impl Default for DiffMatchPatch {
    fn default() -> Self {
        // v1 scope: diff_timeout <= 0 (no deadline / half-match).
        DiffMatchPatch {
            diff_timeout: 0.0,
            diff_edit_cost: 4,
            match_threshold: 0.5,
            match_distance: 1000,
            patch_delete_threshold: 0.5,
            patch_margin: 4,
            match_max_bits: 32,
        }
    }
}

impl DiffMatchPatch {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn diff_main(&self, text1: &str, text2: &str, checklines: bool) -> Vec<Diff> {
        let c1: Vec<char> = text1.chars().collect();
        let c2: Vec<char> = text2.chars().collect();
        to_diff(diff_main_chars(&c1, &c2, checklines))
    }

    pub fn diff_cleanup_merge(&self, diffs: &mut Vec<Diff>) {
        let mut c = to_cdiff(diffs);
        cleanup_merge_chars(&mut c);
        *diffs = to_diff(c);
    }

    pub fn diff_cleanup_semantic(&self, diffs: &mut Vec<Diff>) {
        let mut c = to_cdiff(diffs);
        cleanup_semantic_chars(&mut c);
        *diffs = to_diff(c);
    }

    pub fn diff_cleanup_efficiency(&self, diffs: &mut Vec<Diff>) {
        let mut c = to_cdiff(diffs);
        cleanup_efficiency_chars(&mut c, self.diff_edit_cost);
        *diffs = to_diff(c);
    }

    pub fn diff_cleanup_semantic_lossless(&self, diffs: &mut Vec<Diff>) {
        let mut c = to_cdiff(diffs);
        cleanup_semantic_lossless_chars(&mut c);
        *diffs = to_diff(c);
    }
}

// ---- diff helpers used by the patch (v3) scope. Lengths are in code points. ----

/// Source text: all EQUAL and DELETE op texts concatenated.
pub fn diff_text1(diffs: &[Diff]) -> String {
    diffs
        .iter()
        .filter(|(op, _)| *op != INSERT)
        .map(|(_, t)| t.as_str())
        .collect()
}

/// Destination text: all EQUAL and INSERT op texts concatenated.
pub fn diff_text2(diffs: &[Diff]) -> String {
    diffs
        .iter()
        .filter(|(op, _)| *op != DELETE)
        .map(|(_, t)| t.as_str())
        .collect()
}

/// Levenshtein distance implied by a diff (substitution = max(ins, del) per equality run).
pub fn diff_levenshtein(diffs: &[Diff]) -> i64 {
    let mut lev = 0i64;
    let mut insertions = 0i64;
    let mut deletions = 0i64;
    for (op, data) in diffs {
        let l = data.chars().count() as i64;
        match *op {
            INSERT => insertions += l,
            DELETE => deletions += l,
            _ => {
                lev += insertions.max(deletions);
                insertions = 0;
                deletions = 0;
            }
        }
    }
    lev + insertions.max(deletions)
}

/// Map a location in text1 to the equivalent location in text2.
pub fn diff_x_index(diffs: &[Diff], loc: i64) -> i64 {
    let n = diffs.len();
    let (mut chars1, mut chars2, mut last1, mut last2) = (0i64, 0i64, 0i64, 0i64);
    let mut x = 0usize;
    while x < n {
        let (op, text) = &diffs[x];
        let l = text.chars().count() as i64;
        if *op != INSERT {
            chars1 += l;
        }
        if *op != DELETE {
            chars2 += l;
        }
        if chars1 > loc {
            break;
        }
        last1 = chars1;
        last2 = chars2;
        x += 1;
    }
    let x = if x >= n { n.saturating_sub(1) } else { x };
    if x < n && diffs[x].0 == DELETE {
        return last2;
    }
    last2 + (loc - last1)
}

#[cfg(test)]
mod tests {
    use super::{char_to_line_index, line_index_to_char, MAX_LINE_INDEX, SURROGATE_START};

    #[test]
    fn line_index_encoding_skips_unicode_surrogates() {
        for index in [
            SURROGATE_START - 1,
            SURROGATE_START,
            SURROGATE_START + 1,
            MAX_LINE_INDEX,
        ] {
            assert_eq!(char_to_line_index(line_index_to_char(index)), index);
        }
    }
}
