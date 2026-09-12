"""Local web UI: workflow definitions and workflow runs; answer decisions; retry halts.

Vocabulary (UI only — the engine/CLI keep `input`): a *run* is one input's
passage through a workflow (`.state/inputs/<id>.json`); a run is made of
*steps* (`.state/runs/<id>/NNN-<step>.json`).

A read view over `.state/` plus two writes — `decide` (via `decisions.respond`)
and `retry` (via `control.retry`) — the same paths the CLI uses. The UI never
ticks: `tick --loop` is the sole ticker, so there is one writer of state.

Containment:
- binds 127.0.0.1 only;
- POST requires the per-server random token embedded in the form, and the
  `Origin` header (when a browser sends one) must be this server — so a
  page on another site cannot approve a gate by drive-by form POST.

Theming: every color/font/size is a CSS variable in `runner/theme.css`
(light + dark via prefers-color-scheme). `<project>/workflow/theme.css`,
when present, is appended after it at `/theme.css` and overrides anything.

Stdlib only: http.server + html.escape. Server-rendered, no JavaScript.
"""
import json
import secrets
from dataclasses import dataclass
from datetime import datetime
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from . import control, decisions, graph, state
from .config import RunnerConfig
from .workflow import Workflow

DEFAULT_THEME = Path(__file__).with_name("theme.css")
LOG_TAIL = 200
INDEX_REFRESH_SECONDS = 10


# ---- read ------------------------------------------------------------------


def list_states(inputs_dir: Path) -> list:
    if not inputs_dir.exists():
        return []
    return [state.load_state(p) for p in sorted(inputs_dir.glob("*.json"))]


def list_runs(runs_dir: Path, input_id: str) -> list:
    input_dir = runs_dir / input_id
    if not input_dir.exists():
        return []
    return [{"file": p.name, **json.loads(p.read_text())} for p in sorted(input_dir.glob("*.json"))]


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


def theme_css(project_theme: Path) -> str:
    css = DEFAULT_THEME.read_text()
    if project_theme.exists():
        css += f"\n\n/* project override: {project_theme} */\n" + project_theme.read_text()
    return css


# ---- render (pure) ----------------------------------------------------------


def _e(value) -> str:
    return escape(str(value), quote=True)


def run_url(run_id: str) -> str:
    return f"/runs/{quote(run_id, safe='')}"


def workflow_url(name: str) -> str:
    return f"/workflows/{quote(name, safe='')}"


def _badge(status: str) -> str:
    return f"<span class='badge {_e(status)}'>{_e(status)}</span>"


def _duration(started: str, finished: str) -> str:
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    try:
        seconds = (datetime.strptime(finished, fmt) - datetime.strptime(started, fmt)).total_seconds()
    except ValueError:
        return ""
    return f"{int(seconds)}s" if seconds < 90 else f"{seconds / 60:.1f}m"


def render_value(value) -> str:
    """Structured rendering of a step input/output value: text, lists, mappings."""
    if isinstance(value, str):
        return f"<div class='text'>{_e(value)}</div>"
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return f"<code>{_e(json.dumps(value))}</code>"
    if isinstance(value, list):
        if not value:
            return "<span class='muted'>[]</span>"
        return "<ol class='values'>" + "".join(f"<li>{render_value(v)}</li>" for v in value) + "</ol>"
    if isinstance(value, dict):
        if not value:
            return "<span class='muted'>{}</span>"
        rows = "".join(f"<tr><th>{_e(k)}</th><td>{render_value(v)}</td></tr>" for k, v in value.items())
        return f"<table class='kv'>{rows}</table>"
    return f"<pre>{_e(json.dumps(value, indent=2, sort_keys=True))}</pre>"


