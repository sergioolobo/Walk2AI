"""Provider adapters. Only providers with an API key configured are built.

API keys live in the backend's settings and are handed to the SDK clients here. They
are never returned by any endpoint and never reach a browser or a device.
"""

from app.core.config import Settings
from app.services.ai.providers.anthropic_adapter import AnthropicAdapter
from app.services.ai.providers.base import (
    Completed,
    FailureCode,
    ModelCheck,
    ProviderAdapter,
    ProviderEvent,
    ProviderFailure,
    ProviderRequest,
    Started,
    TextDelta,
    Turn,
    TurnRole,
)
from app.services.ai.providers.openai_adapter import OpenAIAdapter

KNOWN_PROVIDERS = ("anthropic", "openai")


def build_providers(settings: Settings) -> dict[str, ProviderAdapter]:
    providers: dict[str, ProviderAdapter] = {}
    timeout = settings.ai_request_timeout_seconds
    if settings.anthropic_api_key is not None and settings.anthropic_api_key.get_secret_value():
        providers["anthropic"] = AnthropicAdapter(
            settings.anthropic_api_key.get_secret_value(),
            timeout_seconds=timeout,
            base_url=settings.anthropic_base_url,
        )
    if settings.openai_api_key is not None and settings.openai_api_key.get_secret_value():
        providers["openai"] = OpenAIAdapter(
            settings.openai_api_key.get_secret_value(),
            timeout_seconds=timeout,
            base_url=settings.openai_base_url,
        )
    return providers


__all__ = [
    "KNOWN_PROVIDERS",
    "AnthropicAdapter",
    "Completed",
    "FailureCode",
    "ModelCheck",
    "OpenAIAdapter",
    "ProviderAdapter",
    "ProviderEvent",
    "ProviderFailure",
    "ProviderRequest",
    "Started",
    "TextDelta",
    "Turn",
    "TurnRole",
    "build_providers",
]
