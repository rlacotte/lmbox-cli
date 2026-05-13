"""End-to-end tests for `lmbox agent verify` CLI."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from lmbox_cli.cli import app

runner = CliRunner()


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


# ─── --list-checks ────────────────────────────────────────────────


def test_list_checks_no_arg_needed():
    """--list-checks should work without a file argument."""
    result = runner.invoke(app, ["agent", "verify", "--list-checks"])
    assert result.exit_code == 0, result.stdout
    out = result.stdout
    assert "Cassation" in out
    assert "EUR-Lex" in out
    assert "Pièce interne" in out


# ─── Happy path ───────────────────────────────────────────────────


def test_clean_text_passes(tmp_path: Path):
    p = _write(tmp_path, "out.md", "La clause est manifestement excessive.")
    result = runner.invoke(app, ["agent", "verify", str(p), "--no-external"])
    assert result.exit_code == 0
    assert "validée" in result.stdout.lower() or "ok" in result.stdout.lower()


def test_piece_violation_exits_1(tmp_path: Path):
    p = _write(tmp_path, "out.md", "Voir Pièce n° 99 du dossier.")
    result = runner.invoke(
        app, ["agent", "verify", str(p), "--pieces", "1,2,3", "--no-external"]
    )
    assert result.exit_code == 1
    assert "ROUGE" in result.stdout or "hallucination" in result.stdout.lower()


def test_malformed_citation_exits_1(tmp_path: Path):
    p = _write(tmp_path, "out.md", "Voir Cass. Com., 12 jav 2024 sur ce point.")
    result = runner.invoke(app, ["agent", "verify", str(p), "--no-external"])
    assert result.exit_code == 1
    assert "malformed" in result.stdout.lower() or "CRITICAL" in result.stdout


# ─── --severity threshold ────────────────────────────────────────


def test_severity_critical_passes_on_high_only(tmp_path: Path):
    """With --severity critical, a HIGH violation (piece not in dossier)
    should NOT fail the run."""
    p = _write(tmp_path, "out.md", "Voir Pièce n° 99 du dossier.")
    result = runner.invoke(
        app,
        [
            "agent", "verify", str(p),
            "--pieces", "1,2,3",
            "--no-external",
            "--severity", "critical",
        ],
    )
    assert result.exit_code == 0  # HIGH < CRITICAL → not a failure


def test_severity_medium_catches_unverifiable(tmp_path: Path):
    """With --severity medium, MEDIUM violations should fail the run."""
    p = _write(tmp_path, "out.md", "Voir Pièce n° 5 du dossier.")
    # No --pieces → MEDIUM "piece_unverifiable"
    result = runner.invoke(
        app,
        [
            "agent", "verify", str(p),
            "--no-external",
            "--severity", "medium",
        ],
    )
    assert result.exit_code == 1


def test_severity_invalid_exits_2(tmp_path: Path):
    p = _write(tmp_path, "out.md", "Pas de citation.")
    result = runner.invoke(
        app,
        ["agent", "verify", str(p), "--no-external", "--severity", "foo"],
    )
    assert result.exit_code == 2


# ─── --export-report ──────────────────────────────────────────────


def test_export_report_writes_json(tmp_path: Path):
    p = _write(tmp_path, "out.md", "Voir Pièce n° 99 du dossier.")
    report_path = tmp_path / "report.json"
    result = runner.invoke(
        app,
        [
            "agent", "verify", str(p),
            "--pieces", "1,2,3",
            "--no-external",
            "--export-report", str(report_path),
        ],
    )
    assert result.exit_code == 1
    assert report_path.exists()
    data = json.loads(report_path.read_text())
    assert data["ok"] is False
    assert data["citations_total"] >= 1
    assert any(v["kind"] == "piece_not_in_dossier" for v in data["violations"])


def test_export_creates_parent_directory(tmp_path: Path):
    p = _write(tmp_path, "out.md", "Pas de citation.")
    nested = tmp_path / "deep" / "nested" / "report.json"
    result = runner.invoke(
        app,
        ["agent", "verify", str(p), "--no-external", "--export-report", str(nested)],
    )
    assert result.exit_code == 0
    assert nested.exists()


# ─── --json (existing flag) ───────────────────────────────────────


def test_json_output_is_parseable(tmp_path: Path):
    p = _write(tmp_path, "out.md", "Voir Pièce n° 5 sur ce point.")
    result = runner.invoke(
        app,
        [
            "agent", "verify", str(p),
            "--pieces", "1,2,3,4,5",
            "--no-external",
            "--json",
        ],
    )
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["ok"] is True
    assert data["citations_total"] == 1


# ─── Error handling ───────────────────────────────────────────────


def test_missing_file_exits_2(tmp_path: Path):
    missing = tmp_path / "does-not-exist.md"
    result = runner.invoke(app, ["agent", "verify", str(missing), "--no-external"])
    assert result.exit_code == 2


def test_missing_argument_exits_2():
    """Calling `verify` with no path and no --list-checks should exit 2."""
    result = runner.invoke(app, ["agent", "verify"])
    assert result.exit_code == 2
