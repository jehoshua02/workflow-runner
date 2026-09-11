"""Local web UI: browse inputs, runs, decisions and workflows; answer decisions.

A read view over `.state/` plus exactly one write action — `decide` — which
goes through `decisions.respond`, the same path the CLI uses. The UI never
ticks: `tick --loop` is the sole ticker, so there is one writer of state.

Containment:
- binds 127.0.0.1 only;
- POST requires the per-server random token embedded in the form, and the
  `Origin` header (when a browser sends one) must be this server — so a
  page on another site cannot approve a gate by drive-by form POST.

Stdlib only: http.server + html.escape. Server-rendered, no JavaScript.
"""
import json
import secrets
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from . import decisions, state
from .config import RunnerConfig
from .workflow import Workflow

STYLE = """
body{font:14px/1.45 -apple-system,Helvetica,Arial,sans-serif;margin:0;color:#222}
nav{background:#1f2937;padding:10px 20px}nav a{color:#e5e7eb;margin-right:18px;text-decoration:none}
main{padding:20px;max-width:1100px}h1,h2,h3{margin:18px 0 8px}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #e5e7eb;padding:6px 8px;text-align:left;vertical-align:top}
th{background:#f3f4f6}pre{background:#f3f4f6;padding:10px;overflow:auto;margin:4px 0}
code{background:#f3f4f6;padding:1px 4px}
.RUNNING{color:#1d4ed8}.WAITING_DECISION{color:#b45309}.HALTED{color:#b91c1c}.DONE{color:#15803d}
.pending{background:#fef3c7;border:1px solid #f59e0b;padding:12px;margin:8px 0}
.answered{background:#f3f4f6;padding:12px;margin:8px 0}
form.decide{display:inline-block;margin-right:12px}input[type=text]{width:320px}
button{padding:4px 10px}.muted{color:#6b7280}
"""


# ---- read ------------------------------------------------------------------


def list_states(inputs_dir: Path) -> list:
    if not inputs_dir.exists():
        return []
    return [state.load_state(p) for p in sorted(inputs_dir.glob("*.json"))]


def list_runs(runs_dir: Path, input_id: str) -> list:
    input_dir = runs_dir / input_id
    if not input_dir.exists():
        return []
    return [
        {"file": p.name, **json.loads(p.read_text())}
        for p in sorted(input_dir.glob("*.json"))
    ]


def list_decisions(decisions_dir: Path, input_id: str) -> list:
    if not decisions_dir.exists():
        return []
    found = []
    for path in sorted(decisions_dir.glob(f"{input_id}-*.md")):
        status, line = decisions.read_decision(path)
        found.append(
            {
                "gate": path.stem.removeprefix(f"{input_id}-"),
                "status": status,
                "line": line,
                "body": path.read_text(),
            }
        )
    return found


def tail_log(log_path: Path, limit: int) -> list:
    if not log_path.exists():
        return []
    lines = log_path.read_text().splitlines()
    return [json.loads(l) for l in lines[-limit:] if l.strip()]


# ---- render (pure) ----------------------------------------------------------


def _e(value) -> str:
    return escape(str(value), quote=True)


def _pre(value) -> str:
    text = value if isinstance(value, str) else json.dumps(value, indent=2, sort_keys=True)
    return f"<pre>{_e(text)}</pre>"


def input_url(input_id: str) -> str:
    return f"/inputs/{quote(input_id, safe='')}"


def page(title: str, body: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{_e(title)} · workflow-runner</title><style>{STYLE}</style></head><body>"
        "<nav><a href='/'>inputs</a><a href='/workflows'>workflows</a><a href='/log'>log</a></nav>"
        f"<main><h1>{_e(title)}</h1>{body}</main></body></html>"
    )


def render_index(states: list) -> str:
    if not states:
        return page("inputs", "<p class='muted'>no inputs</p>")
    rows = []
    for s in states:
        depth = s.input["id"].count(".")
        indent = "&nbsp;" * 4 * depth
        rows.append(
            "<tr>"
            f"<td>{indent}<a href='{input_url(s.input['id'])}'>{_e(s.input['id'])}</a></td>"
            f"<td>{_e(s.workflow)}</td>"
            f"<td class='{_e(s.status)}'>{_e(s.status)}</td>"
            f"<td>{_e(s.current_step)}</td>"
            f"<td class='muted'>{_e(s.history[-1]['at']) if s.history else ''}</td>"
            "</tr>"
        )
    body = (
        "<table><tr><th>input</th><th>workflow</th><th>status</th><th>step</th><th>last event</th></tr>"
        + "".join(rows)
        + "</table>"
    )
    return page("inputs", body)


