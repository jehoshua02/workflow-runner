"""The engine: advance one candidate through the pipeline until it blocks.

A tick runs steps until the candidate is DONE, HALTED, or WAITING_GATE.
Gates and failures always leave a note in the inbox; the engine never
improvises — any unexpected state halts the candidate and asks a human.
Subagents never move pipeline state: the engine routes on their declared
outputs.

Every step execution writes a run record (inputs + outputs/error) under
runs_dir, and every event appends to the unified JSONL log.
"""
from dataclasses import dataclass
from pathlib import Path

from . import gates, inputs, outputs, rendering, runlog
from .config import RunnerConfig, render_agent_argv
from .executors import agent_exec, python_exec
from .pipeline import Pipeline
from .state import CandidateState, record


@dataclass(frozen=True)
class Context:
    config: RunnerConfig
    steps_module: object
    prompts_dir: Path
    run_command: object
    clock: object  # callable() -> ISO timestamp string


def _event(state: CandidateState, ctx: Context, event: str, detail: dict, now: str) -> None:
    record(state, event, detail, now)
    runlog.append_log(ctx.config.log_path, state.candidate["id"], event, detail, now)


def _halt(state: CandidateState, ctx: Context, reason: str, now: str) -> None:
    state.status = "HALTED"
    _event(state, ctx, "halted", {"reason": reason}, now)
    note = gates.gate_note_path(ctx.config.inbox_dir, state.candidate["id"], "halted")
    ctx.config.inbox_dir.mkdir(parents=True, exist_ok=True)
    gates.write_gate_note(
        note,
        title=f"HALTED: {state.candidate['id']} at {state.current_step}",
        link=f"state file: {state.candidate['id']}.json · runs: runs/{state.candidate['id']}/",
        ask=reason,
        now=now,
    )


def _check_gate(state: CandidateState, ctx: Context, gate_name: str, now: str) -> str:
    note = gates.gate_note_path(ctx.config.inbox_dir, state.candidate["id"], gate_name)
    decision, line = gates.read_gate_decision(note)
    if decision == "pending":
        if not note.exists():
            ctx.config.inbox_dir.mkdir(parents=True, exist_ok=True)
            gates.write_gate_note(
                note,
                title=f"Gate `{gate_name}`: {state.candidate['id']}",
                link=f"state file: {state.candidate['id']}.json",
                ask=f"Approve `{state.current_step}` for {state.candidate['id']}? "
                "Reply `approved` or `rejected: <reason>` below.",
                now=now,
            )
            _event(state, ctx, "gate_opened", {"gate": gate_name}, now)
        state.status = "WAITING_GATE"
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


def _execute(step, resolved: dict, ctx: Context) -> dict:
    if step.executor == "python":
        raw = python_exec.call(ctx.steps_module, step.run, resolved)
    else:
        template = (ctx.prompts_dir / step.prompt).read_text()
        prompt_text = rendering.render(template, resolved)
        argv = render_agent_argv(ctx.config.agent_command, step.model, step.allowed_tools)
        raw = agent_exec.execute(ctx.run_command, argv, prompt_text)
    return outputs.validate(raw, step.outputs, step.id)


def tick_candidate(pipeline: Pipeline, state: CandidateState, ctx: Context) -> CandidateState:
    while state.status == "RUNNING":
        now = ctx.clock()
        step = pipeline.steps[state.current_step]

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
            step_outputs = _execute(step, resolved, ctx)
        except Exception as exc:  # any unexpected state → halt + inbox, never improvise
            runlog.write_run_record(
                ctx.config.runs_dir, state.candidate["id"], step.id,
                resolved, {"error": str(exc)}, now, ctx.clock(),
            )
            _halt(state, ctx, f"step {step.id} failed: {exc}", now)
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

        if not step.route:
            state.status = "DONE"
            _event(state, ctx, "pipeline_done", {"last_step": step.id}, now)
            return state

        target = step.route[step_outputs[step.route_on]]
        if target == "done":
            state.status = "DONE"
            _event(state, ctx, "pipeline_done", {"last_step": step.id}, now)
        elif target == "halt_inbox":
            _halt(
                state, ctx,
                f"step {step.id} routed `{step_outputs[step.route_on]}` to halt_inbox",
                now,
            )
        else:
            state.current_step = target
            _event(state, ctx, "routed", {"from": step.id, "to": target}, now)
    return state
