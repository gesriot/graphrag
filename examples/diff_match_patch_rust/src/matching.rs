//! Match (v2) scope: fuzzy `match_main` via the Bitap algorithm. Mirrors
//! examples/diff_match_patch/diff_match_patch.py.
//!
//! Bitap's bit arrays are unbounded in Python (it ignores Match_MaxBits), so the
//! Rust port uses `BigUint` rather than a fixed-width integer. All indexing is on
//! `Vec<char>` (code points), matching Python str semantics.

use std::collections::HashMap;

use num_bigint::BigUint;
use num_traits::{One, Zero};

use crate::diff::DiffMatchPatch;

fn find_from(text: &[char], pat: &[char], start: i64) -> i64 {
    let start = start.max(0) as usize;
    if pat.len() > text.len() {
        return -1;
    }
    let last = text.len() - pat.len();
    (start..=last)
        .find(|&i| &text[i..i + pat.len()] == pat)
        .map(|i| i as i64)
        .unwrap_or(-1)
}

fn rfind_from(text: &[char], pat: &[char], start: i64) -> i64 {
    let start = start.max(0);
    if pat.len() > text.len() {
        return -1;
    }
    let mut i = (text.len() - pat.len()) as i64;
    while i >= start {
        if &text[i as usize..i as usize + pat.len()] == pat {
            return i;
        }
        i -= 1;
    }
    -1
}

fn match_alphabet(pattern: &[char]) -> HashMap<char, BigUint> {
    let plen = pattern.len();
    let mut s: HashMap<char, BigUint> = HashMap::new();
    for &c in pattern {
        s.entry(c).or_insert_with(BigUint::zero);
    }
    for (i, &c) in pattern.iter().enumerate() {
        let bit = BigUint::one() << (plen - i - 1);
        *s.get_mut(&c).unwrap() |= bit;
    }
    s
}

impl DiffMatchPatch {
    pub fn match_main(&self, text: &str, pattern: &str, loc: i64) -> i64 {
        let t: Vec<char> = text.chars().collect();
        let p: Vec<char> = pattern.chars().collect();
        let loc = loc.max(0).min(t.len() as i64);
        if t == p {
            return 0;
        }
        if t.is_empty() {
            return -1;
        }
        let l = loc as usize;
        if l + p.len() <= t.len() && t[l..l + p.len()] == p[..] {
            // Perfect match at the perfect spot (includes the empty-pattern case).
            return loc;
        }
        self.match_bitap(&t, &p, loc)
    }

    fn match_bitap(&self, text: &[char], pattern: &[char], loc: i64) -> i64 {
        let s = match_alphabet(pattern);
        let plen = pattern.len();
        let tlen = text.len();

        let score = |e: usize, x: i64| -> f64 {
            let accuracy = e as f64 / plen as f64;
            let proximity = (loc - x).abs();
            if self.match_distance == 0 {
                if proximity != 0 {
                    1.0
                } else {
                    accuracy
                }
            } else {
                accuracy + proximity as f64 / self.match_distance as f64
            }
        };

        let mut score_threshold = self.match_threshold;
        let mut probe = find_from(text, pattern, loc);
        if probe != -1 {
            score_threshold = score(0, probe).min(score_threshold);
            probe = rfind_from(text, pattern, loc + plen as i64);
            if probe != -1 {
                score_threshold = score(0, probe).min(score_threshold);
            }
        }

        let matchmask = BigUint::one() << (plen - 1);
        let mut best_loc: i64 = -1;
        let mut bin_max = (plen + tlen) as i64;
        let mut last_rd: Vec<BigUint> = Vec::new();

        for d in 0..plen {
            // Binary search for how far from loc we can stray at this error level.
            let mut bin_min = 0i64;
            let mut bin_mid = bin_max;
            while bin_min < bin_mid {
                if score(d, loc + bin_mid) <= score_threshold {
                    bin_min = bin_mid;
                } else {
                    bin_max = bin_mid;
                }
                bin_mid = (bin_max - bin_min) / 2 + bin_min;
            }
            bin_max = bin_mid;
            let start = (loc - bin_mid + 1).max(1);
            let finish = (loc + bin_mid).min(tlen as i64) + plen as i64;

            let mut rd: Vec<BigUint> = vec![BigUint::zero(); (finish + 2) as usize];
            rd[(finish + 1) as usize] = (BigUint::one() << d) - BigUint::one();
            let mut j = finish;
            while j >= start {
                let char_match: BigUint = if (tlen as i64) < j {
                    BigUint::zero()
                } else {
                    s.get(&text[(j - 1) as usize])
                        .cloned()
                        .unwrap_or_else(BigUint::zero)
                };
                let rd_j = if d == 0 {
                    ((&rd[(j + 1) as usize] << 1) | BigUint::one()) & &char_match
                } else {
                    let term1 = ((&rd[(j + 1) as usize] << 1) | BigUint::one()) & &char_match;
                    let term2 =
                        ((&last_rd[(j + 1) as usize] | &last_rd[j as usize]) << 1) | BigUint::one();
                    let term3 = last_rd[(j + 1) as usize].clone();
                    term1 | term2 | term3
                };
                rd[j as usize] = rd_j;
                if (&rd[j as usize] & &matchmask) != BigUint::zero() {
                    let sc = score(d, j - 1);
                    if sc <= score_threshold {
                        score_threshold = sc;
                        best_loc = j - 1;
                        if best_loc > loc {
                            // Past loc: the upstream `start` reassignment here is dead
                            // (Python's range was already fixed), so we don't apply it.
                        } else {
                            break;
                        }
                    }
                }
                j -= 1;
            }
            if score(d + 1, loc) > score_threshold {
                break;
            }
            last_rd = rd;
        }
        best_loc
    }
}
