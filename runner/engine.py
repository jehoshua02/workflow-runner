"""The engine: advance one candidate through the pipeline until it blocks.

A tick runs steps until the candidate is DONE, HALTED, or WAITING_GATE.
Gates and failures always leave a note in the inbox; the engine never
improvises — any unexpected state halts the candidate and asks a human.
Subagents never move pipeline state: the engine routes on their declared
outputs.
"""
from dataclasses import dataclass
from pathlib import Path

from . import gates, inputs, outputs, rendering
from .executors import agent_exec, python_exec
from .pipeline import Pipeline
from .state import CandidateState, record


@dataclass(frozen=True)
class Context:
    steps_module: object
    prompts_dir: Path
    inbox_dir: Path
    run_command: object
    clock: object  # callable() -> ISO timestamp string


def _halt(state: CandidateState, ctx: Context, reason: str, now: str) -> None:
    state.status = "HALTED"
    record(state, "halted", {"reason": reason}, now)
    note = gates.gate_note_path(ctx.inbox_dir, state.candidate["id"], "halted")
    gates.write_gate_note(
        note,
        title=f"HALTED: {state.candidate['id']} at {state.current_step}",
        link=f"state file: {state.candidate['id']}.json",
        ask=reason,
        now=now,
    )


def _check_gate(state: CandidateState, ctx: Context, gate_name: str, now: str) -> str:
    note = gates.gate_note_path(ctx.inbox_dir, state.candidate["id"], gate_name)
    decision, line = gates.read_gate_decision(note)
    if decision == "pending":
        if not note.exists():
            gates.write_gate_note(
                note,
                title=f"Gate `{gate_name}`: {state.candidate['id']}",
                link=f"state file: {state.candidate['id']}.json",
                ask=f"Approve `{state.current_step}` for {state.candidate['id']}? "
                "Reply `approved` or `rejected: <reason>` below.",
                now=now,
            )
            record(state, "gate_opened", {"gate": gate_name}, now)
        state.status = "WAITING_GATE"
        return "wait"
    if decision == "rejected":
        _halt(state, ctx, f"gate `{gate_name}` rejected: {line}", now)
        return "halt"
    record(state, "gate_approved", {"gate": gate_name, "line": line}, now)
    return "go"


def _execute(step, state: CandidateState, ctx: Context) -> dict:
    resolved = inputs.resolve(step.inputs, state.candidate, state.outputs)
    if step.executor == "python":
        raw = python_exec.call(ctx.steps_module, step.run, resolved)
    else:
        template = (ctx.prompts_dir / step.prompt).read_text()
        prompt_text = rendering.render(template, resolved)
        raw = agent_exec.execute(ctx.run_command, prompt_text, step.model, step.allowed_tools)
    return outputs.validate(raw, step.outputs, step.id)


def tick_candidate(pipeline: Pipeline, state: CandidateState, ctx: Context) -> CandidateState:
    while state.status == "RUNNING":
        now = ctx.clock()
        step = pipeline.steps[state.current_step]

        if step.gate is not None:
            verdict = _check_gate(state, ctx, step.gate, now)
            if verdict != "go":
                return state

        try:
            step_outputs = _execute(step, state, ctx)
        except Exception as exc:  # any unexpected state → halt + inbox, never improvise
            _halt(state, ctx, f"step {step.id} failed: {exc}", now)
            return state

        state.outputs[step.id] = step_outputs
        record(state, "step_done", {"step": step.id, "outputs": step_outputs}, now)

        if not step.route:
            state.status = "DONE"
            record(state, "pipeline_done", {"last_step": step.id}, now)
            return state

        target = step.route[step_outputs[step.route_on]]
        if target == "done":
            state.status = "DONE"
            record(state, "pipeline_done", {"last_step": step.id}, now)
        elif target == "halt_inbox":
            _halt(
                state,
                ctx,
                f"step {step.id} routed `{step_outputs[step.route_on]}` to halt_inbox",
                now,
            )
        else:
            state.current_step = target
            record(state, "routed", {"from": step.id, "to": target}, now)
    return state
