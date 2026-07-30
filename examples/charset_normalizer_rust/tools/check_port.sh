#!/usr/bin/env bash
# check_port.sh
# Small repeatable handoff/CI surface for charset-normalizer Rust port.
#
# Usage (from repo root or anywhere):
#   examples/charset_normalizer_rust/tools/check_port.sh
#   examples/charset_normalizer_rust/tools/check_port.sh --full
#   examples/charset_normalizer_rust/tools/check_port.sh --scale
#   examples/charset_normalizer_rust/tools/check_port.sh --differential-full
#   examples/charset_normalizer_rust/tools/check_port.sh --full --scale --differential-full
#
# Does:
#   - cargo fmt --check (inside crate dir)
#   - cargo test --quiet
#   - targeted pytest for fixed differential/parity tests
#   - if --full: full examples pytest
#   - if --scale: opt-in scale harness (CN_SCALE=1)
#   - if --differential-full: longer seeded Python-oracle differential sweep
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
DIFFERENTIAL_FULL=0

for arg in "$@"; do
  case "$arg" in
    --full) FULL=1 ;;
    --scale) SCALE=1 ;;
    --differential-full) DIFFERENTIAL_FULL=1 ;;
    *)
      echo "Unknown argument: $arg"
      echo "Usage: $0 [--full] [--scale] [--differential-full]"
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

echo ">>> bounded seeded differential harness"
( cd "$REPO_ROOT" && PYTHONPATH=. uv run python examples/charset_normalizer_rust/tools/differential_harness.py --assert-clean )
echo "bounded seeded differential harness: OK"
echo

cat << 'POLICY'
Expected xfail policy (these are documented and stable; not regressions):
  - 2 adversarial detector cases (bom8_badcont, short_high):
      best() tie-break differs on ambiguous short/high-noise inputs by design
      (codec variant or mess edge + candidate order). Both sides still detect text.
      Python is source of truth only for stable cases.
  No codec-policy xfails remain: UTF-7 SIG comparison uses the api.py oracle;
  HZ, EUC-JIS, and Shift-JIS-X-0213 maps are generated from running Python
  codecs; and all 23 formerly excluded UTF output cases are exact.
  The only named non-exact codec scope is five stateful ISO-2022 profiles.
  Each has 3,960–11,365 Python-only scalar encodes relative to encoding_rs;
  see PORT_STATUS.md for the per-profile counts and refusal rationale.
  The default seeded live differential run adds 79 Python-oracle cases;
  use --differential-full for its 530-input every-byte/mutation/long sweep.
  Recent targeted run: 77 passed, 2 xfailed.
  Full examples run: 538 passed, 2 xfailed.
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

if [[ $DIFFERENTIAL_FULL -eq 1 ]]; then
  echo ">>> full seeded differential sweep (--differential-full)"
  ( cd "$REPO_ROOT" && PYTHONPATH=. uv run python examples/charset_normalizer_rust/tools/differential_harness.py --full --assert-clean )
  echo "full seeded differential sweep: OK"
  echo
fi

if [[ $SCALE -eq 1 ]]; then
  echo ">>> scale harness (--scale, opt-in, uses release build)"
  ( cd "$REPO_ROOT" && CN_SCALE=1 PYTHONPATH=. uv run python examples/charset_normalizer_rust/tools/scale_harness.py )
  echo "scale harness: completed"
  echo
fi

echo "=== all requested checks passed ==="
