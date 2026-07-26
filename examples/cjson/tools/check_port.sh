#!/usr/bin/env bash
# Reproduce the complete cJSON C-oracle → Rust-port evidence chain.
#
# From the repository root:
#   examples/cjson/tools/check_port.sh --full
#
# Default/--quick omits only Miri. --full includes it. Missing optional
# toolchains are printed as SKIP and yield PASS WITH SKIPS rather than being
# silently treated as coverage.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: examples/cjson/tools/check_port.sh [--quick|--full]

  --quick  Header audit, compiler-rejection locations, C oracle/ASan when a C
           compiler is available, Cargo tests, and port_eval. (Default.)
  --full   Everything in --quick plus the nightly Miri ownership run.
EOF
}

mode="quick"
if [[ "$#" -eq 0 ]]; then
    argument="--quick"
else
    argument="$1"
fi
case "$argument" in
    --quick) ;;
    --full) mode="full" ;;
    --help|-h)
        usage
        exit 0
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac

script_dir="$(cd "$(dirname "$BASH_SOURCE")" && pwd)"
root="$(cd "$script_dir/../../.." && pwd)"
cd "$root"
skipped=0

skip() {
    printf 'SKIP: %s\n' "$*"
    skipped=1
}

run() {
    printf '+'
    printf ' %q' "$@"
    printf '\n'
    "$@"
}

run_c_oracle_and_asan() {
    local output
    local status

    printf '+ env PYTHONPATH=. uv run pytest examples/cjson/tests/test_cjson_parse_contract.py -q -rs\n'
    output="$(env PYTHONPATH=. uv run pytest examples/cjson/tests/test_cjson_parse_contract.py -q -rs)" || {
        status=$?
        printf '%s\n' "$output"
        return "$status"
    }
    printf '%s\n' "$output"

    # The test deliberately records unsupported ASan with one of these skips.
    # Mirror it in the wrapper's final status so an unavailable sanitizer is
    # never mistaken for full evidence coverage.
    if [[ "$output" == *"AddressSanitizer not supported by this compiler" ||
        "$output" == *"AddressSanitizer API oracle compilation failed" ]]; then
        skip "ASan unavailable; C oracle trace ran but the sanitizer gate was skipped"
    fi
}

if ! command -v uv >/dev/null 2>&1; then
    printf 'ERROR: uv is required to run the cJSON Python gates.\n' >&2
    exit 2
fi

printf '== cJSON header-derived audit ==\n'
run uv run python examples/cjson/tools/api_surface_audit.py --check

if command -v rustc >/dev/null 2>&1; then
    printf '== cJSON compiler-rejection locations ==\n'
    run env PYTHONPATH=. uv run pytest examples/cjson/tests/test_cjson_extract.py -q -rs
else
    skip "rustc unavailable: compiler-rejection candidates and Rust gates cannot run"
fi

c_compiler=""
for candidate in cc gcc clang; do
    if command -v "$candidate" >/dev/null 2>&1; then
        c_compiler="$candidate"
        break
    fi
done
if [[ -n "$c_compiler" && -n "$(command -v rustc || true)" ]]; then
    printf '== cJSON C oracle and ASan ==\n'
    printf 'C compiler: %s; pytest -rs prints an explicit ASan skip if unsupported.\n' "$c_compiler"
    run_c_oracle_and_asan
elif [[ -z "$c_compiler" ]]; then
    skip "no C compiler found: C oracle and ASan evidence cannot run"
else
    skip "rustc unavailable: byte-compared C/Rust trace and ASan evidence cannot run"
fi

if command -v cargo >/dev/null 2>&1 && command -v rustc >/dev/null 2>&1; then
    printf '== cJSON Rust tests ==\n'
    (
        cd examples/cjson_rust
        run cargo test
    )

    printf '== cJSON port_eval ==\n'
    run uv run python scripts/port_eval.py \
        --source examples/cjson \
        --port examples/cjson_rust \
        --graph byog_cjson
else
    skip "cargo/rustc unavailable: Cargo tests and port_eval cannot run"
fi

if [[ "$mode" == "full" ]]; then
    if command -v cargo >/dev/null 2>&1 && cargo +nightly miri --version; then
        printf '== cJSON Miri ownership properties ==\n'
        (
            cd examples/cjson_rust
            run cargo +nightly miri test --test ownership_props
        )
    else
        skip "cargo +nightly miri unavailable: install the nightly miri component to run 11 ownership properties"
    fi
else
    skip "Miri omitted by --quick; rerun with --full for the complete evidence chain"
fi

if [[ "$skipped" -eq 0 ]]; then
    printf 'cJSON evidence: PASS (%s)\n' "$mode"
else
    printf 'cJSON evidence: PASS WITH SKIPS (%s)\n' "$mode"
fi
