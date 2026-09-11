"""CLI — docker-compose style: run from the project directory.

Discovery (defaults live here, at the edge; the engine requires everything):
- workflow file: ./workflow.yaml            (override: --file)
- project dir:   cwd                        (override: --project-dir)
- state:         ./.workflow/state/         (override: config.state_dir)
- inbox:         ./.workflow/inbox/         (override: config.inbox_dir)
- runs:          ./.workflow/runs/          (override: config.runs_dir)
- log:           ./.workflow/log.jsonl      (override: config.log_path)

Commands:
  workflow-runner start --candidate-json c.json   enqueue a candidate
  workflow-runner tick                            advance everything that can move
  workflow-runner watch [--interval 60]           tick in a loop
  workflow-runner status                          one line per candidate
  workflow-runner retry <candidate-id>            HALTED -> RUNNING at the failed step
"""
import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import engine, state
from .config import load_config
from .executors import agent_exec, python_exec
from .pipeline import load_pipeline


def _clock() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load(args) -> tuple:
    project_dir = Path(args.project_dir).resolve()
    workflow_path = project_dir / args.file
    if not workflow_path.exists():
        raise SystemExit(f"no {args.file} in {project_dir}")
    text = workflow_path.read_text()
    pipeline = load_pipeline(text)
    config = load_config(project_dir, yaml.safe_load(text).get("config", {}))
    return pipeline, config


def _context(config, project_dir: Path) -> engine.Context:
    return engine.Context(
        config=config,
        steps_module=python_exec.load_steps_module(project_dir),
        prompts_dir=project_dir / "prompts",
        run_command=agent_exec.run_command_subprocess,
        clock=_clock,
    )


def cmd_start(args) -> None:
    pipeline, config = _load(args)
    candidate = json.loads(Path(args.candidate_json).read_text())
    s = state.new_state(candidate, pipeline.first_step)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    path = state.state_path(config.state_dir, candidate["id"])
    if path.exists():
        raise SystemExit(f"refusing to overwrite existing state: {path}")
    state.save_state(path, s)
    print(f"started {candidate['id']} at {pipeline.first_step}")


def _tick_once(args) -> list:
    pipeline, config = _load(args)
    ctx = _context(config, config.project_dir)
    moved = []
    for path in sorted(config.state_dir.glob("*.json")) if config.state_dir.exists() else []:
        s = state.load_state(path)
        if s.status == "WAITING_GATE":
            s.status = "RUNNING"  # re-enter; the gate re-checks and re-waits if still pending
        if s.status != "RUNNING":
            continue
        s = engine.tick_candidate(pipeline, s, ctx)
        state.save_state(path, s)
        moved.append(f"{s.candidate['id']}: {s.status} @ {s.current_step}")
    return moved


def cmd_tick(args) -> None:
    lines = _tick_once(args)
    print("\n".join(lines) if lines else "nothing to move")


def cmd_watch(args) -> None:
    while True:
        for line in _tick_once(args):
            print(f"{_clock()} {line}", flush=True)
        time.sleep(args.interval)


def cmd_status(args) -> None:
    _, config = _load(args)
    paths = sorted(config.state_dir.glob("*.json")) if config.state_dir.exists() else []
    if not paths:
        print("no candidates")
    for path in paths:
        s = state.load_state(path)
        print(f"{s.candidate['id']}: {s.status} @ {s.current_step}")


def cmd_retry(args) -> None:
    _, config = _load(args)
    path = state.state_path(config.state_dir, args.candidate_id)
    if not path.exists():
        raise SystemExit(f"no state for {args.candidate_id}")
    s = state.load_state(path)
    if s.status != "HALTED":
        raise SystemExit(f"{args.candidate_id} is {s.status}, not HALTED")
    s.status = "RUNNING"
    state.record(s, "retried", {"step": s.current_step}, _clock())
    state.save_state(path, s)
    print(f"{args.candidate_id}: RUNNING @ {s.current_step} — run `tick` to execute")


def main() -> None:
    parser = argparse.ArgumentParser(prog="workflow-runner")
    parser.add_argument("--file", default="workflow.yaml")
    parser.add_argument("--project-dir", default=".")
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start")
    start.add_argument("--candidate-json", required=True)
    start.set_defaults(func=cmd_start)

    sub.add_parser("tick").set_defaults(func=cmd_tick)

    watch = sub.add_parser("watch")
    watch.add_argument("--interval", type=int, default=60)
    watch.set_defaults(func=cmd_watch)

    sub.add_parser("status").set_defaults(func=cmd_status)

    retry = sub.add_parser("retry")
    retry.add_argument("candidate_id")
    retry.set_defaults(func=cmd_retry)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
