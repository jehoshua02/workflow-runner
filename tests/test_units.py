import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from runner import gates, inputs, outputs, rendering, state
from runner.config import ConfigError, load_config, render_agent_argv
from runner.executors import agent_exec, python_exec
from runner.pipeline import PipelineError, load_pipeline

VALID_YAML = """
pipeline: demo
steps:
  - id: triage
    executor: agent
    prompt: triage.md
    model: test-model
    allowed_tools: [Read, Grep]
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
    inputs: [candidate.fqn, triage.evidence]
    outputs:
      branch: str
    gate: merge-approval
"""


class TestPipeline(unittest.TestCase):
    def test_valid_pipeline_loads(self):
        p = load_pipeline(VALID_YAML)
        self.assertEqual(p.name, "demo")
        self.assertEqual(p.first_step, "triage")
        self.assertEqual(p.steps["prepare"].gate, "merge-approval")

    def test_route_target_must_exist(self):
        bad = VALID_YAML.replace("DEAD: prepare", "DEAD: nonexistent")
        with self.assertRaisesRegex(PipelineError, "nonexistent"):
            load_pipeline(bad)

    def test_route_must_cover_enum(self):
        bad = VALID_YAML.replace("      UNCLEAR: halt_inbox\n", "")
        with self.assertRaisesRegex(PipelineError, "UNCLEAR"):
            load_pipeline(bad)

    def test_python_step_requires_run(self):
        bad = VALID_YAML.replace("    run: steps.prepare\n", "")
        with self.assertRaisesRegex(PipelineError, "requires `run`"):
            load_pipeline(bad)

    def test_agent_step_requires_model(self):
        bad = VALID_YAML.replace("    model: test-model\n", "")
        with self.assertRaisesRegex(PipelineError, "requires `model`"):
            load_pipeline(bad)

    def test_agent_step_requires_allowed_tools(self):
        bad = VALID_YAML.replace("    allowed_tools: [Read, Grep]\n", "")
        with self.assertRaisesRegex(PipelineError, "allowed_tools"):
            load_pipeline(bad)

    def test_input_source_must_be_known(self):
        bad = VALID_YAML.replace("triage.evidence", "ghost.evidence")
        with self.assertRaisesRegex(PipelineError, "ghost"):
            load_pipeline(bad)

    def test_route_on_must_be_declared_output(self):
        bad = VALID_YAML.replace("route_on: verdict", "route_on: mood")
        with self.assertRaisesRegex(PipelineError, "mood"):
            load_pipeline(bad)


class TestState(unittest.TestCase):
    def test_roundtrip(self):
        with TemporaryDirectory() as tmp:
            s = state.new_state({"id": "cand-1", "fqn": "A::b"}, "triage")
            path = state.state_path(Path(tmp), "cand-1")
            state.save_state(path, s)
            loaded = state.load_state(path)
            self.assertEqual(loaded, s)
            self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_candidate_requires_id(self):
        with self.assertRaises(state.StateError):
            state.new_state({"fqn": "A::b"}, "triage")


class TestRendering(unittest.TestCase):
    def test_fills_dotted_placeholders(self):
        out = rendering.render("check {{candidate.fqn}} at {{triage.evidence}}",
                               {"candidate.fqn": "A::b", "triage.evidence": "x:1"})
        self.assertEqual(out, "check A::b at x:1")

    def test_missing_value_raises(self):
        with self.assertRaisesRegex(rendering.RenderError, "candidate.fqn"):
            rendering.render("{{candidate.fqn}}", {})


class TestInputs(unittest.TestCase):
    def test_resolves_candidate_and_step_outputs(self):
        values = inputs.resolve(
            ("candidate.fqn", "triage.evidence"),
            {"id": "c", "fqn": "A::b"},
            {"triage": {"evidence": "x:1"}},
        )
        self.assertEqual(values, {"candidate.fqn": "A::b", "triage.evidence": "x:1"})

    def test_unrun_step_raises(self):
        with self.assertRaisesRegex(inputs.InputError, "not produced"):
            inputs.resolve(("triage.evidence",), {"id": "c"}, {})


class TestOutputs(unittest.TestCase):
    SPEC = {"verdict": {"enum": ["DEAD", "ALIVE"]}, "evidence": "text", "count": "int"}

    def test_valid(self):
        out = {"verdict": "DEAD", "evidence": "x", "count": 3}
        self.assertEqual(outputs.validate(out, self.SPEC, "s"), out)

    def test_enum_violation(self):
        with self.assertRaisesRegex(outputs.OutputError, "MAYBE"):
            outputs.validate({"verdict": "MAYBE", "evidence": "x", "count": 3}, self.SPEC, "s")

    def test_missing_key(self):
        with self.assertRaisesRegex(outputs.OutputError, "evidence"):
            outputs.validate({"verdict": "DEAD", "count": 3}, self.SPEC, "s")

    def test_bool_is_not_int(self):
        with self.assertRaisesRegex(outputs.OutputError, "count"):
            outputs.validate({"verdict": "DEAD", "evidence": "x", "count": True}, self.SPEC, "s")


