"""GroundingEnforcer — verify every claimed source_id was retrieved.

After the LLM emits its final output (the structured JSON validated
by Layer C), the enforcer :

  1. Walks the configured `source_id_paths` (dotted paths with `*`
     for arrays).
  2. Collects every claimed source_id at those locations.
  3. Asserts each claimed source_id is in the tracer's captured set.
  4. Reports each mismatch as a `GroundingViolation` with severity
     CRITICAL (claim references something the agent never retrieved).

Path syntax
───────────
Each path is a dot-separated string. The atoms :

    `foo`   match the key `foo` in the current object
    `*`     match every element of the current array (or, if used
            on an object, every value of every key — emergency
            fallback for free-form schemas)

Examples in production manifests :

    spec:
      grounding:
        mode: strict
        source_id_paths:
          - "precedents.*.source_id"
          - "references.*.source_id"
          - "sections.*.cited.*.source_id"

Why not jsonpath-ng?
────────────────────
The full JSONPath spec has ambiguous filter semantics (RFC 9535 is
permissive on edge cases), and the library adds ~2 MB of deps. The
3-atom subset above covers every production agent schema we ship
or have on the partner roadmap, and is trivial to unit-test.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum

from lmbox_cli._grounding.tracer import GroundingTracer


class GroundingMode(str, Enum):
    """How the runtime reacts to a grounding violation."""

    STRICT = "strict"   # CRITICAL violations fail the run
    WARN = "warn"       # report only, never blocks
    OFF = "off"         # no-op, skip enforcement entirely


@dataclass(frozen=True)
class GroundingViolation:
    """One claimed source_id that doesn't match a retrieved one."""

    path: str            # dotted path inside the output, e.g. "precedents.0.source_id"
    claimed_source_id: str
    detail: str = ""

    @property
    def severity(self) -> str:
        # All grounding violations are CRITICAL by construction — the
        # agent claimed something it never retrieved. We keep the
        # `severity` attribute for symmetry with the other layers'
        # Violation records.
        return "critical"


@dataclass
class GroundingReport:
    """Aggregated outcome of a grounding check."""

    mode: GroundingMode = GroundingMode.OFF
    claimed_source_ids: list[str] = field(default_factory=list)
    retrieved_source_ids: list[str] = field(default_factory=list)
    violations: list[GroundingViolation] = field(default_factory=list)
    skipped_paths: list[str] = field(default_factory=list)  # paths that resolved to nothing

    @property
    def ok(self) -> bool:
        return not self.violations

    def should_block(self) -> bool:
        """True iff the report contains violations AND the mode says block."""
        return self.violations and self.mode is GroundingMode.STRICT


# ─── Public API ──────────────────────────────────────────────────


def enforce_grounding(
    output: dict | list,
    tracer: GroundingTracer,
    *,
    mode: GroundingMode = GroundingMode.STRICT,
    source_id_paths: Iterable[str] = (),
) -> GroundingReport:
    """Walk the output, collect claimed source_ids, compare to tracer.

    `output` is the already-parsed JSON the LLM emitted (after Layer C
    validation). `source_id_paths` is the list of dotted paths to
    walk. Empty paths list → no enforcement, but a warning skipped
    path is logged on the report so configuration drift is visible.
    """
    report = GroundingReport(
        mode=mode,
        retrieved_source_ids=sorted(tracer.source_ids),
    )

    if mode is GroundingMode.OFF:
        return report

    if not source_id_paths:
        # Configuration drift: grounding mode is on but no path declared.
        report.skipped_paths.append("(aucun chemin déclaré)")
        return report

    for path in source_id_paths:
        collected = list(_walk_path(output, path))
        if not collected:
            report.skipped_paths.append(path)
            continue
        for value, resolved_path in collected:
            if not isinstance(value, str) or not value:
                # The path resolved to a non-string — caller's schema
                # is inconsistent. We don't try to be clever.
                continue
            report.claimed_source_ids.append(value)
            if not tracer.has_source_id(value):
                report.violations.append(
                    GroundingViolation(
                        path=resolved_path,
                        claimed_source_id=value,
                        detail=(
                            f"L'agent prétend citer la source `{value}` "
                            "mais aucun appel d'outil n'a retourné ce "
                            "source_id pendant cette exécution. "
                            "Hallucination par invention de source."
                        ),
                    )
                )

    return report


class GroundingEnforcer:
    """Stateful wrapper for runtimes that prefer an object API.

    Most callers can use `enforce_grounding(...)` directly. The class
    is useful when the runtime wants to attach event hooks (logging,
    metrics) the same way GuardedStream does."""

    def __init__(
        self,
        tracer: GroundingTracer,
        *,
        mode: GroundingMode = GroundingMode.STRICT,
        source_id_paths: Iterable[str] = (),
    ) -> None:
        self.tracer = tracer
        self.mode = mode
        self.source_id_paths = tuple(source_id_paths)

    def check(self, output: dict | list) -> GroundingReport:
        return enforce_grounding(
            output,
            self.tracer,
            mode=self.mode,
            source_id_paths=self.source_id_paths,
        )


# ─── Path walker ─────────────────────────────────────────────────


def _walk_path(node, path: str):
    """Yield (value, resolved_dotted_path) for every match of `path`
    starting at `node`.

    `path` is a dot-separated list of atoms. Atoms :
        *       → iterate every element of an array, OR every value
                  of an object (for free-form schemas)
        ident   → descend into that key of an object

    Implementation is a small recursive walker rather than a parser
    because the grammar is too simple to deserve one."""
    atoms = [a for a in path.split(".") if a]
    yield from _walk_atoms(node, atoms, "")


def _walk_atoms(node, atoms: list[str], resolved: str):
    if not atoms:
        yield node, resolved
        return
    head, rest = atoms[0], atoms[1:]
    if head == "*":
        if isinstance(node, list):
            for i, item in enumerate(node):
                new_resolved = f"{resolved}.{i}" if resolved else str(i)
                yield from _walk_atoms(item, rest, new_resolved)
        elif isinstance(node, dict):
            for k, v in node.items():
                new_resolved = f"{resolved}.{k}" if resolved else k
                yield from _walk_atoms(v, rest, new_resolved)
        # else: scalar — wildcard expansion gets nothing
        return
    # Named key — descend if it's a dict
    if isinstance(node, dict) and head in node:
        new_resolved = f"{resolved}.{head}" if resolved else head
        yield from _walk_atoms(node[head], rest, new_resolved)
