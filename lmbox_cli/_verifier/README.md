# Citation Verifier

Anti-hallucination guardrail for LMbox agents producing legal /
regulatory output. Catches invented arrêts, code articles, lois,
décrets, EU texts and out-of-dossier piece references **after** the
agent has generated, before the output reaches a lawyer.

See `docs/adr/002-citation-verifier.md` for the full design rationale.

## Quick start

```python
from lmbox_cli._verifier import verify

report = verify(
    text="""Le contrat (Pièce n° 99) prévoit ...
            Voir Cass. Com., 12 jav 2024 sur ce point.""",
    pieces=["1", "2", "3"],
    check_external=True,
)
if not report.ok:
    for v in report.violations:
        print(v.severity, v.kind, v.citation.raw)
```

```bash
$ lmbox agent verify out.md --pieces 1,2,3 --export-report audit.json
```

## Module layout

| File             | Role                                                       |
|------------------|------------------------------------------------------------|
| `extractor.py`   | 11 regex citation families + multi-piece expansion         |
| `legifrance.py`  | OAuth2, LRU cache, token-bucket, 6 lookups + EUR-Lex HEAD  |
| `verifier.py`    | Orchestrator, severity model, dispatch table               |
| `streaming.py`   | Incremental `StreamingVerifier` + `verify_stream` generator|

## Adding a citation family

1. Define a compiled regex in `extractor.py` next to its siblings.
2. Add a `CitationKind` enum entry + relevant `Citation` fields.
3. Add a `for m in _MY_PATTERN.finditer(text):` block in
   `find_citations()`.
4. Add a `lookup_my_family()` in `legifrance.py` with
   `@functools.lru_cache(maxsize=512)`.
5. Wire the dispatch in `verifier._dispatch_lookup`.
6. Add a row to the `--list-checks` table in `commands/verify.py`.
7. Tests : extractor case + dispatch case + (optionally) cache hit.

## Severity → exit-code matrix

| `--severity` | Fails on                       |
|--------------|--------------------------------|
| `low`        | LOW + MEDIUM + HIGH + CRITICAL |
| `medium`     | MEDIUM + HIGH + CRITICAL       |
| `high` *def* | HIGH + CRITICAL                |
| `critical`   | CRITICAL only                  |

## Environment

| Variable                       | Default | Purpose                          |
|--------------------------------|---------|----------------------------------|
| `LEGIFRANCE_CLIENT_ID`         | –       | OAuth2 client_id for PISTE API   |
| `LEGIFRANCE_CLIENT_SECRET`     | –       | OAuth2 client_secret             |
| `LMBOX_VERIFIER_TIMEOUT`       | `15`    | Per-request HTTP timeout (s)     |
| `LMBOX_VERIFIER_RETRIES`       | `3`     | Retries on transient errors      |
| `LMBOX_VERIFIER_RPM`           | `90`    | Token-bucket capacity (req/min)  |

Without creds, the verifier still runs : every external citation
emits `MEDIUM unverifiable`, every malformed citation emits
`CRITICAL`. Useful for CI + offline demo.

## Streaming

```python
from lmbox_cli._verifier import StreamingVerifier

sv = StreamingVerifier(pieces=["1", "2"], check_external=False)
for token in agent.stream():
    for violation in sv.feed(token):
        ui.flash_warning(violation)
report = sv.finalize()
```

The streaming verifier holds a citation back until ≥ 40 chars of
trailing context OR a sentence terminator is observed, so half-emitted
tokens don't fire false positives. The final pass (`finalize()`)
flushes the tail + runs the malformed-citation structural check.

A parametrised parity test (`tests/test_verifier_streaming.py`) asserts
that streaming and batch produce identical reports on the same input.

## Tests

```bash
pytest tests/test_verifier.py tests/test_verifier_streaming.py \
       tests/test_verify_command.py -q
```

Currently 75+ cases covering : every citation family, multi-piece
ranges + lists, severity dispatch, LRU cache hits, token bucket,
code-label resolver, streaming dedup + parity, CLI flags + exit codes.