def _decide_form(input_id: str, gate: str, token: str) -> str:
    action = f"{input_url(input_id)}/decide"
    hidden = (
        f"<input type='hidden' name='gate' value='{_e(gate)}'>"
        f"<input type='hidden' name='token' value='{_e(token)}'>"
    )
    return (
        f"<form class='decide' method='post' action='{action}'>{hidden}"
        "<input type='hidden' name='action' value='approve'><button>approve</button></form>"
        f"<form class='decide' method='post' action='{action}'>{hidden}"
        "<input type='hidden' name='action' value='reject'>"
        "<input type='text' name='reason' placeholder='reason (required)'> <button>reject</button></form>"
    )


def render_input(s: state.InputState, runs: list, decision_list: list, token: str) -> str:
    input_id = s.input["id"]
    parts = [
        f"<p>workflow <code>{_e(s.workflow)}</code> · status <b class='{_e(s.status)}'>{_e(s.status)}</b>"
        f" · step <code>{_e(s.current_step)}</code></p>",
        "<h2>decisions</h2>",
    ]
    if not decision_list:
        parts.append("<p class='muted'>none</p>")
    for d in decision_list:
        css = "pending" if d["status"] == "pending" else "answered"
        parts.append(f"<div class='{css}'><b>{_e(d['gate'])}</b> — {_e(d['status'])}")
        if d["status"] == "pending":
            parts.append("<div>" + _decide_form(input_id, d["gate"], token) + "</div>")
        else:
            parts.append(f" <code>{_e(d['line'])}</code>")
        parts.append(f"<details><summary>note</summary>{_pre(d['body'])}</details></div>")

    parts.append("<h2>input</h2>" + _pre(s.input))

    parts.append("<h2>runs</h2>")
    if not runs:
        parts.append("<p class='muted'>none yet</p>")
    for r in runs:
        outcome = "error" if "error" in r else "outputs"
        parts.append(
            f"<h3>{_e(r['file'])}</h3>"
            f"<p class='muted'>{_e(r['started'])} → {_e(r['finished'])}</p>"
            f"<details open><summary>inputs</summary>{_pre(r['inputs'])}</details>"
            f"<details open><summary>{outcome}</summary>{_pre(r[outcome])}</details>"
        )

    parts.append("<h2>history</h2>")
    rows = "".join(
        f"<tr><td class='muted'>{_e(h['at'])}</td><td>{_e(h['event'])}</td><td>{_pre(h['detail'])}</td></tr>"
        for h in s.history
    )
    parts.append(f"<table><tr><th>at</th><th>event</th><th>detail</th></tr>{rows}</table>")
    return page(input_id, "".join(parts))


def render_workflows(registry: dict) -> str:
    rows = "".join(
        f"<tr><td><a href='/workflows/{_e(name)}'>{_e(name)}</a></td>"
        f"<td>{len(wf.steps)}</td><td><code>{_e(wf.first_step)}</code></td></tr>"
        for name, wf in sorted(registry.items())
    )
    return page("workflows", f"<table><tr><th>workflow</th><th>steps</th><th>first step</th></tr>{rows}</table>")


def _step_target(step) -> str:
    if step.route:
        return "<br>".join(f"{_e(k)} → <code>{_e(v)}</code>" for k, v in step.route.items())
    if step.next_step:
        return f"→ <code>{_e(step.next_step)}</code>"
    return "→ <code>done</code>"


def render_workflow(wf: Workflow, source: str) -> str:
    rows = []
    for step in wf.steps.values():
        what = step.run if step.run else step.prompt
        gate = f"<br><span class='muted'>gate: {_e(step.gate)}</span>" if step.gate else ""
        rows.append(
            "<tr>"
            f"<td><code>{_e(step.id)}</code>{gate}</td>"
            f"<td>{_e(step.kind)}</td>"
            f"<td><code>{_e(what)}</code>{'<br><span class=muted>' + _e(step.model) + '</span>' if step.model else ''}</td>"
            f"<td>{'<br>'.join(_e(i) for i in step.inputs)}</td>"
            f"<td>{'<br>'.join(_e(o) for o in step.outputs)}</td>"
            f"<td>{_step_target(step)}</td>"
            "</tr>"
        )
    table = (
        "<table><tr><th>step</th><th>kind</th><th>run / prompt</th><th>inputs</th><th>outputs</th><th>then</th></tr>"
        + "".join(rows)
        + "</table>"
    )
    returns = _pre(wf.returns) if wf.returns else "<p class='muted'>none (top-level workflow)</p>"
    body = f"{table}<h2>returns</h2>{returns}<h2>source</h2>{_pre(source)}"
    return page(wf.name, body)


