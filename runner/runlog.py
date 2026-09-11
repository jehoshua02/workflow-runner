"""Run records and the unified activity log — both plain filesystem.

- One JSON file per step execution: <runs_dir>/<input>/<NNN>-<step>.json
  holding inputs, outputs (or error), and timestamps. Numbered, append-only.
- One JSONL line per event across all inputs: <log_path>.
"""
import json
from pathlib import Path


def append_log(log_path: Path, input_id: str, event: str, detail: dict, now: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        {"at": now, "input": input_id, "event": event, "detail": detail},
        sort_keys=True,
    )
    with log_path.open("a") as fh:
        fh.write(line + "\n")


def write_run_record(
    runs_dir: Path,
    input_id: str,
    step_id: str,
    inputs: dict,
    outcome: dict,
    started: str,
    finished: str,
) -> Path:
    input_dir = runs_dir / input_id
    input_dir.mkdir(parents=True, exist_ok=True)
    seq = len(list(input_dir.glob("*.json"))) + 1
    path = input_dir / f"{seq:03d}-{step_id}.json"
    path.write_text(
        json.dumps(
            {
                "step": step_id,
                "started": started,
                "finished": finished,
                "inputs": inputs,
                **outcome,  # {"outputs": {...}} or {"error": "..."}
            },
            indent=2,
            sort_keys=True,
        )
    )
    return path
