"""End-to-end tests for `lmbox agent check-grounding`."""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from typer.testing import CliRunner

from lmbox_cli.cli import app

runner = CliRunner()


def _scaffold(tmp_path: Path) -> Path:
    result = runner.invoke(app, ["agent", "new", "g-agent", "-o", str(tmp_path)])
    assert result.exit_code == 0
    return tmp_path / "g-agent"


def _add_grounding(agent: Path, mode: str, paths: list[str]) -> None:
    manifest_path = agent / "manifest.yaml"
    data = yaml.safe_load(manifest_path.read_text())
    data["spec"]["grounding"] = {"mode": mode, "source_id_paths": paths}
    manifest_path.write_text(yaml.safe_dump(data))


def _write(tmp_path: Path, name: str, content) -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(content), encoding="utf-8")
    return p


# ─── Happy + sad paths ────────────────────────────────────────────


def test_strict_passes_when_all_sources_retrieved(tmp_path: Path):
    agent = _scaffold(tmp_path)
    _add_grounding(agent, "strict", ["precedents.*.source_id"])
    output = _write(tmp_path, "out.json", {"precedents": [{"source_id": "doc-A"}]})
    trace = _write(
        tmp_path, "trace.json",
        [{"name": "search", "args": {}, "returned_source_ids": ["doc-A"]}],
    )
    result = runner.invoke(
        app,
        ["agent", "check-grounding", str(agent), "--output", str(output), "--trace", str(trace)],
    )
    assert result.exit_code == 0, result.stdout
    assert "Toutes les sources" in result.stdout


def test_strict_blocks_on_invented_source(tmp_path: Path):
    agent = _scaffold(tmp_path)
    _add_grounding(agent, "strict", ["precedents.*.source_id"])
    output = _write(
        tmp_path, "out.json",
        {"precedents": [{"source_id": "doc-A"}, {"source_id": "doc-INVENTED"}]},
    )
    trace = _write(
        tmp_path, "trace.json",
        [{"name": "search", "args": {}, "returned_source_ids": ["doc-A"]}],
    )
    result = runner.invoke(
        app,
        ["agent", "check-grounding", str(agent), "--output", str(output), "--trace", str(trace)],
    )
    assert result.exit_code == 1
    assert "doc-INVENTED" in result.stdout
    assert "inventée" in result.stdout.lower() or "bloquée" in result.stdout.lower()


def test_warn_mode_does_not_block(tmp_path: Path):
    agent = _scaffold(tmp_path)
    _add_grounding(agent, "warn", ["precedents.*.source_id"])
    output = _write(
        tmp_path, "out.json",
        {"precedents": [{"source_id": "doc-INVENTED"}]},
    )
    trace = _write(tmp_path, "trace.json", [])
    result = runner.invoke(
        app,
        ["agent", "check-grounding", str(agent), "--output", str(output), "--trace", str(trace)],
    )
    assert result.exit_code == 0
    assert "warn" in result.stdout.lower() or "rapport seulement" in result.stdout.lower()


def test_off_mode_skips(tmp_path: Path):
    agent = _scaffold(tmp_path)
    _add_grounding(agent, "off", ["precedents.*.source_id"])
    output = _write(
        tmp_path, "out.json",
        {"precedents": [{"source_id": "doc-INVENTED"}]},
    )
    trace = _write(tmp_path, "trace.json", [])
    result = runner.invoke(
        app,
        ["agent", "check-grounding", str(agent), "--output", str(output), "--trace", str(trace)],
    )
    assert result.exit_code == 0


def test_override_mode_to_strict(tmp_path: Path):
    """Manifest says warn but --mode strict should force strict behavior."""
    agent = _scaffold(tmp_path)
    _add_grounding(agent, "warn", ["precedents.*.source_id"])
    output = _write(
        tmp_path, "out.json",
        {"precedents": [{"source_id": "doc-INVENTED"}]},
    )
    trace = _write(tmp_path, "trace.json", [])
    result = runner.invoke(
        app,
        [
            "agent", "check-grounding", str(agent),
            "--output", str(output),
            "--trace", str(trace),
            "--mode", "strict",
        ],
    )
    assert result.exit_code == 1


# ─── --json ───────────────────────────────────────────────────────


def test_json_output(tmp_path: Path):
    agent = _scaffold(tmp_path)
    _add_grounding(agent, "strict", ["precedents.*.source_id"])
    output = _write(
        tmp_path, "out.json",
        {"precedents": [{"source_id": "doc-A"}, {"source_id": "doc-BAD"}]},
    )
    trace = _write(
        tmp_path, "trace.json",
        [{"name": "search", "args": {}, "returned_source_ids": ["doc-A"]}],
    )
    result = runner.invoke(
        app,
        [
            "agent", "check-grounding", str(agent),
            "--output", str(output), "--trace", str(trace),
            "--json",
        ],
    )
    assert result.exit_code == 1
    data = json.loads(result.stdout)
    assert data["should_block"] is True
    assert any(v["claimed_source_id"] == "doc-BAD" for v in data["violations"])


# ─── Operator errors ──────────────────────────────────────────────


def test_no_grounding_block_exits_0_with_message(tmp_path: Path):
    agent = _scaffold(tmp_path)
    output = _write(tmp_path, "out.json", {})
    trace = _write(tmp_path, "trace.json", [])
    result = runner.invoke(
        app,
        ["agent", "check-grounding", str(agent), "--output", str(output), "--trace", str(trace)],
    )
    assert result.exit_code == 0
    assert "grounding" in result.stdout.lower()


def test_missing_output_file_exits_2(tmp_path: Path):
    agent = _scaffold(tmp_path)
    _add_grounding(agent, "strict", ["x.source_id"])
    trace = _write(tmp_path, "trace.json", [])
    result = runner.invoke(
        app,
        [
            "agent", "check-grounding", str(agent),
            "--output", str(tmp_path / "missing.json"),
            "--trace", str(trace),
        ],
    )
    assert result.exit_code == 2


def test_unknown_override_mode_exits_2(tmp_path: Path):
    agent = _scaffold(tmp_path)
    _add_grounding(agent, "strict", ["x.source_id"])
    output = _write(tmp_path, "out.json", {})
    trace = _write(tmp_path, "trace.json", [])
    result = runner.invoke(
        app,
        [
            "agent", "check-grounding", str(agent),
            "--output", str(output),
            "--trace", str(trace),
            "--mode", "bogus",
        ],
    )
    assert result.exit_code == 2
