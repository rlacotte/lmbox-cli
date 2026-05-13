"""GroundingTracer — capture source_ids returned by agent tool calls.

The tracer sits between the agent runtime and the LLM. Every time the
agent calls a tool (`search_jurisprudence`, `lookup_dossier`, etc.)
and gets back a list of documents, the tracer records each returned
`source_id`. At the end of the run, the enforcer compares this set
against the source_ids the LLM emitted in its final output.

This module is intentionally agnostic about which tool framework is
in use (OpenClaw, LangChain, raw OpenAI function-calling). The tracer
exposes :

  - `record_tool_call(name, args, returned_source_ids)` for the
    runtime to call when a tool finishes
  - `source_ids` to read the accumulated set
  - `calls` for the audit trail (every call, even those that
    returned zero documents)

The tracer is thread-unsafe by design — agents run sequentially in
a single thread per request. Multi-tenant fan-out uses one tracer
per request.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ToolCall:
    """One captured tool invocation. Stored verbatim for the audit log."""

    name: str
    args: dict
    returned_source_ids: tuple[str, ...] = ()


class GroundingTracer:
    """Capture source_ids returned by an agent's tool calls.

    Usage in an agent runtime ::

        tracer = GroundingTracer()
        for step in agent.steps:
            if step.is_tool_call:
                result = step.tool.execute(step.args)
                tracer.record_tool_call(
                    name=step.tool.name,
                    args=step.args,
                    returned_source_ids=[d["source_id"] for d in result.docs],
                )
        # later, when validating the final output :
        report = enforce_grounding(final_output, tracer, mode=GroundingMode.STRICT,
                                   source_id_paths=["precedents.*.source_id"])
    """

    def __init__(self) -> None:
        self._calls: list[ToolCall] = []
        self._source_ids: set[str] = set()

    # ─── Capture ──────────────────────────────────────────────

    def record_tool_call(
        self,
        name: str,
        args: dict | None = None,
        returned_source_ids: Iterable[str] = (),
    ) -> ToolCall:
        """Record one tool invocation and its returned source_ids.

        Idempotent only at the granularity of a call — recording the
        same call twice records two ToolCall entries, but the
        underlying source_ids set deduplicates naturally.
        """
        ids = tuple(s for s in returned_source_ids if s)
        call = ToolCall(
            name=name, args=dict(args or {}), returned_source_ids=ids
        )
        self._calls.append(call)
        self._source_ids.update(ids)
        return call

    # ─── Read ─────────────────────────────────────────────────

    @property
    def source_ids(self) -> set[str]:
        """Every source_id ever returned by a tool call in this run."""
        return set(self._source_ids)

    @property
    def calls(self) -> list[ToolCall]:
        """Full audit trail of tool invocations (read-only copy)."""
        return list(self._calls)

    def has_source_id(self, source_id: str) -> bool:
        """O(1) membership check used by the enforcer."""
        return source_id in self._source_ids

    # ─── Reset ────────────────────────────────────────────────

    def reset(self) -> None:
        """Wipe the captured state. Mostly useful for tests + for
        runtimes that reuse a tracer across requests (not recommended
        in production)."""
        self._calls.clear()
        self._source_ids.clear()
