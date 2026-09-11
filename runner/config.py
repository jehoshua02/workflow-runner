"""Project config: the `config:` section of workflow.yaml.

Defaults live HERE, at the edge — everything downstream (engine, executors)
receives fully-resolved values and has no defaults of its own.

agent_command is an argv template owned by the project layer; the engine only
knows "spawn this command, feed the prompt on stdin, expect JSON back".
Templates: {model} and {allowed_tools} (comma-joined) are substituted per-arg.
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


class ConfigError(ValueError):
    """The config section is invalid."""


@dataclass(frozen=True)
class RunnerConfig:
    project_dir: Path
    state_dir: Path
    inbox_dir: Path
    runs_dir: Path
    log_path: Path
    agent_command: tuple
    max_step_visits: int


def load_config(project_dir: Path, raw: dict) -> RunnerConfig:
    if not isinstance(raw, dict):
        raise ConfigError("`config` must be a mapping when present")
    workdir = project_dir / ".workflow"

    def _dir(key: str, default: Path) -> Path:
        value = raw.get(key)
        if value is None:
            return default
        if not isinstance(value, str) or not value:
            raise ConfigError(f"config.{key} must be a non-empty string path")
        return (project_dir / value).resolve()

    agent_command = raw.get("agent_command")
    if agent_command is None:
        agent_command = DEFAULT_AGENT_COMMAND
    elif (
        not isinstance(agent_command, list)
        or not agent_command
        or not all(isinstance(a, str) for a in agent_command)
    ):
        raise ConfigError("config.agent_command must be a non-empty list of strings")

    max_visits = raw.get("max_step_visits")
    if max_visits is None:
        max_visits = DEFAULT_MAX_STEP_VISITS
    elif not isinstance(max_visits, int) or isinstance(max_visits, bool) or max_visits < 1:
        raise ConfigError("config.max_step_visits must be a positive int")

    return RunnerConfig(
        project_dir=project_dir,
        state_dir=_dir("state_dir", workdir / "state"),
        inbox_dir=_dir("inbox_dir", workdir / "inbox"),
        runs_dir=_dir("runs_dir", workdir / "runs"),
        log_path=_dir("log_path", workdir / "log.jsonl"),
        agent_command=tuple(agent_command),
        max_step_visits=max_visits,
    )


def render_agent_argv(agent_command: tuple, model: str, allowed_tools: tuple) -> list:
    values = {"model": model, "allowed_tools": ",".join(allowed_tools)}
    return [arg.format(**values) for arg in agent_command]
