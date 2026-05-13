"""`lmbox agent test` — run golden evals locally against a configurable LLM.

The dev loop for an agent:

    $ lmbox agent new contract-review --template legal-document
    $ cd contract-review
    $ $EDITOR prompts/system.md
    $ $EDITOR evals/golden.jsonl
    $ lmbox agent test          # ← this command

By default we point at Ollama on localhost (the simplest local setup).
Override with --endpoint or LMBOX_LLM_ENDPOINT.

Exit codes
──────────
0  Suite met manifest's pass_threshold.
1  Suite under threshold OR any case errored.
2  Manifest / golden invalid (partner should fix the source files).
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from lmbox_cli._evals import load_golden, run
from lmbox_cli._evals.loader import GoldenLoadError
from lmbox_cli._evals.runner import CaseResult, load_system_prompt
from lmbox_cli._llm import from_env
from lmbox_cli._manifest import ManifestError, load

console = Console()


def cmd(
    path: Path | None = typer.Argument(
        None,
        help="Path to the agent directory (default: current dir).",
    ),
    endpoint: str | None = typer.Option(
        None,
        "--endpoint",
        envvar="LMBOX_LLM_ENDPOINT",
        help="LLM endpoint (OpenAI-compatible). Default: http://localhost:11434 (Ollama).",
    ),
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        envvar="LMBOX_LLM_API_KEY",
        help="Bearer token sent to the endpoint. Most local backends accept none.",
    ),
    model: str | None = typer.Option(
        None,
        "--model",
        help="Override the model declared in the manifest (useful for A/B testing).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Parse manifest + golden, but don't call the LLM. Catches load-time issues fast.",
    ),
    show_response: bool = typer.Option(
        False,
        "--show-response",
        help="Print each LLM response in full (verbose; useful for prompt debugging).",
    ),
    timeout: float = typer.Option(
        180.0,
        "--timeout",
        envvar="LMBOX_LLM_TIMEOUT",
        help="Per-request timeout in seconds. Bump on slow CPU backends.",
    ),
) -> None:
    """Run the agent's golden eval suite against a local or remote LLM."""

    agent_dir = _resolve_agent_dir(path)
    if agent_dir is None:
        console.print(
            "[red]No agent found.[/red] Run from inside an agent directory, "
            "or pass the path explicitly."
        )
        raise typer.Exit(code=2)

    # ─── Load & validate manifest ─────────────────────────────
    manifest_path = agent_dir / "manifest.yaml"
    try:
        manifest = load(manifest_path)
    except ManifestError as exc:
        console.print(f"[red]✗ Manifest invalid[/red]\n{exc}")
        raise typer.Exit(code=2) from exc

    # ─── Load golden cases ────────────────────────────────────
    golden_path = agent_dir / manifest["spec"]["evals"]["golden"]
    try:
        cases = load_golden(golden_path)
    except GoldenLoadError as exc:
        console.print(f"[red]✗ Golden file invalid[/red]\n  {exc}")
        raise typer.Exit(code=2) from exc

    # ─── System prompt ────────────────────────────────────────
    prompt_path = agent_dir / manifest["spec"]["prompts"]["system"]
    if not prompt_path.exists():
        console.print(f"[red]✗ System prompt missing:[/red] {prompt_path}")
        raise typer.Exit(code=2)
    system_prompt = load_system_prompt(prompt_path)

    md = manifest["metadata"]
    sp = manifest["spec"]
    used_model = model or sp["model"]["primary"]
    pass_threshold = float(sp["evals"].get("pass_threshold", 0.8))
    temperature = float(sp["model"].get("temperature", 0.2))
    max_tokens = int(sp["model"].get("max_tokens", 1024))

    console.print(
        f"[cyan]Running {len(cases)} case(s)[/cyan] for "
        f"[bold]{md['slug']}[/bold] @ [yellow]{md['version']}[/yellow] · "
        f"model=[magenta]{used_model}[/magenta]" + (" · [dim](dry-run)[/dim]" if dry_run else "")
    )

    if dry_run:
        # Parse-only smoke check. Prove the manifest + golden are
        # internally consistent without paying for inference.
        console.print(f"[green]✓ dry-run OK[/green] · {len(cases)} cases ready to run.")
        return

    # ─── Real run ─────────────────────────────────────────────
    client = from_env(endpoint=endpoint, api_key=api_key, timeout=timeout)

    results = []
    with Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("[cyan]Running evals...", total=len(cases))

        def _on_done(cr: CaseResult) -> None:
            results.append(cr)
            mark = "[green]✓[/green]" if cr.passed else "[red]✗[/red]"
            progress.console.print(f"  {mark} {cr.case.display_id}")
            if show_response and cr.response:
                progress.console.print(f"    [dim]{cr.response[:200]}…[/dim]")
            if cr.error:
                progress.console.print(f"    [red]ERROR:[/red] {cr.error}")
            progress.advance(task)

        eval_result = run(
            client=client,
            model=used_model,
            system_prompt=system_prompt,
            cases=cases,
            pass_threshold=pass_threshold,
            temperature=temperature,
            max_tokens=max_tokens,
            on_case_done=_on_done,
        )

    _print_summary(eval_result)

    if not eval_result.succeeded:
        raise typer.Exit(code=1)


# ─── Helpers ───────────────────────────────────────────────────


def _resolve_agent_dir(path: Path | None) -> Path | None:
    """Figure out which directory holds the agent.

    - None → cwd (must contain manifest.yaml).
    - Path is a dir → must contain manifest.yaml.
    - Path is a file pointing to manifest.yaml → use parent.
    """
    if path is None:
        candidate = Path.cwd()
        return candidate if (candidate / "manifest.yaml").exists() else None

    if path.is_dir():
        return path if (path / "manifest.yaml").exists() else None

    if path.is_file() and path.name == "manifest.yaml":
        return path.parent

    return None


def _print_summary(result) -> None:
    """Render the final pass/fail table."""
    table = Table(title="Eval summary", show_header=True, header_style="bold")
    table.add_column("Case", style="cyan", overflow="fold")
    table.add_column("Pass", justify="center")
    table.add_column("Detail", overflow="fold")

    for cr in result.cases:
        mark = "[green]✓[/green]" if cr.passed else "[red]✗[/red]"
        detail = (
            cr.error
            if cr.error
            else "; ".join(o.detail for o in cr.outcomes if not o.passed) or "all assertions passed"
        )
        table.add_row(cr.case.display_id, mark, detail)

    console.print(table)

    if result.succeeded:
        console.print(
            f"\n[green]✓ Suite passed[/green] · "
            f"{result.passed}/{result.total} cases ({result.score:.0%} ≥ "
            f"{result.pass_threshold:.0%} threshold)"
        )
    else:
        console.print(
            f"\n[red]✗ Suite failed[/red] · "
            f"{result.passed}/{result.total} cases ({result.score:.0%} < "
            f"{result.pass_threshold:.0%} threshold)"
        )
