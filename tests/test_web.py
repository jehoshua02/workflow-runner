import json
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlencode

from runner import decisions, state, web
from runner.config import load_config
from runner.workflow import load_registry

WF = """
workflow: greet
steps:
  - id: classify
    kind: python
    run: steps.classify
    inputs: [input.word]
    outputs:
      kind: {enum: [GREETING, OTHER]}
    route_on: kind
    route:
      GREETING: respond
      OTHER: halt
  - id: respond
    kind: python
    run: steps.respond
    inputs: [input.word]
    outputs:
      message: str
    gate: respond-approval
"""


def _project(tmp: Path):
    wf_dir = tmp / "workflow"
    (wf_dir / "workflows").mkdir(parents=True)
    (wf_dir / "workflows" / "greet.yaml").write_text(WF)
    config = load_config(wf_dir, {})
    registry = load_registry({"greet": WF})
    s = state.new_state({"id": "hello-1", "word": "hola"}, "greet", "classify")
    s.status = "WAITING_DECISION"
    s.current_step = "respond"
    state.record(s, "gate_opened", {"gate": "respond-approval"}, "2026-09-11T00:00:00Z")
    config.inputs_dir.mkdir(parents=True)
    state.save_state(state.state_path(config.inputs_dir, "hello-1"), s)
    decisions.write_decision(
        decisions.decision_path(config.decisions_dir, "hello-1", "respond-approval"),
        "Gate", "link", "Approve?", "2026-09-11T00:00:00Z",
    )
    (config.runs_dir / "hello-1").mkdir(parents=True)
    (config.runs_dir / "hello-1" / "001-classify.json").write_text(json.dumps({
        "step": "classify", "started": "s", "finished": "f",
        "inputs": {"input.word": "hola"}, "outputs": {"kind": "GREETING"},
    }))
    config.log_path.write_text(json.dumps({"at": "t", "input": "hello-1", "event": "step_done", "detail": {}}) + "\n")
    return config, registry


class TestRender(unittest.TestCase):
    def test_index_lists_inputs_and_escapes(self):
        s = state.new_state({"id": "x<1>"}, "greet", "classify")
        html = web.render_index([s])
        self.assertIn("x&lt;1&gt;", html)
        self.assertNotIn("x<1>", html)
        self.assertIn("/inputs/x%3C1%3E", html)

    def test_input_page_shows_pending_form_only_when_pending(self):
        s = state.new_state({"id": "hello-1"}, "greet", "classify")
        pending = [{"gate": "g", "status": "pending", "line": "", "body": "b"}]
        html = web.render_input(s, [], pending, "tok")
        self.assertIn("name='token' value='tok'", html)
        self.assertIn("value='approve'", html)
        answered = [{"gate": "g", "status": "approved", "line": "approved", "body": "b"}]
        self.assertNotIn("value='approve'", web.render_input(s, [], answered, "tok"))

    def test_input_page_shows_run_error(self):
        s = state.new_state({"id": "hello-1"}, "greet", "classify")
        runs = [{"file": "001-classify.json", "step": "classify", "started": "s", "finished": "f",
                 "inputs": {}, "error": "boom"}]
        self.assertIn("boom", web.render_input(s, runs, [], "tok"))

    def test_workflow_page_shows_routes_and_gate(self):
        registry = load_registry({"greet": WF})
        html = web.render_workflow(registry["greet"], WF)
        self.assertIn("GREETING → <code>respond</code>", html)
        self.assertIn("gate: respond-approval", html)
        self.assertIn("steps.classify", html)


