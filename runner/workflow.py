"""Load and validate a workflow definition from YAML.

A workflow is a set of steps. Each step is executed by exactly one kind of executor:
- python:   calls a function in the project's steps module
- claude:   renders a prompt file and runs it through the Claude Code CLI
            (built in — no config needed)
- agent:    like claude, but any runtime: requires `agent_command` in config
- workflow: runs a child workflow (composition) and maps its `returns`
            to this step's outputs

Routing is data: a step with a `route` names one of its outputs (`route_on`)
and maps every allowed value of that output to a next step id or a builtin
(`halt`, `done`). A step without a route ends the workflow (`done`).

A workflow may declare top-level `returns` (output name -> `<step>.<field>`)
so a parent workflow-step can consume its results.
"""
from dataclasses import dataclass

import yaml

BUILTIN_TARGETS = {"halt", "done"}
KINDS = {"python", "claude", "agent", "workflow"}
PROMPT_KINDS = {"claude", "agent"}
STATUSES = {"RUNNING", "WAITING_DECISION", "HALTED", "DONE"}


class WorkflowError(ValueError):
    """The workflow definition is invalid."""


@dataclass(frozen=True)
class Step:
    id: str
    kind: str
    run: str | None
    prompt: str | None
    model: str | None
    allowed_tools: tuple[str, ...]
    inputs: tuple[str, ...]
    outputs: dict
    route_on: str | None
    route: dict
    next_step: str | None
    gate: str | None


@dataclass(frozen=True)
class Workflow:
    name: str
    first_step: str
    steps: dict
    returns: dict


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise WorkflowError(message)


def _build_step(raw: dict) -> Step:
    _require(isinstance(raw, dict), f"step must be a mapping, got {type(raw).__name__}")
    _require("id" in raw, "step missing required field: id")
    step_id = raw["id"]
    kind = raw.get("kind")
    _require(kind in KINDS, f"step {step_id}: kind must be one of {sorted(KINDS)}")

    run = raw.get("run")
    prompt = raw.get("prompt")
    model = raw.get("model")
    if kind == "python":
        _require(isinstance(run, str) and run, f"step {step_id}: python step requires `run`")
        _require(prompt is None, f"step {step_id}: python step must not set `prompt`")
    elif kind == "workflow":
        _require(isinstance(run, str) and run, f"step {step_id}: workflow step requires `run` (child workflow name)")
        _require(prompt is None and model is None, f"step {step_id}: workflow step must not set `prompt`/`model`")
    else:  # claude | agent
        _require(isinstance(prompt, str) and prompt, f"step {step_id}: {kind} step requires `prompt`")
        _require(isinstance(model, str) and model, f"step {step_id}: {kind} step requires `model`")
        _require(run is None, f"step {step_id}: {kind} step must not set `run`")

    allowed_tools = tuple(raw.get("allowed_tools", ()))
    _require(
        all(isinstance(t, str) for t in allowed_tools),
        f"step {step_id}: allowed_tools must be strings",
    )
    if kind in PROMPT_KINDS:
        # containment before autonomy: a prompt step without an explicit tool
        # allowlist is uncontained and therefore invalid
        _require(
            len(allowed_tools) > 0,
            f"step {step_id}: {kind} step requires a non-empty allowed_tools list",
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

    next_step = raw.get("next")
    if next_step is not None:
        _require(isinstance(next_step, str) and next_step, f"step {step_id}: next must be a step id")
        _require(not route, f"step {step_id}: `next` and `route` are mutually exclusive")

    gate = raw.get("gate")
    if gate is not None:
        _require(isinstance(gate, str) and gate, f"step {step_id}: gate must be a non-empty string")

    return Step(
        id=step_id,
        kind=kind,
        run=run,
        prompt=prompt,
        model=model,
        allowed_tools=allowed_tools,
        inputs=inputs,
        outputs=outputs,
        route_on=route_on,
        route=route,
        next_step=next_step,
        gate=gate,
    )


def load_workflow(text: str) -> Workflow:
    raw = yaml.safe_load(text)
    _require(isinstance(raw, dict), "workflow file must be a mapping")
    _require(isinstance(raw.get("workflow"), str) and raw["workflow"], "missing `workflow` name")
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
        if step.next_step is not None:
            _require(
                step.next_step in steps,
                f"step {step.id}: next target `{step.next_step}` is not a step",
            )
        for ref in step.inputs:
            _require(
                "." in ref,
                f"step {step.id}: input `{ref}` must be dotted (`input.x` or `<step>.<output>`)",
            )
            source = ref.split(".", 1)[0]
            _require(
                source == "input" or source in steps,
                f"step {step.id}: input `{ref}` references unknown source `{source}`",
            )

    returns = raw.get("returns", {})
    _require(isinstance(returns, dict), "`returns` must be a mapping")
    for name, ref in returns.items():
        _require(
            isinstance(ref, str) and "." in ref,
            f"returns.{name}: must be `<step>.<output>`",
        )
        source, field = ref.split(".", 1)
        _require(source in steps, f"returns.{name}: unknown step `{source}`")
        _require(
            field in steps[source].outputs,
            f"returns.{name}: `{field}` is not an output of `{source}`",
        )

    first_step = raw_steps[0]["id"]
    return Workflow(name=raw["workflow"], first_step=first_step, steps=steps, returns=returns)


def load_registry(texts: dict) -> dict:
    """Load {name: yaml_text} into {name: Workflow}, cross-validating composition.

    Workflow-step `run` targets must exist in the registry; a workflow step's
    declared outputs must be satisfiable by the child's `returns`.
    """
    registry = {}
    for name, text in texts.items():
        wf = load_workflow(text)
        _require(wf.name == name, f"workflow name `{wf.name}` must match its file name `{name}`")
        registry[name] = wf
    for wf in registry.values():
        for step in wf.steps.values():
            if step.kind != "workflow":
                continue
            _require(
                step.run in registry,
                f"{wf.name}/{step.id}: child workflow `{step.run}` not found",
            )
            child = registry[step.run]
            missing = set(step.outputs) - set(child.returns)
            _require(
                not missing,
                f"{wf.name}/{step.id}: child `{step.run}` does not return {sorted(missing)}",
            )
    return registry
