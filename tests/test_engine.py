import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from runner import decisions, engine, state
from runner.config import load_config
from runner.executors.python_exec import load_steps_module
from runner.workflow import load_registry
from tests.test_units import envelope, fake_runner

DEMO_YAML = """
workflow: demo
steps:
  - id: triage
    kind: claude
    prompt: triage.md
    model: test-model
    allowed_tools: [Read]
    inputs: [input.fqn]
    outputs:
      verdict: {enum: [DEAD, ALIVE, UNCLEAR]}
      evidence: text
    route_on: verdict
    route:
      DEAD: prepare
      ALIVE: done
      UNCLEAR: halt
  - id: prepare
    kind: python
    run: steps.prepare
    inputs: [triage.evidence]
    outputs:
      branch: str
    gate: merge-approval
returns:
  branch: prepare.branch
"""

LOOP_YAML = DEMO_YAML.replace("DEAD: prepare", "DEAD: triage")

PARENT_YAML = """
workflow: parent
steps:
  - id: sub
    kind: workflow
    run: demo
    inputs: [input.fqn]
    outputs:
      branch: str
    next: wrap
  - id: wrap
    kind: python
    run: steps.wrap
    inputs: [sub.branch]
    outputs:
      wrapped: str
"""

STEPS_PY = """
events = []

def prepare(inputs):
    return {"branch": "cleanup/" + inputs["triage.evidence"]}

def wrap(inputs):
    return {"wrapped": "<" + inputs["sub.branch"] + ">"}

def notify(event):
    events.append(event)
"""


class EngineHarness:
    def __init__(self, tmp: Path, replies, yamls: dict, on_event, workflow: str):
        wf_dir = tmp / "workflow"
        (wf_dir / "prompts").mkdir(parents=True)
        (wf_dir / "prompts" / "triage.md").write_text("Input: {{input.fqn}}\nReply JSON.")
        (wf_dir / "steps.py").write_text(STEPS_PY)
        raw_config = {"on_event": on_event} if on_event else {}
        self.config = load_config(wf_dir, raw_config)
        self.registry = load_registry(yamls)
        self.run = fake_runner(replies)
        self.steps_module = load_steps_module(wf_dir)
        self.ctx = engine.Context(
            config=self.config,
            workflows=self.registry,
            steps_module=self.steps_module,
            run_command=self.run,
            clock=lambda: "2026-09-11T22:00:00Z",
        )
        self.state = state.new_state(
            {"id": "cand-1", "fqn": "A::b"}, workflow, self.registry[workflow].first_step
        )

    def tick(self):
        return engine.tick_input(self.registry[self.state.workflow], self.state, self.ctx)


def demo_harness(tmp, replies, on_event=None):
    return EngineHarness(tmp, replies, {"demo": DEMO_YAML}, on_event, "demo")


