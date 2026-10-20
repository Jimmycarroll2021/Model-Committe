"""Local models through the Ollama HTTP API (`/api/chat`, non-streaming). No web tools."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import ClassVar

import httpx

from model_committee.providers._http import NO_BROWSING_NOTE, get_json, post_json
from model_committee.providers.base import Provider, ProviderError, ProviderResult, ProviderUnavailable
from model_committee.schemas import ProviderTelemetry

DEFAULT_HOST = "http://localhost:11434"
# Ollama's default context is small and silently truncates long prompts; committee prompts are long.
DEFAULT_NUM_CTX = 16384


class OllamaProvider(Provider):
    name: ClassVar[str] = "ollama"
    vendor: ClassVar[str] = "open-source"

    @property
    def host(self) -> str:
        host = self.extra.get("host") or os.environ.get("OLLAMA_HOST") or DEFAULT_HOST
        if not host.startswith(("http://", "https://")):
            host = f"http://{host}"
        return host.rstrip("/")

    @property
    def transport(self) -> httpx.AsyncBaseTransport | None:
        return self.extra.get("transport")

    async def installed_models(self) -> list[str]:
        tags = await get_json(f"{self.host}/api/tags", transport=self.transport)
        return [m.get("name", "") for m in tags.get("models") or []]

    async def available(self) -> tuple[bool, str]:
        try:
            models = await self.installed_models()
        except (httpx.HTTPError, ValueError) as exc:
            return False, f"ollama not reachable at {self.host} ({type(exc).__name__})"
        if not self.model:
            return False, f"name a model, e.g. ollama:{models[0] if models else '<model>'}"
        if self.model not in models and f"{self.model}:latest" not in models:
            return False, f"model {self.model} not pulled (installed: {', '.join(models) or 'none'})"
        return True, f"{self.model} installed at {self.host}"

    async def generate(self, prompt: str, *, workdir: Path, timeout_s: float, research: bool) -> ProviderResult:
        if not self.model:
            raise ProviderUnavailable("ollama needs a model, e.g. ollama:qwen2.5-coder:1.5b")
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": (NO_BROWSING_NOTE + prompt) if research else prompt}],
            "stream": False,
            "options": {"num_ctx": int(self.extra.get("num_ctx", DEFAULT_NUM_CTX))},
        }
        started = time.monotonic()
        data, raw = await post_json(f"{self.host}/api/chat", body, timeout_s=timeout_s, transport=self.transport)
        if data.get("error"):
            raise ProviderError(f"ollama error: {data['error']}")
        text = ((data.get("message") or {}).get("content") or "").strip()
        if not text:
            raise ProviderError("ollama returned an empty message")
        telemetry = ProviderTelemetry(
            provider=self.name,
            model=self.model,
            duration_s=round(time.monotonic() - started, 2),
            input_tokens=data.get("prompt_eval_count"),
            output_tokens=data.get("eval_count"),
            web_searches=0,
            tool_calls=0,
        )
        return ProviderResult(text=text, telemetry=telemetry, raw=raw)