class TestGates(unittest.TestCase):
    def _note(self, tmp: Path, response: str) -> Path:
        path = gates.gate_note_path(tmp, "cand-1", "merge-approval")
        gates.write_gate_note(path, "t", "l", "ask?", "2026-09-11 15:00 MDT")
        with path.open("a") as fh:
            fh.write(response)
        return path

    def test_missing_note_is_pending(self):
        self.assertEqual(gates.read_gate_decision(Path("/nonexistent-note.md")), ("pending", ""))

    def test_empty_response_is_pending(self):
        with TemporaryDirectory() as tmp:
            self.assertEqual(self._note(Path(tmp), "")
                             and gates.read_gate_decision(self._note(Path(tmp), ""))[0], "pending")

    def test_approved(self):
        with TemporaryDirectory() as tmp:
            decision, _ = gates.read_gate_decision(self._note(Path(tmp), "Approved — ship it\n"))
            self.assertEqual(decision, "approved")

    def test_rejected(self):
        with TemporaryDirectory() as tmp:
            decision, line = gates.read_gate_decision(self._note(Path(tmp), "rejected: too big\n"))
            self.assertEqual(decision, "rejected")
            self.assertIn("too big", line)

    def test_other_text_is_pending(self):
        with TemporaryDirectory() as tmp:
            decision, _ = gates.read_gate_decision(self._note(Path(tmp), "hmm let me think\n"))
            self.assertEqual(decision, "pending")


class TestPythonExec(unittest.TestCase):
    def _module(self, tmp: Path, body: str):
        (tmp / "steps.py").write_text(body)
        return python_exec.load_steps_module(tmp)

    def test_calls_function_with_inputs(self):
        with TemporaryDirectory() as tmp:
            mod = self._module(Path(tmp), "def go(inputs):\n    return {'echo': inputs['candidate.fqn']}\n")
            out = python_exec.call(mod, "steps.go", {"candidate.fqn": "A::b"})
            self.assertEqual(out, {"echo": "A::b"})

    def test_non_dict_return_raises(self):
        with TemporaryDirectory() as tmp:
            mod = self._module(Path(tmp), "def go(inputs):\n    return 42\n")
            with self.assertRaisesRegex(python_exec.PythonStepError, "expected dict"):
                python_exec.call(mod, "steps.go", {})

    def test_unknown_function_raises(self):
        with TemporaryDirectory() as tmp:
            mod = self._module(Path(tmp), "x = 1\n")
            with self.assertRaises(python_exec.PythonStepError):
                python_exec.call(mod, "steps.go", {})


def fake_runner(replies):
    """Return a run_command that pops canned (exit_code, stdout) replies."""
    queue = list(replies)

    def run(argv, stdin_text):
        run.calls.append((argv, stdin_text))
        return queue.pop(0)

    run.calls = []
    return run


def envelope(payload: dict) -> str:
    return json.dumps({"result": json.dumps(payload)})


ARGV = ["fake-agent", "--model", "test-model"]


class TestAgentExec(unittest.TestCase):
    def test_happy_path(self):
        run = fake_runner([(0, envelope({"verdict": "DEAD"}))])
        out = agent_exec.execute(run, ARGV, "prompt")
        self.assertEqual(out, {"verdict": "DEAD"})
        argv, stdin_text = run.calls[0]
        self.assertEqual(argv, ARGV)
        self.assertEqual(stdin_text, "prompt")

    def test_fenced_json_is_extracted(self):
        reply = json.dumps({"result": "```json\n{\"verdict\": \"DEAD\"}\n```"})
        run = fake_runner([(0, reply)])
        self.assertEqual(agent_exec.execute(run, ARGV, "p"), {"verdict": "DEAD"})

    def test_bare_json_reply_accepted(self):
        run = fake_runner([(0, json.dumps({"verdict": "DEAD"}))])
        self.assertEqual(agent_exec.execute(run, ARGV, "p"), {"verdict": "DEAD"})

    def test_retries_once_then_fails(self):
        run = fake_runner([(1, ""), (0, "not json")])
        with self.assertRaises(agent_exec.AgentFailure):
            agent_exec.execute(run, ARGV, "p")
        self.assertEqual(len(run.calls), 2)

    def test_retry_then_success(self):
        run = fake_runner([(0, "garbage"), (0, envelope({"verdict": "ALIVE"}))])
        self.assertEqual(agent_exec.execute(run, ARGV, "p"), {"verdict": "ALIVE"})


class TestConfig(unittest.TestCase):
    def test_defaults_resolve_under_project_workflow_dir(self):
        cfg = load_config(Path("/proj"), {})
        self.assertEqual(cfg.state_dir, Path("/proj/.workflow/state"))
        self.assertEqual(cfg.inbox_dir, Path("/proj/.workflow/inbox"))
        self.assertEqual(cfg.runs_dir, Path("/proj/.workflow/runs"))
        self.assertEqual(cfg.log_path, Path("/proj/.workflow/log.jsonl"))
        self.assertEqual(cfg.max_step_visits, 3)
        self.assertEqual(cfg.agent_command[0], "claude")

    def test_overrides(self):
        cfg = load_config(
            Path("/proj"),
            {"inbox_dir": "../inbox", "max_step_visits": 5, "agent_command": ["my-agent"]},
        )
        self.assertEqual(cfg.inbox_dir, Path("/inbox"))
        self.assertEqual(cfg.max_step_visits, 5)
        self.assertEqual(cfg.agent_command, ("my-agent",))

    def test_bad_max_visits(self):
        with self.assertRaises(ConfigError):
            load_config(Path("/proj"), {"max_step_visits": 0})

    def test_render_agent_argv(self):
        argv = render_agent_argv(
            ("claude", "-p", "--model", "{model}", "--allowedTools", "{allowed_tools}"),
            "m1",
            ("Read", "Grep"),
        )
        self.assertEqual(argv, ["claude", "-p", "--model", "m1", "--allowedTools", "Read,Grep"])


if __name__ == "__main__":
    unittest.main()
