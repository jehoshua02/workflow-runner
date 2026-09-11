"""Load and validate a pipeline definition from YAML.

A pipeline is a set of steps. Each step is executed by exactly one executor:
- python: calls a function in the project's steps module
- agent:  renders a prompt file and runs a headless agent

Routing is data: a step with a `route` names one of its outputs (`route_on`)
and maps every allowed value of that output to a next step id or a builtin
(`halt_inbox`, `done`). A step without a route ends the pipeline (`done`).
"""
from dataclasses import dataclass

import yaml

BUILTIN_TARGETS = {"halt_inbox", "done"}
EXECUTORS = {"python", "agent"}
STATUSES = {"RUNNING", "WAITING_GATE", "HALTED", "DONE"}


class PipelineError(ValueError):
    """The pipeline definition is invalid."""


@dataclass(frozen=True)
class Step:
    id: str
    executor: str
    run: str | None
    prompt: str | None
    model: str | None
    allowed_tools: tuple[str, ...]
    inputs: tuple[str, ...]
    outputs: dict
    route_on: str | None
    route: dict
    gate: str | None


@dataclass(frozen=True)
class Pipeline:
    name: str
    first_step: str
    steps: dict


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PipelineError(message)


def _build_step(raw: dict) -> Step:
    _require(isinstance(raw, dict), f"step must be a mapping, got {type(raw).__name__}")
    _require("id" in raw, "step missing required field: id")
    step_id = raw["id"]
    executor = raw.get("executor")
    _require(executor in EXECUTORS, f"step {step_id}: executor must be one of {sorted(EXECUTORS)}")

    run = raw.get("run")
    prompt = raw.get("prompt")
    model = raw.get("model")
    if executor == "python":
        _require(isinstance(run, str) and run, f"step {step_id}: python step requires `run`")
        _require(prompt is None, f"step {step_id}: python step must not set `prompt`")
    else:
        _require(isinstance(prompt, str) and prompt, f"step {step_id}: agent step requires `prompt`")
        _require(isinstance(model, str) and model, f"step {step_id}: agent step requires `model`")
        _require(run is None, f"step {step_id}: agent step must not set `run`")

    allowed_tools = tuple(raw.get("allowed_tools", ()))
    _require(
        all(isinstance(t, str) for t in allowed_tools),
        f"step {step_id}: allowed_tools must be strings",
    )

    inputs = tuple(raw.get("inputs", ()))
    outputs = raw.get("outputs", {})
    _require(isinstance(outputs, dict), f"step {step_id}: outputs must be a mapping")

    route = raw.get("route", {})
    route_on = raw.get("route_on")
    _require(isinstance(route, dict), f"step {step_id}: route must be a mapping")
    if route:
        _require(
            isinstance(route_on, str) and route_on,
            f"step {step_id}: route requires `route_on` naming the routing output",
        )
        _require(
            route_on in outputs,
            f"step {step_id}: route_on `{route_on}` is not a declared output",
        )
        spec = outputs[route_on]
        if isinstance(spec, dict) and "enum" in spec:
            missing = set(spec["enum"]) - set(route)
            _require(
                not missing,
                f"step {step_id}: route missing enum values: {sorted(missing)}",
            )
    else:
        _require(route_on is None, f"step {step_id}: route_on without route")

    gate = raw.get("gate")
    if gate is not None:
        _require(isinstance(gate, str) and gate, f"step {step_id}: gate must be a non-empty string")

    return Step(
        id=step_id,
        executor=executor,
        run=run,
        prompt=prompt,
        model=model,
        allowed_tools=allowed_tools,
        inputs=inputs,
        outputs=outputs,
        route_on=route_on,
        route=route,
        gate=gate,
    )


def load_pipeline(text: str) -> Pipeline:
    raw = yaml.safe_load(text)
    _require(isinstance(raw, dict), "pipeline file must be a mapping")
    _require(isinstance(raw.get("pipeline"), str) and raw["pipeline"], "missing `pipeline` name")
    raw_steps = raw.get("steps")
    _require(isinstance(raw_steps, list) and raw_steps, "`steps` must be a non-empty list")

    steps: dict = {}
    for raw_step in raw_steps:
        step = _build_step(raw_step)
        _require(step.id not in steps, f"duplicate step id: {step.id}")
        steps[step.id] = step

    for step in steps.values():
        for target in step.route.values():
            _require(
                target in steps or target in BUILTIN_TARGETS,
                f"step {step.id}: route target `{target}` is neither a step nor a builtin",
            )
        for ref in step.inputs:
            _require(
                "." in ref,
                f"step {step.id}: input `{ref}` must be dotted (`candidate.x` or `<step>.<output>`)",
            )
            source = ref.split(".", 1)[0]
            _require(
                source == "candidate" or source in steps,
                f"step {step.id}: input `{ref}` references unknown source `{source}`",
            )

    first_step = raw_steps[0]["id"]
    return Pipeline(name=raw["pipeline"], first_step=first_step, steps=steps)
