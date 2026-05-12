"""End-to-end tests for `lmbox agent test` — the CLI command.

We monkey-patch `lmbox_cli._llm.from_env` to inject the FakeLLMClient,
so the command runs against a deterministic in-memory backend
instead of trying to reach localhost:11434 during CI.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import lmbox_cli.commands.test as test_cmd
from lmbox_cli._llm import CompletionRequest, CompletionResponse
from lmbox_cli.cli import app

runner = CliRunner()


# ─── Fake LLM client identical to the one in test_evals ──────


class FakeLLMClient:
    def __init__(self, default: str = "yes the answer is hello") -> None:
        self._default = default
        self.calls: list[CompletionRequest] = []

    def complete(self, req: CompletionRequest) -> CompletionResponse:
        self.calls.append(req)
        return CompletionResponse(content=self._default, model=req.model)


@pytest.fixture
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> FakeLLMClient:
    """Replace the real client factory in `commands.test`."""
    client = FakeLLMClient()

    def fake_from_env(*, endpoint=None, api_key=None):
        return client

    monkeypatch.setattr(test_cmd, "from_env", fake_from_env)
    return client


# ─── Helpers ──────────────────────────────────────────────────


def _scaffold(tmp_path: Path) -> Path:
    """Scaffold a base agent and return its directory."""
    result = runner.invoke(app, ["agent", "new", "my-agent", "-o", str(tmp_path)])
    assert result.exit_code == 0
    return tmp_path / "my-agent"


def _set_golden(agent: Path, lines: list[str]) -> None:
    """Replace the agent's golden.jsonl with the given lines."""
    (agent / "evals" / "golden.jsonl").write_text("\n".join(lines) + "\n")


# ─── Cases ────────────────────────────────────────────────────


def test_dry_run_skips_llm(tmp_path: Path, fake_llm: FakeLLMClient) -> None:
    agent = _scaffold(tmp_path)
    result = runner.invoke(app, ["agent", "test", str(agent), "--dry-run"])
    assert result.exit_code == 0, result.stdout
    assert "dry-run OK" in result.stdout
    assert len(fake_llm.calls) == 0  # no LLM calls


def test_passing_suite_exits_0(tmp_path: Path, fake_llm: FakeLLMClient) -> None:
    """FakeLLM returns 'yes the answer is hello' → contains 'hello' → pass."""
    agent = _scaffold(tmp_path)
    _set_golden(
        agent,
        [
            '{"id": "c1", "input": "say hello", "expected": {"contains": ["hello"]}}',
        ],
    )
    result = runner.invoke(app, ["agent", "test", str(agent)])
    assert result.exit_code == 0, result.stdout
    assert "Suite passed" in result.stdout


def test_failing_suite_exits_1(tmp_path: Path, fake_llm: FakeLLMClient) -> None:
    """FakeLLM never returns 'goodbye' → assertion fails → suite fails."""
    agent = _scaffold(tmp_path)
    _set_golden(
        agent,
        [
            '{"id": "c1", "input": "x", "expected": {"contains": ["goodbye"]}}',
        ],
    )
    result = runner.invoke(app, ["agent", "test", str(agent)])
    assert result.exit_code == 1
    assert "Suite failed" in result.stdout


def test_invalid_golden_exits_2(tmp_path: Path, fake_llm: FakeLLMClient) -> None:
    agent = _scaffold(tmp_path)
    (agent / "evals" / "golden.jsonl").write_text("not valid json\n")
    result = runner.invoke(app, ["agent", "test", str(agent)])
    assert result.exit_code == 2
    assert "Golden file invalid" in result.stdout


def test_missing_manifest_exits_2(tmp_path: Path, fake_llm: FakeLLMClient) -> None:
    """Run from an empty dir → no manifest → exit 2."""
    result = runner.invoke(app, ["agent", "test", str(tmp_path)])
    assert result.exit_code == 2
    assert "No agent found" in result.stdout


def test_model_override(tmp_path: Path, fake_llm: FakeLLMClient) -> None:
    """--model override is forwarded into the LLM call."""
    agent = _scaffold(tmp_path)
    _set_golden(
        agent,
        [
            '{"id": "c1", "input": "x", "expected": {"contains": ["hello"]}}',
        ],
    )
    result = runner.invoke(
        app,
        [
            "agent",
            "test",
            str(agent),
            "--model",
            "qwen3-32b",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert fake_llm.calls[0].model == "qwen3-32b"


def test_partial_suite_reports_each_case(tmp_path: Path, fake_llm: FakeLLMClient) -> None:
    """Mixed pass+fail → exit 1 unless threshold is met."""
    agent = _scaffold(tmp_path)
    # Threshold default is 0.8 in the base template — set manifest's
    # spec.evals.pass_threshold to 0.5 so 1/2 passing is OK.
    import yaml

    manifest_path = agent / "manifest.yaml"
    data = yaml.safe_load(manifest_path.read_text())
    data["spec"]["evals"]["pass_threshold"] = 0.5
    manifest_path.write_text(yaml.safe_dump(data))

    _set_golden(
        agent,
        [
            '{"id": "ok",   "input": "x", "expected": {"contains": ["hello"]}}',
            '{"id": "fail", "input": "y", "expected": {"contains": ["nope"]}}',
        ],
    )
    result = runner.invoke(app, ["agent", "test", str(agent)])
    assert result.exit_code == 0, result.stdout
    assert "1/2 cases" in result.stdout
    assert "Suite passed" in result.stdout
