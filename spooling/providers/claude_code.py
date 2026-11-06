"""Claude Code provider: reads session transcripts from ``~/.claude/projects``.

Claude Code writes one JSONL transcript per session at
``<config dir>/projects/<encoded-cwd>/<session-uuid>.jsonl``, where the encoded
cwd is the working directory with ``/`` replaced by ``-``. Each line is a
``user`` / ``assistant`` record (plus bookkeeping records the parser ignores)
carrying per-message token ``usage`` including prompt-cache reads and writes,
which is what makes cost tracking accurate.

The config directory is ``$CLAUDE_CONFIG_DIR`` when set, otherwise ``~/.claude``
(falling back to ``~/.config/claude`` where newer installs put it). A different
location can be pinned in ``~/.config/spooling/paths.json`` under the key
``"claude-code"``.

Not yet included: subagent transcripts, which recent Claude Code versions write
to separate files under ``<session-uuid>/subagents/``. Their spend is not part of
the parent session's totals.
"""

import os
import re
from pathlib import Path

from spooling.parser import ParsedSession, parse_session_file
from spooling.providers.base import Provider

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _claude_config_dirs() -> list[Path]:
    env = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if env:
        return [Path(env).expanduser()]
    return [Path.home() / ".claude", Path.home() / ".config" / "claude"]


class ClaudeCodeProvider(Provider):
    type_id = "claude-code"
    name = "Claude Code"
    # 2: subagent calls named "Agent" (not just "Task") now create agent spans.
    parser_revision = 2

    def default_data_path(self) -> Path:
        candidates = [d / "projects" for d in _claude_config_dirs()]
        for c in candidates:
            if c.exists():
                return c
        return candidates[0]

    def discover_session_files(self, data_path: Path | None = None) -> list[Path]:
        base = data_path or self.resolved_data_path()
        if not base.exists():
            return []
        files: list[Path] = []
        for project_dir in base.iterdir():
            if not project_dir.is_dir():
                continue
            # Top level only and UUID-named: skips subagent transcripts and
            # non-session JSONL such as plugin skill-injection logs.
            for f in project_dir.glob("*.jsonl"):
                if _UUID_RE.match(f.stem):
                    files.append(f)
        return sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)

    def parse_session_file(self, file_path: Path) -> list[ParsedSession]:
        session = parse_session_file(file_path, provider_id=self.type_id, project_from_cwd=True)
        return [session] if session else []
