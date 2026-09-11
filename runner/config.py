"""Project config: workflow/config.yml.

Project layout convention (discovered from the project root):

    <project>/workflow/
      config.yml        — this file (optional; all keys have edge defaults)
      workflows/*.yaml  — one workflow per file, name == file stem
      prompts/          — agent prompt templates
      steps.py          — python step functions + optional on_event hook
      .state/           — runner-owned: candidates/, runs/, decisions/, log.jsonl

Defaults live HERE, at the edge — everything downstream (engine, executors)
receives fully-resolved values and has no defaults of its own.

agent_command is an argv template owned by the project layer; the engine only
knows "spawn this command, feed the prompt on stdin, expect JSON back".
Templates: {model} and {allowed_tools} (comma-joined) are substituted per-arg.

on_event names a function in steps.py (`steps.<name>`) called with one dict
{"event", "candidate", "detail", "decision_path"} on gate_opened and halted —
the project's bridge from engine decisions to wherever humans look.
"""
from dataclasses import dataclass
from pathlib import Path

DEFAULT_AGENT_COMMAND = (
    "claude",
    "-p",
    "--output-format",
    "json",
    "--model",
    "{model}",
    "--allowedTools",
    "{allowed_tools}",
)
DEFAULT_MAX_STEP_VISITS = 3
DEFAULT_MAX_DEPTH = 5


class ConfigError(ValueError):
    """The config file is invalid."""


@dataclass(frozen=True)
class RunnerConfig:
    workflow_dir: Path
    workflows_dir: Path
    prompts_dir: Path
    candidates_dir: Path
    runs_dir: Path
    decisions_dir: Path
    log_path: Path
    agent_command: tuple
    max_step_visits: int
    max_depth: int
    on_event: str | None


def load_config(workflow_dir: Path, raw: dict) -> RunnerConfig:
    if not isinstance(raw, dict):
        raise ConfigError("config.yml must be a mapping when present")
    state = workflow_dir / ".state"

    agent_command = raw.get("agent_command")
    if agent_command is None:
        agent_command = DEFAULT_AGENT_COMMAND
    elif (
        not isinstance(agent_command, list)
        or not agent_command
        or not all(isinstance(a, str) for a in agent_command)
    ):
        raise ConfigError("agent_command must be a non-empty list of strings")

    def _positive_int(key: str, default: int) -> int:
        value = raw.get(key)
        if value is None:
            return default
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ConfigError(f"{key} must be a positive int")
        return value

    on_event = raw.get("on_event")
    if on_event is not None and (not isinstance(on_event, str) or not on_event.startswith("steps.")):
        raise ConfigError("on_event must be `steps.<function>`")

    return RunnerConfig(
        workflow_dir=workflow_dir,
        workflows_dir=workflow_dir / "workflows",
        prompts_dir=workflow_dir / "prompts",
        candidates_dir=state / "candidates",
        runs_dir=state / "runs",
        decisions_dir=state / "decisions",
        log_path=state / "log.jsonl",
        agent_command=tuple(agent_command),
        max_step_visits=_positive_int("max_step_visits", DEFAULT_MAX_STEP_VISITS),
        max_depth=_positive_int("max_depth", DEFAULT_MAX_DEPTH),
        on_event=on_event,
    )


def render_agent_argv(agent_command: tuple, model: str, allowed_tools: tuple) -> list:
    values = {"model": model, "allowed_tools": ",".join(allowed_tools)}
    return [arg.format(**values) for arg in agent_command]
