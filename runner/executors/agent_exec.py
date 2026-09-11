"""Agent executor: run a headless agent on a rendered prompt, expect JSON back.

The command runner is injected (callable(argv, stdin_text) -> (exit_code,
stdout_text)) so tests never spawn a real agent. The prompt must instruct the
agent to output only a JSON object; the reply is parsed, fence-stripped if
needed, and validated against the step's output spec by the caller.

One retry on unparseable output, then AgentFailure — the engine turns that
into halt_inbox. Never guess-parse.
"""
import json
import subprocess


class AgentFailure(RuntimeError):
    """The agent did not produce usable output."""


def run_command_subprocess(argv: list, stdin_text: str) -> tuple:
    completed = subprocess.run(
        argv, input=stdin_text, capture_output=True, text=True, timeout=1800
    )
    return (completed.returncode, completed.stdout)


def build_argv(model: str, allowed_tools: tuple) -> list:
    argv = ["claude", "-p", "--output-format", "json", "--model", model]
    if allowed_tools:
        argv += ["--allowedTools", ",".join(allowed_tools)]
    return argv


def _extract_json(text: str) -> dict:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        stripped = "\n".join(lines[1 : -1] if lines[-1].strip() == "```" else lines[1:])
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object found")
    return json.loads(stripped[start : end + 1])


def parse_reply(stdout: str) -> dict:
    """claude --output-format json wraps the reply: {"result": "<agent text>", ...}."""
    envelope = json.loads(stdout)
    if not isinstance(envelope, dict) or "result" not in envelope:
        raise ValueError("missing result envelope")
    return _extract_json(envelope["result"])


def execute(run_command, prompt_text: str, model: str, allowed_tools: tuple) -> dict:
    argv = build_argv(model, allowed_tools)
    last_error = ""
    for attempt in (1, 2):
        code, stdout = run_command(argv, prompt_text)
        if code != 0:
            last_error = f"attempt {attempt}: exit {code}"
            continue
        try:
            return parse_reply(stdout)
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = f"attempt {attempt}: unparseable output ({exc})"
    raise AgentFailure(last_error)
