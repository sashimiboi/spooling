"""Base provider class for session data sources.

Two shapes of provider:

* **Filesystem providers** (Claude Code, Codex, Cursor, etc.) read local
  session files. Subclasses implement ``discover_session_files`` and
  ``parse_session_file``; the default ``iter_sessions`` walks the file
  list and uses file size as the per-file sync watermark.

* **Remote providers** (GitLab, Bitbucket, Jira via MCP, …) talk to an
  HTTP API and have no on-disk files. Subclasses extend ``RemoteProvider``
  and override ``iter_sessions`` directly; state is an opaque JSON dict
  the ingest pipeline persists to ``providers.config['sync_state']``
  between runs (typically a watermark timestamp or page cursor).
"""

from abc import ABC, abstractmethod
from collections.abc import Iterator
from pathlib import Path

from spool.parser import ParsedSession

# Registry populated by Provider subclasses
PROVIDER_REGISTRY: dict[str, type["Provider"]] = {}


class Provider(ABC):
    """Abstract base for session data providers."""

    # Subclasses must set these
    type_id: str = ""
    name: str = ""

    # Set True for API-backed providers; the ingest pipeline uses this to
    # branch between filesystem state (per-file modtime) and the opaque
    # ``sync_state`` JSON stashed on the providers row.
    is_remote: bool = False

    # Whether the connect flow needs to collect API credentials (PAT,
    # OAuth, etc.) instead of a local data path. UI uses this to render
    # the right form.
    requires_credentials: bool = False

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        # Don't register the abstract RemoteProvider base.
        if cls.type_id and cls.__name__ != "RemoteProvider":
            PROVIDER_REGISTRY[cls.type_id] = cls

    # --- Filesystem path: subclasses override these for file-based sources.
    # Remote providers leave these as no-ops and override ``iter_sessions``.

    def default_data_path(self) -> Path:
        """Return the default path where this provider stores session data.

        Filesystem providers must override. Remote providers can leave the
        default — it's only used by ``spool init`` for filesystem detection.
        """
        raise NotImplementedError(f"{type(self).__name__} is not file-based")

    def discover_session_files(self, data_path: Path | None = None) -> list[Path]:
        """Find all session files for this provider, newest-first."""
        raise NotImplementedError(f"{type(self).__name__} is not file-based")

    def parse_session_file(self, file_path: Path) -> list[ParsedSession]:
        """Parse a session file into one or more ParsedSession objects."""
        raise NotImplementedError(f"{type(self).__name__} is not file-based")

    def is_available(self) -> bool:
        """Return True if this provider can be synced right now.

        Filesystem providers default to checking that the data directory
        exists; remote providers default to False (they need explicit
        credentials before they can sync).
        """
        if self.is_remote:
            return False
        try:
            return self.default_data_path().exists()
        except NotImplementedError:
            return False

    # --- Unified sync entry point used by ingest.py.

    def iter_sessions(
        self,
        *,
        data_path: Path | None = None,
        config: dict | None = None,
        state: dict | None = None,
    ) -> Iterator[tuple[ParsedSession, dict]]:
        """Yield (ParsedSession, marker) pairs for ingestion.

        ``marker`` is whatever the ingest pipeline should persist after a
        successful store. For filesystem providers it's
        ``{"path": str, "size": int}``; for remote providers it's an
        opaque cursor advance (e.g. ``{"cursor": "2026-04-30T..."}``).

        The default implementation here covers filesystem providers by
        walking ``discover_session_files`` and parsing each. Remote
        providers must override this method.
        """
        if self.is_remote:
            raise NotImplementedError(
                f"{type(self).__name__} is a remote provider and must override iter_sessions"
            )
        files = self.discover_session_files(data_path)
        seen = (state or {}).get("files", {}) if state else {}
        for f in files:
            try:
                size = f.stat().st_size
            except OSError:
                continue
            if seen.get(str(f)) == size:
                continue
            for session in self.parse_session_file(f):
                yield session, {"kind": "file", "path": str(f), "size": size}


class RemoteProvider(Provider):
    """Base for HTTP-API-backed providers (GitLab, Jira, MCP-sourced, …).

    Subclasses must set ``type_id`` / ``name`` and override
    ``iter_sessions``. Filesystem methods are intentionally not
    implemented — they'll raise if a remote provider is accidentally fed
    into the filesystem code path.
    """

    is_remote = True
    requires_credentials = True

    @abstractmethod
    def iter_sessions(
        self,
        *,
        data_path: Path | None = None,
        config: dict | None = None,
        state: dict | None = None,
    ) -> Iterator[tuple[ParsedSession, dict]]:
        """Override to fetch sessions from the remote API."""
