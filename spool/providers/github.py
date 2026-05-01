"""GitHub provider — ingests pull request and issue threads as sessions.

One PR or issue = one Spool session. Each comment / review comment /
review event becomes a ParsedMessage. The PR/issue title is the session
title; the repo is the session ``project``; ``git_branch`` is the
source branch (PRs only).

Distinct from the existing ``copilot`` provider (which reads VS Code's
workspaceStorage chat sessions). This one is HTTP-only — no local files.

Config (stored on the providers row's ``config`` JSONB):
    api_url  — base URL, default ``https://api.github.com``; for GitHub
               Enterprise Server set ``https://github.example.com/api/v3``.
    token    — personal access token (classic or fine-grained) with
               ``repo`` (or ``public_repo`` for public-only) scope.
    scope    — ``involves`` (default — author OR assignee OR mentioned),
               ``author``, or ``assignee``.

Sync state (``config['sync_state']``):
    updated_after — ISO timestamp; only PRs/issues updated since this
                    point are fetched on subsequent syncs.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from spool.parser import ParsedMessage, ParsedSession, _parse_timestamp
from spool.providers.base import RemoteProvider

DEFAULT_API_URL = "https://api.github.com"
PAGE_SIZE = 50


class GitHubProvider(RemoteProvider):
    type_id = "github"
    name = "GitHub"

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
            raise ValueError("GitHub provider requires a personal access token in config['token']")
        api_url = (cfg.get("api_url") or DEFAULT_API_URL).rstrip("/")
        scope = cfg.get("scope") or "involves"

        st = state or {}
        updated_after = st.get("updated_after")

        for issue in _search_issues(api_url, token, scope, updated_after):
            session = _issue_to_session(api_url, token, issue)
            yield session, {"kind": "remote", "cursor": issue["updated_at"]}


# ---- HTTP helpers -----------------------------------------------------


def _request(url: str, token: str) -> tuple[Any, dict[str, str]]:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "spool-github/1",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.load(resp)
        headers = {k.lower(): v for k, v in resp.headers.items()}
    return body, headers


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


def _search_issues(api_url: str, token: str, scope: str, updated_after: str | None) -> Iterator[dict]:
    """Use the GitHub search API to find PRs/issues touching the user.

    Cheaper than walking every repo, and naturally pages.
    """
    auth_user = _authenticated_user(api_url, token)
    qualifier = {
        "involves": f"involves:{auth_user}",
        "author": f"author:{auth_user}",
        "assignee": f"assignee:{auth_user}",
    }.get(scope, f"involves:{auth_user}")
    q = qualifier
    if updated_after:
        q = f"{q} updated:>={updated_after}"

    params = urllib.parse.urlencode({
        "q": q,
        "sort": "updated",
        "order": "asc",
        "per_page": str(PAGE_SIZE),
    })
    url: str | None = f"{api_url}/search/issues?{params}"
    while url:
        body, headers = _request(url, token)
        for item in body.get("items", []):
            yield item
        url = _next_link(headers.get("link", ""))


def _authenticated_user(api_url: str, token: str) -> str:
    body, _ = _request(f"{api_url}/user", token)
    return body["login"]


# ---- Mapping ---------------------------------------------------------


def _issue_to_session(api_url: str, token: str, issue: dict) -> ParsedSession:
    is_pr = "pull_request" in issue
    repo_full = _repo_from_issue(issue)
    number = issue["number"]
    session_id = f"github-{'pr' if is_pr else 'issue'}-{repo_full.replace('/', '-')}-{number}"

    # Comments timeline (issue + PR share /issues/{n}/comments).
    comments = _paginated(
        f"{api_url}/repos/{repo_full}/issues/{number}/comments?per_page={PAGE_SIZE}",
        token,
    )

    # PR-only: review threads carry the meatiest discussion. Pull review
    # comments and review summaries too.
    review_comments: list[dict] = []
    reviews: list[dict] = []
    if is_pr:
        review_comments = _paginated(
            f"{api_url}/repos/{repo_full}/pulls/{number}/comments?per_page={PAGE_SIZE}",
            token,
        )
        reviews = _paginated(
            f"{api_url}/repos/{repo_full}/pulls/{number}/reviews?per_page={PAGE_SIZE}",
            token,
        )

    author_login = (issue.get("user") or {}).get("login")
    messages: list[ParsedMessage] = []

    if issue.get("body"):
        messages.append(ParsedMessage(
            uuid=f"{session_id}-body",
            session_id=session_id,
            role="user",
            content=issue["body"],
            timestamp=_parse_timestamp(issue.get("created_at")),
            estimated_tokens=max(1, len(issue["body"]) // 4),
        ))

    # Merge all comment streams in chronological order.
    timeline: list[tuple[str, dict]] = []
    timeline.extend(("comment", c) for c in comments)
    timeline.extend(("review_comment", c) for c in review_comments)
    timeline.extend(("review", r) for r in reviews if r.get("body") or r.get("state"))
    timeline.sort(key=lambda kv: kv[1].get("submitted_at") or kv[1].get("created_at") or "")

    for kind, item in timeline:
        if kind == "review":
            body = item.get("body") or f"({item.get('state', 'reviewed').lower()})"
        else:
            body = item.get("body") or ""
        if not body.strip():
            continue
        login = (item.get("user") or {}).get("login")
        role = "assistant" if login == author_login else "user"
        ts = _parse_timestamp(item.get("submitted_at") or item.get("created_at"))
        messages.append(ParsedMessage(
            uuid=f"{session_id}-{kind}-{item.get('id')}",
            session_id=session_id,
            role=role,
            content=body,
            timestamp=ts,
            estimated_tokens=max(1, len(body) // 4),
        ))

    branch = None
    if is_pr:
        # Cheap: don't fetch the PR body, look at the search-result shape.
        # Search API returns pull_request URL but not the head ref; skip
        # the extra fetch and leave branch None unless we already have it.
        head = issue.get("head") if isinstance(issue.get("head"), dict) else None
        if head:
            branch = head.get("ref")

    return ParsedSession(
        session_id=session_id,
        project=repo_full,
        messages=messages,
        started_at=_parse_timestamp(issue.get("created_at")),
        ended_at=_parse_timestamp(issue.get("updated_at")),
        cwd=issue.get("html_url"),
        git_branch=branch,
        title=issue.get("title"),
        provider_id="github",
    )


def _repo_from_issue(issue: dict) -> str:
    """Extract ``owner/repo`` from a search-result issue payload."""
    repo_url = issue.get("repository_url") or ""
    # https://api.github.com/repos/{owner}/{repo}
    parts = repo_url.rstrip("/").split("/")
    if len(parts) >= 2 and parts[-2:]:
        return "/".join(parts[-2:])
    return "unknown/unknown"


def _paginated(url: str, token: str) -> list[dict]:
    out: list[dict] = []
    next_url: str | None = url
    while next_url:
        body, headers = _request(next_url, token)
        if isinstance(body, list):
            out.extend(body)
        next_url = _next_link(headers.get("link", ""))
    return out
