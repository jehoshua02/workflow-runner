# test project

A minimal live end-to-end exercise of every engine feature: composition
(`greet` runs child `analyze`), a real agent step (haiku), enum routing,
a python step, and a human gate.

```
cd test
workflow-runner start greet --candidate-json candidate.json
workflow-runner tick          # runs the agent live, then waits at the gate
echo "approved" >> workflow/.state/decisions/hello-1-respond-approval.md
workflow-runner tick          # finishes
workflow-runner status
```

Expected: `hello-1 [greet]: DONE @ respond`; run records under
`workflow/.state/runs/`, full event trail in `workflow/.state/log.jsonl`.
Delete `workflow/.state/` to rerun (state is gitignored).
