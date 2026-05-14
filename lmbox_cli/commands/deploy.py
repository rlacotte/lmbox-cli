"""`lmbox agent deploy` — full pipeline (build → pack → upload).

The end of the developer story:

    $ lmbox agent new contract-review -t legal-document
    $ ... edit ...
    $ lmbox agent test
    $ lmbox agent deploy --box BOX-S-001

`deploy` orchestrates build + pack + upload. It does NOT re-validate
since `build` already requires a passing validation step. We also
deliberately skip running the eval suite — partners may want to
deploy a known-failing version to a dev box for live debugging.
If they want the guardrail, they wire `test` into their CI.

Auth model
──────────
The bearer token is the target box's API key — same value the box
uses for its own heartbeats. The customer admin copies it from the
portal (one-shot display) and hands it to the partner. Resolution
order:
  1. --token CLI flag
  2. LMBOX_API_TOKEN env var
  3. fail with a clear message

Cloud endpoint
──────────────
Resolution order (highest first):
  1. --api flag
  2. LMBOX_API env var
  3. default https://api.lmbox.eu

Exit codes
──────────
0  Bundle uploaded; BoxCommand queued. The box will pick it up at
   the next heartbeat (≤ 5 minutes typically).
1  Upload failed (HTTP error, auth error, validation error).
2  Local prep failed (manifest invalid, missing build, etc.).
"""

from __future__ import annotations

import os
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel

from lmbox_cli._bundle import pack as pack_bundle
from lmbox_cli._manifest import ManifestError, load
from lmbox_cli._upload import upload_bundle

console = Console()


def cmd(
    path: Path | None = typer.Argument(
        None,
        help="Path to the agent directory (default: current dir).",
    ),
    box: str = typer.Option(
        ...,
        "--box",
        "-b",
        help="Target box serial (e.g. BOX-S-001).",
    ),
    token: str | None = typer.Option(
        None,
        "--token",
        envvar="LMBOX_API_TOKEN",
        help="Box API token. Default: $LMBOX_API_TOKEN.",
    ),
    api_base: str = typer.Option(
        "https://api.lmbox.eu",
        "--api",
        envvar="LMBOX_API",
        help="Cloud API base URL. Default: https://api.lmbox.eu.",
    ),
    kernel: str = typer.Option(
        "openclaw",
        "--kernel",
        "-k",
        help="Which built artefact to deploy.",
    ),
    skip_build: bool = typer.Option(
        False,
        "--skip-build",
        help="Assume `lmbox agent build` was already run. Default: re-run build.",
    ),
    hmac_key_env: str = typer.Option(
        "LMBOX_PACK_KEY",
        "--hmac-key-env",
        help="Env var name for the HMAC signing key.",
    ),
    as_distributor: str | None = typer.Option(
        None,
        "--as-distributor",
        envvar="LMBOX_AS_DISTRIBUTOR",
        help=(
            "Slug du partner distributor LMbox (sopra, magellan, …) qui "
            "installe cet agent. Inscrit dans le sidecar pour attribution "
            "automatique du revenue share marketplace côté cloud. "
            "Optionnel : si omis, le cloud retombe sur le partenaire "
            "attaché au customer de la box (box.customer.partner) ; "
            "sinon, pas de revenue share vendor."
        ),
    ),
    lmbox_signature: str | None = typer.Option(
        None,
        "--lmbox-signature",
        envvar="LMBOX_AGENT_SIGNATURE",
        help=(
            "Signature HMAC LMbox du bundle, requise pour les agents "
            "marketplace publiés. Émise par /admin/marketplace/:id/publish "
            "côté LMbox cloud (commence par 'lmbox-mp-1.'). Inutile pour "
            "les agents partner en cours de dev."
        ),
    ),
) -> None:
    """Deploy a built agent to a target LMbox appliance."""

    if not token:
        console.print(
            "[red]No API token.[/red] Pass --token or set LMBOX_API_TOKEN.\n"
            "Get it from the customer admin via the portal "
            "(one-shot display under /portail/box/<id>)."
        )
        raise typer.Exit(code=2)

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

    slug = manifest["metadata"]["slug"]
    version = manifest["metadata"]["version"]

    # ─── Build (unless skipped) ──────────────────────────────
    if not skip_build:
        from lmbox_cli.commands.build import cmd as build_cmd

        try:
            build_cmd(path=agent_dir, kernel=kernel, output_dir=None, clean=True)
        except typer.Exit as exc:
            if exc.exit_code != 0:
                console.print("[red]Build failed — aborting deploy.[/red]")
                raise

    build_dir = agent_dir / ".build" / kernel / slug
    if not build_dir.exists():
        console.print(f"[red]Build output missing:[/red] {build_dir}")
        raise typer.Exit(code=2)

    # ─── Pack ────────────────────────────────────────────────
    hmac_key = (os.environ.get(hmac_key_env) or "").encode("utf-8") or None
    dist_dir = agent_dir / ".dist"
    try:
        bundle = pack_bundle(
            build_dir=build_dir,
            manifest=manifest,
            output_dir=dist_dir,
            hmac_key=hmac_key,
            distributor_partner_slug=as_distributor,
            lmbox_signature=lmbox_signature,
        )
    except FileNotFoundError as exc:
        console.print(f"[red]Pack failed:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    console.print(
        f"[cyan]Bundle ready:[/cyan] {bundle.tarball.name} "
        f"({bundle.size_bytes:,} bytes, sha256={bundle.sha256[:12]}…)"
    )

    # ─── Upload ──────────────────────────────────────────────
    console.print(f"[cyan]Uploading to[/cyan] {api_base} for box [bold]{box}[/bold]…")
    result = upload_bundle(
        api_base=api_base,
        serial=box,
        token=token,
        bundle_path=bundle.tarball,
        sidecar_path=bundle.sidecar,
    )

    if not result.ok:
        console.print(
            Panel.fit(
                f"[red]✗ Upload failed[/red] (HTTP {result.status_code})\n\n"
                f"Error: {result.error}\n"
                f"Raw:   {result.raw}",
                title="Deploy aborted",
                border_style="red",
            )
        )
        raise typer.Exit(code=1)

    console.print(
        Panel.fit(
            (
                f"[green]✓ Deployed[/green] [bold]{slug}[/bold] @ "
                f"[yellow]{version}[/yellow] to box [bold]{box}[/bold].\n\n"
                f"AgentInstallation #{result.agent_installation_id} — "
                f"state: [magenta]{result.state}[/magenta]\n\n"
                "The box will pick up the install_agent command on its next\n"
                "heartbeat (typically within 5 minutes). Monitor progress:\n"
                f"  https://app.lmbox.eu/portail/box/<id>"
            ),
            title="Deploy queued",
            border_style="green",
        )
    )


def _resolve_agent_dir(path: Path | None) -> Path | None:
    if path is None:
        candidate = Path.cwd()
        return candidate if (candidate / "manifest.yaml").exists() else None
    if path.is_dir():
        return path if (path / "manifest.yaml").exists() else None
    if path.is_file() and path.name == "manifest.yaml":
        return path.parent
    return None