class TestEngine(unittest.TestCase):
    def test_alive_routes_to_done(self):
        with TemporaryDirectory() as tmp:
            h = demo_harness(Path(tmp), [(0, envelope({"verdict": "ALIVE", "evidence": "x:1"}))])
            s = h.tick()
            self.assertEqual(s.status, "DONE")
            self.assertIn("Input: A::b", h.run.calls[0][1])
            self.assertEqual(h.run.calls[0][0][:2], ["claude", "-p"])  # built-in executor

    def test_generic_agent_without_command_halts(self):
        with TemporaryDirectory() as tmp:
            generic = DEMO_YAML.replace("kind: claude", "kind: agent")
            h = EngineHarness(Path(tmp), [], {"demo": generic}, None, "demo")
            s = h.tick()
            self.assertEqual(s.status, "HALTED")
            self.assertIn("agent_command", s.history[-1]["detail"]["reason"])

    def test_unclear_halts_with_decision_file(self):
        with TemporaryDirectory() as tmp:
            h = demo_harness(Path(tmp), [(0, envelope({"verdict": "UNCLEAR", "evidence": "?"}))])
            s = h.tick()
            self.assertEqual(s.status, "HALTED")
            note = decisions.decision_path(h.config.decisions_dir, "cand-1", "halted")
            self.assertTrue(note.exists())

    def test_gate_pauses_then_resumes_on_approval(self):
        with TemporaryDirectory() as tmp:
            h = demo_harness(Path(tmp), [(0, envelope({"verdict": "DEAD", "evidence": "x1"}))])
            s = h.tick()
            self.assertEqual(s.status, "WAITING_DECISION")
            note = decisions.decision_path(h.config.decisions_dir, "cand-1", "merge-approval")
            self.assertTrue(note.exists())
            with note.open("a") as fh:
                fh.write("approved\n")
            s.status = "RUNNING"
            s = h.tick()
            self.assertEqual(s.status, "DONE")
            self.assertEqual(s.outputs["prepare"], {"branch": "cleanup/x1"})

    def test_gate_rejection_halts(self):
        with TemporaryDirectory() as tmp:
            h = demo_harness(Path(tmp), [(0, envelope({"verdict": "DEAD", "evidence": "x1"}))])
            s = h.tick()
            note = decisions.decision_path(h.config.decisions_dir, "cand-1", "merge-approval")
            with note.open("a") as fh:
                fh.write("rejected: not now\n")
            s.status = "RUNNING"
            s = h.tick()
            self.assertEqual(s.status, "HALTED")

    def test_agent_failure_halts_not_raises(self):
        with TemporaryDirectory() as tmp:
            h = demo_harness(Path(tmp), [(1, ""), (1, "")])
            s = h.tick()
            self.assertEqual(s.status, "HALTED")

    def test_run_records_hold_inputs_and_outputs(self):
        with TemporaryDirectory() as tmp:
            h = demo_harness(Path(tmp), [(0, envelope({"verdict": "ALIVE", "evidence": "x:1"}))])
            h.tick()
            records = sorted((h.config.runs_dir / "cand-1").glob("*.json"))
            record = json.loads(records[0].read_text())
            self.assertEqual(record["inputs"], {"input.fqn": "A::b"})
            self.assertEqual(record["outputs"]["verdict"], "ALIVE")

    def test_unified_log_written(self):
        with TemporaryDirectory() as tmp:
            h = demo_harness(Path(tmp), [(0, envelope({"verdict": "ALIVE", "evidence": "x:1"}))])
            h.tick()
            events = [json.loads(l)["event"] for l in h.config.log_path.read_text().splitlines()]
            self.assertEqual(events, ["step_done", "workflow_done"])

    def test_loop_guard_halts_runaway_cycle(self):
        with TemporaryDirectory() as tmp:
            dead = (0, envelope({"verdict": "DEAD", "evidence": "x"}))
            h = EngineHarness(Path(tmp), [dead, dead, dead], {"demo": LOOP_YAML}, None, "demo")
            s = h.tick()
            self.assertEqual(s.status, "HALTED")
            self.assertEqual(len(h.run.calls), 3)

    def test_on_event_hook_fires_on_gate_and_halt(self):
        with TemporaryDirectory() as tmp:
            h = demo_harness(Path(tmp),
                             [(0, envelope({"verdict": "DEAD", "evidence": "x1"}))],
                             on_event="steps.notify")
            h.tick()
            events = h.steps_module.events
            self.assertEqual([e["event"] for e in events], ["gate_opened"])
            self.assertTrue(events[0]["decision_path"].endswith("cand-1-merge-approval.md"))


class TestComposition(unittest.TestCase):
    def _harness(self, tmp, replies):
        return EngineHarness(
            tmp, replies, {"demo": DEMO_YAML, "parent": PARENT_YAML}, None, "parent"
        )

    def test_child_gate_propagates_then_completes(self):
        with TemporaryDirectory() as tmp:
            h = self._harness(Path(tmp), [(0, envelope({"verdict": "DEAD", "evidence": "x1"}))])
            s = h.tick()
            self.assertEqual(s.status, "WAITING_DECISION")  # child waits on merge-approval
            child_note = decisions.decision_path(
                h.config.decisions_dir, "cand-1.sub", "merge-approval"
            )
            self.assertTrue(child_note.exists())
            with child_note.open("a") as fh:
                fh.write("approved\n")
            s.status = "RUNNING"
            s = h.tick()
            self.assertEqual(s.status, "DONE")
            self.assertEqual(s.outputs["sub"], {"branch": "cleanup/x1"})
            self.assertEqual(s.outputs["wrap"], {"wrapped": "<cleanup/x1>"})

    def test_child_halt_halts_parent(self):
        with TemporaryDirectory() as tmp:
            h = self._harness(Path(tmp), [(0, envelope({"verdict": "UNCLEAR", "evidence": "?"}))])
            s = h.tick()
            self.assertEqual(s.status, "HALTED")

    def test_child_state_persisted_separately(self):
        with TemporaryDirectory() as tmp:
            h = self._harness(Path(tmp), [(0, envelope({"verdict": "ALIVE", "evidence": "x"}))])
            h.tick()
            child_path = state.state_path(h.config.inputs_dir, "cand-1.sub")
            self.assertTrue(child_path.exists())
            child = state.load_state(child_path)
            self.assertEqual(child.status, "DONE")
            self.assertEqual(child.input["fqn"], "A::b")


if __name__ == "__main__":
    unittest.main()
