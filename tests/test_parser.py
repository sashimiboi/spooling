"""Tests for spool.parser — Claude Code JSONL session parsing."""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from spool.parser import parse_session_file, ParsedSession, ParsedMessage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts(iso: str) -> str:
    return iso


def _session_id() -> str:
    return str(uuid.uuid4())


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records))


def _user(content: str, uid: str | None = None, ts: str = "2025-01-01T10:00:00.000Z") -> dict:
    return {
        "type": "user",
        "uuid": uid or str(uuid.uuid4()),
        "timestamp": ts,
        "message": {"role": "user", "content": content},
    }


def _assistant(content: str | list, model: str = "claude-sonnet-4-6",
               uid: str | None = None, ts: str = "2025-01-01T10:00:01.000Z") -> dict:
    return {
        "type": "assistant",
        "uuid": uid or str(uuid.uuid4()),
        "timestamp": ts,
        "message": {
            "role": "assistant",
            "content": content,
            "model": model,
        },
    }


# ---------------------------------------------------------------------------
# Basic parsing
# ---------------------------------------------------------------------------

class TestBasicParsing:
    def test_returns_none_for_empty_file(self, tmp_path):
        f = tmp_path / "project" / f"{_session_id()}.jsonl"
        f.parent.mkdir()
        f.write_text("")
        assert parse_session_file(f) is None

    def test_returns_none_for_file_with_no_valid_records(self, tmp_path):
        f = tmp_path / "project" / f"{_session_id()}.jsonl"
        f.parent.mkdir()
        f.write_text('{"type": "meta", "data": "ignored"}\n')
        assert parse_session_file(f) is None

    def test_parses_simple_session(self, tmp_path):
        sid = _session_id()
        f = tmp_path / "my-project" / f"{sid}.jsonl"
        f.parent.mkdir()
        _write_jsonl(f, [
            _user("Hello, Claude!"),
            _assistant("Hello! How can I help?"),
        ])
        result = parse_session_file(f)
        assert isinstance(result, ParsedSession)
        assert result.session_id == sid
        assert result.project == "my-project"
        assert result.message_count == 2

    def test_session_id_comes_from_filename(self, tmp_path):
        sid = _session_id()
        f = tmp_path / "proj" / f"{sid}.jsonl"
        f.parent.mkdir()
        _write_jsonl(f, [_user("hi"), _assistant("hey")])
        result = parse_session_file(f)
        assert result.session_id == sid

    def test_project_comes_from_parent_dir(self, tmp_path):
        f = tmp_path / "my-repo" / f"{_session_id()}.jsonl"
        f.parent.mkdir()
        _write_jsonl(f, [_user("hi"), _assistant("hey")])
        result = parse_session_file(f)
        assert result.project == "my-repo"

    def test_skips_records_with_no_content(self, tmp_path):
        f = tmp_path / "proj" / f"{_session_id()}.jsonl"
        f.parent.mkdir()
        _write_jsonl(f, [
            _user("   "),       # whitespace-only — should be skipped
            _user("real message"),
            _assistant("response"),
        ])
        result = parse_session_file(f)
        assert result is not None
        # Only real-content records become ParsedMessage objects
        assert all(m.content.strip() for m in result.messages)

    def test_returns_none_for_missing_file(self, tmp_path):
        f = tmp_path / "proj" / f"{_session_id()}.jsonl"
        assert parse_session_file(f) is None


# ---------------------------------------------------------------------------
# Metadata extraction
# ---------------------------------------------------------------------------

class TestMetadataExtraction:
    def test_model_extracted(self, tmp_path):
        f = tmp_path / "proj" / f"{_session_id()}.jsonl"
        f.parent.mkdir()
        _write_jsonl(f, [
            _user("hi"),
            _assistant("hey", model="claude-opus-4-6"),
        ])
        result = parse_session_file(f)
        assert result.model == "claude-opus-4-6"

    def test_cwd_extracted(self, tmp_path):
        f = tmp_path / "proj" / f"{_session_id()}.jsonl"
        f.parent.mkdir()
        rec = _user("hi")
        rec["cwd"] = "/home/user/myrepo"
        _write_jsonl(f, [rec, _assistant("hey")])
        result = parse_session_file(f)
        assert result.cwd == "/home/user/myrepo"

    def test_git_branch_extracted(self, tmp_path):
        f = tmp_path / "proj" / f"{_session_id()}.jsonl"
        f.parent.mkdir()
        rec = _user("hi")
        rec["gitBranch"] = "feature/my-feature"
        _write_jsonl(f, [rec, _assistant("hey")])
        result = parse_session_file(f)
        assert result.git_branch == "feature/my-feature"

    def test_timestamps(self, tmp_path):
        f = tmp_path / "proj" / f"{_session_id()}.jsonl"
        f.parent.mkdir()
        _write_jsonl(f, [
            _user("hi", ts="2025-06-01T09:00:00.000Z"),
            _assistant("hey", ts="2025-06-01T09:00:05.000Z"),
        ])
        result = parse_session_file(f)
        assert result.started_at is not None
        assert result.ended_at is not None
        assert result.started_at <= result.ended_at

    def test_title_from_first_user_message(self, tmp_path):
        f = tmp_path / "proj" / f"{_session_id()}.jsonl"
        f.parent.mkdir()
        _write_jsonl(f, [
            _user("How do I reverse a string in Python?"),
            _assistant("You can use slicing: `s[::-1]`"),
        ])
        result = parse_session_file(f)
        assert result.title is not None
        assert "reverse" in result.title.lower() or "string" in result.title.lower()


