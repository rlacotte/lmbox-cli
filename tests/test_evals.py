"""Tests for the eval harness — loader, assertions, runner.

We never call a real LLM in tests. A FakeLLMClient returns canned
responses based on the prompt input. This keeps the suite fast
and offline.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lmbox_cli._evals import load_golden, run
from lmbox_cli._evals.assertions import (
    check_contains,
    check_not_contains,
    check_regex,
)
from lmbox_cli._evals.loader import GoldenLoadError
from lmbox_cli._llm import CompletionRequest, CompletionResponse

# ─── FakeLLMClient ────────────────────────────────────────────


class FakeLLMClient:
    """Returns predetermined responses; records every call for assertions."""

    def __init__(
        self,
        responses: dict[str, str] | None = None,
        default: str = "FAKE_RESPONSE",
        raise_on: str | None = None,
    ) -> None:
        self._responses = responses or {}
        self._default = default
        self._raise_on = raise_on
        self.calls: list[CompletionRequest] = []

    def complete(self, req: CompletionRequest) -> CompletionResponse:
        self.calls.append(req)
        if self._raise_on and self._raise_on in req.user:
            raise RuntimeError("simulated LLM failure")
        content = self._responses.get(req.user, self._default)
        return CompletionResponse(content=content, model=req.model)


# ─── Loader ────────────────────────────────────────────────────


def test_loader_parses_valid_jsonl(tmp_path: Path) -> None:
    f = tmp_path / "golden.jsonl"
    f.write_text(
        '{"id": "c1", "input": "hi", "expected": {"contains": ["hello"]}}\n'
        '{"id": "c2", "input": "bye", "expected": {"contains": ["bye"]}, "tolerance": 0.5}\n'
    )
    cases = load_golden(f)
    assert len(cases) == 2
    assert cases[0].id == "c1"
    assert cases[1].tolerance == 0.5


def test_loader_skips_blank_and_comment_lines(tmp_path: Path) -> None:
    f = tmp_path / "golden.jsonl"
    f.write_text(
        "// header comment\n"
        "\n"
        '{"id": "c1", "input": "hi", "expected": {"contains": ["hello"]}}\n'
        "\n"
        "// trailing comment\n"
    )
    cases = load_golden(f)
    assert len(cases) == 1


def test_loader_rejects_invalid_json(tmp_path: Path) -> None:
    f = tmp_path / "golden.jsonl"
    f.write_text('{"id": "c1", "input": broken}\n')
    with pytest.raises(GoldenLoadError, match="line 1"):
        load_golden(f)


def test_loader_rejects_missing_assertion(tmp_path: Path) -> None:
    f = tmp_path / "golden.jsonl"
    f.write_text('{"id": "c1", "input": "x", "expected": {}}\n')
    with pytest.raises(GoldenLoadError, match="at least one"):
        load_golden(f)


def test_loader_rejects_duplicate_ids(tmp_path: Path) -> None:
    f = tmp_path / "golden.jsonl"
    f.write_text(
        '{"id": "c1", "input": "a", "expected": {"contains": ["x"]}}\n'
        '{"id": "c1", "input": "b", "expected": {"contains": ["y"]}}\n'
    )
    with pytest.raises(GoldenLoadError, match="duplicate"):
        load_golden(f)


def test_loader_rejects_empty_file(tmp_path: Path) -> None:
    f = tmp_path / "golden.jsonl"
    f.write_text("// just comments\n\n")
    with pytest.raises(GoldenLoadError, match="No golden cases"):
        load_golden(f)


def test_loader_rejects_invalid_tolerance(tmp_path: Path) -> None:
    f = tmp_path / "golden.jsonl"
    f.write_text('{"id": "c1", "input": "x", "expected": {"contains": ["x"]}, "tolerance": 1.5}\n')
    with pytest.raises(GoldenLoadError, match="tolerance"):
        load_golden(f)


# ─── Assertions ───────────────────────────────────────────────


def test_contains_case_insensitive() -> None:
    outcomes = check_contains("The Answer is HELLO world", ["hello", "world"])
    assert all(o.passed for o in outcomes)


def test_contains_misses_substring() -> None:
    outcomes = check_contains("only this", ["missing", "this"])
    assert [o.passed for o in outcomes] == [False, True]


def test_not_contains_forbids_substring() -> None:
    outcomes = check_not_contains("clean output", ["bad", "forbidden"])
    assert all(o.passed for o in outcomes)


def test_not_contains_catches_leak() -> None:
    outcomes = check_not_contains("oops the password is 1234", ["password"])
    assert outcomes[0].passed is False


def test_regex_matches() -> None:
    outcomes = check_regex("Order #ORD-12345 confirmed", r"ord-\d+")
    assert outcomes[0].passed


def test_regex_invalid_pattern() -> None:
    outcomes = check_regex("anything", "[invalid(regex")
    assert outcomes[0].passed is False
    assert "invalid" in outcomes[0].detail


# ─── Runner ──────────────────────────────────────────────────


def _case(id_: str, prompt: str, expected: dict, tolerance: float = 1.0):
    from lmbox_cli._evals.loader import GoldenCase

    return GoldenCase(id=id_, input=prompt, expected=expected, tolerance=tolerance)


def test_runner_passes_all() -> None:
    client = FakeLLMClient(default="hello world")
    cases = [
        _case("c1", "q1", {"contains": ["hello"]}),
        _case("c2", "q2", {"contains": ["world"]}),
    ]
    result = run(
        client=client,
        model="m",
        system_prompt="be helpful",
        cases=cases,
        pass_threshold=0.8,
    )
    assert result.passed == 2
    assert result.score == 1.0
    assert result.succeeded


def test_runner_below_threshold() -> None:
    client = FakeLLMClient(default="hello")
    cases = [
        _case("c1", "q1", {"contains": ["hello"]}),
        _case("c2", "q2", {"contains": ["missing"]}),
    ]
    result = run(
        client=client,
        model="m",
        system_prompt="x",
        cases=cases,
        pass_threshold=0.8,
    )
    assert result.passed == 1
    assert result.score == 0.5
    assert not result.succeeded


def test_runner_catches_llm_error() -> None:
    client = FakeLLMClient(raise_on="break")
    cases = [
        _case("c1", "ok", {"contains": ["FAKE"]}),
        _case("c2", "break this", {"contains": ["anything"]}),
    ]
    result = run(
        client=client,
        model="m",
        system_prompt="x",
        cases=cases,
        pass_threshold=0.5,
    )
    assert result.cases[0].passed is True
    assert result.cases[1].passed is False
    assert result.cases[1].error is not None
    assert "simulated" in result.cases[1].error


def test_runner_respects_tolerance() -> None:
    """A case with tolerance=0.5 passes if half its outcomes pass."""
    client = FakeLLMClient(default="hello")
    cases = [
        _case("c1", "q", {"contains": ["hello", "missing"]}, tolerance=0.5),
    ]
    result = run(
        client=client,
        model="m",
        system_prompt="x",
        cases=cases,
        pass_threshold=1.0,
    )
    assert result.cases[0].passed is True


def test_runner_calls_with_correct_messages() -> None:
    """Verify we actually pass the system+user content the manifest declares."""
    client = FakeLLMClient(default="ok")
    cases = [_case("c1", "what is the weather", {"contains": ["ok"]})]
    run(
        client=client,
        model="mistral-large-2",
        system_prompt="You are a weather assistant.",
        cases=cases,
        pass_threshold=0.5,
    )
    assert len(client.calls) == 1
    req = client.calls[0]
    assert req.model == "mistral-large-2"
    assert req.system == "You are a weather assistant."
    assert req.user == "what is the weather"
