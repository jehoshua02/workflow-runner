# workflow-runner

A deterministic workflow runner that uses AI agents only at judgment points.

Workflows are data (YAML); mechanics are Python; agents run headlessly on
templated prompts and return schema-validated JSON. `kind: claude` (the
Claude Code CLI) is built in; `kind: agent` runs any other runtime via a
project-configured `agent_command`. Agents never move
workflow state — the runner routes on their declared outputs. Human
approvals are decision files: the runner halts and resumes on an
`approved` / `rejected: <reason>` reply. Any unexpected state halts the
input with a decision file; the runner never improvises. Workflows
compose: a step can run a child workflow.

## 1. Project layout

```
<project>/workflow/
  config.yml        — optional; on_event hook, agent_command, limits
  workflows/*.yaml  — one workflow per file, `workflow:` name == file stem
  prompts/          — agent prompt templates, {{dotted.ref}} placeholders
  steps.py          — python step functions fn(inputs: dict) -> dict,
                      plus the optional on_event(event: dict) hook
  .state/           — runner-owned:
    inputs/     — one JSON per input (children: <parent>.<step>.json)
    runs/           — per-step run records: <input>/NNN-<step>.json
                      (inputs + outputs/error + timestamps)
    decisions/      — decision files awaiting a human reply
    log.jsonl       — unified event log across all inputs
```

The engine has no notion of inboxes, tickets, or chat — `on_event`
(`steps.<fn>`, fired on gate_opened/halted) is the project's bridge from
decision files to wherever its humans look.

## 2. Usage

Run from the project root (the directory containing `workflow/`):

```
workflow-runner start <workflow> --input-json c.json
workflow-runner tick                    # run once: advance everything that can move
workflow-runner tick --loop --interval 60
workflow-runner status                  # tree: inputs + child workflows
workflow-runner approve <input-id> [gate]        # answer a pending decision + tick
workflow-runner reject <input-id> <reason> [gate] # answer + tick
workflow-runner retry <input-id>    # HALTED -> RUNNING at the failed step
```

Defaults resolve only at this edge (CLI + config.yml); the engine requires
every value explicitly.

## 3. Step schema

```yaml
workflow: my-workflow
steps:
  - id: triage
    kind: claude                 # python | claude | agent | workflow
    prompt: triage.md            # claude/agent: file in prompts/
    model: <model-id>            # claude/agent: required
    allowed_tools: [Read, Grep]  # claude/agent: required — containment before autonomy
    inputs: [input.fqn, other_step.field]   # dotted, explicit, no ambient state
    outputs:
      verdict: {enum: [DEAD, ALIVE, UNCLEAR]}
      evidence: text             # text|str, int, list
    route_on: verdict
    route:                       # must cover every enum value
      DEAD: next_step
      ALIVE: done                # builtins: done, halt
      UNCLEAR: halt
  - id: next_step
    kind: python
    run: steps.prepare           # function in steps.py
    inputs: [triage.evidence]
    outputs: {branch: str}
    next: publish                # unconditional chain (mutually exclusive with route)
    gate: merge-approval         # halts for a human decision before executing
  - id: publish
    kind: workflow           # composition: runs a child workflow
    run: publish-flow            # workflows/publish-flow.yaml
    inputs: [next_step.branch]   # become the child's input fields (last segment)
    outputs: {pr_url: str}       # must be covered by the child's `returns`
returns:                         # what this workflow exposes to a parent
  branch: next_step.branch
```

Composition semantics: the child runs as its own input
(`<parent>.<step>`), with its own state, runs, and decisions. Child DONE →
its `returns` become the step's outputs; child waiting on a decision →
parent waits; child HALTED → parent halts. Depth bounded by
`max_depth` (default 5); re-entering the same step more than
`max_step_visits` times (default 3) halts — the loop guard.

## 4. Design rules

- No optional arguments, no defaults below the edge — everything explicit.
- Typed I/O: outputs validated against the declared spec; an unparseable
  agent reply gets one retry, then the input halts.
- One responsibility per module: workflow (read), state (read/write),
  inputs/outputs/rendering/decisions (calculate), engine (orchestrate).

Dev: `python3 -m venv .venv && .venv/bin/pip install pyyaml`;
tests: `.venv/bin/python -m unittest discover -s tests` (53).
