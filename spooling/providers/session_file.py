"""Session parser - reads JSONL from ~/.sessions/projects/."""

from pathlib import Path

from spooling.config import SESSIONS_PROJECTS_DIR
from spooling.parser import ParsedSession, parse_session_file
from spooling.providers.base import Provider


class SessionFileProvider(Provider):
    type_id = "jsonl-session"
    name = "Session Files"

    def default_data_path(self) -> Path:
        return SESSIONS_PROJECTS_DIR

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
            session.provider_id = "jsonl-session"
            return [session]
        return []
