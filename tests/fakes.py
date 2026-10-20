"""Scripted test double for Provider. Live behaviour is covered by `-m live` tests and real runs."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from model_committee.providers.base import Provider, ProviderError, ProviderResult
from model_committee.schemas import ProviderTelemetry


def member_reply(claims: list[dict], *, recommendation: str = "Choose A", confidence: float = 0.7,
                 prose: str = "## Summary\nShort report.") -> str:
    findings = {
        "summary": "summary", "recommendation": recommendation, "confidence": confidence,
        "assumptions": ["assume x"], "missing_information": [], "claims": claims,
        "counterargument": "B is cheaper short term", "would_change_mind": ["new pricing"],
    }
    return f"{prose}\n\n```json\n{json.dumps(findings)}\n```"


def claim(cid: str, statement: str, url: str | None = None, quote: str | None = None) -> dict:
    ev = [{"source": "src", "url": url, "quote": quote}] if url else []
    return {"id": cid, "statement": statement, "confidence": 0.8, "evidence": ev, "assumptions": []}


NARRATIVE = json.dumps({
    "headline": "Choose A", "answer": "A wins on cost. See https://a.example/pricing",
    "recommendation": "Choose A", "confidence_note": "2 of 3 members, 2 independent domains",
    "decision_matrix": [{"option": "A", "scores": {"cost": "low"}, "verdict": "preferred"}],
    "unresolved_risks": ["lock-in"], "next_actions": ["confirm pricing"],
    "citations": [{"source": "A pricing", "url": "https://a.example/pricing", "quote": None}],
})


@dataclass
class FakeProvider(Provider):
    """`script` maps research=True/False calls to a reply string, an Exception, or a callable(prompt)->str."""

    research_reply: str | Exception | Callable[[str], str] = ""
    synth_replies: list[str] = field(default_factory=list)
    delay_s: float = 0.0
    is_available: bool = True
    calls: list[tuple[bool, str]] = field(default_factory=list)
    name = "fake"
    vendor = "test"

    @property
    def spec(self) -> str:
        return self.model or self.name

    async def generate(self, prompt: str, *, workdir: Path, timeout_s: float, research: bool) -> ProviderResult:
        self.calls.append((research, prompt))
        assert workdir.is_dir()
        if self.delay_s:
            await asyncio.wait_for(asyncio.sleep(self.delay_s), timeout=timeout_s)
        if research:
            reply = self.research_reply
        else:
            if not self.synth_replies:
                raise ProviderError("no scripted synth reply left")
            reply = self.synth_replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        text = reply(prompt) if callable(reply) else reply
        tel = ProviderTelemetry(provider=self.name, model=self.model, duration_s=0.01, web_searches=2)
        return ProviderResult(text=text, telemetry=tel, raw=text)

    async def available(self) -> tuple[bool, str]:
        return self.is_available, "ok" if self.is_available else "fake unavailable"


def factory(mapping: dict[str, FakeProvider]):
    def build(spec: str) -> FakeProvider:
        provider = mapping[spec]
        provider.model = provider.model or spec  # member keys become m<i>-<spec>
        return provider

    return build
