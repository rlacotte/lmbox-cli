"""Structured-output repair loop.

When the LLM returns JSON that doesn't validate against the agent's
declared schema, we re-prompt with the validation errors attached
and ask the model to fix its output. This is bounded :

  - Default `max_attempts = 3` (1 initial + 2 retries).
  - Exponential backoff between attempts (0.5 s → 1 s → 2 s,
    capped at 4 s) so we don't hammer the backend on a misbehaving
    model.
  - Each attempt's prompt + raw response + failures are recorded
    in a `RepairResult` for the audit trail.

If we still don't have a valid output after `max_attempts`, the
caller gets a `StructuredOutputError` carrying the FULL attempt
trail so the operator can see what the model did, exactly.

Why a repair loop instead of grammar-constrained decoding?
──────────────────────────────────────────────────────────
Two reasons :

  1. Portability. Grammar-constrained decoding (GBNF, Outlines,
     LMQL) is backend-specific and not uniformly supported by Ollama,
     LiteLLM and OpenAI cloud endpoints. The repair loop works the
     same on all three.
  2. Observability. Each repair attempt is visible to the operator
     — partners running cabinets need to SEE the model misbehaving
     to decide whether to swap the model or rewrite the prompt.
     A grammar-constrained model fails silently when the grammar
     can't be satisfied.

When the backend supports it (OpenAI cloud `response_format`,
recent Ollama versions), we still pass the schema as a hint via
`response_format=json_schema` — but we never trust it alone.
The validator + repair loop are the contract.

See `docs/adr/004-structured-output.md` for the design rationale.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from lmbox_cli._llm import CompletionRequest, LLMClient
from lmbox_cli._outputs.validator import ValidationFailure, validate_output


class RepairOutcome(str, Enum):
    SUCCESS = "success"
    EXHAUSTED = "exhausted"  # max_attempts reached, still invalid
    LLM_ERROR = "llm_error"  # backend raised before we could validate


@dataclass(frozen=True)
class RepairAttempt:
    """One round of "ask the model + validate" against the schema."""

    attempt: int          # 1-indexed
    raw_response: str
    failures: list[ValidationFailure]
    duration_ms: int
    is_valid: bool


@dataclass
class RepairResult:
    """Aggregated result of running enforce_structured_output.

    `output` is the parsed JSON object on success, None otherwise.
    `attempts` is the FULL trail (including the failed attempts) —
    useful for the audit log and for explaining why a strict run
    failed to the operator."""

    outcome: RepairOutcome
    output: object = None
    attempts: list[RepairAttempt] = field(default_factory=list)
    error_message: str = ""

    @property
    def succeeded(self) -> bool:
        return self.outcome is RepairOutcome.SUCCESS


class StructuredOutputError(RuntimeError):
    """Raised when the repair loop is exhausted and the caller asked
    for strict enforcement.

    Carries the `RepairResult` so the caller can still archive every
    attempt + the final failure for the audit trail."""

    def __init__(self, message: str, result: RepairResult) -> None:
        super().__init__(message)
        self.result = result


# ─── Public entrypoint ───────────────────────────────────────────


def enforce_structured_output(
    client: LLMClient,
    request: CompletionRequest,
    schema: dict,
    *,
    max_attempts: int = 3,
    backoff_base: float = 0.5,
    backoff_cap: float = 4.0,
    on_attempt: Callable[[RepairAttempt], None] | None = None,
) -> RepairResult:
    """Call the model, validate against schema, repair-loop on failure.

    Parameters
    ──────────
    client        : any LLMClient (real OpenAIClient in prod, fake in tests).
    request       : the initial CompletionRequest. We mutate the .user
                    prompt across attempts to attach the repair guidance.
    schema        : JSON Schema (draft 2020-12) to enforce.
    max_attempts  : hard cap on number of model calls. Default 3
                    (1 initial + 2 retries). >=1.
    on_attempt    : optional hook fired after each attempt. Used by the
                    CLI to show live progress + by the audit log.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    attempts: list[RepairAttempt] = []
    current_request = request

    for n in range(1, max_attempts + 1):
        started = time.monotonic()
        try:
            response = client.complete(current_request)
        except Exception as e:  # pragma: no cover — backend-specific
            return RepairResult(
                outcome=RepairOutcome.LLM_ERROR,
                attempts=attempts,
                error_message=f"LLM backend error on attempt {n}: {e!r}",
            )
        elapsed_ms = int((time.monotonic() - started) * 1000)

        raw = response.content
        failures = validate_output(raw, schema)
        is_valid = not failures
        attempt = RepairAttempt(
            attempt=n,
            raw_response=raw,
            failures=failures,
            duration_ms=elapsed_ms,
            is_valid=is_valid,
        )
        attempts.append(attempt)
        if on_attempt is not None:
            try:
                on_attempt(attempt)
            except Exception:
                pass  # observability hook must not break the loop

        if is_valid:
            return RepairResult(
                outcome=RepairOutcome.SUCCESS,
                output=json.loads(raw),
                attempts=attempts,
            )

        # Not valid → prepare the next attempt's prompt
        if n < max_attempts:
            current_request = _build_repair_request(
                request, raw, failures, attempt=n
            )
            # Exponential backoff + cap. Small base so a sub-second
            # local model isn't slowed by 5+ s of sleep.
            time.sleep(min(backoff_base * (2 ** (n - 1)), backoff_cap))

    # Exhausted attempts
    last = attempts[-1] if attempts else None
    summary = (
        f"Sortie structurée invalide après {max_attempts} tentative(s). "
        f"{len(last.failures) if last else 0} erreur(s) résiduelle(s)."
    )
    return RepairResult(
        outcome=RepairOutcome.EXHAUSTED,
        attempts=attempts,
        error_message=summary,
    )


# ─── Internals ───────────────────────────────────────────────────


_REPAIR_PREAMBLE = (
    "\n\n---\n"
    "⚠ Ta sortie précédente n'a PAS validé le schéma JSON requis. "
    "Voici les erreurs détectées :\n\n"
    "{errors}\n\n"
    "Refais ta sortie en respectant STRICTEMENT le schéma. "
    "Ne renvoie QUE le JSON, sans préambule, sans ``` markdown, "
    "sans commentaire."
)


def _build_repair_request(
    original: CompletionRequest,
    raw_response: str,
    failures: list[ValidationFailure],
    *,
    attempt: int,
) -> CompletionRequest:
    """Construct the next attempt's CompletionRequest by appending the
    structured failure list to the user prompt.

    We keep the original system prompt verbatim — the schema-respect
    directive belongs to the user-turn so the system prompt remains
    cacheable across attempts (important for KV-cache reuse on
    backends that support it).
    """
    error_lines = "\n".join(f.as_prompt_line() for f in failures[:10])
    if len(failures) > 10:
        error_lines += f"\n- … ({len(failures) - 10} autre(s) erreur(s) non listée(s))"
    augmented_user = (
        original.user
        + _REPAIR_PREAMBLE.format(errors=error_lines)
        + f"\n\n[Tentative {attempt + 1} de réparation — sois précis.]"
    )
    # `dataclasses.replace` would work but CompletionRequest is frozen
    # so we just reconstruct.
    return CompletionRequest(
        model=original.model,
        system=original.system,
        user=augmented_user,
        temperature=original.temperature,
        max_tokens=original.max_tokens,
    )
