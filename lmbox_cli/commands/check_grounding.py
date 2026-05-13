"""`lmbox agent check-grounding` — Layer D verifier.

Use case
────────
The agent runtime (on the LMbox appliance) executes the agent step
by step. Every tool call records its returned `source_id`s into a
trace file. After the run finishes, the partner's pipeline invokes :

    $ lmbox agent check-grounding ./jurisrecall \\
          --output ./run/output.json \\
          --trace  ./run/trace.json

…to confirm every `source_id` claimed by the agent in the final
output corresponds to a `source_id` that was actually retrieved.

Trace file format
─────────────────
    [
      {
        "name": "search_dossiers_internes",
        "args": {"query": "non-concurrence"},
        "returned_source_ids": ["interne-2019-453", "interne-2021-712"]
      },
      ...
    ]

Exit codes
──────────
0  All claimed source_ids match retrieved ones (OR mode=off / warn).
1  Strict mode + at least one source_id claimed without retrieval.
2  Operator error (file missing, manifest invalid, etc.).
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from lmbox_cli._grounding import (
    GroundingMode,
    GroundingTracer,
    enforce_grounding,
)
from lmbox_cli._manifest import ManifestError, load

console = Console()


def cmd(
    path: Path | None = typer.Argument(
        None,
        help="Path to the agent directory (default: cwd).",
    ),
    output_file: Path = typer.Option(
        ...,
        "--output",
        help="Path to the agent's final structured JSON output.",
    ),
    trace_file: Path = typer.Option(
        ...,
        "--trace",
        help="Path to the JSON trace file listing every tool call and its "
        "returned source_ids.",
    ),
    override_mode: str | None = typer.Option(
        None,
        "--mode",
        help="Override the manifest's spec.grounding.mode. Choices: "
        "strict / warn / off.",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit the report as JSON instead of a Rich table."
    ),
) -> None:
    """Check that every source_id in the output was actually retrieved."""

    agent_dir = _resolve_agent_dir(path)
    if agent_dir is None:
        console.print(
            "[red]No agent found.[/red] Run from inside an agent directory, "
            "or pass the path explicitly."
        )
        raise typer.Exit(code=2)

    try:
        manifest = load(agent_dir / "manifest.yaml")
    except ManifestError as exc:
        console.print(f"[red]✗ Manifest invalid[/red]\n{exc}")
        raise typer.Exit(code=2) from exc

    grounding_cfg = (manifest.get("spec") or {}).get("grounding") or {}
    if not grounding_cfg:
        console.print(
            "[yellow]Pas de bloc `spec.grounding` dans le manifest. "
            "Ajouter `spec.grounding: { mode: strict, source_id_paths: [...] }` "
            "pour activer Layer D.[/yellow]"
        )
        raise typer.Exit(code=0)

    if override_mode:
        try:
            mode = GroundingMode(override_mode.lower())
        except ValueError:
            console.print(
                f"[red]Mode '{override_mode}' inconnu. "
                f"Choix : strict / warn / off.[/red]"
            )
            raise typer.Exit(code=2)
    else:
        mode = GroundingMode(grounding_cfg.get("mode", "strict"))

    source_id_paths = grounding_cfg.get("source_id_paths") or []

    # Load output + trace
    if not output_file.exists():
        console.print(f"[red]Fichier de sortie introuvable : {output_file}[/red]")
        raise typer.Exit(code=2)
    if not trace_file.exists():
        console.print(f"[red]Fichier de trace introuvable : {trace_file}[/red]")
        raise typer.Exit(code=2)
    try:
        output = json.loads(output_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        console.print(f"[red]Sortie JSON invalide : {e.msg}[/red]")
        raise typer.Exit(code=2)
    try:
        trace_data = json.loads(trace_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        console.print(f"[red]Trace JSON invalide : {e.msg}[/red]")
        raise typer.Exit(code=2)

    tracer = GroundingTracer()
    for entry in trace_data:
        if not isinstance(entry, dict):
            continue
        tracer.record_tool_call(
            name=entry.get("name", "?"),
            args=entry.get("args", {}),
            returned_source_ids=entry.get("returned_source_ids", []),
        )

    report = enforce_grounding(
        output, tracer, mode=mode, source_id_paths=source_id_paths
    )

    if json_output:
        _emit_json(report)
    else:
        _emit_human(report, mode)

    if report.should_block():
        raise typer.Exit(code=1)


# ─── Helpers ─────────────────────────────────────────────────────


def _resolve_agent_dir(path: Path | None) -> Path | None:
    if path is None:
        candidate = Path.cwd()
        return candidate if (candidate / "manifest.yaml").exists() else None
    if path.is_dir():
        return path if (path / "manifest.yaml").exists() else None
    if path.is_file() and path.name == "manifest.yaml":
        return path.parent
    return None


def _emit_human(report, mode: GroundingMode) -> None:
    console.print(
        f"[bold]Grounding check[/bold] · mode "
        f"[yellow]{mode.value}[/yellow] · "
        f"retrieved [cyan]{len(report.retrieved_source_ids)}[/cyan] · "
        f"claimed [cyan]{len(report.claimed_source_ids)}[/cyan]"
    )
    if report.skipped_paths:
        for sp in report.skipped_paths:
            console.print(
                f"[yellow]ℹ chemin sans correspondance dans la sortie : {sp}[/yellow]"
            )
    if not report.violations:
        console.print("[green]✓ Toutes les sources citées ont été récupérées.[/green]")
        return
    table = Table(
        title="Sources inventées",
        show_lines=True,
        title_style="bold red",
        title_justify="left",
    )
    table.add_column("Chemin", width=36, overflow="fold")
    table.add_column("source_id revendiqué", width=28, overflow="fold")
    table.add_column("Détail", overflow="fold")
    for v in report.violations:
        table.add_row(v.path, v.claimed_source_id, v.detail)
    console.print(table)
    if mode is GroundingMode.STRICT:
        console.print(
            f"\n[red]✗ {len(report.violations)} source(s) inventée(s) — "
            "exécution bloquée.[/red]"
        )
    else:
        console.print(
            f"\n[yellow]⚠ {len(report.violations)} source(s) inventée(s) — "
            "rapport seulement (mode warn).[/yellow]"
        )


def _emit_json(report) -> None:
    payload = {
        "mode": report.mode.value,
        "ok": report.ok,
        "should_block": report.should_block(),
        "retrieved_source_ids": report.retrieved_source_ids,
        "claimed_source_ids": report.claimed_source_ids,
        "skipped_paths": report.skipped_paths,
        "violations": [
            {
                "path": v.path,
                "claimed_source_id": v.claimed_source_id,
                "detail": v.detail,
                "severity": v.severity,
            }
            for v in report.violations
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
