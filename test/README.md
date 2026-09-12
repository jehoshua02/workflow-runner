# test project

A minimal live end-to-end exercise of every engine feature: composition
(`greet` runs child `analyze`), a real agent step (haiku), enum routing,
a python step, and a human gate.

```
cd test
workflow-runner start greet --input-json input.json
workflow-runner tick          # runs the agent live, then waits at the gate
workflow-runner approve hello-1   # answers the gate (write-only)
workflow-runner tick              # acts on the answer and finishes
workflow-runner status
workflow-runner serve             # optional: browse it all at http://127.0.0.1:8765
```

Expected: `hello-1 [greet]: DONE @ respond`; run records under
`workflow/.state/runs/`, full event trail in `workflow/.state/log.jsonl`.
Delete `workflow/.state/` to rerun (state is gitignored).
