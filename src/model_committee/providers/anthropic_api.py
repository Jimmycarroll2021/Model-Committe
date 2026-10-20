"""Claude through the Anthropic Messages API over raw HTTP (key-gated on ANTHROPIC_API_KEY).

Research mode adds Anthropic's server-side web search and web fetch tools; the API runs the search loop
itself, and a `pause_turn` stop is resumed by re-sending the conversation (no extra user message).
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import ClassVar

import httpx

from model_committee.providers._http import post_json
from model_committee.providers.base import Provider, ProviderError, ProviderResult, ProviderUnavailable
from model_committee.schemas import ProviderTelemetry

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-opus-5-5"
MAX_TOKENS = 16000  # non-streaming request; keeps the response under HTTP timeouts
MAX_CONTINUATIONS = 5
# Models that take the dynamic-filtering web tool versions; anything else gets the basic versions.
DYNAMIC_WEB_MODELS = (
    "claude-opus-5-5", "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
    "claude-sonnet-5-5", "claude-sonnet-5", "claude-sonnet-4-6",
)
# Models whose safety classifiers may decline; server-side fallback re-runs a declined request on the
# model Anthropic recommends for that refusal category.
FALLBACK_MODELS = ("claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5")
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def web_tools(model: str, max_uses: int) -> list[dict]:
    if model in DYNAMIC_WEB_MODELS:
        search, fetch = "web_search_20260209", "web_fetch_20260209"
    else:
        search, fetch = "web_search_20250305", "web_fetch_20250910"
    return [
        {"type": search, "name": "web_search", "max_uses": max_uses},
        {"type": fetch, "name": "web_fetch", "max_uses": max_uses},
    ]


class AnthropicApiProvider(Provider):
    name: ClassVar[str] = "anthropic"
    vendor: ClassVar[str] = "anthropic"
    known_models: ClassVar[list[str]] = [DEFAULT_MODEL, "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1"]

    @property
    def resolved_model(self) -> str:
        return self.model or DEFAULT_MODEL

    async def available(self) -> tuple[bool, str]:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return False, "ANTHROPIC_API_KEY is not set"
        return True, "ANTHROPIC_API_KEY set"

    def build_request(self, messages: list[dict], research: bool) -> tuple[dict, dict[str, str]]:
        model = self.resolved_model
        body: dict = {"model": model, "max_tokens": int(self.extra.get("max_tokens", MAX_TOKENS)), "messages": messages}
        headers = {"anthropic-version": API_VERSION}
        if research:
            body["tools"] = web_tools(model, int(self.extra.get("max_web_uses", 10)))
        if model in FALLBACK_MODELS:
            body["fallbacks"] = "default"
            headers["anthropic-beta"] = FALLBACK_BETA
        return body, headers

    async def generate(self, prompt: str, *, workdir: Path, timeout_s: float, research: bool) -> ProviderResult:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise ProviderUnavailable("ANTHROPIC_API_KEY is not set")
        transport: httpx.AsyncBaseTransport | None = self.extra.get("transport")
        messages: list[dict] = [{"role": "user", "content": prompt}]
        assistant_so_far: list[dict] = []
        texts: list[str] = []
        raws: list[str] = []
        input_tokens = output_tokens = searches = tool_calls = 0
        started = time.monotonic()
        model_used = self.resolved_model

        for _ in range(MAX_CONTINUATIONS + 1):
            remaining = timeout_s - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError(f"anthropic call exceeded {timeout_s}s")
            body, headers = self.build_request(messages, research)
            data, raw = await post_json(
                API_URL, body, headers={**headers, "x-api-key": key}, timeout_s=remaining, transport=transport
            )
            raws.append(raw)
            usage = data.get("usage") or {}
            input_tokens += sum(
                int(usage.get(k) or 0)
                for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
            )
            output_tokens += int(usage.get("output_tokens") or 0)
            server = usage.get("server_tool_use") or {}
            searches += int(server.get("web_search_requests") or 0) + int(server.get("web_fetch_requests") or 0)
            content = data.get("content") or []
            tool_calls += sum(1 for b in content if b.get("type") == "server_tool_use")
            texts += [b.get("text", "") for b in content if b.get("type") == "text"]
            model_used = data.get("model") or model_used
            stop = data.get("stop_reason")
            if stop == "refusal":
                category = (data.get("stop_details") or {}).get("category")
                raise ProviderError(f"anthropic declined the request (refusal, category={category})")
            if stop != "pause_turn":
                break
            assistant_so_far += content  # the server resumes from the trailing server_tool_use block
            messages = [messages[0], {"role": "assistant", "content": assistant_so_far}]
        else:
            raise ProviderError(f"anthropic still paused after {MAX_CONTINUATIONS} continuations")

        text = "".join(texts).strip()
        if not text:
            raise ProviderError(f"anthropic returned no text (stop_reason={stop})")
        telemetry = ProviderTelemetry(
            provider=self.name,
            model=model_used,
            duration_s=round(time.monotonic() - started, 2),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            web_searches=searches,
            tool_calls=tool_calls,
        )
        return ProviderResult(text=text, telemetry=telemetry, raw="\n".join(raws))
