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
        None,
        help="Path to the text file to verify (the agent's generated output). "
        "Optional when --list-checks is used.",
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
    list_checks: bool = typer.Option(
        False,
        "--list-checks",
        help="Print every check the verifier knows about (citation families "
        "+ external lookup endpoints) and exit. No file argument required.",
    ),
    export_report: Path | None = typer.Option(
        None,
        "--export-report",
        help="Write the full JSON report to this file IN ADDITION to "
        "console output. Useful when piping a human table to the terminal "
        "while archiving a machine-readable report for the audit trail.",
    ),
    severity_threshold: str = typer.Option(
        "high",
        "--severity",
        help="Minimum severity that triggers exit code 1. "
        "Choices: critical / high / medium / low. Default: high (HIGH and "
        "CRITICAL fail the run; MEDIUM unverifiable do not).",
    ),
) -> None:
    """Run the citation verifier on an agent-generated text."""

    if list_checks:
        _emit_list_checks()
        raise typer.Exit(code=0)

    if path is None:
        console.print("[red]Argument manquant : chemin du fichier à vérifier.[/red]")
        console.print("Usage : [bold]lmbox agent verify <path>[/bold]  "
                      "(ou --list-checks pour lister les contrôles).")
        raise typer.Exit(code=2)
    if not path.exists():
        console.print(f"[red]Fichier introuvable : {path}[/red]")
        raise typer.Exit(code=2)

    threshold = _parse_severity(severity_threshold)

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

    if export_report is not None:
        export_report.parent.mkdir(parents=True, exist_ok=True)
        export_report.write_text(_report_to_json(report), encoding="utf-8")
        if not json_output:
            console.print(f"[dim]Rapport JSON archivé : {export_report}[/dim]")

    # Exit code is driven by the severity threshold
    if _has_violation_at_or_above(report, threshold):
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
    print(_report_to_json(report))


def _report_to_json(report) -> str:
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
    return json.dumps(out, ensure_ascii=False, indent=2)


def _emit_list_checks() -> None:
    """Print every check the verifier supports — discoverable docs
    for partners writing pipelines around it."""
    table = Table(
        title="Checks réalisés par le verifier",
        show_lines=True,
        title_style="bold",
        title_justify="left",
    )
    table.add_column("Famille", style="bold", width=22)
    table.add_column("Source", width=22)
    table.add_column("Détecte", overflow="fold")

    rows = [
        ("Cassation",          "Légifrance JURI",     "Cass. Com., Soc., Civ., Crim. — pourvoi, date, formation"),
        ("Conseil d'État",     "Légifrance CETAT",    "CE, n° XXXXXX — arrêts admin"),
        ("Conseil const.",     "Légifrance CONSTIT",  "Cons. const. DC / QPC / LP"),
        ("Cour d'appel",       "Légifrance JURI",     "CA Paris, Versailles, Lyon… (couverture partielle)"),
        ("Article de Code",    "Légifrance CODE",     "L./R./D. + ID Code (13 codes mappés, 6 acronymes)"),
        ("Loi / ordonnance",   "Légifrance JORF",     "Loi ou ordonnance n° AAAA-NNNN du jour mois année"),
        ("Décret",             "Légifrance JORF",     "Décret n° AAAA-NNNN du jour mois année"),
        ("Règlement UE",       "EUR-Lex CELEX",       "Règlement (UE) AAAA/NNNN → 3AAAARNNNN"),
        ("Directive UE",       "EUR-Lex CELEX",       "Directive AAAA/NNNN/UE → 3AAAALNNNN"),
        ("Pièce interne",      "Inventaire dossier",  "Pièce n° X, Pièces n°s 4 à 7, Pièces n°s 4, 5 et 12"),
        ("Citation malformée", "Structural",          "Mois invalide (jav), séparateur incorrect, mixte FR/EN"),
    ]
    for family, source, what in rows:
        table.add_row(family, source, what)
    console.print(table)
    console.print(
        "\n[dim]Sévérités émises : "
        "[red]CRITICAL[/red] (référence inexistante / malformée), "
        "[magenta]HIGH[/magenta] (pièce hors dossier), "
        "[yellow]MEDIUM[/yellow] (non vérifiable — clé API absente ou API down), "
        "[dim white]LOW[/dim white] (informationnel).[/dim]"
    )


_SEVERITY_RANK = {
    Severity.LOW: 0,
    Severity.MEDIUM: 1,
    Severity.HIGH: 2,
    Severity.CRITICAL: 3,
}


def _parse_severity(s: str) -> Severity:
    try:
        return Severity(s.lower())
    except ValueError:
        console.print(
            f"[red]Sévérité inconnue : '{s}'. "
            f"Choix : critical / high / medium / low.[/red]"
        )
        raise typer.Exit(code=2)


def _has_violation_at_or_above(report, threshold: Severity) -> bool:
    """True iff at least one violation has severity >= threshold."""
    needed = _SEVERITY_RANK[threshold]
    return any(_SEVERITY_RANK[v.severity] >= needed for v in report.violations)
