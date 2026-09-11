"""Validate step outputs against the step's declared output spec.

Spec forms per output name:
- "str" / "text": value must be a string
- "int":          value must be an int
- "list":         value must be a list
- {"enum": [..]}: value must be one of the listed values
"""


class OutputError(ValueError):
    """Step outputs do not match the declared spec."""


def validate(outputs: dict, spec: dict, step_id: str) -> dict:
    if not isinstance(outputs, dict):
        raise OutputError(f"step {step_id}: outputs must be a mapping, got {type(outputs).__name__}")
    missing = set(spec) - set(outputs)
    if missing:
        raise OutputError(f"step {step_id}: missing outputs: {sorted(missing)}")
    for name, kind in spec.items():
        value = outputs[name]
        if isinstance(kind, dict) and "enum" in kind:
            if value not in kind["enum"]:
                raise OutputError(
                    f"step {step_id}: output `{name}` = {value!r} not in enum {kind['enum']}"
                )
        elif kind in ("str", "text"):
            if not isinstance(value, str):
                raise OutputError(f"step {step_id}: output `{name}` must be a string")
        elif kind == "int":
            if not isinstance(value, int) or isinstance(value, bool):
                raise OutputError(f"step {step_id}: output `{name}` must be an int")
        elif kind == "list":
            if not isinstance(value, list):
                raise OutputError(f"step {step_id}: output `{name}` must be a list")
        else:
            raise OutputError(f"step {step_id}: unknown output spec for `{name}`: {kind!r}")
    return outputs
