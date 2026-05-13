"""`lmbox agent run` — single-shot agent execution with runtime guard.

Use case
────────
This is the command that wires Layer A (post-hoc verifier) + Layer B
(runtime guard) together into one user-facing surface. Given an agent
directory + a user input, it :

  1. Loads the agent's manifest + system prompt.
  2. Streams the LLM response chunk-by-chunk.
  3. Pipes the stream through a GuardedStream (citation verifier
     live-checking every chunk).
  4. Reacts to violations according to the chosen `--guard` mode.
  5. Prints the final report + writes an optional audit trail.

Exit codes
──────────
0  Success — no blocking violation.
1  GuardedStream cancelled (strict mode) OR violations at/above the
   configured severity threshold.
2  Operator error (missing manifest, bad agent dir, etc.).

Typical invocations
───────────────────
    # Live demo for a partner — annotate mode preserves the full
    # response with inline [⚠] markers
    $ lmbox agent run ./conclusions-drafter \\
        --input "Rédige des conclusions sur..." \\
        --guard annotate

    # Production gate — strict mode cancels on first hallucination
    $ lmbox agent run ./jurisrecall \\
        --input-file ./brief.txt \\
        --pieces 1,2,3,7 \\
        --guard strict \\
        --export-audit ./audit/jurisrecall-run-$(date +%s).json
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.live import Live
from rich.panel import Panel

from lmbox_cli._evals.runner import load_system_prompt
from lmbox_cli._llm import CompletionRequest, from_env
from lmbox_cli._manifest import ManifestError, load
from lmbox_cli._runtime import (
    GuardEvent,
    GuardEventType,
    GuardedStream,
    GuardedStreamViolation,
    GuardMode,
)
from lmbox_cli._verifier import Severity

console = Console()


def cmd(
    path: Path | None = typer.Argument(
        None,
        help="Path to the agent directory (default: current dir).",
    ),
    input_text: str | None = typer.Option(
        None,
        "--input",
        help="Inline user prompt. Mutually exclusive with --input-file and stdin.",
    ),
    input_file: Path | None = typer.Option(
        None,
        "--input-file",
        help="Path to a file containing the user prompt.",
    ),
    pieces: str | None = typer.Option(
        None,
        "--pieces",
        help="Comma-separated pieces inventory (e.g. '1,2,3,7'). Without it, "
        "internal `Pièce n° X` references emit MEDIUM 'unverifiable'.",
    ),
    guard: str = typer.Option(
        "warn",
        "--guard",
        help="Runtime guard mode: strict | annotate | warn. "
        "strict   = cancel generation on first HIGH/CRITICAL.\n"
        "annotate = insert [⚠] markers, keep generating.\n"
        "warn     = pass-through, report at the end (default).",
    ),
    block_severity: str = typer.Option(
        "high",
        "--block-severity",
        help="Minimum severity that triggers the guard action. "
        "Choices: critical / high / medium / low. Default: high.",
    ),
    endpoint: str | None = typer.Option(
        None,
        "--endpoint",
        envvar="LMBOX_LLM_ENDPOINT",
        help="LLM endpoint (OpenAI-compatible). Default: http://localhost:11434.",
    ),
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        envvar="LMBOX_LLM_API_KEY",
        help="Bearer token. Most local backends accept none.",
    ),
    model: str | None = typer.Option(
        None,
        "--model",
        help="Override the model declared in the manifest.",
    ),
    no_external: bool = typer.Option(
        False,
        "--no-external",
        help="Skip Légifrance + EUR-Lex calls (offline / faster demo).",
    ),
    timeout: float = typer.Option(
        180.0,
        "--timeout",
        envvar="LMBOX_LLM_TIMEOUT",
        help="Per-request timeout in seconds.",
    ),
    export_audit: Path | None = typer.Option(
        None,
        "--export-audit",
        help="Write a structured JSON audit trail (events + final report) "
        "to this path. Used by partner pipelines for compliance.",
    ),
) -> None:
    """Run an agent once with the runtime guard live-checking citations."""

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

    user_prompt = _resolve_user_prompt(input_text, input_file)
    if user_prompt is None:
        console.print(
            "[red]No user input.[/red] Pass --input '...' or --input-file ./brief.txt "
            "or pipe text via stdin."
        )
        raise typer.Exit(code=2)

    try:
        guard_mode = GuardMode(guard.lower())
    except ValueError:
        console.print(
            f"[red]Unknown guard mode '{guard}'. "
            f"Choose: strict / annotate / warn.[/red]"
        )
        raise typer.Exit(code=2)

    try:
        threshold = Severity(block_severity.lower())
    except ValueError:
        console.print(
            f"[red]Unknown severity '{block_severity}'. "
            f"Choose: critical / high / medium / low.[/red]"
        )
        raise typer.Exit(code=2)

    pieces_list = (
        [p.strip() for p in pieces.split(",") if p.strip()]
        if pieces
        else None
    )

    sp = manifest["spec"]
    used_model = model or sp["model"]["primary"]
    system_prompt = load_system_prompt(agent_dir / sp["prompts"]["system"])

    req = CompletionRequest(
        model=used_model,
        system=system_prompt,
        user=user_prompt,
        temperature=float(sp["model"].get("temperature", 0.2)),
        max_tokens=int(sp["model"].get("max_tokens", 1024)),
    )
    client = from_env(endpoint=endpoint, api_key=api_key, timeout=timeout)

    console.print(
        Panel(
            f"[bold]{manifest['metadata']['slug']}[/bold] · model "
            f"[magenta]{used_model}[/magenta] · guard [yellow]{guard_mode.value}[/yellow] "
            f"· block-severity [yellow]{threshold.value}[/yellow]",
            border_style="cyan",
        )
    )

    # Audit-trail accumulator
    events_log: list[dict] = []

    def on_event(ev: GuardEvent) -> None:
        # We don't log CHUNK to keep the trail readable — it's the
        # full text anyway, available on result.text at the end.
        if ev.type is GuardEventType.CHUNK:
            return
        entry: dict = {"t": time.time(), "type": ev.type.value}
        if ev.text:
            entry["text"] = ev.text
        if ev.violation:
            entry["violation"] = {
                "severity": ev.violation.severity.value,
                "kind": ev.violation.kind,
                "raw": ev.violation.citation.raw,
                "detail": ev.violation.detail,
            }
        events_log.append(entry)

    gs = GuardedStream(
        producer=client.stream(req),
        pieces=pieces_list,
        check_external=not no_external,
        mode=guard_mode,
        block_severity=threshold,
        on_event=on_event,
    )

    # Print chunks as they arrive
    try:
        for chunk in gs:
            sys.stdout.write(chunk)
            sys.stdout.flush()
        sys.stdout.write("\n")
        success = True
    except GuardedStreamViolation as exc:
        sys.stdout.write("\n")
        console.print(
            f"\n[red bold]✗ GUARD CANCELLED[/red bold] — "
            f"{len(exc.violations)} blocking violation(s) detected."
        )
        success = False

    _print_final_report(gs)

    if export_audit is not None:
        export_audit.parent.mkdir(parents=True, exist_ok=True)
        export_audit.write_text(
            json.dumps(
                {
                    "agent": manifest["metadata"]["slug"],
                    "model": used_model,
                    "guard_mode": guard_mode.value,
                    "block_severity": threshold.value,
                    "user_prompt": user_prompt,
                    "events": events_log,
                    "result": {
                        "text": gs.result.text,
                        "cancelled": gs.result.cancelled,
                        "annotations_inserted": gs.result.annotations_inserted,
                        "report": _report_to_dict(gs.result.report),
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        console.print(f"[dim]Audit trail archived: {export_audit}[/dim]")

    if not success or _has_blocking(gs, threshold):
        raise typer.Exit(code=1)


# ─── Helpers ───────────────────────────────────────────────────────


def _resolve_agent_dir(path: Path | None) -> Path | None:
    if path is None:
        candidate = Path.cwd()
        return candidate if (candidate / "manifest.yaml").exists() else None
    if path.is_dir():
        return path if (path / "manifest.yaml").exists() else None
    if path.is_file() and path.name == "manifest.yaml":
        return path.parent
    return None


def _resolve_user_prompt(
    input_text: str | None, input_file: Path | None
) -> str | None:
    if input_text:
        return input_text
    if input_file:
        return input_file.read_text(encoding="utf-8")
    # Stdin fallback (when piped, not interactive)
    if not sys.stdin.isatty():
        data = sys.stdin.read()
        return data if data.strip() else None
    return None


def _print_final_report(gs: GuardedStream) -> None:
    report = gs.result.report
    if report is None:
        return
    if report.ok and not gs.result.cancelled:
        console.print(
            f"\n[green]✓ {report.citations_total} citation(s) analysée(s) — "
            f"aucune hallucination.[/green]"
            + (
                f" [dim]({gs.result.annotations_inserted} annotation(s))[/dim]"
                if gs.result.annotations_inserted
                else ""
            )
        )
        return
    blocking = sum(
        1
        for v in report.violations
        if v.severity in (Severity.HIGH, Severity.CRITICAL)
    )
    console.print(
        f"\n[red]✗ {blocking} violation(s) bloquante(s) sur "
        f"{report.citations_total} citation(s) analysée(s).[/red]"
    )
    for v in report.violations[:5]:
        console.print(
            f"  [yellow]→[/yellow] {v.severity.value.upper():8} "
            f"{v.kind:24} :: {v.citation.raw}"
        )


def _has_blocking(gs: GuardedStream, threshold: Severity) -> bool:
    rank = {
        Severity.LOW: 0,
        Severity.MEDIUM: 1,
        Severity.HIGH: 2,
        Severity.CRITICAL: 3,
    }
    if gs.result.report is None:
        return False
    return any(
        rank[v.severity] >= rank[threshold]
        for v in gs.result.report.violations
    )


def _report_to_dict(report) -> dict:
    if report is None:
        return {}
    return {
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
