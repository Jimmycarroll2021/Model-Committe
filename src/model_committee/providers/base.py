"""Provider interface: one model-agent backend a committee member (or the synthesizer) runs on."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from model_committee.schemas import ProviderTelemetry


class ProviderError(RuntimeError):
    """The backend ran but failed (non-zero exit, HTTP error, empty output)."""


class ProviderUnavailable(ProviderError):
    """The backend cannot run at all here (binary missing, no credentials)."""


@dataclass
class ProviderResult:
    text: str
    telemetry: ProviderTelemetry
    raw: str = ""  # untouched stdout/body for the audit trail


@dataclass
class Provider(abc.ABC):
    """A configured backend. `spec` is the user-facing string, e.g. 'agy:gemini-3.8-flash-high'."""

    model: str | None = None
    extra: dict = field(default_factory=dict)

    name: ClassVar[str] = ""  # registry key, e.g. "claude-cli" (set by subclasses)
    vendor: ClassVar[str] = ""  # "anthropic" | "openai" | "google" | "open-source" | ...

    @property
    def spec(self) -> str:
        return f"{self.name}:{self.model}" if self.model else self.name

    @property
    def label(self) -> str:
        return f"{self.vendor} / {self.model or 'default'} ({self.name})"

    @abc.abstractmethod
    async def generate(
        self,
        prompt: str,
        *,
        workdir: Path,
        timeout_s: float,
        research: bool,
    ) -> ProviderResult:
        """Run one prompt to completion.

        workdir   -- an isolated, empty-ish directory owned by this call; CLIs run with it as cwd.
        timeout_s -- hard wall-clock limit; implementations must kill the subprocess/request on expiry
                     and raise asyncio.TimeoutError.
        research  -- True for committee members (allow web search/fetch, read-only tools),
                     False for the synthesizer (pure reasoning over supplied text, no tools).
        """

    @abc.abstractmethod
    async def available(self) -> tuple[bool, str]:
        """Cheap local check (binary on PATH / key present). Returns (ok, reason)."""
