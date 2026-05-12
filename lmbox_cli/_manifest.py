"""Manifest loader + validator.

The CLI standardises on a single way to read manifest.yaml: load the
YAML, validate against `agent_v1.schema.json`, return either a dict
(parsed) or raise ManifestError with a human-friendly message.

We deliberately do not wrap the dict into a typed model (pydantic,
attrs) at this stage — partners may add forward-compatible keys
under `spec.runtime_hints.*` and we don't want to drop them silently.
A typed wrapper would either reject them or require a permanent
schema migration. For now, raw dict + schema validation is enough.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from lmbox_cli._paths import AGENT_V1_SCHEMA


class ManifestError(Exception):
    """Raised when a manifest is unreadable or invalid."""


def _load_schema() -> dict[str, Any]:
    """Read the JSON Schema once. Result is small enough that we don't cache."""
    return json.loads(AGENT_V1_SCHEMA.read_text(encoding="utf-8"))


def load(manifest_path: Path) -> dict[str, Any]:
    """Load and validate a manifest.yaml. Raises ManifestError on failure.

    Validation errors are aggregated into a single multi-line message
    rather than raising on the first one — partners get the whole
    picture in one shot rather than fixing one at a time.
    """
    if not manifest_path.exists():
        raise ManifestError(f"Manifest not found: {manifest_path}")

    try:
        data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ManifestError(f"Manifest is not valid YAML: {exc}") from exc

    if not isinstance(data, dict):
        raise ManifestError(
            f"Manifest must be a YAML mapping at the top level, got {type(data).__name__}."
        )

    validator = Draft202012Validator(_load_schema())
    errors: list[ValidationError] = sorted(validator.iter_errors(data), key=lambda e: e.path)
    if errors:
        formatted = "\n".join(f"  • {_format_error(e)}" for e in errors)
        raise ManifestError(
            f"Manifest does not conform to lmbox.eu/v1 schema ({len(errors)} error(s)):\n{formatted}"
        )

    return data


def _format_error(err: ValidationError) -> str:
    """Render a jsonschema error in a short, human-readable form."""
    path = ".".join(str(p) for p in err.absolute_path) or "<root>"
    return f"{path}: {err.message}"
