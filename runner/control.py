"""State transitions a human may trigger, shared by the CLI and the web UI.

These write state; they never execute steps. `tick` is the only executor.
"""
from pathlib import Path

from . import state
from .config import RunnerConfig


class ControlError(ValueError):
    """The requested transition is not allowed from the input's current state."""


def retry(config: RunnerConfig, input_id: str, now: str) -> state.InputState:
    """HALTED -> RUNNING at the failed step. The next tick re-executes it."""
    path = state.state_path(config.inputs_dir, input_id)
    if not path.exists():
        raise FileNotFoundError(f"no state for {input_id}")
    s = state.load_state(path)
    if s.status != "HALTED":
        raise ControlError(f"{input_id} is {s.status}, not HALTED")
    s.status = "RUNNING"
    state.record(s, "retried", {"step": s.current_step}, now)
    state.save_state(path, s)
    return s


def project_theme_path(workflow_dir: Path) -> Path:
    return workflow_dir / "theme.css"
