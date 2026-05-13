"""Grounding layer (Layer D) — every claim must trace to a retrieved
source.

The premise
───────────
Layers A/B/C catch hallucinated *strings* (a citation that doesn't
exist in Légifrance, a `Pièce n° 99` that's not in the dossier). But
the agent can still hallucinate *content* — it can attribute a real
arrêt to the wrong holding, or paraphrase a precedent that says the
opposite of what the agent claims.

Layer D closes that gap by an architectural constraint :

  > Every `source_id` field in the final structured output MUST
  > match a source_id that the agent actually retrieved during the
  > run (via a tool call to `search_*` / `lookup_*`).

If the agent emits `source_id: "interne-2019-453"` but never called
a tool that returned `interne-2019-453`, the source is invented.

How
───
1. The agent runtime captures every tool-call result via a
   `GroundingTracer`. Each call's returned documents contribute
   their `source_id`s to a per-run set.

2. After the LLM produces the final output, the `GroundingEnforcer`
   reads it (parsed JSON), walks the configured `source_id_paths`,
   collects every claimed source_id, and asserts each one is in
   the tracer's captured set.

3. Mismatches are reported as `GroundingViolation`s with severity :
       CRITICAL  → source_id present in output but never retrieved
       MEDIUM    → source_id_paths is empty / wildcard path matches
                   no field (configuration drift)

4. `mode` on the agent manifest controls the enforcement :
       strict  → CRITICAL → exit non-zero, output rejected
       warn    → reports, never blocks (canary / rollout phase)
       off     → no-op (disabled per agent, e.g. for free-form
                 agents like Meeting Summarizer)

The path syntax
───────────────
Production-grade but minimal. Each path is a dot-separated string
where `*` matches every element of an array, and bare identifiers
match object keys. Examples :

    `source_id`                          → top-level scalar
    `precedents.*.source_id`             → list of precedents
    `sections.*.references.*.source_id`  → nested lists

We deliberately avoid pulling in `jsonpath-ng` (heavy + ambiguous on
ergonomics). The subset above covers >95 % of LMbox agent schemas.
"""

from __future__ import annotations

from lmbox_cli._grounding.enforcer import (
    GroundingEnforcer,
    GroundingMode,
    GroundingReport,
    GroundingViolation,
    enforce_grounding,
)
from lmbox_cli._grounding.tracer import GroundingTracer, ToolCall

__all__ = [
    "GroundingEnforcer",
    "GroundingMode",
    "GroundingReport",
    "GroundingTracer",
    "GroundingViolation",
    "ToolCall",
    "enforce_grounding",
]
