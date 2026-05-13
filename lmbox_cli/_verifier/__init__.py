"""Citation verifier — anti-hallucination guardrail for LMbox agents.

The premise: prompt-engineering alone (« ne jamais inventer ») reduces
hallucinations but doesn't eliminate them. A motivated LLM, especially
a small one (3-7B), will still cite an arrêt that doesn't exist when
asked the right kind of question.

This module catches them post-hoc. Three checks, each independent:

  1. EXTERNAL JURISPRUDENCE — every citation matching a known
     French legal format (Cass. Com., n° XX-XXXXX, etc.) is looked up
     against Légifrance's public API. Citations not found → flagged.

  2. INTERNAL PIECES — every `Pièce n° X` reference is checked against
     the list of pieces actually present in the dossier (passed in
     as input). References to non-existent pieces → flagged.

  3. JURISPRUDENCE FORMAT — citations that look almost-right but
     fail the canonical format (`Cass. Com., 12 jav 2024` instead of
     `Cass. Com., 12 janvier 2024`) → flagged as malformed.

Usage
─────
    from lmbox_cli._verifier import verify

    report = verify(
        text="...generated agent output...",
        pieces=["1", "2", "3", "4"],   # optional internal pieces list
        check_external=True,           # call Légifrance API
    )
    if not report.ok:
        for v in report.violations:
            print(f"  {v.severity}  {v.kind}  →  {v.snippet}")

The verifier is designed to be run AS POST-PROCESSING after the
agent generates its output. Callers decide whether to (a) just warn
the user, (b) auto-strip the bad citations, or (c) re-prompt the
agent. The CLI command `lmbox agent verify` defaults to (a) — show
the report, let the operator decide.
"""

from __future__ import annotations

from lmbox_cli._verifier.streaming import StreamingVerifier, verify_stream
from lmbox_cli._verifier.verifier import (
    Severity,
    VerificationReport,
    Violation,
    verify,
)

__all__ = [
    "Severity",
    "StreamingVerifier",
    "VerificationReport",
    "Violation",
    "verify",
    "verify_stream",
]
