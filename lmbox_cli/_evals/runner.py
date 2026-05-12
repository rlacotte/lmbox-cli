"""Orchestrates the eval cycle: each case → LLM call → assertions → aggregate.

The runner is purposely synchronous and sequential. Eval suites
are small (typically 5-50 cases per agent), and a strict order
makes the live progress bar trivial. We can swap to asyncio if
suites grow past 200 cases.

Aggregation rule
────────────────
A case passes if *all* its individual assertions pass — strict AND.
The case score (used against `pass_threshold` from the manifest)
is the fraction of passing cases.

If a case has a `tolerance < 1.0`, we relax: the case passes as
long as `tolerance` of its assertions pass. Useful for fuzzy
golden cases where you accept one missing keyword out of three.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from lmbox_cli._evals.assertions import AssertionOutcome, evaluate
from lmbox_cli._evals.loader import GoldenCase
from lmbox_cli._llm import CompletionRequest, LLMClient


@dataclass(frozen=True)
class CaseResult:
    """The full picture of one case after running."""

    case: GoldenCase
    response: str
    outcomes: list[AssertionOutcome]
    error: str | None = None  # set only if the LLM call itself failed

    @property
    def passed(self) -> bool:
        """Case passes if outcomes meet the case's own tolerance."""
        if self.error:
            return False
        if not self.outcomes:
            return False
        n_pass = sum(1 for o in self.outcomes if o.passed)
        ratio = n_pass / len(self.outcomes)
        return ratio >= self.case.tolerance


@dataclass
class EvalResult:
    """Aggregate over all cases."""

    cases: list[CaseResult] = field(default_factory=list)
    pass_threshold: float = 0.8

    @property
    def total(self) -> int:
        return len(self.cases)

    @property
    def passed(self) -> int:
        return sum(1 for c in self.cases if c.passed)

    @property
    def score(self) -> float:
        return self.passed / self.total if self.total else 0.0

    @property
    def succeeded(self) -> bool:
        """Whether the whole suite meets the manifest's pass_threshold."""
        return self.score >= self.pass_threshold


def run(
    *,
    client: LLMClient,
    model: str,
    system_prompt: str,
    cases: list[GoldenCase],
    pass_threshold: float = 0.8,
    temperature: float = 0.2,
    max_tokens: int = 1024,
    on_case_done: callable[[CaseResult], None] | None = None,
) -> EvalResult:
    """Run every case through the LLM, collect results.

    `on_case_done` is a callback invoked after each case — used by
    the CLI to update a live progress bar. None means silent run.
    """
    result = EvalResult(pass_threshold=pass_threshold)

    for case in cases:
        cr = _run_one(client, model, system_prompt, case, temperature, max_tokens)
        result.cases.append(cr)
        if on_case_done is not None:
            on_case_done(cr)

    return result


def _run_one(
    client: LLMClient,
    model: str,
    system_prompt: str,
    case: GoldenCase,
    temperature: float,
    max_tokens: int,
) -> CaseResult:
    """Single case execution — isolates exception handling per case."""
    req = CompletionRequest(
        model=model,
        system=system_prompt,
        user=case.input,
        temperature=temperature,
        max_tokens=max_tokens,
    )

    try:
        resp = client.complete(req)
    except Exception as exc:
        return CaseResult(
            case=case,
            response="",
            outcomes=[],
            error=f"{type(exc).__name__}: {exc}",
        )

    outcomes = evaluate(case.expected, resp.content)
    return CaseResult(case=case, response=resp.content, outcomes=outcomes)


def load_system_prompt(prompt_path: Path) -> str:
    """Read a system prompt file. Errors are caller's problem (intentional).

    Kept in this module because the CLI command always pairs it with
    a runner invocation — colocating them avoids a separate helper file.
    """
    return prompt_path.read_text(encoding="utf-8")
