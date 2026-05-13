"""`lmbox agent verify` — post-process check on an agent's output text.

Use case
────────
Partner / cabinet integrators run this on every generated écriture
or research result before it goes back to the lawyer :

    $ lmbox agent generate ... > /tmp/out.md
    $ lmbox agent verify /tmp/out.md --pieces 1,2,3,4,7,12

The verifier flags CRITICAL violations (arrêts that don't exist,
malformed citations, pieces references absent from the dossier) and
exits non-zero so the integrator can stop the chain BEFORE sending
the output to a human reviewer.

Exit codes
──────────
0  Output passes — no HIGH or CRITICAL violations.
1  Output FAILS — at least one HIGH or CRITICAL violation. Stop
   the pipeline.
2  Input invalid (file not found, etc.) — operator error.

Légifrance API
──────────────
External jurisprudence is checked against the public Légifrance
API (https://api.piste.gouv.fr). Without credentials, the verifier
falls back to "unverifiable" — format + malformed checks still
catch the most blatant hallucinations.

To enable real lookup, set the env vars before running :

    export LEGIFRANCE_CLIENT_ID=...
    export LEGIFRANCE_CLIENT_SECRET=...

(Free tier on data.gouv.fr, ~100 req/min.)
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from lmbox_cli._verifier import Severity, verify

console = Console()


def cmd(
    path: Path = typer.Argument(
        ...,
        exists=True,
        readable=True,
        help="Path to the text file to verify (the agent's generated output).",
    ),
    pieces: str | None = typer.Option(
        None,
        "--pieces",
        help="Comma-separated list of piece numbers actually present in the "
        "dossier (e.g. '1,2,3,7'). Required to verify internal `Pièce n° X` "
        "references.",
    ),
    no_external: bool = typer.Option(
        False,
        "--no-external",
        help="Skip the Légifrance API calls (offline mode). Format checks still run.",
    ),
    show_ok: bool = typer.Option(
        False,
        "--show-ok",
        help="Also list citations that passed (default: only show violations).",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit the report as JSON instead of a human table. "
        "Suitable for piping into downstream pipelines / CI.",
    ),
) -> None:
    """Run the citation verifier on an agent-generated text."""

    text = path.read_text(encoding="utf-8")
    pieces_list = (
        [p.strip() for p in pieces.split(",") if p.strip()]
        if pieces is not None
        else None
    )

    report = verify(
        text=text,
        pieces=pieces_list,
        check_external=not no_external,
    )

    if json_output:
        _emit_json(report)
    else:
        _emit_human(report, pieces_list)

    if not report.ok:
        raise typer.Exit(code=1)


# ─── Output formatters ───────────────────────────────────────────


def _emit_human(report, pieces_list) -> None:
    # Summary banner
    citations = report.citations_total
    flagged = sum(
        1
        for v in report.violations
        if v.severity in (Severity.HIGH, Severity.CRITICAL)
    )
    if report.ok:
        banner = Panel(
            f"[bold green]✓ Sortie validée[/bold green]\n"
            f"{citations} citation(s) analysée(s), {report.citations_ok} OK, "
            f"0 hallucination détectée.",
            border_style="green",
        )
    else:
        banner = Panel(
            f"[bold red]✗ Sortie ROUGE — {flagged} hallucination(s) détectée(s)[/bold red]\n"
            f"{citations} citation(s) analysée(s), {report.citations_ok} OK, "
            f"{flagged} bloquante(s).\n"
            f"[red]Ne PAS envoyer cette sortie à l'avocat sans correction.[/red]",
            border_style="red",
        )
    console.print(banner)

    if pieces_list is None:
        console.print(
            "[yellow]ℹ Aucune liste de pièces fournie — les références "
            "internes ne sont pas vérifiées. Passer --pieces '1,2,3,…' "
            "pour activer cette couche.[/yellow]"
        )
    if not report.legifrance_configured:
        console.print(
            "[yellow]ℹ LEGIFRANCE_CLIENT_ID/SECRET non configuré — la "
            "jurisprudence externe est en mode 'format only' (cf. README "
            "verifier).[/yellow]"
        )

    if not report.violations:
        return

    table = Table(
        title="Violations détectées",
        show_lines=True,
        title_style="bold",
        title_justify="left",
    )
    table.add_column("Sévérité", style="bold", width=10)
    table.add_column("Type", width=22)
    table.add_column("Citation extraite", width=40, overflow="fold")
    table.add_column("Détail", overflow="fold")

    severity_styles = {
        Severity.CRITICAL: "red",
        Severity.HIGH: "magenta",
        Severity.MEDIUM: "yellow",
        Severity.LOW: "dim",
    }

    for v in report.violations:
        table.add_row(
            Text(v.severity.value.upper(), style=severity_styles[v.severity]),
            v.kind,
            v.citation.raw,
            v.detail,
        )

    console.print(table)


def _emit_json(report) -> None:
    import json

    out = {
        "ok": report.ok,
        "citations_total": report.citations_total,
        "citations_ok": report.citations_ok,
        "legifrance_configured": report.legifrance_configured,
        "violations": [
            {
                "severity": v.severity.value,
                "kind": v.kind,
                "raw": v.citation.raw,
                "context": v.citation.context,
                "position": v.citation.position,
                "detail": v.detail,
            }
            for v in report.violations
        ],
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
