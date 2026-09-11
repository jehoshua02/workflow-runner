import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from runner import decisions, inputs, outputs, rendering, state
from runner.config import ConfigError, load_config, render_agent_argv
from runner.executors import agent_exec, python_exec
from runner.workflow import WorkflowError, load_registry, load_workflow

VALID_YAML = """
workflow: demo
steps:
  - id: triage
    kind: claude
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
      UNCLEAR: halt
  - id: prepare
    kind: python
    run: steps.prepare
    inputs: [candidate.fqn, triage.evidence]
    outputs:
      branch: str
    gate: merge-approval
returns:
  branch: prepare.branch
"""


class TestWorkflow(unittest.TestCase):
    def test_valid_workflow_loads(self):
        wf = load_workflow(VALID_YAML)
        self.assertEqual(wf.name, "demo")
        self.assertEqual(wf.first_step, "triage")
        self.assertEqual(wf.steps["prepare"].gate, "merge-approval")
        self.assertEqual(wf.returns, {"branch": "prepare.branch"})

    def test_route_target_must_exist(self):
        bad = VALID_YAML.replace("DEAD: prepare", "DEAD: nonexistent")
        with self.assertRaisesRegex(WorkflowError, "nonexistent"):
            load_workflow(bad)

    def test_route_must_cover_enum(self):
        bad = VALID_YAML.replace("      UNCLEAR: halt\n", "")
        with self.assertRaisesRegex(WorkflowError, "UNCLEAR"):
            load_workflow(bad)

    def test_python_step_requires_run(self):
        bad = VALID_YAML.replace("    run: steps.prepare\n", "")
        with self.assertRaisesRegex(WorkflowError, "requires `run`"):
            load_workflow(bad)

    def test_agent_step_requires_model(self):
        bad = VALID_YAML.replace("    model: test-model\n", "")
        with self.assertRaisesRegex(WorkflowError, "requires `model`"):
            load_workflow(bad)

    def test_agent_step_requires_allowed_tools(self):
        bad = VALID_YAML.replace("    allowed_tools: [Read, Grep]\n", "")
        with self.assertRaisesRegex(WorkflowError, "allowed_tools"):
            load_workflow(bad)

    def test_input_source_must_be_known(self):
        bad = VALID_YAML.replace("triage.evidence", "ghost.evidence")
        with self.assertRaisesRegex(WorkflowError, "ghost"):
            load_workflow(bad)

    def test_route_on_must_be_declared_output(self):
        bad = VALID_YAML.replace("route_on: verdict", "route_on: mood")
        with self.assertRaisesRegex(WorkflowError, "mood"):
            load_workflow(bad)

    def test_returns_must_reference_real_outputs(self):
        bad = VALID_YAML.replace("branch: prepare.branch", "branch: prepare.nope")
        with self.assertRaisesRegex(WorkflowError, "nope"):
            load_workflow(bad)


PARENT_YAML = """
workflow: parent
steps:
  - id: sub
    kind: workflow
    run: demo
    inputs: [candidate.fqn]
    outputs:
      branch: str
"""


class TestRegistry(unittest.TestCase):
    def test_composition_validates(self):
        registry = load_registry({"demo": VALID_YAML, "parent": PARENT_YAML})
        self.assertEqual(set(registry), {"demo", "parent"})

    def test_missing_child_workflow(self):
        with self.assertRaisesRegex(WorkflowError, "not found"):
            load_registry({"parent": PARENT_YAML})

    def test_child_must_return_declared_outputs(self):
        parent = PARENT_YAML.replace("branch: str", "pr_url: str")
        with self.assertRaisesRegex(WorkflowError, "pr_url"):
            load_registry({"demo": VALID_YAML, "parent": parent})

    def test_name_must_match_file_stem(self):
        with self.assertRaisesRegex(WorkflowError, "match"):
            load_registry({"other": VALID_YAML})


