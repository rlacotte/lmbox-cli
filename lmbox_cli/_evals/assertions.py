"""Assertion functions — given an LLM response, check it against an expected pattern.

Each function returns an `AssertionOutcome` rather than raising, so
the runner can aggregate multiple assertions per case and report
them all even if one fails.

We deliberately keep assertions string-based for v0.1. Semantic
matching (embedding similarity) and structured-output validation
land in 0.2 once we wire an embedding model into the runtime.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AssertionOutcome:
    name: str  # 'contains', 'not_contains', 'regex'
    passed: bool
    detail: str  # short human-readable message for the report


def check_contains(response: str, expected: list[str]) -> list[AssertionOutcome]:
    """All `expected` substrings must be present in `response` (case-insensitive).

    Returns one outcome per expected substring so partial-match cases
    are visible in the report.
    """
    needle_outcomes: list[AssertionOutcome] = []
    haystack = response.lower()
    for needle in expected:
        hit = needle.lower() in haystack
        needle_outcomes.append(
            AssertionOutcome(
                name="contains",
                passed=hit,
                detail=f"'{_truncate(needle)}'"
                if hit
                else f"'{_truncate(needle)}' not found in response",
            )
        )
    return needle_outcomes


def check_not_contains(response: str, forbidden: list[str]) -> list[AssertionOutcome]:
    """None of `forbidden` may appear in `response`. Case-insensitive."""
    outcomes: list[AssertionOutcome] = []
    haystack = response.lower()
    for word in forbidden:
        hit = word.lower() in haystack
        outcomes.append(
            AssertionOutcome(
                name="not_contains",
                passed=not hit,
                detail=f"'{_truncate(word)}' correctly absent"
                if not hit
                else f"forbidden '{_truncate(word)}' appeared in response",
            )
        )
    return outcomes


def check_regex(response: str, pattern: str) -> list[AssertionOutcome]:
    """`pattern` (Python regex) must match somewhere in `response`."""
    try:
        compiled = re.compile(pattern, re.DOTALL | re.IGNORECASE)
    except re.error as exc:
        return [
            AssertionOutcome(
                name="regex",
                passed=False,
                detail=f"invalid regex pattern: {exc}",
            )
        ]

    matched = bool(compiled.search(response))
    return [
        AssertionOutcome(
            name="regex",
            passed=matched,
            detail=f"matched /{_truncate(pattern)}/"
            if matched
            else f"no match for /{_truncate(pattern)}/",
        )
    ]


def evaluate(expected: dict[str, Any], response: str) -> list[AssertionOutcome]:
    """Dispatch a case's `expected` dict to the right assertion fns.

    Unknown keys are silently ignored so we can add new assertion
    types in future versions without breaking existing golden files.
    """
    outcomes: list[AssertionOutcome] = []

    if "contains" in expected:
        outcomes.extend(check_contains(response, list(expected["contains"])))
    if "not_contains" in expected:
        outcomes.extend(check_not_contains(response, list(expected["not_contains"])))
    if "regex" in expected:
        outcomes.extend(check_regex(response, str(expected["regex"])))

    return outcomes


def _truncate(s: str, n: int = 60) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"
