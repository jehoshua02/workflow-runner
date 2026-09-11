"""Human decisions: the engine's only human-interaction concept.

When a gate needs approval or a input halts, the engine writes a decision
file under its own state area (`.state/decisions/`). How a human finds out —
an inbox note, a DM, a ticket — is the project layer's job, via the optional
`on_event` hook in steps.py. The engine knows nothing about inboxes.

A decision file asks one question. The answer goes in the `## Response`
section: a line starting with `approved` or `rejected` (case-insensitive).
Anything else means the decision is still pending.
"""
from pathlib import Path

NOTE_TEMPLATE = """# {title}

- **From → To:** runner → human
- **Date:** {now}
- **Needs:** decision
- **Link:** {link}

{ask}

## Response

"""


def decision_path(decisions_dir: Path, input_id: str, gate: str) -> Path:
    return decisions_dir / f"{input_id}-{gate}.md"


def write_decision(path: Path, title: str, link: str, ask: str, now: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(NOTE_TEMPLATE.format(title=title, link=link, ask=ask, now=now))


def respond(path: Path, line: str) -> None:
    """Write a decision (`approved` / `rejected: <reason>`) into the Response section."""
    if not path.exists():
        raise FileNotFoundError(f"no decision file at {path}")
    current, _ = read_decision(path)
    if current != "pending":
        raise ValueError(f"decision already {current}: {path}")
    with path.open("a") as fh:
        fh.write(line.rstrip() + "\n")


def pending_for(decisions_dir: Path, input_id: str) -> list:
    """Paths of pending decision files for one input."""
    if not decisions_dir.exists():
        return []
    return [
        p
        for p in sorted(decisions_dir.glob(f"{input_id}-*.md"))
        if read_decision(p)[0] == "pending"
    ]


def read_decision(path: Path) -> tuple:
    """Return ("pending", ""), ("approved", <line>), or ("rejected", <line>)."""
    if not path.exists():
        return ("pending", "")
    lines = path.read_text().splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if l.strip().lower() == "## response")
    except StopIteration:
        return ("pending", "")
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if lowered.startswith("approved"):
            return ("approved", stripped)
        if lowered.startswith("rejected"):
            return ("rejected", stripped)
        return ("pending", "")
    return ("pending", "")