@dataclass(frozen=True)
class Site:
    project: str

    def page(self, title: str, heading: str, body: str, active: str, crumbs: str, refresh: int | None) -> str:
        """`title` is plain text for the browser tab; `heading` is HTML for the h1."""
        links = "".join(
            f"<a href='{href}' class='{'active' if key == active else ''}'>{key}</a>"
            for key, href in (("workflows", "/workflows"), ("runs", "/runs"), ("log", "/log"))
        )
        meta_refresh = f"<meta http-equiv='refresh' content='{refresh}'>" if refresh else ""
        crumbs_html = f"<div class='crumbs'>{crumbs}</div>" if crumbs else ""
        return (
            "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
            f"{meta_refresh}<title>{_e(title)} · workflow-runner</title>"
            "<link rel='stylesheet' href='/theme.css'></head><body>"
            f"<nav><span class='brand'>workflow-runner</span>{links}<span class='project'>{_e(self.project)}</span></nav>"
            f"<main>{crumbs_html}<h1>{heading}</h1>{body}</main></body></html>"
        )


def _run_rows(states: list, show_workflow: bool) -> str:
    rows = []
    for s in states:
        depth = s.input["id"].count(".")
        indent = "<span class='muted'>└ </span>" if depth else ""
        rows.append(
            "<tr>"
            f"<td style='padding-left:{12 + depth * 22}px'>{indent}<a href='{run_url(s.input['id'])}'><code>{_e(s.input['id'])}</code></a></td>"
            + (f"<td><a href='{workflow_url(s.workflow)}'>{_e(s.workflow)}</a></td>" if show_workflow else "")
            + f"<td>{_badge(s.status)}</td>"
            f"<td><code>{_e(s.current_step)}</code></td>"
            f"<td class='muted'>{_e(s.history[-1]['at']) if s.history else ''}</td>"
            "</tr>"
        )
    head = "<th>run</th>" + ("<th>workflow</th>" if show_workflow else "") + "<th>status</th><th>step</th><th>last event</th>"
    return f"<table><tr>{head}</tr>{''.join(rows)}</table>"


def render_runs(site: Site, states: list) -> str:
    if not states:
        return site.page("runs", "runs", "<p class='muted'>no runs yet — <code>workflow-runner start &lt;workflow&gt; --input-json …</code></p>", "runs", "", INDEX_REFRESH_SECONDS)
    body = _run_rows(states, True) + f"<p class='muted'>refreshes every {INDEX_REFRESH_SECONDS}s</p>"
    return site.page("runs", "runs", body, "runs", "", INDEX_REFRESH_SECONDS)


def _hidden(token: str, **fields) -> str:
    fields["token"] = token
    return "".join(f"<input type='hidden' name='{_e(k)}' value='{_e(v)}'>" for k, v in fields.items())


def _decide_forms(input_id: str, gate: str, token: str) -> str:
    action = f"{run_url(input_id)}/decide"
    return (
        f"<form class='inline' method='post' action='{action}'>{_hidden(token, gate=gate, action='approve')}"
        "<button class='primary'>approve</button></form>"
        f"<form class='inline' method='post' action='{action}'>{_hidden(token, gate=gate, action='reject')}"
        "<input type='text' name='reason' placeholder='reason (required)' required>"
        "<button class='danger'>reject</button></form>"
    )


def _retry_form(input_id: str, token: str) -> str:
    return (
        f"<form class='inline' method='post' action='{run_url(input_id)}/retry'>{_hidden(token)}"
        "<button class='primary'>retry from this step</button> "
        "<span class='muted'>sets RUNNING; the next tick re-executes the step</span></form>"
    )


def _child_links(step, registry: dict) -> dict:
    return {s.id: workflow_url(s.run) for s in step.values() if s.kind == "workflow" and s.run in registry}


