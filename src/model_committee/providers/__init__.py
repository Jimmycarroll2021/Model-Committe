"""Provider data plane: one adapter per model backend (CLIs and HTTP APIs) behind a common interface."""

from model_committee.providers.base import Provider, ProviderError, ProviderResult, ProviderUnavailable
from model_committee.providers.registry import DEFAULT_LINEUP, PROVIDERS, build_provider, detect_available, parse_spec

__all__ = [
    "DEFAULT_LINEUP",
    "PROVIDERS",
    "Provider",
    "ProviderError",
    "ProviderResult",
    "ProviderUnavailable",
    "build_provider",
    "detect_available",
    "parse_spec",
]
