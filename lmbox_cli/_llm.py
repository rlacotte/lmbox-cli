"""Minimal LLM client.

Speaks the OpenAI-compatible `/v1/chat/completions` API — which is
exactly what Ollama (since 0.1.30), LiteLLM, vLLM, and most local
runtimes expose. One client, three backends:

    Endpoint                          | Auth
    ──────────────────────────────────|─────────────────
    http://localhost:11434/v1         | (none — Ollama)
    http://litellm:4000/v1            | master key
    https://api.openai.com/v1         | API key (cloud — not for prod)

The client is intentionally tiny — we only need `complete()` for
the eval runner. No streaming, no tool calls, no async. When we
need those, the `lmbox agent run` command (0.3+) will get its own
richer client.

For testability, the public API is the abstract `LLMClient` protocol.
Tests inject `FakeLLMClient` instead of `OpenAIClient`.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urljoin

import httpx


@dataclass(frozen=True)
class CompletionRequest:
    """Single chat completion call. Keep it dumb on purpose."""

    model: str
    system: str
    user: str
    temperature: float = 0.2
    max_tokens: int = 1024


@dataclass(frozen=True)
class CompletionResponse:
    """What the LLM gave back. Same level of detail across backends."""

    content: str
    model: str  # echo back what was actually used
    prompt_tokens: int = 0
    completion_tokens: int = 0


class LLMClient(Protocol):
    """Operations exposed by the LMbox LLM client.

    `complete` is the one-shot path used by the eval runner. `stream`
    is the incremental path used by the runtime guard (Layer B) — the
    GuardedStream wraps it and intercepts hallucinated citations the
    moment they're emitted, before the full response is buffered.
    """

    def complete(self, req: CompletionRequest) -> CompletionResponse: ...

    def stream(self, req: CompletionRequest) -> Iterator[str]:
        """Yield content chunks as the model generates them.

        Each yielded value is a string (may be empty). Implementations
        MUST close the underlying HTTP connection on caller cleanup —
        the GuardedStream relies on this to cancel generation when
        a CRITICAL violation is detected in strict mode.
        """
        ...


class OpenAIClient:
    """OpenAI-compatible HTTP client (works against Ollama, LiteLLM, vLLM)."""

    def __init__(
        self,
        endpoint: str,
        api_key: str = "",
        *,
        timeout: float = 60.0,
    ) -> None:
        # Normalise the endpoint — accept both "http://x:11434" and
        # "http://x:11434/v1" by ensuring exactly one /v1 suffix.
        e = endpoint.rstrip("/")
        if not e.endswith("/v1"):
            e = e + "/v1"
        self._base = e + "/"
        self._headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}" if api_key else "Bearer none",
        }
        self._timeout = timeout

    def complete(self, req: CompletionRequest) -> CompletionResponse:
        payload = {
            "model": req.model,
            "messages": [
                {"role": "system", "content": req.system},
                {"role": "user", "content": req.user},
            ],
            "temperature": req.temperature,
            "max_tokens": req.max_tokens,
            "stream": False,
        }
        url = urljoin(self._base, "chat/completions")
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(url, json=payload, headers=self._headers)
            resp.raise_for_status()
            data = resp.json()

        choice = data["choices"][0]["message"]["content"] or ""
        usage = data.get("usage") or {}
        return CompletionResponse(
            content=choice,
            model=data.get("model", req.model),
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
        )

    def stream(self, req: CompletionRequest) -> Iterator[str]:
        """Stream content chunks using OpenAI-compatible SSE.

        Each SSE event is `data: {json}\\n\\n`. We parse the `delta.content`
        out of each chunk and yield it. The terminating `data: [DONE]`
        signal ends the iteration. HTTP-level errors are raised as
        httpx.HTTPStatusError so the GuardedStream can fall back.
        """
        payload = {
            "model": req.model,
            "messages": [
                {"role": "system", "content": req.system},
                {"role": "user", "content": req.user},
            ],
            "temperature": req.temperature,
            "max_tokens": req.max_tokens,
            "stream": True,
        }
        url = urljoin(self._base, "chat/completions")
        # Use a Client (not a context manager) so the GuardedStream can
        # close the underlying connection mid-stream to cancel generation.
        client = httpx.Client(timeout=self._timeout)
        try:
            with client.stream(
                "POST", url, json=payload, headers=self._headers
            ) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line:
                        continue
                    if line.startswith("data: "):
                        line = line[6:]
                    if line.strip() == "[DONE]":
                        return
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        # Tolerate keep-alive / unknown lines; LiteLLM
                        # sometimes emits comment frames.
                        continue
                    choices = event.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    chunk = delta.get("content")
                    if chunk:
                        yield chunk
        finally:
            client.close()


def from_env(
    *,
    endpoint: str | None = None,
    api_key: str | None = None,
    timeout: float | None = None,
) -> OpenAIClient:
    """Build a client from CLI args + env vars, with sensible defaults.

    Resolution order (highest first):
    1. Explicit argument passed to this function.
    2. Env var (LMBOX_LLM_ENDPOINT / LMBOX_LLM_API_KEY / LMBOX_LLM_TIMEOUT).
    3. Default (Ollama local — http://localhost:11434, no auth, 180 s timeout).

    The default timeout (180 s) is intentionally generous so that
    small CPU-only models (qwen2.5:3b on a 4-vCPU Scaleway PRO2-XS,
    for example) can comfortably finish a multi-paragraph response
    even with a 5 KB system prompt. Override with --timeout / env var
    when targeting a GPU backend where 30 s is plenty.
    """
    resolved_endpoint = endpoint or os.environ.get("LMBOX_LLM_ENDPOINT") or "http://localhost:11434"
    resolved_key = api_key or os.environ.get("LMBOX_LLM_API_KEY") or ""
    resolved_timeout = timeout if timeout is not None else float(os.environ.get("LMBOX_LLM_TIMEOUT", "180"))
    return OpenAIClient(resolved_endpoint, resolved_key, timeout=resolved_timeout)
