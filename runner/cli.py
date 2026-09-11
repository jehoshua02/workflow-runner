"""CLI: start a candidate, tick all candidates, or show status.

Every path is explicit — the runner has no ambient config and no defaults.

  python -m runner.cli start --pipeline p.yaml --project-dir P --state-dir S \\
      --inbox-dir I --candidate-json c.json
  python -m runner.cli tick  --pipeline p.yaml --project-dir P --state-dir S --inbox-dir I
  python -m runner.cli status --state-dir S
"""
import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import engine, state
from .executors import agent_exec, python_exec
from .pipeline import load_pipeline


def _clock() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _context(args) -> engine.Context:
    return engine.Context(
        steps_module=python_exec.load_steps_module(Path(args.project_dir)),
        prompts_dir=Path(args.project_dir) / "prompts",
        inbox_dir=Path(args.inbox_dir),
        run_command=agent_exec.run_command_subprocess,
        clock=_clock,
    )


def cmd_start(args) -> None:
    pipeline = load_pipeline(Path(args.pipeline).read_text())
    candidate = json.loads(Path(args.candidate_json).read_text())
    s = state.new_state(candidate, pipeline.first_step)
    path = state.state_path(Path(args.state_dir), candidate["id"])
    if path.exists():
        raise SystemExit(f"refusing to overwrite existing state: {path}")
    state.save_state(path, s)
    print(f"started {candidate['id']} at {pipeline.first_step}")


def cmd_tick(args) -> None:
    pipeline = load_pipeline(Path(args.pipeline).read_text())
    ctx = _context(args)
    for path in sorted(Path(args.state_dir).glob("*.json")):
        s = state.load_state(path)
        if s.status == "WAITING_GATE":
            s.status = "RUNNING"  # re-enter; the gate re-checks and re-waits if still pending
        if s.status != "RUNNING":
            continue
        s = engine.tick_candidate(pipeline, s, ctx)
        state.save_state(path, s)
        print(f"{s.candidate['id']}: {s.status} @ {s.current_step}")


def cmd_status(args) -> None:
    for path in sorted(Path(args.state_dir).glob("*.json")):
        s = state.load_state(path)
        print(f"{s.candidate['id']}: {s.status} @ {s.current_step}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="runner")
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start")
    for flag in ("--pipeline", "--project-dir", "--state-dir", "--inbox-dir", "--candidate-json"):
        start.add_argument(flag, required=True)
    start.set_defaults(func=cmd_start)

    tick = sub.add_parser("tick")
    for flag in ("--pipeline", "--project-dir", "--state-dir", "--inbox-dir"):
        tick.add_argument(flag, required=True)
    tick.set_defaults(func=cmd_tick)

    status = sub.add_parser("status")
    status.add_argument("--state-dir", required=True)
    status.set_defaults(func=cmd_status)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
