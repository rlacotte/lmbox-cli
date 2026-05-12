"""Tests for `lmbox agent validate`.

Covers:
- Happy path on a fresh scaffold
- Schema violations (missing required keys, wrong types, bad slug pattern)
- Cross-reference errors (manifest points to a non-existent prompt)
- Path resolution (passing a directory vs a file vs nothing)
"""

from __future__ import annotations

from pathlib import Path

import yaml
from typer.testing import CliRunner

from lmbox_cli.cli import app

runner = CliRunner()


def _scaffold(tmp_path: Path, slug: str = "my-agent", template: str = "_base") -> Path:
    """Helper — produce a known-valid scaffold and return its directory."""
    result = runner.invoke(
        app, ["agent", "new", slug, "-t", template, "-o", str(tmp_path)]
    )
    assert result.exit_code == 0, result.stdout
    return tmp_path / slug


def test_valid_scaffold_passes(tmp_path: Path) -> None:
    agent = _scaffold(tmp_path)
    result = runner.invoke(app, ["agent", "validate", str(agent)])
    assert result.exit_code == 0, result.stdout
    assert "Valid manifest" in result.stdout


def test_missing_manifest_returns_2(tmp_path: Path) -> None:
    result = runner.invoke(app, ["agent", "validate", str(tmp_path)])
    assert result.exit_code == 2
    assert "No manifest.yaml found" in result.stdout


def test_schema_violation_detected(tmp_path: Path) -> None:
    """Manifest with a missing required field must fail validation."""
    agent = _scaffold(tmp_path)
    manifest_path = agent / "manifest.yaml"
    data = yaml.safe_load(manifest_path.read_text())
    del data["metadata"]["slug"]
    manifest_path.write_text(yaml.safe_dump(data))

    result = runner.invoke(app, ["agent", "validate", str(agent)])
    assert result.exit_code == 1
    assert "Schema invalid" in result.stdout
    assert "slug" in result.stdout


def test_bad_slug_pattern_rejected(tmp_path: Path) -> None:
    """Slug with underscores fails the regex pattern."""
    agent = _scaffold(tmp_path)
    manifest_path = agent / "manifest.yaml"
    data = yaml.safe_load(manifest_path.read_text())
    data["metadata"]["slug"] = "Bad_Slug"
    manifest_path.write_text(yaml.safe_dump(data))

    result = runner.invoke(app, ["agent", "validate", str(agent)])
    assert result.exit_code == 1
    assert "slug" in result.stdout


def test_missing_referenced_file_detected(tmp_path: Path) -> None:
    """If manifest points to a file that does not exist on disk, fail."""
    agent = _scaffold(tmp_path)
    (agent / "prompts" / "system.md").unlink()

    result = runner.invoke(app, ["agent", "validate", str(agent)])
    assert result.exit_code == 1
    assert "system.md" in result.stdout
    assert "don't exist" in result.stdout or "does not exist" in result.stdout or "exist" in result.stdout


def test_validate_accepts_explicit_manifest_path(tmp_path: Path) -> None:
    """Passing the manifest file directly works just as well as the dir."""
    agent = _scaffold(tmp_path)
    result = runner.invoke(app, ["agent", "validate", str(agent / "manifest.yaml")])
    assert result.exit_code == 0, result.stdout


def test_unknown_api_version_rejected(tmp_path: Path) -> None:
    """Wrong apiVersion is fatal — we don't silently accept future versions."""
    agent = _scaffold(tmp_path)
    manifest_path = agent / "manifest.yaml"
    data = yaml.safe_load(manifest_path.read_text())
    data["apiVersion"] = "lmbox.eu/v99"
    manifest_path.write_text(yaml.safe_dump(data))

    result = runner.invoke(app, ["agent", "validate", str(agent)])
    assert result.exit_code == 1
    assert "apiVersion" in result.stdout
