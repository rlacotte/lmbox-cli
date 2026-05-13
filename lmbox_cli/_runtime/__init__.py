"""Runtime enforcement layer — intercept hallucinated citations as the
model generates, not just after the full response is in.

Layer A (the post-hoc citation verifier in `lmbox_cli._verifier`) gives
us a hard pass/fail on a finished output. Layer B — this module —
catches the violation **while the model is still streaming**, with
three escalation modes :

    strict    Cancel generation the moment a CRITICAL/HIGH is detected.
              The caller gets the partial output up to the violation,
              plus a GuardedStreamViolation describing what happened.
              Use for high-stakes flows (production-ready briefs).
    annotate  Inject a visible `[⚠ HALLUCINATION SUSPECTED: …]` marker
              into the stream right after the offending citation, and
              let the model continue. Use when you want the human
              reviewer to see the suspect inline.
    warn      Pass-through. Violations are emitted as side-channel
              events for observability + audit, but the stream is
              never modified. Use when you want telemetry without
              changing UX (rollout / canary phase).

The guard is a thin pure-Python wrapper that consumes any iterator of
str chunks. It does NOT know about httpx, OpenAI SSE, Anthropic events,
or any specific backend — the caller hands it a `producer` iterator
and gets back a `guarded` iterator. This makes the unit tests trivial
(feed a list of strings, assert the wrapped output).

See `docs/adr/003-runtime-enforcement.md` for the full design.
"""

from __future__ import annotations

from lmbox_cli._runtime.guarded_stream import (
    GuardedStream,
    GuardedStreamViolation,
    GuardMode,
    GuardEvent,
    GuardEventType,
    StreamResult,
)

__all__ = [
    "GuardedStream",
    "GuardedStreamViolation",
    "GuardMode",
    "GuardEvent",
    "GuardEventType",
    "StreamResult",
]
