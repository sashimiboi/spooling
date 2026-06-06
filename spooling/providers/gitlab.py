"""GitLab provider — ingests merge request discussion threads as sessions.

One MR = one Spooling session. Each note (comment / system event / review
thread reply) becomes a ParsedMessage. The MR title is the session
title; the project path is the session ``project``; ``git_branch`` is
the source branch.

Config (stored on the providers row's ``config`` JSONB):
    gitlab_url   — base URL, e.g. ``https://gitlab.com`` or self-hosted
    token        — personal access token (scope: ``read_api``)
    scope        — ``assigned_to_me`` (default), ``created_by_me``, or ``all``

Sync state (kept in ``config['sync_state']`` between runs):
    updated_after — ISO timestamp; only MRs updated since this point are
                    fetched on subsequent syncs.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from spooling.parser import ParsedMessage, ParsedSession, _parse_timestamp
from spooling.providers.base import RemoteProvider
from spooling.tracing import build_flat_trace_from_messages

DEFAULT_URL = "https://gitlab.com"
PAGE_SIZE = 50


class GitLabProvider(RemoteProvider):
    type_id = "gitlab"
    name = "GitLab"

    def iter_sessions(
        self,
        *,
        data_path: Path | None = None,
        config: dict | None = None,
        state: dict | None = None,
    ) -> Iterator[tuple[ParsedSession, dict]]:
        cfg = config or {}
        token = cfg.get("token")
        if not token:
            raise ValueError("GitLab provider requires a personal access token in config['token']")
        base_url = (cfg.get("gitlab_url") or DEFAULT_URL).rstrip("/")
        scope = cfg.get("scope") or "assigned_to_me"

        st = state or {}
        updated_after = st.get("updated_after")

        # Fetch MRs for the authenticated user, oldest-updated first so
        # the cursor advances monotonically and a crashed run picks up
        # where it left off.
        latest_seen = updated_after
        for mr in _paginated_mrs(base_url, token, scope, updated_after):
            session = _mr_to_session(base_url, token, mr)
            yield session, {"kind": "remote", "cursor": mr["updated_at"]}
            latest_seen = max(latest_seen, mr["updated_at"]) if latest_seen else mr["updated_at"]


# ---- HTTP helpers -----------------------------------------------------


def _request(url: str, token: str) -> tuple[Any, dict[str, str]]:
    req = urllib.request.Request(
        url,
        headers={
            "PRIVATE-TOKEN": token,
            "User-Agent": "spooling-gitlab/1",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.load(resp)
        headers = {k.lower(): v for k, v in resp.headers.items()}
    return body, headers


def _paginated_mrs(base_url: str, token: str, scope: str, updated_after: str | None) -> Iterator[dict]:
    params: dict[str, str] = {
        "scope": scope,
        "order_by": "updated_at",
        "sort": "asc",
        "per_page": str(PAGE_SIZE),
    }
    if updated_after:
        params["updated_after"] = updated_after

    url = f"{base_url}/api/v4/merge_requests?{urllib.parse.urlencode(params)}"
    while url:
        body, headers = _request(url, token)
        for mr in body:
            yield mr
        url = _next_link(headers.get("link", ""))


def _next_link(link_header: str) -> str | None:
    if not link_header:
        return None
    for part in link_header.split(","):
        chunks = part.strip().split(";")
        if len(chunks) < 2:
            continue
        url_part = chunks[0].strip().lstrip("<").rstrip(">")
        rel = next((c for c in chunks[1:] if "rel=" in c), "")
        if 'rel="next"' in rel:
            return url_part
    return None


# ---- Mapping ---------------------------------------------------------


def _mr_to_session(base_url: str, token: str, mr: dict) -> ParsedSession:
    project_id = mr["project_id"]
    iid = mr["iid"]
    session_id = f"gitlab-mr-{project_id}-{iid}"
    notes_url = (
        f"{base_url}/api/v4/projects/{project_id}/merge_requests/{iid}"
        f"/notes?sort=asc&order_by=created_at&per_page={PAGE_SIZE}"
    )
    notes: list[dict] = []
    url: str | None = notes_url
    while url:
        body, headers = _request(url, token)
        notes.extend(body)
        url = _next_link(headers.get("link", ""))

    messages: list[ParsedMessage] = []
    author_username = (mr.get("author") or {}).get("username")

    # MR description as the opening "user" message — it sets the context
    # for the thread.
    if mr.get("description"):
        messages.append(
            ParsedMessage(
                uuid=f"{session_id}-desc",
                session_id=session_id,
                role="user",
                content=mr["description"],
                timestamp=_parse_timestamp(mr.get("created_at")),
                estimated_tokens=max(1, len(mr["description"]) // 4),
            )
        )

    for i, n in enumerate(notes):
        body = n.get("body") or ""
        if not body.strip():
            continue
        # System notes (label changes, milestone updates, etc.) become
        # "system" role messages so they're searchable but distinct.
        role = "system" if n.get("system") else (
            "assistant" if (n.get("author") or {}).get("username") == author_username
            else "user"
        )
        messages.append(
            ParsedMessage(
                uuid=f"{session_id}-note-{n.get('id', i)}",
                session_id=session_id,
                role=role,
                content=body,
                timestamp=_parse_timestamp(n.get("created_at")),
                estimated_tokens=max(1, len(body) // 4),
            )
        )

    project_path = (mr.get("references") or {}).get("full", "").rsplit("!", 1)[0]
    if not project_path:
        project_path = f"project:{project_id}"

    session = ParsedSession(
        session_id=session_id,
        project=project_path,
        messages=messages,
        started_at=_parse_timestamp(mr.get("created_at")),
        ended_at=_parse_timestamp(mr.get("updated_at")),
        cwd=mr.get("web_url"),
        git_branch=mr.get("source_branch"),
        title=mr.get("title"),
        provider_id="gitlab",
    )
    session.trace = build_flat_trace_from_messages(
        provider_id="gitlab",
        session_id=session_id,
        project=project_path,
        title=mr.get("title"),
        messages=messages,
        git_branch=mr.get("source_branch"),
    )
    return session
