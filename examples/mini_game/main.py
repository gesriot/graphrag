"""CLI entrypoint for running the mini side-scroller and dumping traces."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from .core import Config
from .sim import run_simulation, GOLDEN_INPUTS, events_from_list

app = typer.Typer(help="Mini side-scroller simulator (GraphRAG code experiment target)")


@app.command()
def run(
    jumps: str = typer.Option("", "--jumps", "-j", help="Comma-separated jump ticks, e.g. '3,8'"),
    name: str = typer.Option("custom", "--name", "-n", help="Name for trace output"),
    max_ticks: Optional[int] = typer.Option(None, "--ticks"),
    out: Path = typer.Option(Path("output/trace.json"), "--out", "-o"),
):
    """Run simulation with given jump schedule and write JSON trace."""
    jump_list = [int(x) for x in jumps.split(",") if x.strip()] if jumps else []
    cfg = Config()
    if max_ticks:
        cfg.max_ticks = max_ticks

    events = events_from_list(jump_list)
    trace = run_simulation(events, cfg=cfg)
    payload = {
        "name": name,
        "jumps": jump_list,
        "config": {
            "max_ticks": cfg.max_ticks,
            "gravity": cfg.gravity,
            "jump_velocity": cfg.jump_velocity,
            "obstacles": cfg.obstacles,
        },
        "trace": [r.__dict__ for r in trace],
    }

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2))
    typer.echo(f"Wrote trace ({len(trace)} frames) to {out}")


@app.command()
def dump_golden(out_dir: Path = typer.Option(Path("examples/mini_game/tests"), "--out-dir")):
    """Dump all predefined golden traces as JSON for reference / port verification."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, jumps in GOLDEN_INPUTS.items():
        cfg = Config()
        events = events_from_list(jumps)
        trace = run_simulation(events, cfg=cfg)
        payload = {
            "name": name,
            "jumps": jumps,
            "trace": [r.__dict__ for r in trace],
        }
        (out_dir / f"golden_{name}.json").write_text(json.dumps(payload, indent=2))
        typer.echo(f"Dumped golden_{name}.json ({len(trace)} frames)")


if __name__ == "__main__":
    app()
