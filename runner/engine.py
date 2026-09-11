"""The engine: advance one candidate through its workflow until it blocks.

A tick runs steps until the candidate is DONE, HALTED, or WAITING_DECISION.
Gates and failures write a decision file under the runner's own state area
and fire the project's optional on_event hook — the engine never improvises
and knows nothing about inboxes. Subagents never move workflow state: the
engine routes on their declared outputs.

Composition: `executor: workflow` runs a child workflow as its own candidate
(id `<parent>.<step>`), recursively. Child DONE → its `returns` become the
step's outputs; child WAITING_DECISION propagates; child HALTED halts the
parent. Depth is bounded by config.max_depth.

Every step execution writes a run record (inputs + outputs/error) under
runs_dir, and every event appends to the unified JSONL log.
"""
from dataclasses import dataclass

from . import decisions, inputs, outputs, rendering, runlog, state as state_mod
from .config import RunnerConfig, render_agent_argv
from .executors import agent_exec, python_exec
from .state import CandidateState, record

_WAIT = object()  # sentinel: child workflow is waiting on a human decision


class ChildHalted(RuntimeError):
    """A child workflow halted; the parent must halt too."""


@dataclass(frozen=True)
class Context:
    config: RunnerConfig
    workflows: dict
    steps_module: object
    run_command: object
    clock: object  # callable() -> ISO timestamp string


def _fire_hook(state: CandidateState, ctx: Context, event: str, detail: dict, path) -> None:
    if ctx.config.on_event is None:
        return
    hook = getattr(ctx.steps_module, ctx.config.on_event.split(".", 1)[1], None)
    if hook is None:
        return  # a missing hook must never take the runner down
    try:
        hook({
            "event": event,
            "candidate": state.candidate["id"],
            "detail": detail,
            "decision_path": str(path) if path else "",
        })
    except Exception:
        pass  # hooks are best-effort notification, never control flow


def _event(state: CandidateState, ctx: Context, event: str, detail: dict, now: str) -> None:
    record(state, event, detail, now)
    runlog.append_log(ctx.config.log_path, state.candidate["id"], event, detail, now)


def _halt(state: CandidateState, ctx: Context, reason: str, now: str) -> None:
    state.status = "HALTED"
    _event(state, ctx, "halted", {"reason": reason}, now)
    path = decisions.decision_path(ctx.config.decisions_dir, state.candidate["id"], "halted")
    decisions.write_decision(
        path,
        title=f"HALTED: {state.candidate['id']} at {state.current_step}",
        link=f"state: candidates/{state.candidate['id']}.json · runs: runs/{state.candidate['id']}/",
        ask=reason,
        now=now,
    )
    _fire_hook(state, ctx, "halted", {"reason": reason}, path)


def _check_gate(state: CandidateState, ctx: Context, gate_name: str, now: str) -> str:
    path = decisions.decision_path(ctx.config.decisions_dir, state.candidate["id"], gate_name)
    decision, line = decisions.read_decision(path)
    if decision == "pending":
        if not path.exists():
            decisions.write_decision(
                path,
                title=f"Gate `{gate_name}`: {state.candidate['id']}",
                link=f"state: candidates/{state.candidate['id']}.json",
                ask=f"Approve `{state.current_step}` for {state.candidate['id']}? "
                "Reply `approved` or `rejected: <reason>` below.",
                now=now,
            )
            _event(state, ctx, "gate_opened", {"gate": gate_name}, now)
            _fire_hook(state, ctx, "gate_opened", {"gate": gate_name}, path)
        state.status = "WAITING_DECISION"
        return "wait"
    if decision == "rejected":
        _halt(state, ctx, f"gate `{gate_name}` rejected: {line}", now)
        return "halt"
    _event(state, ctx, "gate_approved", {"gate": gate_name, "line": line}, now)
    return "go"


def _visits(state: CandidateState, step_id: str) -> int:
    return sum(
        1
        for entry in state.history
        if entry["event"] == "step_done" and entry["detail"].get("step") == step_id
    )


