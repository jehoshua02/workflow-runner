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

docker-compose style: run from the project directory (the one holding
`workflow.yaml`, `prompts/`, `steps.py`). Defaults resolve at this edge only;
the engine itself has no defaults.

```
cd <project>
workflow-runner start --candidate-json c.json   # enqueue (refuses overwrite)
workflow-runner tick                            # advance everything that can move
workflow-runner watch --interval 60             # tick in a loop
workflow-runner status                          # one line per candidate
workflow-runner retry <candidate-id>            # HALTED -> RUNNING at the failed step
```

Working files (overridable in `config:`): state `./.workflow/state/`, gate/halt
notes `./.workflow/inbox/`, per-step run records (inputs + outputs/error)
`./.workflow/runs/<candidate>/NNN-<step>.json`, unified event log
`./.workflow/log.jsonl`.

```yaml
config:
  inbox_dir: ../inbox            # project decides where humans read gates
  agent_command: [claude, -p, --output-format, json, --model, "{model}",
                  --allowedTools, "{allowed_tools}"]
  max_step_visits: 3             # loop guard: same step > N times -> halt
```

Dev: `python3 -m venv .venv && .venv/bin/pip install pyyaml`, tests via
`.venv/bin/python -m unittest discover -s tests`.

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