# ---------------------------------------------------------------------------
# Tool-use extraction
# ---------------------------------------------------------------------------

class TestToolUseExtraction:
    def test_tool_use_counted(self, tmp_path):
        f = tmp_path / "proj" / f"{_session_id()}.jsonl"
        f.parent.mkdir()
        _write_jsonl(f, [
            _user("read the file"),
            _assistant([
                {"type": "text", "text": "Sure, reading..."},
                {"type": "tool_use", "id": "tu_1", "name": "Read", "input": {"file_path": "/foo.py"}},
            ]),
        ])
        result = parse_session_file(f)
        assert result.tool_call_count == 1
        tool_msg = next(m for m in result.messages if m.role == "assistant")
        assert "Read" in tool_msg.tools_used

    def test_multiple_tools_in_one_message(self, tmp_path):
        f = tmp_path / "proj" / f"{_session_id()}.jsonl"
        f.parent.mkdir()
        _write_jsonl(f, [
            _user("do stuff"),
            _assistant([
                {"type": "tool_use", "id": "tu_1", "name": "Read", "input": {}},
                {"type": "tool_use", "id": "tu_2", "name": "Bash", "input": {"command": "ls"}},
                {"type": "text", "text": "done"},
            ]),
        ])
        result = parse_session_file(f)
        assert result.tool_call_count == 2


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

class TestTokenEstimation:
    def test_tokens_estimated_from_content_length(self, tmp_path):
        content = "A" * 400  # 400 chars / 4 = 100 tokens
        f = tmp_path / "proj" / f"{_session_id()}.jsonl"
        f.parent.mkdir()
        _write_jsonl(f, [_user(content), _assistant("ok")])
        result = parse_session_file(f)
        user_msg = next(m for m in result.messages if m.role == "user")
        assert user_msg.estimated_tokens == 100

    def test_minimum_one_token(self, tmp_path):
        f = tmp_path / "proj" / f"{_session_id()}.jsonl"
        f.parent.mkdir()
        _write_jsonl(f, [_user("hi"), _assistant("ok")])
        result = parse_session_file(f)
        for m in result.messages:
            assert m.estimated_tokens >= 1


# ---------------------------------------------------------------------------
# Malformed input resilience
# ---------------------------------------------------------------------------

class TestMalformedInput:
    def test_skips_invalid_json_lines(self, tmp_path):
        f = tmp_path / "proj" / f"{_session_id()}.jsonl"
        f.parent.mkdir()
        f.write_text(
            'not-json\n'
            + json.dumps(_user("valid message")) + "\n"
            + json.dumps(_assistant("response")) + "\n"
        )
        result = parse_session_file(f)
        assert result is not None
        assert result.message_count == 2

    def test_skips_unknown_record_types(self, tmp_path):
        f = tmp_path / "proj" / f"{_session_id()}.jsonl"
        f.parent.mkdir()
        _write_jsonl(f, [
            {"type": "summary", "content": "some summary"},
            _user("real message"),
            _assistant("response"),
        ])
        result = parse_session_file(f)
        assert result is not None
        assert result.message_count == 2

    def test_content_as_list_with_text_blocks(self, tmp_path):
        f = tmp_path / "proj" / f"{_session_id()}.jsonl"
        f.parent.mkdir()
        _write_jsonl(f, [
            _user("question"),
            _assistant([
                {"type": "text", "text": "Part one."},
                {"type": "text", "text": "Part two."},
            ]),
        ])
        result = parse_session_file(f)
        asst = next(m for m in result.messages if m.role == "assistant")
        assert "Part one" in asst.content
        assert "Part two" in asst.content
