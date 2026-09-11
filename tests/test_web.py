import json
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlencode

from runner import decisions, graph, state, web

SITE = web.Site(project="proj")
CLOCK = lambda: "2026-09-11T01:00:00Z"
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
        html = web.render_index(SITE, [s])
        self.assertIn("x&lt;1&gt;", html)
        self.assertNotIn("x<1>", html)
        self.assertIn("/inputs/x%3C1%3E", html)

    def test_input_page_shows_pending_form_only_when_pending(self):
        s = state.new_state({"id": "hello-1"}, "greet", "classify")
        pending = [{"gate": "g", "status": "pending", "line": "", "body": "b"}]
        registry = load_registry({"greet": WF})
        html = web.render_input(SITE, s, registry["greet"], registry, [], pending, [], "tok")
        self.assertIn("name='token' value='tok'", html)
        self.assertIn("value='approve'", html)
        answered = [{"gate": "g", "status": "approved", "line": "approved", "body": "b"}]
        self.assertNotIn("value='approve'", web.render_input(SITE, s, registry["greet"], registry, [], answered, [], "tok"))

    def test_input_page_shows_run_error(self):
        s = state.new_state({"id": "hello-1"}, "greet", "classify")
        runs = [{"file": "001-classify.json", "step": "classify", "started": "s", "finished": "f",
                 "inputs": {}, "error": "boom"}]
        registry = load_registry({"greet": WF})
        self.assertIn("boom", web.render_input(SITE, s, registry["greet"], registry, runs, [], [], "tok"))

    def test_halted_input_shows_retry_and_marks_graph(self):
        s = state.new_state({"id": "hello-1"}, "greet", "classify")
        s.status = "HALTED"
        registry = load_registry({"greet": WF})
        html = web.render_input(SITE, s, registry["greet"], registry, [], [], [], "tok")
        self.assertIn("/inputs/hello-1/retry", html)
        self.assertIn("node kind-python halted", html)

    def test_render_value_structures(self):
        self.assertIn("<ol class='values'>", web.render_value(["a", "b"]))
        self.assertIn("<table class='kv'>", web.render_value({"k": "v"}))
        self.assertIn("class='text'", web.render_value("plain <text>"))
        self.assertIn("&lt;text&gt;", web.render_value("plain <text>"))

    def test_theme_appends_project_override(self):
        with TemporaryDirectory() as tmp:
            override = Path(tmp) / "theme.css"
            self.assertNotIn("/* project override:", web.theme_css(override))
            override.write_text(":root{--accent:red}")
            css = web.theme_css(override)
            self.assertIn("--running", css)
            self.assertTrue(css.rstrip().endswith("--accent:red}"))

    def test_workflow_page_shows_routes_and_gate(self):
        registry = load_registry({"greet": WF})
        html = web.render_workflow(SITE, registry["greet"], registry, WF)
        self.assertIn("<code>GREETING</code> → <code>respond</code>", html)
        self.assertIn("<svg class='graph'", html)
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
        with self.assertRaises(web.WriteError) as cm:
            web.decide(self.config, "hello-1", {"token": "nope", "gate": "respond-approval", "action": "approve"}, "tok")
        self.assertEqual(cm.exception.status, 403)
        self.assertEqual(decisions.read_decision(self.path)[0], "pending")

    def test_approve_writes(self):
        web.decide(self.config, "hello-1", {"token": "tok", "gate": "respond-approval", "action": "approve"}, "tok")
        self.assertEqual(decisions.read_decision(self.path)[0], "approved")

    def test_reject_requires_reason(self):
        with self.assertRaises(web.WriteError) as cm:
            web.decide(self.config, "hello-1", {"token": "tok", "gate": "respond-approval", "action": "reject", "reason": " "}, "tok")
        self.assertEqual(cm.exception.status, 400)

    def test_reject_writes_reason(self):
        web.decide(self.config, "hello-1", {"token": "tok", "gate": "respond-approval", "action": "reject", "reason": "too big"}, "tok")
        self.assertEqual(decisions.read_decision(self.path), ("rejected", "rejected: too big"))

    def test_answered_twice_409(self):
        form = {"token": "tok", "gate": "respond-approval", "action": "approve"}
        web.decide(self.config, "hello-1", form, "tok")
        with self.assertRaises(web.WriteError) as cm:
            web.decide(self.config, "hello-1", form, "tok")
        self.assertEqual(cm.exception.status, 409)

    def test_unknown_gate_404(self):
        with self.assertRaises(web.WriteError) as cm:
            web.decide(self.config, "hello-1", {"token": "tok", "gate": "nope", "action": "approve"}, "tok")
        self.assertEqual(cm.exception.status, 404)


