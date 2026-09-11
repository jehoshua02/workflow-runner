import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from runner import engine, gates, state
from runner.config import load_config
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
    allowed_tools: [Read]
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

LOOP_PIPELINE = PIPELINE.replace("DEAD: prepare", "DEAD: triage")

STEPS_PY = """
def prepare(inputs):
    return {"branch": "cleanup/" + inputs["triage.evidence"]}
"""


class EngineHarness:
    def __init__(self, tmp: Path, replies, pipeline_yaml: str):
        (tmp / "prompts").mkdir()
        (tmp / "prompts" / "triage.md").write_text("Candidate: {{candidate.fqn}}\nReply JSON.")
        (tmp / "steps.py").write_text(STEPS_PY)
        self.config = load_config(tmp, {})
        self.inbox = self.config.inbox_dir
        self.run = fake_runner(replies)
        self.ctx = engine.Context(
            config=self.config,
            steps_module=load_steps_module(tmp),
            prompts_dir=tmp / "prompts",
            run_command=self.run,
            clock=lambda: "2026-09-11T21:00:00Z",
        )
        self.pipeline = load_pipeline(pipeline_yaml)
        self.state = state.new_state({"id": "cand-1", "fqn": "A::b"}, "triage")

    def tick(self):
        return engine.tick_candidate(self.pipeline, self.state, self.ctx)


class TestEngine(unittest.TestCase):
    def test_alive_routes_to_done(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(Path(tmp), [(0, envelope({"verdict": "ALIVE", "evidence": "x:1"}))], PIPELINE)
            s = h.tick()
            self.assertEqual(s.status, "DONE")
            self.assertIn("Candidate: A::b", h.run.calls[0][1])
            self.assertIn("--allowedTools", h.run.calls[0][0])

    def test_unclear_halts_with_inbox_note(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(Path(tmp), [(0, envelope({"verdict": "UNCLEAR", "evidence": "?"}))], PIPELINE)
            s = h.tick()
            self.assertEqual(s.status, "HALTED")
            note = gates.gate_note_path(h.inbox, "cand-1", "halted")
            self.assertTrue(note.exists())
            self.assertIn("halt_inbox", note.read_text())

    def test_dead_pauses_at_gate_then_resumes_on_approval(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(Path(tmp), [(0, envelope({"verdict": "DEAD", "evidence": "x1"}))], PIPELINE)
            s = h.tick()
            self.assertEqual(s.status, "WAITING_GATE")
            note = gates.gate_note_path(h.inbox, "cand-1", "merge-approval")
            self.assertTrue(note.exists())
            with note.open("a") as fh:
                fh.write("approved\n")
            s.status = "RUNNING"
            s = h.tick()
            self.assertEqual(s.status, "DONE")
            self.assertEqual(s.outputs["prepare"], {"branch": "cleanup/x1"})

    def test_gate_rejection_halts(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(Path(tmp), [(0, envelope({"verdict": "DEAD", "evidence": "x1"}))], PIPELINE)
            s = h.tick()
            note = gates.gate_note_path(h.inbox, "cand-1", "merge-approval")
            with note.open("a") as fh:
                fh.write("rejected: not now\n")
            s.status = "RUNNING"
            s = h.tick()
            self.assertEqual(s.status, "HALTED")

    def test_agent_failure_halts_not_raises(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(Path(tmp), [(1, ""), (1, "")], PIPELINE)
            s = h.tick()
            self.assertEqual(s.status, "HALTED")
            self.assertTrue(gates.gate_note_path(h.inbox, "cand-1", "halted").exists())

    def test_bad_agent_output_schema_halts(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(Path(tmp), [(0, envelope({"verdict": "MAYBE", "evidence": "x"}))], PIPELINE)
            s = h.tick()
            self.assertEqual(s.status, "HALTED")

    def test_run_records_hold_inputs_and_outputs(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(Path(tmp), [(0, envelope({"verdict": "ALIVE", "evidence": "x:1"}))], PIPELINE)
            h.tick()
            records = sorted((h.config.runs_dir / "cand-1").glob("*.json"))
            self.assertEqual(len(records), 1)
            record = json.loads(records[0].read_text())
            self.assertEqual(record["inputs"], {"candidate.fqn": "A::b"})
            self.assertEqual(record["outputs"]["verdict"], "ALIVE")

    def test_failed_run_records_error(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(Path(tmp), [(1, ""), (1, "")], PIPELINE)
            h.tick()
            record = json.loads(next((h.config.runs_dir / "cand-1").glob("*.json")).read_text())
            self.assertIn("error", record)

    def test_unified_log_written(self):
        with TemporaryDirectory() as tmp:
            h = EngineHarness(Path(tmp), [(0, envelope({"verdict": "ALIVE", "evidence": "x:1"}))], PIPELINE)
            h.tick()
            lines = [json.loads(l) for l in h.config.log_path.read_text().splitlines()]
            events = [l["event"] for l in lines]
            self.assertEqual(events, ["step_done", "pipeline_done"])
            self.assertTrue(all(l["candidate"] == "cand-1" for l in lines))

    def test_loop_guard_halts_runaway_cycle(self):
        with TemporaryDirectory() as tmp:
            dead = (0, envelope({"verdict": "DEAD", "evidence": "x"}))
            h = EngineHarness(Path(tmp), [dead, dead, dead], LOOP_PIPELINE)
            s = h.tick()
            self.assertEqual(s.status, "HALTED")
            self.assertEqual(len(h.run.calls), 3)  # max_step_visits default
            note = gates.gate_note_path(h.inbox, "cand-1", "halted").read_text()
            self.assertIn("loop guard", note)


if __name__ == "__main__":
    unittest.main()
