#!/usr/bin/env python
"""
Agent loop (no external API) for local-first porting.

1. (Re)generate BYOG for the target.
2. Generate context-pack(s) for key symbols / the whole module.
3. Run a local "porting checklist" (cargo check + any other static steps).
4. Run the golden contract verifier (cargo test).

Example:
    uv run python scripts/agent_port_loop.py --graph byog_mini_game --port-dir examples/mini_game_rust
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import typer

app = typer.Typer(help="Local agent loop: BYOG -> context-pack -> porting checklist -> golden tests (zero external API)")


def log(message: str = "") -> None:
    print(message, flush=True)


@app.command()
def run(
    graph: Path = typer.Option(Path("byog_mini_game"), "--graph", help="BYOG directory to (re)generate and query"),
    port_dir: Path = typer.Option(Path("examples/mini_game_rust"), "--port-dir", help="Directory containing the port (must have Cargo.toml + golden test)"),
    target: str = typer.Option("mini_game", "--target", help="Logical target name"),
    keep_snapshots: int = typer.Option(5, "--keep-snapshots", help="Max snapshots to retain via the generator."),
):
    root = Path(__file__).resolve().parents[1]
    if not graph.is_absolute():
        graph = root / graph
    if not port_dir.is_absolute():
        port_dir = root / port_dir

    log(f"=== Agent port loop for {target} (fully local, no API) ===")

    # 1. (Re)generate BYOG using the existing bridge
    log("\n[1/4] Regenerating BYOG via bridge...")
    bridge_script = root / "scripts" / "mini_game_to_byog.py"
    subprocess.check_call(
        [sys.executable, str(bridge_script), "--keep-snapshots", str(keep_snapshots)],
        cwd=root,
    )
    log("    BYOG regenerated.")

    # 2. Generate context packs (symbol + module level)
    log("\n[2/4] Generating context packs...")
    pack_script = root / "scripts" / "context_pack.py"
    packs = [
        ("sim:run_simulation", "symbol"),
        ("sim", "module"),   # the sim module
    ]
    for sym, kind in packs:
        out_file = root / "output" / f"context_pack_{kind}_{sym.replace(':', '_')}.json"
        out_file.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            sys.executable, str(pack_script),
            sym,
            "--graph", str(graph),
            "--purpose", "port-to-rust",
            "--max-text-chars", "0",   # full text for porting
            "--output", str(out_file),
        ]
        subprocess.check_call(cmd, cwd=root)
        log(f"    Wrote {out_file}")

    # 3. Local porting checklist
    log("\n[3/4] Running local porting checklist...")
    log("    - cargo fmt --check")
    subprocess.check_call(["cargo", "fmt", "--check"], cwd=port_dir)
    log("    - cargo check (structure + compile)")
    subprocess.check_call(["cargo", "check"], cwd=port_dir)
    log("    - (placeholder) would run clippy here when configured")
    log("    Checklist passed (deterministic checks only).")

    # 4. Golden contract verifier
    log("\n[4/4] Running golden contract verifier (cargo test)...")
    subprocess.check_call(["cargo", "test", "--test", "golden_contract", "--", "--quiet"], cwd=port_dir)
    log("    All golden scenarios passed frame-by-frame.")

    log("\n=== Agent loop completed successfully (local only) ===")


if __name__ == "__main__":
    app()
