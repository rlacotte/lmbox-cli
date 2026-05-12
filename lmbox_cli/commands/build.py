"""`lmbox agent build` — compile a manifest into kernel-native artefacts.

Default target is `--kernel openclaw` (the only adapter shipped in 0.3).
The build output lands in `<agent>/.build/<kernel>/<slug>/` and is the
artefact `lmbox agent deploy` will sign + push to a box.

Why a separate `build` step (instead of building on deploy)?
────────────────────────────────────────────────────────────
- Partners can inspect the kernel-native output before deploy
  (catch frontmatter mistakes, missing tool files, etc.).
- CI pipelines can build once and deploy to multiple boxes from
  the same artefact (reproducible deploys).
- Build is offline; deploy needs network + auth. Keeping them
  separate lets partners commit `.build/` artefacts to a release
  branch if their security policy requires it.

Exit codes
──────────
0  Build succeeded.
1  Adapter raised AdapterError (e.g. unknown tool type for kernel).
2  Manifest invalid / agent dir missing.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import typer
from rich.console import Console
from rich.tree import Tree

from lmbox_cli._adapters import AVAILABLE_ADAPTERS
from lmbox_cli._adapters.base import AdapterError
from lmbox_cli._manifest import ManifestError, load

console = Console()


def cmd(
    path: Path | None = typer.Argument(
        None,
        help="Path to the agent directory (default: current dir).",
    ),
    kernel: str = typer.Option(
        "openclaw",
        "--kernel",
        "-k",
        help="Target runtime kernel. 'openclaw' is the only adapter shipped in 0.3.",
    ),
    output_dir: Path | None = typer.Option(
        None,
        "--output-dir",
        "-o",
        help="Where to write the build. Defaults to <agent>/.build/<kernel>/.",
    ),
    clean: bool = typer.Option(
        True,
        "--clean/--no-clean",
        help="Wipe the output dir before building. Default: on.",
    ),
) -> None:
    """Compile a manifest into a kernel-native artefact."""

    # ─── Resolve agent directory ──────────────────────────────
    agent_dir = _resolve_agent_dir(path)
    if agent_dir is None:
        console.print(
            "[red]No agent found.[/red] Run from inside an agent directory, "
            "or pass the path explicitly."
        )
        raise typer.Exit(code=2)

    # ─── Pick the adapter ────────────────────────────────────
    if kernel not in AVAILABLE_ADAPTERS:
        avail = ", ".join(sorted(AVAILABLE_ADAPTERS))
        console.print(f"[red]Unknown kernel '{kernel}'.[/red] Available: {avail}")
        raise typer.Exit(code=2)
    adapter = AVAILABLE_ADAPTERS[kernel]()

    # ─── Load + validate manifest ────────────────────────────
    try:
        manifest = load(agent_dir / "manifest.yaml")
    except ManifestError as exc:
        console.print(f"[red]✗ Manifest invalid[/red]\n{exc}")
        raise typer.Exit(code=2) from exc

    # ─── Prepare output dir ──────────────────────────────────
    out = output_dir or (agent_dir / ".build" / kernel)
    if clean and out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    # ─── Compile ─────────────────────────────────────────────
    try:
        result = adapter.compile(manifest, agent_dir=agent_dir, output_dir=out)
    except AdapterError as exc:
        console.print(f"[red]✗ Build failed[/red]: {exc}")
        raise typer.Exit(code=1) from exc

    # ─── Report ──────────────────────────────────────────────
    _print_result(manifest, result)


# ─── Helpers ───────────────────────────────────────────────────


def _resolve_agent_dir(path: Path | None) -> Path | None:
    if path is None:
        candidate = Path.cwd()
        return candidate if (candidate / "manifest.yaml").exists() else None
    if path.is_dir():
        return path if (path / "manifest.yaml").exists() else None
    if path.is_file() and path.name == "manifest.yaml":
        return path.parent
    return None


def _print_result(manifest: dict, result) -> None:
    """Render a tree of the produced files + size + warnings."""
    md = manifest["metadata"]

    tree = Tree(
        f"[green]✓[/green] Built [bold]{md['slug']}[/bold] @ "
        f"[yellow]{md['version']}[/yellow] for kernel "
        f"[magenta]{result.kernel}[/magenta] (min {result.kernel_min_version})"
    )

    # Group by relative path under artefact_dir
    sub = tree.add(f"[cyan]{result.artefact_dir}[/cyan]")
    for f in sorted(result.files):
        size = f.stat().st_size
        size_str = f"{size}B" if size < 1024 else f"{size / 1024:.1f}KB"
        sub.add(f"{f.relative_to(result.artefact_dir)}  [dim]{size_str}[/dim]")

    console.print(tree)

    for w in result.warnings:
        console.print(f"[yellow]⚠[/yellow]  {w}")
