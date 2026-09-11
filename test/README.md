# test project

A minimal live end-to-end exercise of every engine feature: composition
(`greet` runs child `analyze`), a real agent step (haiku), enum routing,
a python step, and a human gate.

```
cd test
workflow-runner start greet --input-json input.json
workflow-runner tick          # runs the agent live, then waits at the gate
workflow-runner approve hello-1   # answers the gate and finishes
workflow-runner status
```

Expected: `hello-1 [greet]: DONE @ respond`; run records under
`workflow/.state/runs/`, full event trail in `workflow/.state/log.jsonl`.
Delete `workflow/.state/` to rerun (state is gitignored).
