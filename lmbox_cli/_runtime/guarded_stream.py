"""GuardedStream — wrap an LLM token stream with a runtime citation check.

Architecture
────────────
A token producer (anything yielding `str` chunks) is fed to a
GuardedStream. The GuardedStream :

  1. Forwards each chunk to a `StreamingVerifier` (Layer A).
  2. Yields the chunk to the consumer.
  3. After every chunk, checks if any new violations were discovered.
  4. Acts on those violations according to the chosen `GuardMode` :
       - strict    : stop iterating (the producer is closed via its
                     `close()` if it has one, see `_close_producer`).
                     Raise GuardedStreamViolation at the end.
       - annotate  : inject an inline marker chunk after the chunk
                     that completed the offending citation.
       - warn      : do nothing to the stream; events go out-of-band.
  5. At iterator exhaustion, runs `finalize()` to flush any pending
     violations (citations at the very end of the buffer, malformed
     scan) and exposes the full report on `StreamResult`.

The guard is intentionally backend-agnostic. The httpx-based
OpenAI/Ollama client in `_llm.py` already exposes a chunk iterator
via `OpenAIClient.stream()` — we wrap that. A different backend
(Anthropic, vLLM raw, file replay for tests) can be plugged in with
zero changes here.

Why a class, not just a generator wrapper?
──────────────────────────────────────────
The consumer needs three things after iteration ends :
  - the final VerificationReport (for the audit trail),
  - the full accumulated text (so they can persist it),
  - any GuardedStreamViolation that was raised in strict mode.
A class gives a stable place to attach those, and gives the consumer
the option to register event hooks before the first chunk flows.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from lmbox_cli._verifier import (
    Severity,
    StreamingVerifier,
    VerificationReport,
    Violation,
)


class GuardMode(str, Enum):
    """How the guard reacts to a HIGH/CRITICAL violation."""

    STRICT = "strict"
    ANNOTATE = "annotate"
    WARN = "warn"


class GuardEventType(str, Enum):
    """Side-channel events fired during streaming. Useful for live
    UIs and structured audit logs."""

    CHUNK = "chunk"               # forwarded LLM chunk
    VIOLATION = "violation"       # new violation detected
    ANNOTATION = "annotation"     # an inline annotation was inserted
    CANCELLED = "cancelled"       # strict mode aborted the stream
    FINALIZED = "finalized"       # finalize() returned a report


@dataclass(frozen=True)
class GuardEvent:
    type: GuardEventType
    text: str = ""
    violation: Violation | None = None


@dataclass
class StreamResult:
    """Everything the caller needs after the stream ends."""

    text: str = ""
    report: VerificationReport | None = None
    cancelled: bool = False
    annotations_inserted: int = 0


class GuardedStreamViolation(RuntimeError):
    """Raised at the end of strict-mode iteration when at least one
    HIGH/CRITICAL violation was detected and the stream was cancelled.

    The partial generated text + the report are attached as
    attributes so the caller can still log them for the audit
    trail."""

    def __init__(
        self,
        message: str,
        *,
        partial_text: str,
        report: VerificationReport,
        violations: list[Violation],
    ) -> None:
        super().__init__(message)
        self.partial_text = partial_text
        self.report = report
        self.violations = violations


# Default annotation template — French because the audience is
# avocats/compliance reviewing the brief inline. Tunable via
# constructor argument.
_DEFAULT_ANNOTATION = (
    " [⚠ HALLUCINATION SUSPECTÉE — {kind}: {detail}]"
)


class GuardedStream:
    """Wrap an iterator of LLM chunks with a citation verifier guard.

    Usage ::

        gs = GuardedStream(
            producer=llm.stream(req),
            pieces=["1", "2", "3"],
            mode=GuardMode.STRICT,
        )
        try:
            for chunk in gs:
                ui.print(chunk)
        except GuardedStreamViolation as e:
            audit.log(e.report)
            ui.flash_red(e.violations)
        else:
            audit.log(gs.result.report)
    """

    def __init__(
        self,
        producer: Iterable[str],
        *,
        pieces: list[str] | None = None,
        check_external: bool = True,
        mode: GuardMode = GuardMode.STRICT,
        annotation_template: str = _DEFAULT_ANNOTATION,
        block_severity: Severity = Severity.HIGH,
        on_event: Callable[[GuardEvent], None] | None = None,
    ) -> None:
        self._producer = producer
        self._verifier = StreamingVerifier(
            pieces=pieces, check_external=check_external
        )
        self.mode = mode
        self.annotation_template = annotation_template
        self.block_severity = block_severity
        self._on_event = on_event or (lambda _e: None)
        self.result = StreamResult()
        self._cancelled = False
        self._pending_violations: list[Violation] = []

    # ─── Iterator protocol ────────────────────────────────────

    def __iter__(self) -> Iterator[str]:
        return self._run()

    def _run(self) -> Iterator[str]:
        producer_iter = iter(self._producer)
        try:
            for chunk in producer_iter:
                if not chunk:
                    continue
                self._emit_chunk(chunk)
                yield chunk
                new_violations = self._verifier.feed(chunk)
                if new_violations:
                    yield from self._handle_violations(new_violations)
                if self._cancelled:
                    self._close_producer(producer_iter)
                    self._emit(GuardEventType.CANCELLED)
                    break
        finally:
            # Drain remaining violations + malformed scan, then expose
            # the final report on `.result` for the consumer.
            tail = self._verifier.drain()
            if tail and not self._cancelled:
                # Tail violations can't trigger cancellation (the stream
                # already ended), but they MAY still be annotated/warned.
                yield from self._handle_violations(tail, post_stream=True)
            report = self._verifier.finalize()
            self.result.report = report
            self.result.cancelled = self._cancelled
            self.result.text = self._verifier.text
            self._emit(GuardEventType.FINALIZED)

            if self._cancelled and self.mode is GuardMode.STRICT:
                # Surface the violation as a hard error so callers don't
                # silently consume a partial output.
                raise GuardedStreamViolation(
                    f"GuardedStream cancelled — {len(self._pending_violations)} "
                    f"violation(s) at or above {self.block_severity.value}.",
                    partial_text=self.result.text,
                    report=report,
                    violations=list(self._pending_violations),
                )

    # ─── Violation handling ───────────────────────────────────

    def _handle_violations(
        self, violations: list[Violation], *, post_stream: bool = False
    ) -> Iterator[str]:
        for v in violations:
            self._emit(GuardEventType.VIOLATION, violation=v)
            if not self._should_act_on(v):
                continue
            self._pending_violations.append(v)
            if self.mode is GuardMode.STRICT and not post_stream:
                self._cancelled = True
                return  # stop yielding further chunks this round
            if self.mode is GuardMode.ANNOTATE:
                marker = self._build_annotation(v)
                self.result.annotations_inserted += 1
                self._emit(GuardEventType.ANNOTATION, text=marker)
                yield marker
            # WARN mode is event-only; nothing to yield.

    def _should_act_on(self, v: Violation) -> bool:
        """Map severity to the configured block threshold."""
        order = {
            Severity.LOW: 0,
            Severity.MEDIUM: 1,
            Severity.HIGH: 2,
            Severity.CRITICAL: 3,
        }
        return order[v.severity] >= order[self.block_severity]

    def _build_annotation(self, v: Violation) -> str:
        return self.annotation_template.format(
            kind=v.kind,
            severity=v.severity.value,
            detail=(v.detail[:140] + "…") if len(v.detail) > 140 else v.detail,
        )

    # ─── Event helpers ────────────────────────────────────────

    def _emit_chunk(self, chunk: str) -> None:
        self._emit(GuardEventType.CHUNK, text=chunk)

    def _emit(
        self,
        type_: GuardEventType,
        *,
        text: str = "",
        violation: Violation | None = None,
    ) -> None:
        try:
            self._on_event(GuardEvent(type=type_, text=text, violation=violation))
        except Exception:
            # Event hooks must NEVER break the stream. A failed
            # observability sink is a deployment issue, not a brief-
            # generation issue.
            pass

    def _close_producer(self, producer: Iterator[str]) -> None:
        """Best-effort cancellation of the upstream generator.

        Python generators have a `close()` method that raises
        GeneratorExit on the next iteration — backends like
        OpenAIClient.stream() wrap that in a try/finally that closes
        the httpx connection. For non-generator iterables (e.g. a
        list, in tests), close() is a no-op.
        """
        close = getattr(producer, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
