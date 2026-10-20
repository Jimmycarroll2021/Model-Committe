"""OpenAI-compatible chat-completions backends (OpenAI, OpenRouter, Google's OpenAI endpoint).

Key-gated: unavailable unless the vendor's API key is in the environment. No tools, so research runs get
an explicit no-browsing note instead of web access.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import ClassVar

import httpx

from model_committee.providers._http import NO_BROWSING_NOTE, post_json
from model_committee.providers.base import Provider, ProviderError, ProviderResult, ProviderUnavailable
from model_committee.schemas import ProviderTelemetry

# OpenRouter model ids are "<org>/<model>"; map the org to the vendor label.
OPENROUTER_VENDORS = {
    "anthropic": "anthropic",
    "openai": "openai",
    "google": "google",
    "x-ai": "xai",
    "meta-llama": "open-source",
    "mistralai": "open-source",
    "qwen": "open-source",
    "deepseek": "open-source",
    "nousresearch": "open-source",
}


class OpenAICompatProvider(Provider):
    """Base for chat-completions endpoints. Subclasses set name, vendor, key_env and base_url."""

    key_env: ClassVar[str] = ""
    base_url: ClassVar[str] = ""
    max_tokens: ClassVar[int] = 16000
    max_tokens_field: ClassVar[str] = "max_tokens"

    def api_key(self) -> str | None:
        return os.environ.get(self.key_env) or None

    def endpoint(self) -> str:
        return (self.extra.get("base_url") or self.base_url).rstrip("/") + "/chat/completions"

    async def available(self) -> tuple[bool, str]:
        if not self.api_key():
            return False, f"{self.key_env} is not set"
        if not self.model:
            return False, f"name a model, e.g. {self.name}:<model>"
        return True, f"{self.key_env} set"

    async def generate(self, prompt: str, *, workdir: Path, timeout_s: float, research: bool) -> ProviderResult:
        key = self.api_key()
        if not key:
            raise ProviderUnavailable(f"{self.key_env} is not set")
        if not self.model:
            raise ProviderUnavailable(f"{self.name} needs a model, e.g. {self.name}:<model>")
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": (NO_BROWSING_NOTE + prompt) if research else prompt}],
            self.max_tokens_field: int(self.extra.get("max_tokens", self.max_tokens)),
        }
        transport: httpx.AsyncBaseTransport | None = self.extra.get("transport")
        started = time.monotonic()
        data, raw = await post_json(
            self.endpoint(), body, headers={"Authorization": f"Bearer {key}"},
            timeout_s=timeout_s, transport=transport,
        )
        choices = data.get("choices") or []
        if data.get("error") or not choices:
            raise ProviderError(f"{self.name} returned no choices: {data.get('error') or 'empty response'}")
        text = ((choices[0].get("message") or {}).get("content") or "").strip()
        if not text:
            raise ProviderError(f"{self.name} returned empty content (finish_reason={choices[0].get('finish_reason')})")
        usage = data.get("usage") or {}
        telemetry = ProviderTelemetry(
            provider=self.name,
            model=data.get("model") or self.model,
            duration_s=round(time.monotonic() - started, 2),
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            web_searches=0,
            tool_calls=0,
        )
        return ProviderResult(text=text, telemetry=telemetry, raw=raw)


class OpenAIProvider(OpenAICompatProvider):
    name: ClassVar[str] = "openai"
    vendor: ClassVar[str] = "openai"
    key_env: ClassVar[str] = "OPENAI_API_KEY"
    base_url: ClassVar[str] = "https://api.openai.com/v1"
    max_tokens_field: ClassVar[str] = "max_completion_tokens"  # reasoning models reject max_tokens


class OpenRouterProvider(OpenAICompatProvider):
    name: ClassVar[str] = "openrouter"
    key_env: ClassVar[str] = "OPENROUTER_API_KEY"
    base_url: ClassVar[str] = "https://openrouter.ai/api/v1"

    @property
    def vendor(self) -> str:  # type: ignore[override]  # depends on the routed model's org
        org = (self.model or "").split("/", 1)[0].lower()
        return OPENROUTER_VENDORS.get(org, org or "openrouter")


class GoogleProvider(OpenAICompatProvider):
    name: ClassVar[str] = "google"
    vendor: ClassVar[str] = "google"
    key_env: ClassVar[str] = "GEMINI_API_KEY"
    base_url: ClassVar[str] = "https://generativelanguage.googleapis.com/v1beta/openai"
