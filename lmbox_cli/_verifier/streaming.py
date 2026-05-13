"""Streaming verifier — flag hallucinated citations as they appear.

Why streaming?
──────────────
When an agent generates a 4-page brief, waiting for the full output
before checking citations costs the operator several seconds. Worse,
in a live `lmbox agent run`, the cabinet's avocat is watching the
tokens stream into the UI — they want to see the red warning under
"Cass. Com., 12 janvier 2024, n° 99-99.999" the moment it's emitted,
not 30 s later when the brief is done.

The streaming verifier exposes two APIs :

  1. Push-style ::

        sv = StreamingVerifier(pieces=["1", "2", "3"])
        for chunk in agent.stream():
            for violation in sv.feed(chunk):
                ui.flash_warning(violation)
        report = sv.finalize()

  2. Pull-style (generator wrapper) ::

        for violation in verify_stream(agent.stream(), pieces=[...]):
            ui.flash_warning(violation)

Both share the same internals. The extractor is non-streaming by
nature (regex on a buffer), so we re-run it after every "stable
boundary" (sentence terminator) and dedupe against an already-seen
set. The cost of re-extraction is negligible compared to the LLM
generation cost — measured at ~80 µs / 1 KB of buffer.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Optional

from lmbox_cli._verifier.extractor import (
    Citation,
    CitationKind,
    find_citations,
    find_malformed,
)
from lmbox_cli._verifier.verifier import (
    Severity,
    VerificationReport,
    Violation,
    _check_external,
    _check_piece,
)


# A citation is "stable" once it's followed by whitespace + a sentence
# terminator OR a closing parenthesis OR ~40 chars of more text. We use
# the trailing-text heuristic so a citation deep in a sentence still
# gets verified before the period.
_STABILITY_TAIL = 40


class StreamingVerifier:
    """Stateful verifier that emits violations as chunks arrive.

    Thread-unsafe by design — one instance per agent run. Callers
    that parallelize should use one verifier per stream.
    """

    def __init__(
        self,
        *,
        pieces: list[str] | None = None,
        check_external: bool = True,
    ) -> None:
        self.pieces_set: set[str] | None = (
            {str(p).strip() for p in pieces} if pieces is not None else None
        )
        self.check_external = check_external
        self._buffer: list[str] = []  # list-of-chunks → ''.join when needed
        # Citations already emitted, keyed by (position, kind, raw).
        # Same identity rule as the non-streaming dedupe in extractor.
        self._seen: set[tuple[int, str, str]] = set()
        self._report = VerificationReport()

    @property
    def buffer_size(self) -> int:
        return sum(len(c) for c in self._buffer)

    @property
    def text(self) -> str:
        """The accumulated buffer as a single string. Convenient for
        downstream consumers (audit log, GuardedStream) that need the
        full generated output."""
        return "".join(self._buffer)

    def feed(self, chunk: str) -> list[Violation]:
        """Append `chunk` to the buffer and return any NEW violations
        unlocked by the addition.

        A citation is checked once enough trailing context exists to
        be sure it's complete — currently a fixed 40-char tail. This
        means the last citation in the brief is held back until
        `finalize()` is called.
        """
        self._buffer.append(chunk)
        return self._scan(final=False)

    def drain(self) -> list[Violation]:
        """Flush pending citations + run the malformed pass.

        Returns ONLY the violations newly discovered by this call
        (so a streaming caller can yield them). Idempotent : calling
        `drain()` then `finalize()` won't double-report.
        """
        if getattr(self, "_drained", False):
            return []
        new = self._scan(final=True)
        # Malformed detection runs once at the end — structural check.
        text = "".join(self._buffer)
        for m in find_malformed(text):
            key = (m.position, m.kind.value, m.raw)
            if key in self._seen:
                continue
            self._seen.add(key)
            v = Violation(
                severity=Severity.CRITICAL,
                kind="malformed_citation",
                citation=m,
                detail="Citation au format quasi-canonique mais avec une "
                "faute (mois invalide, séparateur incorrect, etc.) — "
                "l'agent a inventé une référence en se rappelant "
                "approximativement le format.",
            )
            self._report.violations.append(v)
            new.append(v)
        self._drained = True
        return new

    def finalize(self) -> VerificationReport:
        """Drain + sort + stamp env signals. Returns the full report."""
        self.drain()
        severity_order = {
            Severity.CRITICAL: 0,
            Severity.HIGH: 1,
            Severity.MEDIUM: 2,
            Severity.LOW: 3,
        }
        self._report.violations.sort(
            key=lambda v: (severity_order[v.severity], v.citation.position)
        )
        import os
        self._report.legifrance_configured = self.check_external and bool(
            os.environ.get("LEGIFRANCE_CLIENT_ID")
        )
        return self._report

    # ─── Internals ────────────────────────────────────────────

    def _scan(self, *, final: bool) -> list[Violation]:
        """Re-extract over the full buffer, dispatch each NEW citation,
        return the freshly-found Violations.

        `final=True` skips the stability check — at finalize() we must
        report everything, including citations at the very end of the
        buffer that haven't been "settled" by trailing context.
        """
        text = "".join(self._buffer)
        new_violations: list[Violation] = []

        for c in find_citations(text):
            key = (c.position, c.kind.value, c.raw)
            if key in self._seen:
                continue
            if not final and not self._is_stable(c, text):
                continue
            self._seen.add(key)
            before = len(self._report.violations)
            self._report.citations_total += 1
            if c.kind == CitationKind.PIECE_INTERNE:
                _check_piece(c, self.pieces_set, self._report)
            elif self.check_external:
                _check_external(c, self._report)
            else:
                self._report.citations_ok += 1
            # Anything added to report.violations is newly found
            new_violations.extend(self._report.violations[before:])

        return new_violations

    def _is_stable(self, c: Citation, text: str) -> bool:
        """Has enough trailing context been seen to be confident
        this citation is complete?

        We require either (a) ≥ `_STABILITY_TAIL` chars of trailing
        text, or (b) a sentence terminator immediately after."""
        tail_start = c.position + len(c.raw)
        if tail_start >= len(text):
            return False
        tail = text[tail_start:tail_start + _STABILITY_TAIL]
        if len(tail) >= _STABILITY_TAIL:
            return True
        # Sentence terminator within the existing tail counts as stable
        return any(ch in tail for ch in ".!?\n")


def verify_stream(
    chunks: Iterable[str],
    *,
    pieces: list[str] | None = None,
    check_external: bool = True,
) -> Iterator[Violation]:
    """Generator wrapper around StreamingVerifier.

    Yields each Violation as it's discovered. After exhaustion, the
    final report (including malformed-citation pass) is available via
    the `.report` attribute on the wrapper class — but for most
    callers this generator is enough.
    """
    sv = StreamingVerifier(pieces=pieces, check_external=check_external)
    for chunk in chunks:
        yield from sv.feed(chunk)
    # Final pass — yield the tail + malformed, then stamp env signals
    yield from sv.drain()
    sv.finalize()
