# ADR-002 — Citation Verifier (Layer A "à fond")

| | |
|---|---|
| Status | Accepted |
| Date | 2026-05-13 |
| Decider | LMbox core team |

## Context

Every LMbox agent that produces legal / regulatory output (NDA
Reviewer, Conclusions Drafter, JurisRecall, KYB Monitor, Meeting
Summarizer with legal addendum) is exposed to the same failure
mode : the LLM invents a citation that *looks* canonical but doesn't
correspond to any real arrêt, code article, loi, décret or EU text.
The damage in a cabinet d'avocat is asymmetric : one fabricated
`Cass. Com., 12 jav 2024` slipping into a conclusion is enough to
destroy a partner's confidence in the whole stack.

Prompt-engineering alone reduces but does not eliminate the issue,
particularly on the smaller models we ship by default (Mistral-7B,
Gemma-2 9B). We need a post-hoc, deterministic verifier that an
operator can trust as a hard gate before output reaches a lawyer.

This ADR is the design of that verifier. It explicitly excludes
runtime-enforcement layers (B), structured-output schemas (C) and
architectural retrieval-only patterns (D) — those are tracked in
their own ADRs. We invest in **A à fond** because :

1. It is the most leverageable layer : every agent benefits the same
   day, without any prompt or tool rewrite.
2. It is the most demonstrable layer to a partner : we can show a
   live red-light/green-light on a brief during a 30-minute pitch.
3. It is the most defensible layer to a regulator : a chained
   audit log of « citation Y, looked up against Légifrance ID X, at
   timestamp T » is a primary-source attestation, not a vibe check.

## Decision

The verifier is a pure-stdlib Python module `lmbox_cli._verifier`
exposing :

```python
from lmbox_cli._verifier import (
    verify,              # batch / one-shot
    StreamingVerifier,   # incremental for live UIs
    verify_stream,       # generator wrapper around the above
    Severity,            # LOW / MEDIUM / HIGH / CRITICAL
    Violation,           # immutable record
    VerificationReport,  # aggregated result
)
```

### Citation families (11)

| Kind             | Pattern source                                       | External oracle      |
|------------------|------------------------------------------------------|----------------------|
| CASSATION        | `Cass. Com., 12 janvier 2024, n° 22-15.487`          | Légifrance JURI      |
| COUR_APPEL       | `CA Paris, 14 mars 2023, n° 21/04567`                | Légifrance JURI *    |
| CONSEIL_ETAT     | `CE, 5 avril 2024, n° 472385`                        | Légifrance CETAT     |
| CONSEIL_CONST    | `Cons. const., 12 mai 2023, n° 2023-1042 DC`         | Légifrance CONSTIT   |
| ARTICLE_CODE     | `article L. 1121-1 du Code du travail`               | Légifrance CODE      |
| LOI              | `loi n° 2024-1234 du 15 mars 2024`                   | Légifrance JORF      |
| DECRET           | `décret n° 2024-456 du 20 mai 2024`                  | Légifrance JORF      |
| EU_REGLEMENT     | `règlement (UE) 2016/679` → CELEX `32016R0679`       | EUR-Lex (HEAD)       |
| EU_DIRECTIVE     | `directive 2019/770/UE` → CELEX `32019L0770`         | EUR-Lex (HEAD)       |
| PIECE_INTERNE    | `Pièce n° 7`, `Pièces n°s 4 à 7`, `Pièces 4, 5 et 12`| Dossier inventory    |
| MALFORMED        | `Cass. Com., 12 jav 2024` (invalid month, etc.)      | Structural only      |

\* CA NOT_FOUND is downgraded to UNVERIFIABLE due to known coverage
gaps on judicial appeals in Légifrance.

### Severity model

| Severity   | When                                                             | CLI default exit |
|------------|------------------------------------------------------------------|------------------|
| CRITICAL   | Citation NOT_FOUND on its oracle, OR malformed structure.        | fail             |
| HIGH       | `Pièce n° X` referenced but X not in the provided dossier list.  | fail             |
| MEDIUM     | UNVERIFIABLE (creds missing, API down, CA coverage gap, etc.).   | pass             |
| LOW        | Informational. Not emitted by the current set of checks.         | pass             |

The CLI exposes `--severity {critical,high,medium,low}` to tune the
exit code (default `high` — i.e., HIGH + CRITICAL fail; MEDIUM does
not).

### Reliability primitives

| Concern              | Mechanism                                                   |
|----------------------|-------------------------------------------------------------|
| Auth                 | OAuth2 client_credentials cached 50 min, thread-safe        |
| Rate limit           | Token bucket, 90 req/min default (under 100 free tier)      |
| Transient errors     | 3 retries, exponential backoff + jitter, cap 8 s            |
| Duplicate citations  | `functools.lru_cache(512)` per lookup function              |
| Hard timeout         | 15 s per request (env-tunable `LMBOX_VERIFIER_TIMEOUT`)     |
| Dependency surface   | Pure stdlib — `urllib` + `json` + `threading`               |

### Streaming model

`StreamingVerifier.feed(chunk)` returns NEW violations as the buffer
grows. A citation is held back until either ≥ 40 chars of trailing
text are seen OR a sentence terminator is hit — this avoids matching
half-emitted tokens. `finalize()` flushes the tail and runs the
malformed-citation pass.

For the same input, the streaming and batch paths produce the same
report (asserted by a parametrised parity test).

### CLI surface

```
lmbox agent verify <path>
  --pieces "1,2,3,7"          # required to verify internal Pièce n° X
  --no-external               # skip Légifrance + EUR-Lex (offline mode)
  --severity {crit|high|med|low}
  --export-report <path>      # archive JSON report (audit trail)
  --json                      # JSON to stdout instead of Rich table
  --list-checks               # discover supported families + sources
```

Exit codes : `0` pass, `1` fail at the chosen severity, `2` operator
error.

`lmbox agent test --strict` runs the verifier on every golden case ;
a HIGH/CRITICAL violation fails the case even if the textual
assertions pass.

## Consequences

### Positive

- Partners writing pipelines have a hard, deterministic gate they
  can wire into a CI/CD or pre-send step. No vibe-checking.
- The audit trail becomes primary-source : every CRITICAL has a
  Légifrance / EUR-Lex URL attached (or a "no creds, unverifiable"
  marker the regulator can audit).
- Adding a citation family in the future is mechanical : one regex
  in `extractor.py`, one lookup function in `legifrance.py`, one
  dispatch branch in `verifier._dispatch_lookup`.

### Negative / accepted trade-offs

- We do not attempt to verify the **semantic** correctness of a
  citation (does this arrêt say what the agent claims it says).
  That is reserved for layer C (structured tool-call traces).
- Coverage of pre-2000 jurisprudence on Légifrance is patchy. Our
  CA downgrade reflects this ; for other juridictions we surface
  NOT_FOUND honestly and let the operator decide.
- Légifrance API outage degrades the verifier to MEDIUM-only.
  Acceptable : we never silently pass — operators see the
  unverifiable signal and can re-run.

## References

- `lmbox_cli/_verifier/extractor.py` — regex extraction, 11 families
- `lmbox_cli/_verifier/legifrance.py` — OAuth + lookups + reliability
- `lmbox_cli/_verifier/verifier.py` — orchestrator + dispatch
- `lmbox_cli/_verifier/streaming.py` — incremental verifier
- `lmbox_cli/commands/verify.py` — CLI surface
- `tests/test_verifier.py`, `tests/test_verifier_streaming.py`,
  `tests/test_verify_command.py` — 75+ unit + integration cases
