"""Claude Code session parser - reads JSONL from ~/.claude/projects/."""

from pathlib import Path

from spool.config import CLAUDE_PROJECTS_DIR
from spool.parser import ParsedSession, parse_session_file
from spool.providers.base import Provider


class ClaudeCodeProvider(Provider):
    type_id = "claude-code"
    name = "Claude Code"

    def default_data_path(self) -> Path:
        return CLAUDE_PROJECTS_DIR

    def discover_session_files(self, data_path: Path | None = None) -> list[Path]:
        base = data_path or self.resolved_data_path()
        if not base.exists():
            return []
        files = []
        for project_dir in base.iterdir():
            if not project_dir.is_dir():
                continue
            for f in project_dir.glob("*.jsonl"):
                name = f.stem
                if len(name) == 36 and name.count("-") == 4:
                    files.append(f)
        return sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)

    def parse_session_file(self, file_path: Path) -> list[ParsedSession]:
        session = parse_session_file(file_path)
        if session:
            session.provider_id = "claude-code"
            return [session]
        return []
