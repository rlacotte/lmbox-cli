"""Adapter base — the contract every kernel implementation must follow.

A `KernelAdapter` knows how to take a LMbox manifest (parsed dict) and
emit the kernel's native artefact set inside a build directory. The
build directory is fully owned by the adapter — partners never touch
the contents directly, they touch their `manifest.yaml` and run
`lmbox agent build`.

Why not abstract base class?
────────────────────────────
Protocol typing gives us:
- Duck typing (no inheritance imposed on third-party adapters)
- Structural compatibility so adapter classes can be exchanged
  without registering with a base class
- Static type-checkers can still verify conformance

The trade-off is that we don't get free dispatch on adapter type
(no isinstance check works the way you'd want). For three or four
known adapters that's a non-issue — the dispatch happens at the
CLI level via the `AVAILABLE_ADAPTERS` registry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class BuildResult:
    """What an adapter produced after `compile()`.

    The paths are absolute so the CLI can print them as-is. `kernel`
    + `version` are used for compatibility checks when the artefact
    later reaches a Box (the box's installed kernel version must be
    >= the version the artefact was built for).
    """

    kernel: str  # 'openclaw', 'hermes', ...
    kernel_min_version: str  # min runtime version required
    artefact_dir: Path  # where the build landed
    files: list[Path]  # every file produced, for tooling
    warnings: list[str] = field(default_factory=list)


class KernelAdapter(Protocol):
    """Compile a LMbox manifest into a kernel-native artefact set.

    Implementations live next to this file (`openclaw.py`, etc.) and
    are registered in `AVAILABLE_ADAPTERS`. The adapter is stateless
    — it can be instantiated with no args, and `compile()` is the
    only public method the CLI ever calls.
    """

    name: str
    """Stable identifier (e.g. 'openclaw'). Used by the CLI to map
    `--kernel openclaw` to the adapter class."""

    def compile(
        self,
        manifest: dict[str, Any],
        *,
        agent_dir: Path,
        output_dir: Path,
    ) -> BuildResult:
        """Build the artefact set into `output_dir`.

        Args:
          manifest:   The parsed `manifest.yaml` (already schema-validated
                      by the caller — adapters trust the shape).
          agent_dir:  Where the agent project lives on disk; used to
                      resolve relative paths like `prompts/system.md`.
          output_dir: Where to write artefacts. Caller has already
                      cleared it.

        Returns:
          A `BuildResult` describing what landed.

        Raises:
          AdapterError on anything the adapter cannot reconcile
          (e.g. a manifest tool of type `shell` that the kernel
          forbids in production). Caller turns this into a clean
          CLI error.
        """
        ...


class AdapterError(Exception):
    """Raised by an adapter when the manifest cannot be compiled."""
