"""CLI entry point: read source, run it, print outputs.

Exit code 0 on success, 1 on any MiniLangError (message printed to stderr).
"""

from __future__ import annotations

import sys
from typing import List, Optional, Tuple

from errors import MiniLangError
from eval import run as eval_run
from lexer import tokenize
from parser import parse


def run_source(source: str) -> Tuple[List[str], Optional[str]]:
    """Run a program. Returns (output_lines, error). error is None on success,
    otherwise the stable ``"<label>: <message>"`` string."""
    try:
        tokens = tokenize(source)
        stmts = parse(tokens)
        return eval_run(stmts), None
    except MiniLangError as exc:
        return [], exc.formatted()


def main(argv: List[str]) -> int:
    if len(argv) > 1:
        source = open(argv[1]).read()
    else:
        source = sys.stdin.read()

    outputs, error = run_source(source)
    for line in outputs:
        print(line)
    if error is not None:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
