"""`lmbox agent validate` — check a manifest against the v1 schema.

Validates more than just the YAML structure: also checks that the
files the manifest references actually exist on disk (system prompt,
golden eval). Catches the most common partner mistake — typo in a
path — before they reach `lmbox agent build`.

Exit codes
──────────
0  Valid.
1  Manifest is invalid OR a referenced file is missing.
2  Manifest file not found (wrong path passed).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from lmbox_cli._manifest import ManifestError, load

console = Console()


def cmd(
    path: Optional[Path] = typer.Argument(
        None,
        help="Path to the agent directory or directly to a manifest.yaml. Defaults to current dir.",
    ),
) -> None:
    """Validate an agent manifest.yaml against the lmbox.eu/v1 schema."""

    manifest_path = _resolve_manifest_path(path)
    if manifest_path is None:
        console.print(
            "[red]No manifest.yaml found.[/red] Run this from inside an agent directory, "
            "or pass the path explicitly."
        )
        raise typer.Exit(code=2)

    # ─── Schema validation ─────────────────────────────────────
    try:
        manifest = load(manifest_path)
    except ManifestError as exc:
        console.print(f"[red]✗ Schema invalid[/red]\n{exc}")
        raise typer.Exit(code=1) from exc

    # ─── Cross-reference checks (files exist) ─────────────────
    agent_dir = manifest_path.parent
    missing: list[str] = []

    prompts_system = manifest["spec"]["prompts"]["system"]
    if not (agent_dir / prompts_system).exists():
        missing.append(f"spec.prompts.system → {prompts_system}")

    evals_golden = manifest["spec"]["evals"]["golden"]
    if not (agent_dir / evals_golden).exists():
        missing.append(f"spec.evals.golden → {evals_golden}")

    if missing:
        console.print(
            "[red]✗ Manifest references files that don't exist:[/red]\n"
            + "\n".join(f"  • {m}" for m in missing)
        )
        raise typer.Exit(code=1)

    # ─── Success report ───────────────────────────────────────
    md = manifest["metadata"]
    sp = manifest["spec"]

    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column(style="cyan", no_wrap=True)
    table.add_column()
    table.add_row("apiVersion", manifest["apiVersion"])
    table.add_row("slug", f"[bold]{md['slug']}[/bold]")
    table.add_row("version", md["version"])
    table.add_row("vendor", md["vendor"])
    table.add_row("vertical", md.get("vertical", "generic"))
    table.add_row("model", sp["model"]["primary"])
    table.add_row("tools", str(len(sp.get("tools", []))))
    connectors = sp.get("connectors") or {}
    table.add_row("connectors", f"{len(connectors.get('required', []))} required, {len(connectors.get('optional', []))} optional")

    console.print("[green]✓ Valid manifest[/green]")
    console.print(table)


def _resolve_manifest_path(path: Optional[Path]) -> Optional[Path]:
    """Figure out which file the user means.

    - No path given → ./manifest.yaml in cwd.
    - Path is a directory → manifest.yaml inside it.
    - Path is a file → that file.
    - Anything else → None (caller raises).
    """
    if path is None:
        candidate = Path.cwd() / "manifest.yaml"
        return candidate if candidate.exists() else None

    if path.is_dir():
        candidate = path / "manifest.yaml"
        return candidate if candidate.exists() else None

    if path.is_file():
        return path

    return None