class TestState(unittest.TestCase):
    def test_roundtrip(self):
        with TemporaryDirectory() as tmp:
            s = state.new_state({"id": "cand-1", "fqn": "A::b"}, "demo", "triage")
            path = state.state_path(Path(tmp), "cand-1")
            state.save_state(path, s)
            loaded = state.load_state(path)
            self.assertEqual(loaded, s)
            self.assertEqual(loaded.workflow, "demo")

    def test_candidate_requires_id(self):
        with self.assertRaises(state.StateError):
            state.new_state({"fqn": "A::b"}, "demo", "triage")


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


class TestDecisions(unittest.TestCase):
    def _note(self, tmp: Path, response: str) -> Path:
        path = decisions.decision_path(tmp, "cand-1", "merge-approval")
        decisions.write_decision(path, "t", "l", "ask?", "2026-09-11 15:00 MDT")
        with path.open("a") as fh:
            fh.write(response)
        return path

    def test_missing_note_is_pending(self):
        self.assertEqual(decisions.read_decision(Path("/nonexistent-note.md")), ("pending", ""))

    def test_empty_response_is_pending(self):
        with TemporaryDirectory() as tmp:
            decision, _ = decisions.read_decision(self._note(Path(tmp), ""))
            self.assertEqual(decision, "pending")

    def test_approved(self):
        with TemporaryDirectory() as tmp:
            decision, _ = decisions.read_decision(self._note(Path(tmp), "Approved — ship it\n"))
            self.assertEqual(decision, "approved")

    def test_rejected(self):
        with TemporaryDirectory() as tmp:
            decision, line = decisions.read_decision(self._note(Path(tmp), "rejected: too big\n"))
            self.assertEqual(decision, "rejected")
            self.assertIn("too big", line)

    def test_other_text_is_pending(self):
        with TemporaryDirectory() as tmp:
            decision, _ = decisions.read_decision(self._note(Path(tmp), "hmm let me think\n"))
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
    def test_defaults_resolve_under_state_dir(self):
        cfg = load_config(Path("/proj/workflow"), {})
        self.assertEqual(cfg.candidates_dir, Path("/proj/workflow/.state/candidates"))
        self.assertEqual(cfg.decisions_dir, Path("/proj/workflow/.state/decisions"))
        self.assertEqual(cfg.runs_dir, Path("/proj/workflow/.state/runs"))
        self.assertEqual(cfg.log_path, Path("/proj/workflow/.state/log.jsonl"))
        self.assertEqual(cfg.workflows_dir, Path("/proj/workflow/workflows"))
        self.assertEqual(cfg.max_step_visits, 3)
        self.assertEqual(cfg.max_depth, 5)
        self.assertIsNone(cfg.on_event)
        self.assertIsNone(cfg.agent_command)  # generic agents opt in; claude is built in

    def test_overrides(self):
        cfg = load_config(
            Path("/proj/workflow"),
            {"max_step_visits": 5, "agent_command": ["my-agent"], "on_event": "steps.notify"},
        )
        self.assertEqual(cfg.max_step_visits, 5)
        self.assertEqual(cfg.agent_command, ("my-agent",))
        self.assertEqual(cfg.on_event, "steps.notify")

    def test_bad_max_visits(self):
        with self.assertRaises(ConfigError):
            load_config(Path("/proj/workflow"), {"max_step_visits": 0})

    def test_bad_on_event(self):
        with self.assertRaises(ConfigError):
            load_config(Path("/proj/workflow"), {"on_event": "notify"})

    def test_claude_argv_is_built_in(self):
        from runner.executors.claude_exec import build_argv

        argv = build_argv("claude-opus-4-6", ("Read", "Grep"))
        self.assertEqual(argv[:2], ["claude", "-p"])
        self.assertIn("--allowedTools", argv)
        self.assertIn("Read,Grep", argv)

    def test_render_agent_argv(self):
        argv = render_agent_argv(
            ("claude", "-p", "--model", "{model}", "--allowedTools", "{allowed_tools}"),
            "m1",
            ("Read", "Grep"),
        )
        self.assertEqual(argv, ["claude", "-p", "--model", "m1", "--allowedTools", "Read,Grep"])


if __name__ == "__main__":
    unittest.main()
