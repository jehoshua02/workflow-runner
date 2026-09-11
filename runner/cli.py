"""CLI — docker-compose style: run from the project root.

Discovery (defaults live here, at the edge; the engine requires everything):
- workflow home:  ./workflow/                (override: --workflow-dir)
- config:         ./workflow/config.yml      (optional)
- workflows:      ./workflow/workflows/*.yaml (name == file stem)
- runtime state:  ./workflow/.state/{candidates,runs,decisions,log.jsonl}

Commands:
  workflow-runner start <workflow> --candidate-json c.json   enqueue a candidate
  workflow-runner tick [--loop] [--interval 60]              run once; --loop runs forever
  workflow-runner status                                     one line per candidate
  workflow-runner retry <candidate-id>                       HALTED -> RUNNING at failed step
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
from .workflow import load_registry


def _clock() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load(args) -> tuple:
    workflow_dir = Path(args.workflow_dir).resolve()
    if not workflow_dir.is_dir():
        raise SystemExit(f"no workflow/ directory at {workflow_dir}")
    config_path = workflow_dir / "config.yml"
    raw_config = yaml.safe_load(config_path.read_text()) if config_path.exists() else {}
    config = load_config(workflow_dir, raw_config or {})
    texts = {p.stem: p.read_text() for p in sorted(config.workflows_dir.glob("*.yaml"))}
    if not texts:
        raise SystemExit(f"no workflows in {config.workflows_dir}")
    registry = load_registry(texts)
    if config.agent_command is None:
        for wf in registry.values():
            for step in wf.steps.values():
                if step.kind == "agent":
                    raise SystemExit(
                        f"{wf.name}/{step.id}: kind `agent` requires `agent_command` in "
                        "config.yml (use `kind: claude` for the Claude Code CLI)"
                    )
    return config, registry


def _context(config, registry) -> engine.Context:
    return engine.Context(
        config=config,
        workflows=registry,
        steps_module=python_exec.load_steps_module(config.workflow_dir),
        run_command=agent_exec.run_command_subprocess,
        clock=_clock,
    )


def cmd_start(args) -> None:
    config, registry = _load(args)
    if args.workflow not in registry:
        raise SystemExit(f"unknown workflow `{args.workflow}` — have: {sorted(registry)}")
    workflow = registry[args.workflow]
    candidate = json.loads(Path(args.candidate_json).read_text())
    s = state.new_state(candidate, args.workflow, workflow.first_step)
    config.candidates_dir.mkdir(parents=True, exist_ok=True)
    path = state.state_path(config.candidates_dir, candidate["id"])
    if path.exists():
        raise SystemExit(f"refusing to overwrite existing state: {path}")
    state.save_state(path, s)
    print(f"started {candidate['id']} on `{args.workflow}` at {workflow.first_step}")


def _is_child(candidate_id: str) -> bool:
    return "." in candidate_id


def _tick_once(config, registry) -> list:
    ctx = _context(config, registry)
    moved = []
    paths = sorted(config.candidates_dir.glob("*.json")) if config.candidates_dir.exists() else []
    for path in paths:
        s = state.load_state(path)
        if _is_child(s.candidate["id"]):
            continue  # children are ticked by their parent step
        if s.status == "WAITING_DECISION":
            s.status = "RUNNING"  # re-enter; gates re-check and re-wait if still pending
        if s.status != "RUNNING":
            continue
        s = engine.tick_candidate(registry[s.workflow], s, ctx)
        state.save_state(path, s)
        moved.append(f"{s.candidate['id']} [{s.workflow}]: {s.status} @ {s.current_step}")
    return moved


def cmd_tick(args) -> None:
    config, registry = _load(args)
    while True:
        lines = _tick_once(config, registry)
        if not args.loop:
            print("\n".join(lines) if lines else "nothing to move")
            return
        for line in lines:
            print(f"{_clock()} {line}", flush=True)
        time.sleep(args.interval)


def cmd_status(args) -> None:
    config, _ = _load(args)
    paths = sorted(config.candidates_dir.glob("*.json")) if config.candidates_dir.exists() else []
    if not paths:
        print("no candidates")
    for path in paths:
        s = state.load_state(path)
        indent = "  " * s.candidate["id"].count(".")
        print(f"{indent}{s.candidate['id']} [{s.workflow}]: {s.status} @ {s.current_step}")


def cmd_retry(args) -> None:
    config, _ = _load(args)
    path = state.state_path(config.candidates_dir, args.candidate_id)
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
    parser.add_argument("--workflow-dir", default="workflow")
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start")
    start.add_argument("workflow")
    start.add_argument("--candidate-json", required=True)
    start.set_defaults(func=cmd_start)

    tick = sub.add_parser("tick")
    tick.add_argument("--loop", action="store_true")
    tick.add_argument("--interval", type=int, default=60)
    tick.set_defaults(func=cmd_tick)

    sub.add_parser("status").set_defaults(func=cmd_status)

    retry = sub.add_parser("retry")
    retry.add_argument("candidate_id")
    retry.set_defaults(func=cmd_retry)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
