# ADR-003 — Runtime Enforcement (Layer B "à fond")

| | |
|---|---|
| Status | Accepted |
| Date | 2026-05-13 |
| Decider | LMbox core team |

## Context

ADR-002 (Layer A — Citation Verifier) gives us a deterministic
post-hoc pass/fail on a finished agent output. That's enough for
batch flows (`lmbox agent test --strict`) but not for live agent
runs where the avocat is *watching* tokens stream into the UI. By
the time Layer A speaks, the bad citation has already been read.

Two production needs that ADR-002 alone doesn't cover :

1. **Stop the bleeding mid-generation.** A 4-page brief with a
   hallucinated `Cass. Com., 12 jav 2024` in paragraph 1 should
   not waste 30 s of inference. Cancel, alert, retry.
2. **Surface the suspicious citation in-context.** Even if we
   don't cancel, the human reviewer needs to see the marker
   next to the offending text, not buried in a report 4 screens
   below.

This ADR is the design of the runtime enforcement layer.

## Decision

We ship a `GuardedStream` class in `lmbox_cli._runtime` that wraps
**any iterator of `str` chunks** (an LLM stream, a file replay, a
test list) with the Layer A `StreamingVerifier` and three operator-
chosen behaviors.

### Three modes

| Mode       | Behavior on a violation at/above `block_severity`           |
|------------|-------------------------------------------------------------|
| `strict`   | Stop iterating. Call `.close()` on the producer (if any).   |
|            | Raise `GuardedStreamViolation` with partial text + report.  |
| `annotate` | Insert an inline `[⚠ HALLUCINATION SUSPECTÉE: …]` marker    |
|            | after the chunk that completed the offending citation.      |
|            | Continue generating.                                        |
| `warn`     | Pass-through. Fire `GuardEventType.VIOLATION` events for    |
|            | side-channel observability. The stream is never modified.   |

`block_severity` (default `HIGH`) sets which Layer A severities
trigger the chosen action. CRITICAL violations always trigger.
MEDIUM/LOW never trigger unless explicitly lowered (`--block-severity
medium` for paranoid rollouts).

### LLM client integration

`OpenAIClient.stream(req)` was added in the same change. It wraps
the existing `complete()` payload with `stream: True`, parses the
OpenAI-compatible SSE event stream, and yields `delta.content`
chunks one at a time. The httpx connection is closed on caller
cleanup — generator `close()` triggers `try/finally` which closes
the underlying HTTP connection, so strict-mode cancellation actually
releases the upstream model.

### Event model

Every chunk + every violation + every annotation + cancellation
emits a `GuardEvent`. The optional `on_event` hook is fed each
event in order. Used by :

- The CLI `lmbox agent run` command to build the structured audit
  trail JSON (`--export-audit`).
- Partner pipelines that want to publish to their own observability
  stack (Datadog, Grafana Tempo).

Event handlers that raise are silently swallowed — a failed
observability sink must NEVER break a brief generation.

### CLI surface (`lmbox agent run`)

```
lmbox agent run <agent>
  --input "..."   |   --input-file path   |   (stdin)
  --pieces "1,2,3,7"           # for Layer A piece check
  --guard strict|annotate|warn # Layer B mode (default: warn)
  --block-severity crit|high|med|low
  --no-external                # offline (skip Légifrance)
  --model <name>               # override manifest's model
  --export-audit <path>        # JSON trail (events + final report)
```

Exit codes : `0` pass, `1` blocking violation or strict cancellation,
`2` operator error.

## Consequences

### Positive

- The cabinet's avocat sees the verdict in real time. Live demos
  go from "trust me on the report at the end" to "watch the brief
  fail in 200 ms" — much more visceral, much more credible.
- Strict cancellation saves money on small CPU backends (a
  Mistral-7B brief is 30 s of token generation we now cancel
  after the first hallucination).
- Pure Python, no fancy decoding tricks, runs on any OpenAI-
  compatible backend (Ollama, LiteLLM, vLLM, cloud).

### Negative / accepted trade-offs

- We act on **complete** citations, not partial ones. A citation
  is held back until 40 chars of trailing text settle it — so the
  *very first* characters after a hallucination still reach the
  consumer. Annotate mode is the recovery surface.
- Strict mode terminates the brief; some agents (Meeting
  Summarizer) prefer best-effort completion. Operators choose mode
  per agent / per run via `--guard`.
- We do **not** try grammar-constrained decoding here. That's a
  different lever (GBNF / Outlines) and would tie us to specific
  backends. The guard works the same on every OpenAI-compatible
  endpoint — that portability matters for the appliance use case.

## References

- `lmbox_cli/_runtime/guarded_stream.py` — guard + modes + events
- `lmbox_cli/_llm.py` — `OpenAIClient.stream()` SSE consumer
- `lmbox_cli/commands/run.py` — `lmbox agent run` CLI
- `tests/test_runtime_guard.py` (14 cases) — unit tests for modes,
  cancellation, event hook isolation
- `tests/test_run_command.py` (8 cases) — CLI integration
