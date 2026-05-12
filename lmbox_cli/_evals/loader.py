"""Loads golden.jsonl files — one JSON object per line.

Each line is a `GoldenCase`. We validate as we parse so that
partners get clean error messages (line number + reason) instead
of mysterious AttributeErrors downstream.

Format
──────
{
  "id": "case-001",
  "input": "What is X?",
  "expected": {
    "contains": ["expected substring", ...],          # optional
    "not_contains": ["forbidden substring", ...],     # optional
    "regex": "pattern",                               # optional
    "json_path": {"$.field": "expected_value"}        # optional, future
  },
  "tolerance": 0.8                                    # optional, 0..1
}

At least one of `expected.contains` / `not_contains` / `regex`
must be present. We don't enforce *all* the assertion types at
load time — assertion modules validate their own keys at run time.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class GoldenLoadError(Exception):
    """Raised on any malformed line — message includes the line number."""


@dataclass(frozen=True)
class GoldenCase:
    id: str
    input: str
    expected: dict[str, Any]
    tolerance: float = 1.0
    tags: list[str] = field(default_factory=list)

    @property
    def display_id(self) -> str:
        """Truncated id for terminal display — keeps tables aligned."""
        return self.id if len(self.id) <= 30 else self.id[:27] + "..."


def load_golden(path: Path) -> list[GoldenCase]:
    """Read a golden.jsonl file, return a list of GoldenCase.

    Each line is parsed independently. The first error stops the
    load with a clear "line N: <reason>" message.
    """
    if not path.exists():
        raise GoldenLoadError(f"Golden file not found: {path}")

    cases: list[GoldenCase] = []
    seen_ids: set[str] = set()

    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("//"):
            continue  # blank lines + comment lines are skipped

        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise GoldenLoadError(f"line {lineno}: invalid JSON — {exc.msg}") from exc

        if not isinstance(data, dict):
            raise GoldenLoadError(f"line {lineno}: each line must be a JSON object")

        for required in ("id", "input", "expected"):
            if required not in data:
                raise GoldenLoadError(f"line {lineno}: missing required field '{required}'")

        if not isinstance(data["expected"], dict):
            raise GoldenLoadError(f"line {lineno}: 'expected' must be an object")

        # At least one assertion key must be present so the case has
        # *something* to check.
        assertion_keys = {"contains", "not_contains", "regex"}
        if not (assertion_keys & data["expected"].keys()):
            raise GoldenLoadError(
                f"line {lineno}: 'expected' must contain at least one of {sorted(assertion_keys)}"
            )

        case_id = str(data["id"])
        if case_id in seen_ids:
            raise GoldenLoadError(f"line {lineno}: duplicate id '{case_id}'")
        seen_ids.add(case_id)

        tolerance = float(data.get("tolerance", 1.0))
        if not 0.0 <= tolerance <= 1.0:
            raise GoldenLoadError(f"line {lineno}: tolerance must be in [0, 1], got {tolerance}")

        cases.append(
            GoldenCase(
                id=case_id,
                input=str(data["input"]),
                expected=data["expected"],
                tolerance=tolerance,
                tags=list(data.get("tags", [])),
            )
        )

    if not cases:
        raise GoldenLoadError(f"No golden cases found in {path}")

    return cases
