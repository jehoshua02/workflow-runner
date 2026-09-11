"""Human-approval gates: halt on a templated inbox note, resume on a marker.

A gate note asks one question. The recipient answers in the `## Response`
section: a line starting with `approved` or `rejected` (case-insensitive).
Anything else means the gate is still pending.
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


def gate_note_path(inbox_dir: Path, candidate_id: str, gate: str) -> Path:
    return inbox_dir / f"{candidate_id}-{gate}.md"


def write_gate_note(path: Path, title: str, link: str, ask: str, now: str) -> None:
    path.write_text(NOTE_TEMPLATE.format(title=title, link=link, ask=ask, now=now))


def read_gate_decision(path: Path) -> tuple:
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