class TestDecide(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.config, _ = _project(Path(self.tmp.name))
        self.path = decisions.decision_path(self.config.decisions_dir, "hello-1", "respond-approval")

    def tearDown(self):
        self.tmp.cleanup()

    def test_bad_token_403(self):
        with self.assertRaises(web.DecideError) as cm:
            web.decide(self.config, "hello-1", {"token": "nope", "gate": "respond-approval", "action": "approve"}, "tok")
        self.assertEqual(cm.exception.status, 403)
        self.assertEqual(decisions.read_decision(self.path)[0], "pending")

    def test_approve_writes(self):
        web.decide(self.config, "hello-1", {"token": "tok", "gate": "respond-approval", "action": "approve"}, "tok")
        self.assertEqual(decisions.read_decision(self.path)[0], "approved")

    def test_reject_requires_reason(self):
        with self.assertRaises(web.DecideError) as cm:
            web.decide(self.config, "hello-1", {"token": "tok", "gate": "respond-approval", "action": "reject", "reason": " "}, "tok")
        self.assertEqual(cm.exception.status, 400)

    def test_reject_writes_reason(self):
        web.decide(self.config, "hello-1", {"token": "tok", "gate": "respond-approval", "action": "reject", "reason": "too big"}, "tok")
        self.assertEqual(decisions.read_decision(self.path), ("rejected", "rejected: too big"))

    def test_answered_twice_409(self):
        form = {"token": "tok", "gate": "respond-approval", "action": "approve"}
        web.decide(self.config, "hello-1", form, "tok")
        with self.assertRaises(web.DecideError) as cm:
            web.decide(self.config, "hello-1", form, "tok")
        self.assertEqual(cm.exception.status, 409)

    def test_unknown_gate_404(self):
        with self.assertRaises(web.DecideError) as cm:
            web.decide(self.config, "hello-1", {"token": "tok", "gate": "nope", "action": "approve"}, "tok")
        self.assertEqual(cm.exception.status, 404)


class TestServer(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.config, registry = _project(Path(self.tmp.name))
        self.server = web.make_server(self.config, registry, {"greet": WF}, "127.0.0.1", 0)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _get(self, path: str) -> str:
        with urllib.request.urlopen(self.base + path) as r:
            return r.read().decode()

    def _post(self, path: str, form: dict, headers: dict):
        req = urllib.request.Request(self.base + path, data=urlencode(form).encode(), headers=headers, method="POST")
        try:
            return urllib.request.build_opener(_NoRedirect).open(req)
        except urllib.error.HTTPError as exc:
            if exc.code == 303:
                return exc  # urllib surfaces an un-followed redirect as an HTTPError
            raise

    def _token(self) -> str:
        html = self._get("/inputs/hello-1")
        marker = "name='token' value='"
        start = html.index(marker) + len(marker)
        return html[start : html.index("'", start)]

    def test_pages_render(self):
        self.assertIn("hello-1", self._get("/"))
        self.assertIn("respond-approval", self._get("/inputs/hello-1"))
        self.assertIn("001-classify.json", self._get("/inputs/hello-1"))
        self.assertIn("greet", self._get("/workflows"))
        self.assertIn("steps.respond", self._get("/workflows/greet"))
        self.assertIn("step_done", self._get("/log"))

    def test_unknown_input_404(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._get("/inputs/nope")
        self.assertEqual(cm.exception.code, 404)

    def test_approve_via_form_redirects_and_writes(self):
        resp = self._post("/inputs/hello-1/decide",
                          {"token": self._token(), "gate": "respond-approval", "action": "approve"},
                          {"Origin": self.base})
        self.assertEqual(resp.status, 303)
        self.assertEqual(resp.headers["Location"], "/inputs/hello-1")
        path = decisions.decision_path(self.config.decisions_dir, "hello-1", "respond-approval")
        self.assertEqual(decisions.read_decision(path)[0], "approved")

    def test_cross_origin_post_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._post("/inputs/hello-1/decide",
                       {"token": self._token(), "gate": "respond-approval", "action": "approve"},
                       {"Origin": "http://evil.example"})
        self.assertEqual(cm.exception.code, 403)

    def test_missing_token_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._post("/inputs/hello-1/decide", {"gate": "respond-approval", "action": "approve"}, {})
        self.assertEqual(cm.exception.code, 403)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


if __name__ == "__main__":
    unittest.main()
