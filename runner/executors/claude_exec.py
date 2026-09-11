"""Built-in Claude Code executor.

`kind: claude` is specifically the Claude Code CLI — the engine knows its
invocation and reply envelope, so projects need zero config for it. For any
other agent runtime, use `kind: agent` with an explicit `agent_command`.
"""


def build_argv(model: str, allowed_tools: tuple) -> list:
    return [
        "claude",
        "-p",
        "--output-format",
        "json",
        "--model",
        model,
        "--allowedTools",
        ",".join(allowed_tools),
    ]
