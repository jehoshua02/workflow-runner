import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from runner import engine, gates, state
from runner.executors.python_exec import load_steps_module
from runner.pipeline import load_pipeline
from tests.test_units import envelope, fake_runner

PIPELINE = """
pipeline: demo
steps:
  - id: triage
    executor: agent
    prompt: triage.md
    model: test-model
    inputs: [candidate.fqn]
    outputs:
      verdict: {enum: [DEAD, ALIVE, UNCLEAR]}
      evidence: text
    route_on: verdict
    route:
      DEAD: prepare
      ALIVE: done
      UNCLEAR: halt_inbox
  - id: prepare
    executor: python
    run: steps.prepare
    inputs: [triage.evidence]
    outputs:
      branch: str
    gate: merge-approval
"""

STEPS_PY = """
def prepare(inputs):
    return {"branch": "cleanup/" + inputs["triage.evidence"]}
"""


class EngineHarness:
    def __init__(self, tmp: Path, replies):
        (tmp / "prompts").mkdir()
        (tmp / "prompts" / "triage.md").write_text("Candidate: {{candidate.fqn}}\nReply JSON.")
        (tmp / "inbox").mkdir()
        (tmp / "steps.py").write_text(STEPS_PY)
        self.inbox = tmp / "inbox"
        self.run = fake_runner(replies)
        self.ctx = engine.Context(
            steps_module=load_steps_module(tmp),
            prompts_dir=tmp / "prompts",
            inbox_dir=self.inbox,
            run_command=self.run,
            clock=lambda: "2026-09-11 15:00 MDT",
        )
        self.pipeline = load_pipeline(PIPELINE)
        self.state = state.new_state({"id": "cand-1", "fqn": "A::b"}, "triage")

    def tick(self):
        return engine.tick_candidate(self.pipeline, self.state, self.ctx)


class TestEngine(unittest.TestCase):
    def test_alive_routes_to_done(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(Path(tmp), [(0, envelope({"verdict": "ALIVE", "evidence": "x:1"}))])
            s = h.tick()
            self.assertEqual(s.status, "DONE")
            self.assertIn("Candidate: A::b", h.run.calls[0][1])

    def test_unclear_halts_with_inbox_note(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(Path(tmp), [(0, envelope({"verdict": "UNCLEAR", "evidence": "?"}))])
            s = h.tick()
            self.assertEqual(s.status, "HALTED")
            note = gates.gate_note_path(h.inbox, "cand-1", "halted")
            self.assertTrue(note.exists())
            self.assertIn("halt_inbox", note.read_text())

    def test_dead_pauses_at_gate_then_resumes_on_approval(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(Path(tmp), [(0, envelope({"verdict": "DEAD", "evidence": "x1"}))])
            s = h.tick()
            self.assertEqual(s.status, "WAITING_GATE")
            note = gates.gate_note_path(h.inbox, "cand-1", "merge-approval")
            self.assertTrue(note.exists())
            # human approves in the Response section
            with note.open("a") as fh:
                fh.write("approved\n")
            s.status = "RUNNING"
            s = h.tick()
            self.assertEqual(s.status, "DONE")
            self.assertEqual(s.outputs["prepare"], {"branch": "cleanup/x1"})

    def test_gate_rejection_halts(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(Path(tmp), [(0, envelope({"verdict": "DEAD", "evidence": "x1"}))])
            s = h.tick()
            note = gates.gate_note_path(h.inbox, "cand-1", "merge-approval")
            with note.open("a") as fh:
                fh.write("rejected: not now\n")
            s.status = "RUNNING"
            s = h.tick()
            self.assertEqual(s.status, "HALTED")

    def test_agent_failure_halts_not_raises(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(Path(tmp), [(1, ""), (1, "")])
            s = h.tick()
            self.assertEqual(s.status, "HALTED")
            self.assertTrue(gates.gate_note_path(h.inbox, "cand-1", "halted").exists())

    def test_bad_agent_output_schema_halts(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(Path(tmp), [(0, envelope({"verdict": "MAYBE", "evidence": "x"})),
                                          (0, envelope({"verdict": "MAYBE", "evidence": "x"}))])
            s = h.tick()
            self.assertEqual(s.status, "HALTED")


if __name__ == "__main__":
    unittest.main()
