"""Provider registry: spec strings -> configured Provider instances, plus local availability detection."""

from __future__ import annotations

import asyncio

from model_committee.providers.anthropic_api import AnthropicApiProvider
from model_committee.providers.antigravity_cli import AntigravityCliProvider
from model_committee.providers.base import Provider
from model_committee.providers.claude_cli import ClaudeCliProvider
from model_committee.providers.codex_cli import CodexCliProvider, user_default_model
from model_committee.providers.ollama import OllamaProvider
from model_committee.providers.openai_compat import GoogleProvider, OpenAIProvider, OpenRouterProvider

PROVIDERS: dict[str, type[Provider]] = {
    cls.name: cls
    for cls in (
        ClaudeCliProvider,
        CodexCliProvider,
        AntigravityCliProvider,
        OllamaProvider,
        OpenAIProvider,
        OpenRouterProvider,
        GoogleProvider,
        AnthropicApiProvider,
    )
}

DEFAULT_LINEUP = ["claude-cli", "codex-cli", "agy:gemini-3.8-flash-high"]

EXAMPLE_SPECS = {
    "claude-cli": "claude-cli:sonnet",
    "codex-cli": "codex-cli",
    "agy": "agy:gemini-3.8-flash-high",
    "ollama": "ollama:qwen2.5-coder:1.5b",
    "openai": "openai:<model>",
    "openrouter": "openrouter:<org>/<model>",
    "google": "google:<model>",
    "anthropic": "anthropic:claude-opus-5-5",
}


def parse_spec(spec: str) -> tuple[str, str | None]:
    """'name' or 'name:model', split on the first colon only (ollama tags contain colons)."""
    name, sep, model = spec.strip().partition(":")
    return name.strip(), (model.strip() or None) if sep else None


def build_provider(spec: str, extra: dict | None = None) -> Provider:
    name, model = parse_spec(spec)
    cls = PROVIDERS.get(name)
    if cls is None:
        raise ValueError(f"unknown provider {name!r} in spec {spec!r}; known: {', '.join(PROVIDERS)}")
    return cls(model=model, extra=dict(extra or {}))


async def _known_models(provider: Provider) -> list[str]:
    if isinstance(provider, OllamaProvider):
        try:
            return await provider.installed_models()
        except Exception:  # noqa: BLE001 - detection must never fail the listing
            return []
    if isinstance(provider, CodexCliProvider):
        model = user_default_model()
        return [model] if model else []
    return list(getattr(type(provider), "known_models", []))


async def _detect_one(name: str, cls: type[Provider]) -> dict:
    example = EXAMPLE_SPECS.get(name, name)
    probe = cls(model=parse_spec(example)[1] if "<" not in example else None)
    models = await _known_models(probe)
    if isinstance(probe, OllamaProvider) and models and probe.model not in models:
        example = f"{name}:{models[0]}"  # show a model that is actually pulled here
        probe = cls(model=models[0])
    try:
        ok, reason = await probe.available()
    except Exception as exc:  # noqa: BLE001
        ok, reason = False, f"availability check failed: {type(exc).__name__}: {exc}"
    return {
        "name": name,
        "vendor": probe.vendor,
        "available": ok,
        "reason": reason,
        "example_spec": example,
        "models": models,
    }


async def detect_available() -> list[dict]:
    """One row per registered provider: {name, vendor, available, reason, example_spec, models}."""
    return list(await asyncio.gather(*(_detect_one(n, c) for n, c in PROVIDERS.items())))