def render_run(site: Site, s: state.InputState, workflow: Workflow, registry: dict, steps: list, decision_list: list, children: list, token: str) -> str:
    input_id = s.input["id"]
    heading = f"<code>{_e(input_id)}</code> {_badge(s.status)} <span class='muted'>@ {_e(s.current_step)}</span>"
    crumbs = f"<a href='/workflows'>workflows</a> / <a href='{workflow_url(s.workflow)}'>{_e(s.workflow)}</a> / <a href='/runs'>runs</a> / {_e(input_id)}"
    parts = []

    marks = graph.marks_for(s.status, s.current_step, s.history)
    parts.append(f"<div class='graph-wrap'>{graph.render_svg(workflow, marks, _child_links(workflow.steps, registry))}</div>")

    if s.status == "HALTED":
        parts.append(f"<div class='card decision pending'><header><h3>halted at <code>{_e(s.current_step)}</code></h3></header>{_retry_form(input_id, token)}</div>")

    pending = [d for d in decision_list if d["status"] == "pending"]
    answered = [d for d in decision_list if d["status"] != "pending"]
    if pending:
        parts.append("<h2>needs a decision</h2>")
    for d in pending:
        parts.append(
            f"<div class='card decision pending'><header><h3>{_e(d['gate'])}</h3><span class='when'>pending</span></header>"
            f"<pre>{_e(d['body'].strip())}</pre>"
            f"<div class='actions'>{_decide_forms(input_id, d['gate'], token)}</div></div>"
        )

    if children:
        parts.append("<h2>child runs</h2>" + _run_rows(children, True))

    parts.append("<h2>steps</h2>")
    if not steps:
        parts.append("<p class='muted'>none executed yet</p>")
    for r in steps:
        outcome = "error" if "error" in r else "outputs"
        parts.append(
            f"<div class='card'><header><h3><code>{_e(r['step'])}</code></h3><span class='muted'>{_e(r['file'])}</span>"
            f"<span class='when'>{_e(r['started'])} · {_duration(r['started'], r['finished'])}</span></header>"
            f"<details><summary>step inputs</summary>{render_value(r['inputs'])}</details>"
            f"<h3 style='margin-top:10px'>{outcome}</h3>{render_value(r[outcome])}</div>"
        )

    parts.append("<h2>input</h2>" + render_value(s.input))

    if answered:
        parts.append("<h2>answered decisions</h2>")
        for d in answered:
            parts.append(
                f"<div class='card decision'><header><h3>{_e(d['gate'])}</h3><span class='when'>{_e(d['line'])}</span></header>"
                f"<details><summary>note</summary><pre>{_e(d['body'].strip())}</pre></details></div>"
            )

    rows = "".join(
        f"<tr><td class='muted mono'>{_e(h['at'])}</td><td><code>{_e(h['event'])}</code></td><td>{render_value(h['detail'])}</td></tr>"
        for h in reversed(s.history)
    )
    parts.append(f"<h2>history</h2><details><summary>{len(s.history)} events, newest first</summary><table><tr><th>at</th><th>event</th><th>detail</th></tr>{rows}</table></details>")
    return site.page(f"{input_id} · {s.workflow}", heading, "".join(parts), "runs", crumbs, None)


def render_workflows(site: Site, registry: dict, states: list) -> str:
    counts = {}
    for s in states:
        counts.setdefault(s.workflow, {}).setdefault(s.status, 0)
        counts[s.workflow][s.status] += 1
    rows = "".join(
        f"<tr><td><a href='{workflow_url(name)}'><code>{_e(name)}</code></a></td>"
        f"<td>{len(wf.steps)}</td><td><code>{_e(wf.first_step)}</code></td>"
        f"<td>{', '.join(sorted({s.kind for s in wf.steps.values()}))}</td>"
        f"<td>{'yes' if wf.returns else ''}</td>"
        f"<td>{' '.join(_badge(st) + ' ' + str(n) for st, n in sorted(counts.get(name, {}).items()))}</td></tr>"
        for name, wf in sorted(registry.items())
    )
    return site.page("workflows", "workflows", f"<table><tr><th>workflow</th><th>steps</th><th>first step</th><th>kinds</th><th>composable</th><th>runs</th></tr>{rows}</table>", "workflows", "", None)


def _step_target(step) -> str:
    if step.route:
        return "<br>".join(f"<code>{_e(k)}</code> → <code>{_e(v)}</code>" for k, v in step.route.items())
    if step.next_step:
        return f"→ <code>{_e(step.next_step)}</code>"
    return "→ <code>done</code>"


