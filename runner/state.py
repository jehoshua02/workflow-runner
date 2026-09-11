"""Per-input state: one JSON file per input, written atomically.

The state file is the source of truth for where a input sits in the
pipeline. Markdown/trail files are rendered views, never the state itself.
"""
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


class StateError(ValueError):
    """The state file or transition is invalid."""


@dataclass
class InputState:
    input: dict
    workflow: str
    status: str
    current_step: str
    outputs: dict
    history: list


def new_state(input_record: dict, workflow: str, first_step: str) -> InputState:
    if not isinstance(input_record, dict) or not input_record.get("id"):
        raise StateError("input must be a mapping with a non-empty `id`")
    return InputState(
        input=input_record,
        workflow=workflow,
        status="RUNNING",
        current_step=first_step,
        outputs={},
        history=[],
    )


def state_path(state_dir: Path, input_id: str) -> Path:
    return state_dir / f"{input_id}.json"


def load_state(path: Path) -> InputState:
    raw = json.loads(path.read_text())
    missing = {"input", "workflow", "status", "current_step", "outputs", "history"} - set(raw)
    if missing:
        raise StateError(f"state file {path} missing fields: {sorted(missing)}")
    return InputState(**raw)


def save_state(path: Path, state: InputState) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(state), indent=2, sort_keys=True))
    os.replace(tmp, path)


def record(state: InputState, event: str, detail: dict, now: str) -> None:
    state.history.append({"at": now, "event": event, "detail": detail})
