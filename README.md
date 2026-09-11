# workflow-runner

A deterministic pipeline runner that uses AI agents only at judgment points.

The pipeline is data (`pipeline.yaml`); mechanics are Python; agents run
headlessly (`claude -p`) on templated prompts and return schema-validated
JSON. Agents never move pipeline state — the runner routes on their declared
outputs. Human approvals are gates: the runner halts on a templated inbox
note and resumes on an `approved` / `rejected: <reason>` reply. Any
unexpected state halts the candidate with an inbox note; the runner never
improvises.

## 1. Layout

```
runner/            — the engine (this repo, project-agnostic)
<project>/         — per-project, lives with the project:
  pipeline.yaml    — steps: executor (python|agent), inputs, outputs, route, gate
  prompts/         — one file per agent step, {{dotted.ref}} placeholders
  steps.py         — python step functions: fn(inputs: dict) -> dict
```

## 2. Usage

```
python3 -m venv .venv && .venv/bin/pip install pyyaml
.venv/bin/python -m unittest discover -s tests

.venv/bin/python -m runner.cli start --pipeline P.yaml --project-dir D \
    --state-dir S --inbox-dir I --candidate-json c.json
.venv/bin/python -m runner.cli tick --pipeline P.yaml --project-dir D \
    --state-dir S --inbox-dir I
.venv/bin/python -m runner.cli status --state-dir S
```

## 3. Step schema

```yaml
pipeline: my-pipeline
steps:
  - id: triage
    executor: agent            # python | agent
    prompt: triage.md          # agent: file in <project>/prompts/
    model: <model-id>          # agent: required
    allowed_tools: [Read, Grep]  # agent: enforced tool allowlist
    inputs: [candidate.fqn, other_step.field]  # dotted, explicit, no ambient state
    outputs:
      verdict: {enum: [DEAD, ALIVE, UNCLEAR]}
      evidence: text           # text|str, int, list
    route_on: verdict
    route:                     # must cover every enum value
      DEAD: next_step
      ALIVE: done              # builtins: done, halt_inbox
      UNCLEAR: halt_inbox
  - id: next_step
    executor: python
    run: steps.prepare         # function in <project>/steps.py
    inputs: [triage.evidence]
    outputs: {branch: str}
    gate: merge-approval       # halts for human approval before executing
```

## 4. Design rules

- No optional arguments, no defaults — every path and value is explicit.
- Typed I/O: outputs are validated against the declared spec; an agent reply
  that doesn't parse gets one retry, then the candidate halts.
- One responsibility per module: pipeline (read), state (read/write),
  inputs/outputs/rendering/routing (calculate), engine (orchestrate).