def render_log(entries: list) -> str:
    rows = "".join(
        f"<tr><td class='muted'>{_e(e['at'])}</td>"
        f"<td><a href='{input_url(e['input'])}'>{_e(e['input'])}</a></td>"
        f"<td>{_e(e['event'])}</td><td>{_pre(e['detail'])}</td></tr>"
        for e in reversed(entries)
    )
    return page("log", f"<table><tr><th>at</th><th>input</th><th>event</th><th>detail</th></tr>{rows}</table>")


# ---- decide (the one write) ---------------------------------------------------


class DecideError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def decide(config: RunnerConfig, input_id: str, form: dict, token: str) -> str:
    """Validate the form and answer the decision. Returns the line written."""
    if form.get("token") != token:
        raise DecideError(403, "bad token")
    gate = form.get("gate", "")
    action = form.get("action", "")
    if not gate:
        raise DecideError(400, "gate required")
    if action == "approve":
        line = "approved"
    elif action == "reject":
        reason = form.get("reason", "").strip()
        if not reason:
            raise DecideError(400, "reject requires a reason")
        line = f"rejected: {reason}"
    else:
        raise DecideError(400, f"unknown action `{action}`")
    path = decisions.decision_path(config.decisions_dir, input_id, gate)
    try:
        decisions.respond(path, line)
    except FileNotFoundError as exc:
        raise DecideError(404, str(exc))
    except ValueError as exc:
        raise DecideError(409, str(exc))
    return line


# ---- http -------------------------------------------------------------------


def make_handler(config: RunnerConfig, registry: dict, sources: dict, token: str, origin: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quiet
            pass

        def _send(self, status: int, html: str) -> None:
            data = html.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _redirect(self, location: str) -> None:
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _error(self, status: int, message: str) -> None:
            self._send(status, page(f"{status}", f"<p>{_e(message)}</p>"))

        def do_GET(self):
            path = urlsplit(self.path).path
            parts = [unquote(p) for p in path.strip("/").split("/") if p]
            if not parts:
                return self._send(200, render_index(list_states(config.inputs_dir)))
            if parts == ["log"]:
                return self._send(200, render_log(tail_log(config.log_path, 200)))
            if parts == ["workflows"]:
                return self._send(200, render_workflows(registry))
            if len(parts) == 2 and parts[0] == "workflows":
                if parts[1] not in registry:
                    return self._error(404, f"unknown workflow `{parts[1]}`")
                return self._send(200, render_workflow(registry[parts[1]], sources[parts[1]]))
            if len(parts) == 2 and parts[0] == "inputs":
                state_path = state.state_path(config.inputs_dir, parts[1])
                if not state_path.exists():
                    return self._error(404, f"no input `{parts[1]}`")
                s = state.load_state(state_path)
                return self._send(
                    200,
                    render_input(
                        s,
                        list_runs(config.runs_dir, parts[1]),
                        list_decisions(config.decisions_dir, parts[1]),
                        token,
                    ),
                )
            return self._error(404, "not found")

        def do_POST(self):
            path = urlsplit(self.path).path
            parts = [unquote(p) for p in path.strip("/").split("/") if p]
            if len(parts) != 3 or parts[0] != "inputs" or parts[2] != "decide":
                return self._error(404, "not found")
            sent_origin = self.headers.get("Origin")
            if sent_origin is not None and sent_origin != origin:
                return self._error(403, "cross-origin POST refused")
            length = int(self.headers.get("Content-Length", "0"))
            form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
            try:
                decide(config, parts[1], form, token)
            except DecideError as exc:
                return self._error(exc.status, str(exc))
            return self._redirect(input_url(parts[1]))

    return Handler


def make_server(config: RunnerConfig, registry: dict, sources: dict, host: str, port: int) -> ThreadingHTTPServer:
    token = secrets.token_urlsafe(24)
    server = ThreadingHTTPServer((host, port), BaseHTTPRequestHandler)  # placeholder class
    bound_port = server.server_address[1]
    origin = f"http://{host}:{bound_port}"
    server.RequestHandlerClass = make_handler(config, registry, sources, token, origin)
    return server
