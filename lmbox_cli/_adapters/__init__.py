"""Kernel adapters — translate a neutral LMbox manifest into the native
artefact a runtime kernel expects (OpenClaw, Hermes, future LMbox-native).

The CLI talks only to the `KernelAdapter` protocol, never to a concrete
kernel. To swap kernels later, write a new adapter and register it in
`AVAILABLE_ADAPTERS` — partners' manifests + skills don't change.
See ADR-001 for the full rationale.
"""

from lmbox_cli._adapters.base import BuildResult, KernelAdapter
from lmbox_cli._adapters.openclaw import OpenClawAdapter

AVAILABLE_ADAPTERS: dict[str, type[KernelAdapter]] = {
    "openclaw": OpenClawAdapter,
}

__all__ = ["AVAILABLE_ADAPTERS", "BuildResult", "KernelAdapter", "OpenClawAdapter"]
