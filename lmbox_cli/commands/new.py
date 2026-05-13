"""`lmbox agent new` — scaffold a new agent project from a template.

Usage:
    lmbox agent new my-agent
    lmbox agent new my-agent --template legal-document
    lmbox agent new my-agent --template legal-document --vendor sopra --vertical legal

The output is a directory my-agent/ with a working manifest, a system
prompt placeholder, an empty tools/ dir, and one golden eval case
seeded so `lmbox agent validate` passes immediately.

Design choices
──────────────
- Templates ship as real directories inside lmbox_cli/templates/, not
  string blobs. Easier to maintain, partners can browse the source.
- File substitution uses Jinja2 with {{slug}}, {{vendor}}, ... — the
  same syntax the deploy pipeline will use later so partners only
  learn one templating language.
- We refuse to write into a non-empty existing directory unless the
  user passes --force. Better than overwriting work.
"""

from __future__ import annotations

import re
import shutil
from datetime import date
from pathlib import Path

import typer
from jinja2 import Template
from rich.console import Console
from rich.panel import Panel

from lmbox_cli._paths import template_dir

console = Console()

SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{2,63}$")

# Files that should be treated as Jinja templates and rendered.
# Other files (e.g. binary assets, .gitkeep) are copied as-is.
RENDERED_EXTENSIONS = {".yaml", ".yml", ".md", ".txt", ".jsonl", ".py"}


def cmd(
    slug: str = typer.Argument(
        ...,
        help="Agent slug — kebab-case, 3 to 64 chars (e.g. contract-review).",
    ),
    template: str = typer.Option(
        "_base",
        "--template",
        "-t",
        help="Template to scaffold from. Use `_base` for an empty minimal agent.",
    ),
    vendor: str = typer.Option(
        "lmbox",
        "--vendor",
        help="Author of the agent (your company, kebab-case).",
    ),
    vertical: str = typer.Option(
        "generic",
        "--vertical",
        help="Vertical category — legal | finance | health | hr | sales | dev | ops | compliance | public | industry | generic",
    ),
    output_dir: Path | None = typer.Option(
        None,
        "--output-dir",
        "-o",
        help="Parent directory for the new agent (default: current directory).",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite existing files. Use with care.",
    ),
) -> None:
    """Scaffold a new LMbox agent from a template."""

    # ─── Validate inputs ───────────────────────────────────────
    if not SLUG_RE.match(slug):
        console.print(
            f"[red]Invalid slug:[/red] '{slug}'. "
            "Slugs must be kebab-case, 3-64 chars, start with a letter."
        )
        raise typer.Exit(code=2)

    try:
        src = template_dir(template)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc

    dest = (output_dir or Path.cwd()) / slug
    if dest.exists() and any(dest.iterdir()) and not force:
        console.print(
            f"[red]Refusing to scaffold:[/red] {dest} exists and is not empty. "
            "Pass --force to overwrite."
        )
        raise typer.Exit(code=1)

    # ─── Render ────────────────────────────────────────────────
    context = {
        "slug": slug,
        "vendor": vendor,
        "vertical": vertical,
        "today": date.today().isoformat(),
        "display_name": _humanize(slug),
    }

    dest.mkdir(parents=True, exist_ok=True)
    _scaffold(src, dest, context)

    # ─── Done ──────────────────────────────────────────────────
    relative = dest.relative_to(Path.cwd()) if dest.is_relative_to(Path.cwd()) else dest
    console.print(
        Panel.fit(
            (
                f"[green]✓[/green] Scaffolded agent '[bold]{slug}[/bold]' "
                f"using template [cyan]{template}[/cyan].\n\n"
                f"  cd {relative}\n"
                "  $EDITOR manifest.yaml\n"
                "  $EDITOR prompts/system.md\n"
                "  lmbox agent validate"
            ),
            title="LMbox agent created",
            border_style="green",
        )
    )


def _scaffold(src: Path, dest: Path, context: dict[str, str]) -> None:
    """Copy every file from `src` to `dest`, rendering Jinja templates inline.

    Walks the source tree. For each file:
    - If the extension is in RENDERED_EXTENSIONS, render the content
      through Jinja2 with `context`.
    - Otherwise copy verbatim (binary-safe).

    Filenames themselves are NOT rendered — keeps the implementation
    simple. If we ever need {{slug}} in filenames, swap to a more
    elaborate renamer.
    """
    for src_path in src.rglob("*"):
        rel = src_path.relative_to(src)
        target = dest / rel

        if src_path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue

        target.parent.mkdir(parents=True, exist_ok=True)

        if src_path.suffix in RENDERED_EXTENSIONS:
            content = src_path.read_text(encoding="utf-8")
            rendered = Template(content, keep_trailing_newline=True).render(**context)
            target.write_text(rendered, encoding="utf-8")
        else:
            shutil.copyfile(src_path, target)


# Tokens that should keep their canonical casing in display names —
# acronyms (NDA, KYB) and brand-style words. Add to this list as the
# catalogue grows; the override stays small enough to eyeball.
_DISPLAY_CASE_OVERRIDES = {
    "nda": "NDA",
    "kyb": "KYB",
    "kyc": "KYC",
    "msa": "MSA",
    "sow": "SOW",
    "rgpd": "RGPD",
    "soc2": "SOC2",
    "hds": "HDS",
    "owui": "OWUI",
    "llm": "LLM",
    "ia": "IA",
    "ai": "AI",
    "api": "API",
    "ml": "ML",
    "osint": "OSINT",
    "ubo": "UBO",
    "rh": "RH",
    "ops": "Ops",
}


def _humanize(slug: str) -> str:
    """Convert kebab-case-slug → "Kebab Case Slug" for display_name default.

    Acronyms in `_DISPLAY_CASE_OVERRIDES` keep their canonical
    casing (e.g. `nda-reviewer` → "NDA Reviewer", not
    "Nda Reviewer"). Partners can always override the default in
    `manifest.yaml` after scaffolding.
    """
    parts = []
    for word in slug.split("-"):
        parts.append(_DISPLAY_CASE_OVERRIDES.get(word.lower(), word.capitalize()))
    return " ".join(parts)
