"""`lmbox agent lint-schema` — audit an agent's JSON Schema output contract.

Why
───
Small models (Mistral-7B, Gemma-2 9B) are sensitive to schema design.
A field without a `description`, a string without `maxLength`, an
enum with 50 options — these all cause silent compliance failures
that look like model hallucinations but are actually schema mistakes.

This command lints the schema declared in the agent manifest BEFORE
the agent is deployed, surfacing the issues with concrete advice.

Exit codes
──────────
0  No issues OR only INFO-level findings.
1  WARNING and above (with --strict) or ERROR-level findings.
2  Operator error (no manifest, no schema declared, etc.).
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from lmbox_cli._manifest import ManifestError, load
from lmbox_cli._outputs import LintLevel, lint_schema

console = Console()


def cmd(
    path: Path | None = typer.Argument(
        None,
        help="Path to the agent directory (default: current dir).",
    ),
    strict: bool = typer.Option(
        False,
        "--strict",
        help="Exit non-zero on WARNING findings (default: only ERRORs fail).",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit findings as JSON instead of a Rich table.",
    ),
    schema_file: Path | None = typer.Option(
        None,
        "--schema-file",
        help="Lint a standalone JSON Schema file instead of the manifest's "
        "`spec.output_format.schema` block. Useful for partners writing "
        "schemas in isolation.",
    ),
) -> None:
    """Audit the agent's structured-output schema for design foot-guns."""

    schema, status = _resolve_schema(path, schema_file)
    if schema is None:
        # _resolve_schema printed the diagnostic already. `status`
        # distinguishes a fatal operator error (exit 2) from a
        # benign "no schema declared" case (exit 0).
        raise typer.Exit(code=2 if status == "fatal" else 0)

    issues = lint_schema(schema)

    if json_output:
        _emit_json(issues)
    else:
        _emit_human(issues)

    if any(i.level == LintLevel.ERROR for i in issues):
        raise typer.Exit(code=1)
    if strict and any(i.level == LintLevel.WARNING for i in issues):
        raise typer.Exit(code=1)


# ─── Schema resolution ───────────────────────────────────────────


def _resolve_schema(
    path: Path | None, schema_file: Path | None
) -> tuple[dict | None, str]:
    """Return (schema, status).

    status ∈ {"ok", "fatal", "skip"} :
      ok    → schema is non-None and ready to lint
      fatal → operator error (missing file, broken manifest) → exit 2
      skip  → benign no-op (no output_format declared) → exit 0
    """
    if schema_file is not None:
        if not schema_file.exists():
            console.print(f"[red]Schéma introuvable : {schema_file}[/red]")
            return None, "fatal"
        try:
            return json.loads(schema_file.read_text(encoding="utf-8")), "ok"
        except json.JSONDecodeError as e:
            console.print(f"[red]Schéma JSON invalide : {e.msg}[/red]")
            return None, "fatal"

    agent_dir = _resolve_agent_dir(path)
    if agent_dir is None:
        console.print(
            "[red]No agent found.[/red] Run from inside an agent directory, "
            "pass the path explicitly, or use --schema-file <path>."
        )
        return None, "fatal"

    try:
        manifest = load(agent_dir / "manifest.yaml")
    except ManifestError as exc:
        console.print(f"[red]✗ Manifest invalid[/red]\n{exc}")
        return None, "fatal"

    output_format = (manifest.get("spec") or {}).get("output_format") or {}
    if output_format.get("kind") != "json_schema":
        console.print(
            "[yellow]Cet agent n'a pas de contrat de sortie JSON Schema "
            "(spec.output_format.kind != 'json_schema'). Rien à auditer.[/yellow]"
        )
        return None, "skip"
    schema = output_format.get("schema")
    if not isinstance(schema, dict):
        console.print(
            "[red]spec.output_format.schema absent ou non-objet. "
            "Vérifier le manifest.[/red]"
        )
        return None, "fatal"
    return schema, "ok"


def _resolve_agent_dir(path: Path | None) -> Path | None:
    if path is None:
        candidate = Path.cwd()
        return candidate if (candidate / "manifest.yaml").exists() else None
    if path.is_dir():
        return path if (path / "manifest.yaml").exists() else None
    if path.is_file() and path.name == "manifest.yaml":
        return path.parent
    return None


# ─── Output formatters ───────────────────────────────────────────


def _emit_human(issues) -> None:
    counts = {lv: 0 for lv in LintLevel}
    for i in issues:
        counts[i.level] += 1

    if not issues:
        console.print(
            "[green]✓ Schéma propre — aucune anomalie détectée.[/green]"
        )
        return

    table = Table(
        title="Linter — schéma de sortie de l'agent",
        show_lines=True,
        title_style="bold",
        title_justify="left",
    )
    table.add_column("Niveau", style="bold", width=8)
    table.add_column("Règle", width=34)
    table.add_column("Chemin", width=36, overflow="fold")
    table.add_column("Message", overflow="fold")

    level_styles = {
        LintLevel.ERROR: "red",
        LintLevel.WARNING: "yellow",
        LintLevel.INFO: "cyan",
    }
    for i in issues:
        table.add_row(
            f"[{level_styles[i.level]}]{i.level.value.upper()}[/{level_styles[i.level]}]",
            i.rule,
            i.path or "(racine)",
            i.message,
        )
    console.print(table)
    console.print(
        f"\n[red]{counts[LintLevel.ERROR]} erreur(s)[/red] · "
        f"[yellow]{counts[LintLevel.WARNING]} avertissement(s)[/yellow] · "
        f"[cyan]{counts[LintLevel.INFO]} info[/cyan]"
    )


def _emit_json(issues) -> None:
    payload = [
        {
            "level": i.level.value,
            "rule": i.rule,
            "path": i.path,
            "message": i.message,
        }
        for i in issues
    ]
    print(json.dumps(payload, ensure_ascii=False, indent=2))
