"""End-to-end tests for `lmbox agent run` — single-shot guarded run.

We monkey-patch `lmbox_cli._llm.from_env` to inject a FakeStreamingClient
so the command runs against a deterministic in-memory backend instead
of trying to reach localhost:11434.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

import lmbox_cli.commands.run as run_cmd
from lmbox_cli._llm import CompletionRequest, CompletionResponse
from lmbox_cli.cli import app

runner = CliRunner()


class FakeStreamingClient:
    """Yields a configurable scripted stream."""

    def __init__(self, chunks: list[str]) -> None:
        self.chunks = chunks
        self.cancelled = False

    def complete(self, req: CompletionRequest) -> CompletionResponse:
        return CompletionResponse(content="".join(self.chunks), model=req.model)

    def stream(self, req: CompletionRequest) -> Iterator[str]:
        try:
            for c in self.chunks:
                yield c
        except GeneratorExit:
            self.cancelled = True
            raise


@pytest.fixture
def fake_stream(monkeypatch: pytest.MonkeyPatch):
    """Replace the LLM factory with a stub returning the desired stream."""
    holder: dict = {"client": None}

    def make(chunks: list[str]) -> FakeStreamingClient:
        client = FakeStreamingClient(chunks)
        holder["client"] = client

        def fake_from_env(*, endpoint=None, api_key=None, timeout=None):
            return client

        monkeypatch.setattr(run_cmd, "from_env", fake_from_env)
        return client

    return make


def _scaffold(tmp_path: Path) -> Path:
    result = runner.invoke(app, ["agent", "new", "my-agent", "-o", str(tmp_path)])
    assert result.exit_code == 0, result.stdout
    return tmp_path / "my-agent"


# ─── Basic flows ──────────────────────────────────────────────────


def test_clean_stream_passes(tmp_path: Path, fake_stream):
    agent = _scaffold(tmp_path)
    fake_stream(["Bonjour. ", "Le contrat est clair. ", "Fin."])
    result = runner.invoke(
        app,
        [
            "agent", "run", str(agent),
            "--input", "Salut",
            "--guard", "warn",
            "--no-external",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "Bonjour" in result.stdout


def test_strict_cancels_on_bad_piece(tmp_path: Path, fake_stream):
    agent = _scaffold(tmp_path)
    client = fake_stream([
        "Le contrat ",
        "(Pièce n° 99) ",
        "prévoit que ceci. ",
        "Fin.",
    ])
    result = runner.invoke(
        app,
        [
            "agent", "run", str(agent),
            "--input", "Faisons ceci",
            "--pieces", "1,2,3",
            "--guard", "strict",
            "--no-external",
        ],
    )
    assert result.exit_code == 1
    assert "GUARD CANCELLED" in result.stdout
    # The generator was closed by the guard
    assert client.cancelled is True


def test_annotate_inserts_marker(tmp_path: Path, fake_stream):
    agent = _scaffold(tmp_path)
    fake_stream([
        "Le contrat ",
        "(Pièce n° 99) ",
        "prévoit que ceci. ",
        "Fin.",
    ])
    result = runner.invoke(
        app,
        [
            "agent", "run", str(agent),
            "--input", "Faisons ceci",
            "--pieces", "1,2,3",
            "--guard", "annotate",
            "--no-external",
        ],
    )
    # Brief completes (annotate keeps generating)
    assert "Fin." in result.stdout
    assert "HALLUCINATION" in result.stdout
    # Exit 1 because a HIGH violation was found (default block severity)
    assert result.exit_code == 1


def test_warn_passes_through(tmp_path: Path, fake_stream):
    agent = _scaffold(tmp_path)
    fake_stream([
        "Le contrat (Pièce n° 99) prévoit. ",
        "Fin.",
    ])
    result = runner.invoke(
        app,
        [
            "agent", "run", str(agent),
            "--input", "Salut",
            "--pieces", "1,2,3",
            "--guard", "warn",
            "--no-external",
        ],
    )
    # Stream not modified — Pièce n° 99 should appear verbatim
    assert "Pièce n° 99" in result.stdout
    # But exit code reflects the violation
    assert result.exit_code == 1


# ─── Audit trail ──────────────────────────────────────────────────


def test_export_audit_writes_json(tmp_path: Path, fake_stream):
    agent = _scaffold(tmp_path)
    fake_stream([
        "Le contrat (Pièce n° 99) prévoit. ",
        "Fin.",
    ])
    audit_path = tmp_path / "audit" / "run.json"
    result = runner.invoke(
        app,
        [
            "agent", "run", str(agent),
            "--input", "Salut",
            "--pieces", "1,2,3",
            "--guard", "warn",
            "--no-external",
            "--export-audit", str(audit_path),
        ],
    )
    assert audit_path.exists()
    data = json.loads(audit_path.read_text())
    assert data["agent"] == "my-agent"
    assert data["guard_mode"] == "warn"
    assert any(
        e["type"] == "violation" for e in data["events"]
    )
    assert data["result"]["report"]["ok"] is False


# ─── Operator errors ──────────────────────────────────────────────


def test_unknown_guard_mode_exits_2(tmp_path: Path, fake_stream):
    agent = _scaffold(tmp_path)
    fake_stream(["irrelevant"])
    result = runner.invoke(
        app,
        ["agent", "run", str(agent), "--input", "x", "--guard", "bogus"],
    )
    assert result.exit_code == 2


def test_no_input_exits_2(tmp_path: Path, fake_stream):
    agent = _scaffold(tmp_path)
    fake_stream(["irrelevant"])
    result = runner.invoke(app, ["agent", "run", str(agent)])
    assert result.exit_code == 2
    assert "No user input" in result.stdout


def test_input_file(tmp_path: Path, fake_stream):
    agent = _scaffold(tmp_path)
    fake_stream(["Réponse propre.", " Fin."])
    brief = tmp_path / "brief.txt"
    brief.write_text("Voici mon brief")
    result = runner.invoke(
        app,
        [
            "agent", "run", str(agent),
            "--input-file", str(brief),
            "--guard", "warn",
            "--no-external",
        ],
    )
    assert result.exit_code == 0
    assert "Réponse propre" in result.stdout
