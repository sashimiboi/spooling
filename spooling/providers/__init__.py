"""Multi-provider support for Spooling."""

from spooling.providers.base import Provider, RemoteProvider, PROVIDER_REGISTRY
from spooling.providers.session_file import SessionFileProvider
from spooling.providers.codex import CodexProvider
from spooling.providers.cursor import CursorProvider
from spooling.providers.copilot import CopilotProvider
from spooling.providers.windsurf import WindsurfProvider
from spooling.providers.kiro import KiroProvider
from spooling.providers.antigravity import AntigravityProvider
from spooling.providers.gemini import GeminiProvider
from spooling.providers.cortex_code import CortexCodeProvider
from spooling.providers.opencode import OpencodeProvider
from spooling.providers.gitlab import GitLabProvider
from spooling.providers.github import GitHubProvider
from spooling.providers.hermes import HermesProvider

__all__ = [
    "Provider",
    "RemoteProvider",
    "PROVIDER_REGISTRY",
    "SessionFileProvider",
    "CodexProvider",
    "CursorProvider",
    "CopilotProvider",
    "WindsurfProvider",
    "KiroProvider",
    "AntigravityProvider",
    "GeminiProvider",
    "CortexCodeProvider",
    "OpencodeProvider",
    "GitLabProvider",
    "GitHubProvider",
    "HermesProvider",
]


def get_provider(provider_type: str) -> Provider | None:
    """Get a provider instance by type ID."""
    cls = PROVIDER_REGISTRY.get(provider_type)
    if cls:
        return cls()
    return None


def get_all_providers() -> dict[str, Provider]:
    """Get instances of all registered providers."""
    return {type_id: cls() for type_id, cls in PROVIDER_REGISTRY.items()}
