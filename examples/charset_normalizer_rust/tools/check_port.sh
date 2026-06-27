#!/usr/bin/env bash
# check_port.sh
# Small repeatable handoff/CI surface for charset-normalizer Rust port.
#
# Usage (from repo root or anywhere):
#   examples/charset_normalizer_rust/tools/check_port.sh
#   examples/charset_normalizer_rust/tools/check_port.sh --full
#   examples/charset_normalizer_rust/tools/check_port.sh --scale
#   examples/charset_normalizer_rust/tools/check_port.sh --full --scale
#
# Does:
#   - cargo fmt --check (inside crate dir)
#   - cargo test --quiet
#   - targeted pytest for charset_normalizer_rust tests (the differential + parity)
#   - if --full: full examples pytest
#   - if --scale: opt-in scale harness (CN_SCALE=1)
#   - prints expected xfail policy
#   - exits non-zero on unexpected (real) failures
#
# Portable: macOS/zsh/bash. No network. Scale not run by default.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
RUST_DIR="$REPO_ROOT/examples/charset_normalizer_rust"

FULL=0
SCALE=0

for arg in "$@"; do
  case "$arg" in
    --full) FULL=1 ;;
    --scale) SCALE=1 ;;
    *)
      echo "Unknown argument: $arg"
      echo "Usage: $0 [--full] [--scale]"
      exit 2
      ;;
  esac
done

echo "=== charset_normalizer_rust handoff check ==="
echo "Repo: $REPO_ROOT"
echo "Crate: $RUST_DIR"
echo

echo ">>> cargo fmt --check"
( cd "$RUST_DIR" && cargo fmt --check )
echo "cargo fmt --check: OK"
echo

echo ">>> cargo test --quiet"
( cd "$RUST_DIR" && cargo test --quiet )
echo "cargo test --quiet: OK"
echo

echo ">>> targeted pytest (charset_normalizer_rust tests)"
set +e
TARGETED_OUT=$( cd "$REPO_ROOT" && PYTHONPATH=. uv run pytest examples/charset_normalizer_rust -q --tb=no 2>&1 )
TARGETED_STATUS=$?
set -e
echo "$TARGETED_OUT"
if [[ $TARGETED_STATUS -ne 0 ]]; then
  echo "ERROR: targeted pytest exited with status $TARGETED_STATUS"
  exit "$TARGETED_STATUS"
fi
if echo "$TARGETED_OUT" | grep -qE '[0-9]+ (failed|error)'; then
  echo "ERROR: unexpected failures detected in targeted pytest"
  exit 1
fi
echo "targeted pytest: OK (xfails are expected per policy below)"
echo

cat << 'POLICY'
Expected xfail policy (these are documented and stable; not regressions):
  - 2 adversarial detector cases (bom8_badcont, short_high):
      best() tie-break differs on ambiguous short/high-noise inputs by design
      (codec variant or mess edge + candidate order). Both sides still detect text.
      Python is source of truth only for stable cases.
  - 2 codec-policy cases:
      - utf_7: SIG/BOM strip policy per charset_normalizer/api.py vs raw stdlib decode
      - euc_jis_2004: extension handling vs encoding_rs profile
  Single-byte codecs: exact. Most multibyte: via encoding_rs + custom (Korean/HZ/UTF).
  Recent targeted run: 74 passed, 4 xfailed.
  Full examples run: 440 passed, 4 xfailed.
POLICY

echo

if [[ $FULL -eq 1 ]]; then
  echo ">>> full examples pytest (--full)"
  set +e
  FULL_OUT=$( cd "$REPO_ROOT" && PYTHONPATH=. uv run pytest examples -q --tb=no 2>&1 )
  FULL_STATUS=$?
  set -e
  echo "$FULL_OUT"
  if [[ $FULL_STATUS -ne 0 ]]; then
    echo "ERROR: full examples pytest exited with status $FULL_STATUS"
    exit "$FULL_STATUS"
  fi
  if echo "$FULL_OUT" | grep -qE '[0-9]+ (failed|error)'; then
    echo "ERROR: unexpected failures detected in full examples pytest"
    exit 1
  fi
  echo "full examples pytest: OK"
  echo
fi

if [[ $SCALE -eq 1 ]]; then
  echo ">>> scale harness (--scale, opt-in, uses release build)"
  ( cd "$REPO_ROOT" && CN_SCALE=1 PYTHONPATH=. uv run python examples/charset_normalizer_rust/tools/scale_harness.py )
  echo "scale harness: completed"
  echo
fi

echo "=== all requested checks passed ==="