class TestServer(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.config, registry = _project(Path(self.tmp.name))
        self.server = web.make_server(self.config, registry, {"greet": WF}, "127.0.0.1", 0, CLOCK)
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
        self.assertIn("--running", self._get("/theme.css"))

    def test_retry_via_form(self):
        path = state.state_path(self.config.inputs_dir, "hello-1")
        s = state.load_state(path)
        s.status = "HALTED"
        state.save_state(path, s)
        resp = self._post("/inputs/hello-1/retry", {"token": self._token()}, {"Origin": self.base})
        self.assertEqual(resp.status, 303)
        s = state.load_state(path)
        self.assertEqual(s.status, "RUNNING")
        self.assertEqual(s.history[-1]["event"], "retried")
        self.assertEqual(s.history[-1]["at"], CLOCK())

    def test_retry_non_halted_409(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._post("/inputs/hello-1/retry", {"token": self._token()}, {"Origin": self.base})
        self.assertEqual(cm.exception.code, 409)

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


LOOP_WF = """
workflow: loopy
steps:
  - id: a
    kind: python
    run: steps.a
    inputs: [input.x]
    outputs:
      r: {enum: [AGAIN, NEXT]}
    route_on: r
    route:
      AGAIN: a
      NEXT: b
  - id: b
    kind: python
    run: steps.b
    inputs: [input.x]
    outputs:
      r: {enum: [OK, BAD]}
    route_on: r
    route:
      OK: done
      BAD: halt
"""


class TestGraph(unittest.TestCase):
    def test_layout_ranks_and_terminals(self):
        wf = load_registry({"loopy": LOOP_WF})["loopy"]
        lay = graph.layout(wf)
        self.assertEqual(lay["nodes"]["a"]["rank"], 0)
        self.assertEqual(lay["nodes"]["b"]["rank"], 1)
        self.assertEqual(lay["nodes"]["done"]["rank"], 2)
        self.assertEqual(lay["nodes"]["halt"]["rank"], 2)
        self.assertEqual(lay["cols"], 3)
        self.assertEqual(lay["rows"], 2)
        self.assertIn(("a", "a", "AGAIN"), lay["edges"])

    def test_implicit_done_edge_and_gate_step(self):
        wf = load_registry({"greet": WF})["greet"]
        edges = graph.edges_of(wf)
        self.assertIn(("respond", "done", ""), edges)
        self.assertIn(("classify", "halt", "OTHER"), edges)

    def test_svg_marks_and_back_edge(self):
        wf = load_registry({"loopy": LOOP_WF})["loopy"]
        svg = graph.render_svg(wf, {"a": "visited", "b": "current"}, {})
        self.assertIn("class='edge back'", svg)
        self.assertIn("node kind-python visited", svg)
        self.assertIn("node kind-python current", svg)
        self.assertIn("class='node kind-halt terminal'", svg)

    def test_marks_for_statuses(self):
        history = [{"at": "t", "event": "step_done", "detail": {"step": "a"}}]
        self.assertEqual(graph.marks_for("RUNNING", "b", history), {"a": "visited", "b": "current"})
        self.assertEqual(graph.marks_for("HALTED", "b", history), {"a": "visited", "b": "halted", "halt": "reached"})
        self.assertEqual(graph.marks_for("WAITING_DECISION", "b", history), {"a": "visited", "b": "waiting"})
        self.assertEqual(graph.marks_for("DONE", "b", history), {"a": "visited", "done": "reached"})


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


if __name__ == "__main__":
    unittest.main()
