"""Tests for `lmbox agent new`.

We use Typer's CliRunner so the tests exercise the same argument
parsing path real users hit — not an internal API. Each test runs in
its own tmp_path so they're fully isolated.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from lmbox_cli.cli import app

runner = CliRunner()


def test_scaffold_base_template_produces_valid_manifest(tmp_path: Path) -> None:
    """A vanilla `new my-agent -o tmp` produces a scaffold that validates clean."""
    result = runner.invoke(app, ["agent", "new", "my-agent", "-o", str(tmp_path)])
    assert result.exit_code == 0, result.stdout

    agent_dir = tmp_path / "my-agent"
    assert (agent_dir / "manifest.yaml").exists()
    assert (agent_dir / "prompts" / "system.md").exists()
    assert (agent_dir / "evals" / "golden.jsonl").exists()
    assert (agent_dir / "README.md").exists()

    # Validate the freshly-scaffolded project
    result_val = runner.invoke(app, ["agent", "validate", str(agent_dir)])
    assert result_val.exit_code == 0, result_val.stdout


def test_scaffold_legal_template(tmp_path: Path) -> None:
    """Legal template scaffolds and references the legal vertical correctly."""
    result = runner.invoke(
        app,
        ["agent", "new", "contract-review", "-t", "legal-document", "-o", str(tmp_path)],
    )
    assert result.exit_code == 0, result.stdout

    manifest = (tmp_path / "contract-review" / "manifest.yaml").read_text()
    assert "vertical: legal" in manifest
    assert "search_clause_library" in manifest
    assert "{{slug}}" not in manifest  # ensure Jinja was rendered, not copied raw

    result_val = runner.invoke(app, ["agent", "validate", str(tmp_path / "contract-review")])
    assert result_val.exit_code == 0, result_val.stdout


def test_invalid_slug_rejected(tmp_path: Path) -> None:
    """A slug with uppercase or short name must be rejected with exit code 2."""
    result = runner.invoke(app, ["agent", "new", "BadSlug", "-o", str(tmp_path)])
    assert result.exit_code == 2
    assert "Invalid slug" in result.stdout


def test_refuses_to_overwrite_existing_non_empty_dir(tmp_path: Path) -> None:
    """Without --force, scaffold refuses if the target dir has files."""
    target = tmp_path / "existing"
    target.mkdir()
    (target / "important.txt").write_text("don't overwrite me")

    result = runner.invoke(app, ["agent", "new", "existing", "-o", str(tmp_path)])
    assert result.exit_code == 1
    assert "Refusing" in result.stdout
    assert (target / "important.txt").read_text() == "don't overwrite me"


def test_force_overwrites(tmp_path: Path) -> None:
    """--force lets the scaffold proceed even with existing files."""
    target = tmp_path / "existing"
    target.mkdir()
    (target / "old.txt").write_text("legacy")

    result = runner.invoke(app, ["agent", "new", "existing", "-o", str(tmp_path), "--force"])
    assert result.exit_code == 0, result.stdout
    assert (target / "manifest.yaml").exists()