def _run_child_workflow(step, resolved: dict, state: CandidateState, ctx: Context):
    child_id = f"{state.candidate['id']}.{step.id}"
    if child_id.count(".") >= ctx.config.max_depth:
        raise ChildHalted(f"max workflow depth {ctx.config.max_depth} exceeded at {child_id}")
    child_wf = ctx.workflows[step.run]
    path = state_mod.state_path(ctx.config.candidates_dir, child_id)
    if path.exists():
        child = state_mod.load_state(path)
        if child.status == "WAITING_DECISION":
            child.status = "RUNNING"  # re-enter; gates re-check and re-wait if pending
    else:
        candidate = {"id": child_id}
        for ref, value in resolved.items():
            candidate[ref.split(".", 1)[1]] = value
        child = state_mod.new_state(candidate, step.run, child_wf.first_step)
    if child.status == "RUNNING":
        child = tick_candidate(child_wf, child, ctx)
    ctx.config.candidates_dir.mkdir(parents=True, exist_ok=True)
    state_mod.save_state(path, child)
    if child.status == "WAITING_DECISION":
        return _WAIT
    if child.status == "HALTED":
        raise ChildHalted(f"child workflow `{step.run}` ({child_id}) halted")
    return {name: child.outputs[ref.split(".", 1)[0]][ref.split(".", 1)[1]]
            for name, ref in child_wf.returns.items()}


def _execute(step, resolved: dict, state: CandidateState, ctx: Context):
    if step.executor == "python":
        raw = python_exec.call(ctx.steps_module, step.run, resolved)
    elif step.executor == "workflow":
        raw = _run_child_workflow(step, resolved, state, ctx)
        if raw is _WAIT:
            return _WAIT
    else:
        template = (ctx.config.prompts_dir / step.prompt).read_text()
        prompt_text = rendering.render(template, resolved)
        argv = render_agent_argv(ctx.config.agent_command, step.model, step.allowed_tools)
        raw = agent_exec.execute(ctx.run_command, argv, prompt_text)
    return outputs.validate(raw, step.outputs, step.id)


def tick_candidate(workflow, state: CandidateState, ctx: Context) -> CandidateState:
    while state.status == "RUNNING":
        now = ctx.clock()
        step = workflow.steps[state.current_step]

        if _visits(state, step.id) >= ctx.config.max_step_visits:
            _halt(
                state, ctx,
                f"step {step.id} already ran {ctx.config.max_step_visits} times — loop guard",
                now,
            )
            return state

        if step.gate is not None:
            verdict = _check_gate(state, ctx, step.gate, now)
            if verdict != "go":
                return state

        try:
            resolved = inputs.resolve(step.inputs, state.candidate, state.outputs)
        except Exception as exc:
            _halt(state, ctx, f"step {step.id} inputs unresolvable: {exc}", now)
            return state

        try:
            step_outputs = _execute(step, resolved, state, ctx)
        except Exception as exc:  # any unexpected state → halt + decision file, never improvise
            runlog.write_run_record(
                ctx.config.runs_dir, state.candidate["id"], step.id,
                resolved, {"error": str(exc)}, now, ctx.clock(),
            )
            _halt(state, ctx, f"step {step.id} failed: {exc}", now)
            return state

        if step_outputs is _WAIT:
            state.status = "WAITING_DECISION"
            _event(state, ctx, "waiting_on_child", {"step": step.id, "child": step.run}, now)
            return state

        runlog.write_run_record(
            ctx.config.runs_dir, state.candidate["id"], step.id,
            resolved, {"outputs": step_outputs}, now, ctx.clock(),
        )
        state.outputs[step.id] = step_outputs
        _event(
            state, ctx, "step_done",
            {"step": step.id, "inputs": resolved, "outputs": step_outputs}, now,
        )

        if step.next_step is not None:
            state.current_step = step.next_step
            _event(state, ctx, "routed", {"from": step.id, "to": step.next_step}, now)
            continue

        if not step.route:
            state.status = "DONE"
            _event(state, ctx, "workflow_done", {"last_step": step.id}, now)
            return state

        target = step.route[step_outputs[step.route_on]]
        if target == "done":
            state.status = "DONE"
            _event(state, ctx, "workflow_done", {"last_step": step.id}, now)
        elif target == "halt":
            _halt(
                state, ctx,
                f"step {step.id} routed `{step_outputs[step.route_on]}` to halt",
                now,
            )
        else:
            state.current_step = target
            _event(state, ctx, "routed", {"from": step.id, "to": target}, now)
    return state
