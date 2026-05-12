"""Eval harness — load golden cases, run them through the LLM, score.

Lives in its own package so the `lmbox agent test` command stays
thin and so we can grow the harness (semantic_match, structured
output, etc.) without bloating the commands/ module.
"""

from lmbox_cli._evals.loader import GoldenCase, load_golden
from lmbox_cli._evals.runner import EvalResult, run

__all__ = ["EvalResult", "GoldenCase", "load_golden", "run"]
