"""Multi-provider support for Spool."""

from spool.providers.base import Provider, RemoteProvider, PROVIDER_REGISTRY
from spool.providers.claude_code import ClaudeCodeProvider
from spool.providers.codex import CodexProvider
from spool.providers.cursor import CursorProvider
from spool.providers.copilot import CopilotProvider
from spool.providers.windsurf import WindsurfProvider
from spool.providers.kiro import KiroProvider
from spool.providers.antigravity import AntigravityProvider
from spool.providers.gemini import GeminiProvider
from spool.providers.gitlab import GitLabProvider
from spool.providers.github import GitHubProvider

__all__ = [
    "Provider",
    "RemoteProvider",
    "PROVIDER_REGISTRY",
    "ClaudeCodeProvider",
    "CodexProvider",
    "CursorProvider",
    "CopilotProvider",
    "WindsurfProvider",
    "KiroProvider",
    "AntigravityProvider",
    "GeminiProvider",
    "GitLabProvider",
    "GitHubProvider",
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
