"""Python executor: call a named function in the project's steps module.

Contract: the function takes exactly one argument (the resolved inputs dict)
and returns the step's outputs dict. Anything it raises halts the candidate.
"""
import importlib.util
from pathlib import Path


class PythonStepError(ValueError):
    """The project steps module or function is unusable."""


def load_steps_module(project_dir: Path):
    steps_path = project_dir / "steps.py"
    if not steps_path.exists():
        raise PythonStepError(f"no steps.py in {project_dir}")
    spec = importlib.util.spec_from_file_location("project_steps", steps_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def call(module, run: str, inputs: dict) -> dict:
    prefix, _, func_name = run.rpartition(".")
    if prefix != "steps":
        raise PythonStepError(f"`run` must be `steps.<function>`, got {run!r}")
    fn = getattr(module, func_name, None)
    if fn is None:
        raise PythonStepError(f"steps module has no function {func_name!r}")
    result = fn(inputs)
    if not isinstance(result, dict):
        raise PythonStepError(f"{run} returned {type(result).__name__}, expected dict")
    return result
