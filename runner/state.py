"""Per-candidate state: one JSON file per candidate, written atomically.

The state file is the source of truth for where a candidate sits in the
pipeline. Markdown/trail files are rendered views, never the state itself.
"""
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


class StateError(ValueError):
    """The state file or transition is invalid."""


@dataclass
class CandidateState:
    candidate: dict
    workflow: str
    status: str
    current_step: str
    outputs: dict
    history: list


def new_state(candidate: dict, workflow: str, first_step: str) -> CandidateState:
    if not isinstance(candidate, dict) or not candidate.get("id"):
        raise StateError("candidate must be a mapping with a non-empty `id`")
    return CandidateState(
        candidate=candidate,
        workflow=workflow,
        status="RUNNING",
        current_step=first_step,
        outputs={},
        history=[],
    )


def state_path(state_dir: Path, candidate_id: str) -> Path:
    return state_dir / f"{candidate_id}.json"


def load_state(path: Path) -> CandidateState:
    raw = json.loads(path.read_text())
    missing = {"candidate", "workflow", "status", "current_step", "outputs", "history"} - set(raw)
    if missing:
        raise StateError(f"state file {path} missing fields: {sorted(missing)}")
    return CandidateState(**raw)


def save_state(path: Path, state: CandidateState) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(state), indent=2, sort_keys=True))
    os.replace(tmp, path)


def record(state: CandidateState, event: str, detail: dict, now: str) -> None:
    state.history.append({"at": now, "event": event, "detail": detail})
