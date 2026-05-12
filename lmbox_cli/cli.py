"""Top-level Typer app — wires together `lmbox <subcommand>` routing.

Subcommands live in lmbox_cli/commands/. The CLI itself stays thin:
each command owns its own argument schema, validation, and side
effects so that they remain testable in isolation.
"""

from __future__ import annotations

import typer
from rich.console import Console

from lmbox_cli import __version__
from lmbox_cli.commands import build, new, pack, test, validate

console = Console()

app = typer.Typer(
    name="lmbox",
    help=(
        "LMbox CLI — scaffold, test, and deploy sovereign AI agents on the "
        "LMbox appliance. See https://docs.lmbox.eu/agent-sdk for the full guide."
    ),
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
)

# Subcommand group: `lmbox agent ...`
agent_app = typer.Typer(
    name="agent",
    help="Manage LMbox agents (scaffold, validate, build, deploy).",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(agent_app)

agent_app.command("new")(new.cmd)
agent_app.command("validate")(validate.cmd)
agent_app.command("test")(test.cmd)
agent_app.command("build")(build.cmd)
agent_app.command("pack")(pack.cmd)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: bool = typer.Option(
        False, "--version", "-V", help="Print the lmbox CLI version and exit."
    ),
) -> None:
    """Top-level callback — handles --version, sets up shared state."""
    if version:
        console.print(f"lmbox {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        # Mimic the no_args_is_help behavior manually since we set
        # invoke_without_command=True to make --version work.
        typer.echo(ctx.get_help())
        raise typer.Exit()


if __name__ == "__main__":
    app()
