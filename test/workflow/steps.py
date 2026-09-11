"""Python steps for the test project."""


def respond(inputs: dict) -> dict:
    return {
        "message": f"{inputs['candidate.word'].upper()}! ({inputs['analyze.reason']})"
    }