def render_workflow(site: Site, wf: Workflow, registry: dict, source: str, states: list) -> str:
    rows = []
    for step in wf.steps.values():
        what = step.run if step.run else step.prompt
        if step.kind == "workflow" and step.run in registry:
            what_html = f"<a href='{workflow_url(step.run)}'><code>{_e(what)}</code></a>"
        else:
            what_html = f"<code>{_e(what)}</code>"
        model = f"<br><span class='muted'>{_e(step.model)}</span>" if step.model else ""
        tools = f"<br><span class='muted'>tools: {_e(', '.join(step.allowed_tools))}</span>" if step.allowed_tools else ""
        gate = f"<br><span class='muted'>gate: {_e(step.gate)}</span>" if step.gate else ""
        outputs = "<br>".join(
            f"<code>{_e(k)}</code> <span class='muted'>{_e(':'.join(v['enum']) if isinstance(v, dict) and 'enum' in v else v)}</span>"
            for k, v in step.outputs.items()
        )
        rows.append(
            "<tr>"
            f"<td><code>{_e(step.id)}</code>{gate}</td>"
            f"<td>{_e(step.kind)}</td>"
            f"<td>{what_html}{model}{tools}</td>"
            f"<td>{'<br>'.join(f'<code>{_e(i)}</code>' for i in step.inputs)}</td>"
            f"<td>{outputs}</td>"
            f"<td>{_step_target(step)}</td>"
            "</tr>"
        )
    table = (
        "<table><tr><th>step</th><th>kind</th><th>run / prompt</th><th>inputs</th><th>outputs</th><th>then</th></tr>"
        + "".join(rows)
        + "</table>"
    )
    returns = render_value(wf.returns) if wf.returns else "<p class='muted'>none — top-level workflow</p>"
    mine = [s for s in states if s.workflow == wf.name]
    runs = _run_rows(mine, False) if mine else "<p class='muted'>none yet</p>"
    body = (
        f"<div class='graph-wrap'>{graph.render_svg(wf, {}, _child_links(wf.steps, registry))}</div>"
        f"<h2>runs</h2>{runs}"
        f"<h2>steps</h2>{table}<h2>returns</h2>{returns}"
        f"<h2>source</h2><details><summary>yaml</summary><pre>{_e(source)}</pre></details>"
    )
    crumbs = f"<a href='/workflows'>workflows</a> / {_e(wf.name)}"
    return site.page(wf.name, f"<code>{_e(wf.name)}</code>", body, "workflows", crumbs, None)


def render_log(site: Site, entries: list) -> str:
    rows = "".join(
        f"<tr><td class='muted mono'>{_e(e['at'])}</td>"
        f"<td><a href='{run_url(e['input'])}'><code>{_e(e['input'])}</code></a></td>"
        f"<td><code>{_e(e['event'])}</code></td><td>{render_value(e['detail'])}</td></tr>"
        for e in reversed(entries)
    )
    body = f"<table><tr><th>at</th><th>run</th><th>event</th><th>detail</th></tr>{rows}</table><p class='muted'>last {LOG_TAIL} events, newest first</p>"
    return site.page("log", "log", body, "log", "", INDEX_REFRESH_SECONDS)


# ---- writes -------------------------------------------------------------------


class WriteError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def decide(config: RunnerConfig, input_id: str, form: dict, token: str) -> str:
    """Validate the form and answer the decision. Returns the line written."""
    if form.get("token") != token:
        raise WriteError(403, "bad token")
    gate = form.get("gate", "")
    action = form.get("action", "")
    if not gate:
        raise WriteError(400, "gate required")
    if action == "approve":
        line = "approved"
    elif action == "reject":
        reason = form.get("reason", "").strip()
        if not reason:
            raise WriteError(400, "reject requires a reason")
        line = f"rejected: {reason}"
    else:
        raise WriteError(400, f"unknown action `{action}`")
    path = decisions.decision_path(config.decisions_dir, input_id, gate)
    try:
        decisions.respond(path, line)
    except FileNotFoundError as exc:
        raise WriteError(404, str(exc))
    except ValueError as exc:
        raise WriteError(409, str(exc))
    return line


