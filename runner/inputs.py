"""Resolve a step's declared inputs from the input record and prior outputs.

Refs are dotted and explicit: `input.<field>` or `<step_id>.<output_field>`.
The resolved mapping is keyed by the full ref — prompts use {{input.fqn}},
never bare names, so there is no ambiguity and no implicit ambient state.
"""


class InputError(ValueError):
    """A declared input could not be resolved."""


def resolve(refs: tuple, input: dict, outputs: dict) -> dict:
    values: dict = {}
    for ref in refs:
        source, field = ref.split(".", 1)
        if source == "input":
            pool = input
        elif source in outputs:
            pool = outputs[source]
        else:
            raise InputError(f"input `{ref}`: step `{source}` has not produced outputs")
        if field not in pool:
            raise InputError(f"input `{ref}`: `{field}` not present in `{source}`")
        values[ref] = pool[field]
    return values
