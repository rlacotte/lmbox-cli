"""`lmbox agent pack` — bundle a built agent into a signed .lmbox tarball.

The pipeline is:
    new → validate → test → build → **pack** → deploy

`pack` is decoupled from `deploy` so that:
- CI can produce a release artefact (`.lmbox`) and attach it to a
  GitHub release / S3 / artefact registry, deploying later from
  a stable source.
- A partner that operates in an air-gapped environment can hand a
  `.lmbox` to the customer manually (USB transfer) — `deploy` is
  optional infrastructure, not a requirement.

The tarball is REPRODUCIBLE: two `pack` runs from the same source
produce byte-identical artefacts (deterministic mtime + sorted
entries + cleared uid/gid). Lets partners pin a sha256 in their
release notes and customers verify it.

Exit codes
──────────
0  Bundle produced.
1  Build dir missing or empty.
2  Manifest invalid / agent dir missing.
"""

from __future__ import annotations

import os
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from lmbox_cli._bundle import pack
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
        help="Which built artefact to pack. Must match what `lmbox agent build` produced.",
    ),
    output_dir: Path | None = typer.Option(
        None,
        "--output-dir",
        "-o",
        help="Where to drop the .lmbox + .lmbox.json. Default: <agent>/.dist/",
    ),
    hmac_key_env: str = typer.Option(
        "LMBOX_PACK_KEY",
        "--hmac-key-env",
        help="Env var name to read the HMAC signing key from. Bytes interpreted as utf-8.",
    ),
    no_hmac: bool = typer.Option(
        False,
        "--no-hmac",
        help="Skip HMAC signing even if the key is set. Sidecar carries only sha256.",
    ),
) -> None:
    """Pack a built agent into a reproducible .lmbox tarball."""

    agent_dir = _resolve_agent_dir(path)
    if agent_dir is None:
        console.print("[red]No agent found.[/red] Run from an agent directory or pass the path.")
        raise typer.Exit(code=2)

    # ─── Manifest ────────────────────────────────────────────
    try:
        manifest = load(agent_dir / "manifest.yaml")
    except ManifestError as exc:
        console.print(f"[red]✗ Manifest invalid[/red]\n{exc}")
        raise typer.Exit(code=2) from exc

    # ─── Resolve build dir (must have been built first) ──────
    build_root = agent_dir / ".build" / kernel
    if not build_root.exists():
        console.print(
            f"[red]No build for kernel '{kernel}' found.[/red] "
            f"Run `lmbox agent build -k {kernel}` first."
        )
        raise typer.Exit(code=1)
    # The build adapter writes into <build_root>/<slug>/
    slug = manifest["metadata"]["slug"]
    build_dir = build_root / slug
    if not build_dir.exists():
        console.print(f"[red]Built artefact missing:[/red] {build_dir}")
        raise typer.Exit(code=1)

    # ─── HMAC key ────────────────────────────────────────────
    hmac_key: bytes | None = None
    if not no_hmac:
        raw = os.environ.get(hmac_key_env)
        if raw:
            hmac_key = raw.encode("utf-8")

    # ─── Pack ────────────────────────────────────────────────
    out = output_dir or (agent_dir / ".dist")
    try:
        bundle = pack(
            build_dir=build_dir,
            manifest=manifest,
            output_dir=out,
            hmac_key=hmac_key,
        )
    except FileNotFoundError as exc:
        console.print(f"[red]Pack failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    # ─── Report ──────────────────────────────────────────────
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column(style="cyan", no_wrap=True)
    table.add_column()
    table.add_row("tarball", str(bundle.tarball))
    table.add_row("sidecar", str(bundle.sidecar))
    table.add_row("size", _fmt_size(bundle.size_bytes))
    table.add_row("sha256", bundle.sha256)
    if bundle.hmac_sha256:
        table.add_row("hmac", bundle.hmac_sha256[:12] + "…")
    else:
        table.add_row("hmac", "[dim](not signed — set LMBOX_PACK_KEY)[/dim]")

    console.print(
        f"[green]✓ Packed[/green] [bold]{slug}[/bold] @ "
        f"[yellow]{manifest['metadata']['version']}[/yellow]"
    )
    console.print(table)


def _resolve_agent_dir(path: Path | None) -> Path | None:
    if path is None:
        candidate = Path.cwd()
        return candidate if (candidate / "manifest.yaml").exists() else None
    if path.is_dir():
        return path if (path / "manifest.yaml").exists() else None
    if path.is_file() and path.name == "manifest.yaml":
        return path.parent
    return None


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.2f} MB"
