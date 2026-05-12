"""Filesystem layout helpers.

Centralises the few path lookups we do at runtime (schema location,
templates directory) so packaging-time path resolution lives in
exactly one place. If we ever ship the CLI as a binary (PyInstaller,
Nuitka), this is the file that adapts.
"""

from __future__ import annotations

from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
SCHEMA_DIR = PACKAGE_ROOT / "schema"
TEMPLATES_DIR = PACKAGE_ROOT / "templates"

AGENT_V1_SCHEMA = SCHEMA_DIR / "agent_v1.schema.json"


def template_dir(name: str) -> Path:
    """Resolve a template name (e.g. '_base', 'legal-document') to its path.

    Raises FileNotFoundError if the template does not ship with this
    CLI version, with a list of available templates in the message so
    the user can fix the typo without leaving the shell.
    """
    candidate = TEMPLATES_DIR / name
    if not candidate.is_dir():
        available = sorted(p.name for p in TEMPLATES_DIR.iterdir() if p.is_dir())
        raise FileNotFoundError(
            f"Template '{name}' not found. Available templates: {', '.join(available)}"
        )
    return candidate
