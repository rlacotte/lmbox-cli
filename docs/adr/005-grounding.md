# ADR-005 — Grounding (Layer D "à fond")

| | |
|---|---|
| Status | Accepted |
| Date | 2026-05-13 |
| Decider | LMbox core team |

## Context

Layers A/B/C close most of the obvious hallucination surfaces — fake
citations, drifted shapes. But one class remains :

> The agent claims `source_id: "interne-2019-453"` in its output.
> The citation looks valid (the format passes Layer A). The shape
> validates (Layer C). The string was even emitted before the
> stream finished (Layer B is happy).
>
> But the agent NEVER called the tool that would have returned
> `interne-2019-453`. The source_id is invented.

This is the most insidious failure mode for a legal agent : the
agent fabricates a *retrieval result* that looks like everything
else in the brief. Layer A can't catch it (the citation isn't a
French jurisprudence reference, it's an opaque internal ID).

The architectural fix : every claimed source_id must trace back to
a tool call that actually returned it.

## Decision

We ship `lmbox_cli._grounding` with three pieces :

### 1. `GroundingTracer`

A per-run, in-memory recorder of tool calls and their returned
source_ids. The agent runtime (on the LMbox appliance) feeds it :

```python
tracer.record_tool_call(
    name="search_dossiers_internes",
    args={"query": "non-concurrence"},
    returned_source_ids=["interne-2019-453", "interne-2021-712"],
)
```

The tracer keeps :
- a **set** of all source_ids ever returned (O(1) membership)
- a **list** of every call (audit trail, even calls returning zero)

Thread-unsafe by design — one tracer per request.

### 2. `enforce_grounding(output, tracer, mode, source_id_paths)`

Walks the configured dotted paths in the final structured output,
collects every claimed source_id, and asserts each one is in the
tracer's set. Mismatches → `GroundingViolation` (severity CRITICAL).

**Path syntax**. Each path is dot-separated; atoms are :

- `*` → iterate every element of an array (or, fallback, every
  value of an object)
- `ident` → descend into that key

Examples in production manifests :

```yaml
spec:
  grounding:
    mode: strict
    source_id_paths:
      - "precedents.*.source_id"
      - "references.*.source_id"
      - "sections.*.cited.*.source_id"
```

We don't ship full RFC 9535 JSONPath because (a) it brings ambiguity
on filter semantics, (b) jsonpath-ng adds ~2 MB of deps, (c) the
3-atom subset covers every real LMbox agent schema we have or have
on the partner roadmap.

### 3. Modes

| Mode    | Behavior                                                          |
|---------|-------------------------------------------------------------------|
| strict  | CRITICAL violations block the run (exit 1, output rejected)       |
| warn    | Violations are reported but never block — rollout / canary phase  |
| off     | No enforcement at all (free-form agents like Meeting Summarizer)  |

### Manifest extension

`spec.grounding` is now a first-class block in `agent_v1.schema.json`
with strict validation : `mode` enum + `source_id_paths` array of
strings matching `^[A-Za-z_][A-Za-z0-9_*.]*$`.

### CLI surface (`lmbox agent check-grounding`)

```
lmbox agent check-grounding <agent>
  --output ./run/output.json
  --trace  ./run/trace.json
  [--mode strict|warn|off]   # override manifest's mode
  [--json]
```

The trace file format is the natural serialization of the tracer :
a list of `{name, args, returned_source_ids}` objects. Partner
pipelines emit it from their appliance-side runtime and pass it
to this command as the final compliance gate.

Exit codes : `0` clean OR warn/off, `1` strict blocking, `2` operator
error.

## Consequences

### Positive

- The "invented source" hallucination class is now catchable
  deterministically. No model trickery, no probability threshold —
  the source_id is either in the captured set or it isn't.
- The trace file is the primary-source artifact a regulator wants :
  "show me which tool calls happened, what they returned, and what
  was claimed in the final output." We hand them that file.
- Path syntax stays simple. Partner schemas (Conclusions Drafter,
  JurisRecall, NDA Reviewer) all express their source_id locations
  in 1-3 paths each.

### Negative / accepted trade-offs

- The runtime MUST capture tool-call results. Agents that don't
  use the LMbox tracer (because they integrate through a different
  runtime) need a wrapper. We provide the type + the API; the
  appliance hooks it up. This ADR doesn't ship the appliance-side
  capture — that's runtime team's territory.
- Source_id mismatch on a non-string field (e.g. integer) is
  silently skipped. We treat that as a Layer C problem (schema
  enforcement), not a Layer D problem (semantic grounding).
- Wildcard `*` over an object iterates every value. Useful escape
  hatch for free-form schemas; can produce noisy false-positives
  on confusing schemas. Operators are advised to use specific paths.

## References

- `lmbox_cli/_grounding/tracer.py` — `GroundingTracer` + `ToolCall`
- `lmbox_cli/_grounding/enforcer.py` — `enforce_grounding` + walker
- `lmbox_cli/schema/agent_v1.schema.json` — `spec.grounding` block
- `lmbox_cli/commands/check_grounding.py` — CLI
- `tests/test_grounding.py` (19 cases) — walker + tracer + enforcer
- `tests/test_check_grounding_command.py` (9 cases) — CLI
