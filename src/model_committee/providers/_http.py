"""Shared JSON-over-HTTP call for the API-backed providers (ollama, OpenAI-compatible, Anthropic)."""

from __future__ import annotations

import asyncio
import json

import httpx

from model_committee.providers._subprocess import tail
from model_committee.providers.base import ProviderError

NO_BROWSING_NOTE = (
    "NOTE: You have no web browsing or search tools in this session. Do not invent sources or URLs. "
    "Rely only on the context supplied below and your own training knowledge, state your knowledge "
    "cutoff where it matters, and say explicitly when a question needs current information you cannot "
    "verify.\n\n"
)


async def post_json(
    url: str,
    body: dict,
    *,
    headers: dict[str, str] | None = None,
    timeout_s: float,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[dict, str]:
    """POST body, return (parsed JSON, raw text). Raises TimeoutError past timeout_s, ProviderError on HTTP errors.

    Error messages carry the status and a body tail, never the request headers (API keys live there).
    """

    async def _call() -> httpx.Response:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s), transport=transport) as client:
            return await client.post(url, json=body, headers=headers or {})

    try:
        resp = await asyncio.wait_for(_call(), timeout=timeout_s)
    except httpx.TimeoutException as exc:
        raise TimeoutError(f"{url} timed out after {timeout_s}s") from exc
    except httpx.HTTPError as exc:
        raise ProviderError(f"{url}: {type(exc).__name__}: {exc}") from exc
    raw = resp.text
    if resp.status_code >= 400:
        raise ProviderError(f"{url} returned HTTP {resp.status_code}: {tail(raw, 800)}")
    try:
        return json.loads(raw), raw
    except json.JSONDecodeError as exc:
        raise ProviderError(f"{url} returned non-JSON: {tail(raw, 400)}") from exc


async def get_json(url: str, *, timeout_s: float = 3.0, transport: httpx.AsyncBaseTransport | None = None) -> dict:
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s), transport=transport) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()
