"""Prompt rendering: fill {{dotted.ref}} placeholders, refuse partial fills."""
import re

PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_.-]+)\s*\}\}")


class RenderError(ValueError):
    """A placeholder could not be filled."""


def render(template: str, values: dict) -> str:
    def _sub(match: re.Match) -> str:
        key = match.group(1)
        if key not in values:
            raise RenderError(f"unfilled placeholder: {{{{{key}}}}}")
        return str(values[key])

    return PLACEHOLDER.sub(_sub, template)