def retry(config: RunnerConfig, input_id: str, form: dict, token: str, now: str) -> None:
    if form.get("token") != token:
        raise WriteError(403, "bad token")
    try:
        control.retry(config, input_id, now)
    except FileNotFoundError as exc:
        raise WriteError(404, str(exc))
    except control.ControlError as exc:
        raise WriteError(409, str(exc))


# ---- http -------------------------------------------------------------------


def make_handler(config: RunnerConfig, registry: dict, sources: dict, token: str, origin: str, clock):
    site = Site(project=config.workflow_dir.parent.name)
    project_theme = control.project_theme_path(config.workflow_dir)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quiet
            pass

        def _send(self, status: int, body: str, content_type: str) -> None:
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _html(self, status: int, html: str) -> None:
            self._send(status, html, "text/html; charset=utf-8")

        def _redirect(self, location: str) -> None:
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _error(self, status: int, message: str) -> None:
            self._html(status, site.page(str(status), str(status), f"<p>{_e(message)}</p>", "", "", None))

        def _parts(self) -> list:
            return [unquote(p) for p in urlsplit(self.path).path.strip("/").split("/") if p]

        def do_GET(self):
            parts = self._parts()
            if not parts:
                return self._redirect("/workflows")
            if parts == ["runs"]:
                return self._html(200, render_runs(site, list_states(config.inputs_dir)))
            if parts == ["theme.css"]:
                return self._send(200, theme_css(project_theme), "text/css; charset=utf-8")
            if parts == ["log"]:
                return self._html(200, render_log(site, tail_log(config.log_path, LOG_TAIL)))
            if parts == ["workflows"]:
                return self._html(200, render_workflows(site, registry, list_states(config.inputs_dir)))
            if len(parts) == 2 and parts[0] == "workflows":
                if parts[1] not in registry:
                    return self._error(404, f"unknown workflow `{parts[1]}`")
                return self._html(200, render_workflow(site, registry[parts[1]], registry, sources[parts[1]], list_states(config.inputs_dir)))
            if len(parts) == 2 and parts[0] == "runs":
                input_id = parts[1]
                state_path = state.state_path(config.inputs_dir, input_id)
                if not state_path.exists():
                    return self._error(404, f"no run `{input_id}`")
                s = state.load_state(state_path)
                children = [c for c in list_states(config.inputs_dir) if c.input["id"].startswith(input_id + ".")]
                return self._html(
                    200,
                    render_run(
                        site, s, registry[s.workflow], registry,
                        list_runs(config.runs_dir, input_id),
                        list_decisions(config.decisions_dir, input_id),
                        children, token,
                    ),
                )
            return self._error(404, "not found")

        def do_POST(self):
            parts = self._parts()
            if len(parts) != 3 or parts[0] != "runs" or parts[2] not in ("decide", "retry"):
                return self._error(404, "not found")
            sent_origin = self.headers.get("Origin")
            if sent_origin is not None and sent_origin != origin:
                return self._error(403, "cross-origin POST refused")
            length = int(self.headers.get("Content-Length", "0"))
            form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
            try:
                if parts[2] == "decide":
                    decide(config, parts[1], form, token)
                else:
                    retry(config, parts[1], form, token, clock())
            except WriteError as exc:
                return self._error(exc.status, str(exc))
            return self._redirect(run_url(parts[1]))

    return Handler


def make_server(config: RunnerConfig, registry: dict, sources: dict, host: str, port: int, clock) -> ThreadingHTTPServer:
    token = secrets.token_urlsafe(24)
    server = ThreadingHTTPServer((host, port), BaseHTTPRequestHandler)  # placeholder class
    origin = f"http://{host}:{server.server_address[1]}"
    server.RequestHandlerClass = make_handler(config, registry, sources, token, origin, clock)
    return server
