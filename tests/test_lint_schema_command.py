"""End-to-end tests for `lmbox agent lint-schema` CLI."""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from typer.testing import CliRunner

from lmbox_cli.cli import app

runner = CliRunner()


def _scaffold(tmp_path: Path) -> Path:
    result = runner.invoke(app, ["agent", "new", "my-agent", "-o", str(tmp_path)])
    assert result.exit_code == 0
    return tmp_path / "my-agent"


def _set_schema(agent: Path, schema: dict) -> None:
    """Inject a JSON Schema output contract into the agent manifest."""
    manifest_path = agent / "manifest.yaml"
    data = yaml.safe_load(manifest_path.read_text())
    data["spec"]["output_format"] = {"kind": "json_schema", "schema": schema}
    manifest_path.write_text(yaml.safe_dump(data))


# ─── Manifest-based ───────────────────────────────────────────────


def test_lint_clean_schema_exits_0(tmp_path: Path):
    agent = _scaffold(tmp_path)
    _set_schema(
        agent,
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["title"],
            "properties": {
                "title": {
                    "type": "string",
                    "maxLength": 80,
                    "description": "Title of the document.",
                }
            },
        },
    )
    result = runner.invoke(app, ["agent", "lint-schema", str(agent)])
    assert result.exit_code == 0
    assert "propre" in result.stdout.lower() or "aucune" in result.stdout.lower()


def test_lint_error_exits_1(tmp_path: Path):
    agent = _scaffold(tmp_path)
    _set_schema(
        agent,
        {
            "type": "object",
            "required": ["ghost"],  # ghost not in properties → ERROR
            "properties": {"a": {"type": "string", "maxLength": 80}},
        },
    )
    result = runner.invoke(app, ["agent", "lint-schema", str(agent)])
    assert result.exit_code == 1
    assert "required_missing_in_properties" in result.stdout


def test_lint_strict_fails_on_warning(tmp_path: Path):
    agent = _scaffold(tmp_path)
    _set_schema(
        agent,
        {
            "type": "object",
            "properties": {"a": {"type": "string", "maxLength": 80}},
            # missing additionalProperties (WARNING) + missing description (WARNING)
        },
    )
    result = runner.invoke(app, ["agent", "lint-schema", str(agent), "--strict"])
    assert result.exit_code == 1


def test_lint_strict_passes_when_only_info(tmp_path: Path):
    agent = _scaffold(tmp_path)
    _set_schema(
        agent,
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "name": {"type": "string", "description": "x"},  # no maxLength → INFO
            },
        },
    )
    result = runner.invoke(app, ["agent", "lint-schema", str(agent), "--strict"])
    assert result.exit_code == 0


def test_no_output_format_exits_0_with_message(tmp_path: Path):
    agent = _scaffold(tmp_path)
    # Default scaffold has no output_format declared
    result = runner.invoke(app, ["agent", "lint-schema", str(agent)])
    assert result.exit_code == 0
    assert "pas de contrat" in result.stdout.lower()


# ─── --schema-file ────────────────────────────────────────────────


def test_schema_file_argument(tmp_path: Path):
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(
        json.dumps(
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "x": {"type": "string", "maxLength": 80, "description": "x"}
                },
            }
        )
    )
    result = runner.invoke(
        app, ["agent", "lint-schema", "--schema-file", str(schema_path)]
    )
    assert result.exit_code == 0


def test_schema_file_missing_exits_2(tmp_path: Path):
    result = runner.invoke(
        app,
        ["agent", "lint-schema", "--schema-file", str(tmp_path / "absent.json")],
    )
    assert result.exit_code == 2


# ─── --json ────────────────────────────────────────────────────────


def test_json_output(tmp_path: Path):
    agent = _scaffold(tmp_path)
    _set_schema(
        agent,
        {
            "type": "object",
            "properties": {"a": {"type": "string", "maxLength": 80}},
            # missing additionalProperties + description
        },
    )
    result = runner.invoke(
        app, ["agent", "lint-schema", str(agent), "--json"]
    )
    # WARNINGs present but exit code 0 (without --strict)
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert any(i["rule"] == "missing_additional_properties" for i in data)
