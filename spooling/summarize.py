"""Generate short summaries of local sessions with a user-configured LLM.

Powers ``spooling summarize``. Nothing here runs during sync/ingest; it only
runs when the user asks for it.

Backends:

* ``openai``     (default) any OpenAI-compatible chat-completions endpoint:
                 local Ollama (``http://localhost:11434/v1``), OpenAI, OpenRouter,
                 vLLM, LiteLLM, ... By default no session text leaves the machine.
* ``anthropic``  the Anthropic API (``ANTHROPIC_API_KEY``)
* ``bedrock``    AWS Bedrock via the Converse API (``AWS_BEARER_TOKEN_BEDROCK`` or
                 any normal AWS credentials; ``AWS_REGION``)

Transcripts are passed through the secret redactor before they are sent.

Configuration (flags on the CLI override these):

* ``SPOOLING_SUMMARY_BACKEND``   default ``openai``
* ``SPOOLING_SUMMARY_BASE_URL``  default ``http://localhost:11434/v1``
* ``SPOOLING_SUMMARY_MODEL``     default ``qwen2.5:7b`` (openai), ``claude-haiku-4-5`` (anthropic);
                                 required for bedrock
* ``SPOOLING_SUMMARY_API_KEY``   optional bearer token for hosted endpoints
"""

from __future__ import annotations

import functools
import os
from urllib.parse import urlparse

import httpx

from spooling.redact import redact_text

DEFAULT_BASE_URL = os.environ.get("SPOOLING_SUMMARY_BASE_URL", "http://localhost:11434/v1")
DEFAULT_MODEL = os.environ.get("SPOOLING_SUMMARY_MODEL", "qwen2.5:7b")

# Transcript budget sent to the model, in characters (~4 chars/token).
MAX_TRANSCRIPT_CHARS = 12_000
# Each message is clipped to this many chars before the budget is applied, so
# one huge paste or tool dump can't crowd out the rest of the conversation.
MAX_MESSAGE_CHARS = 1_200
MAX_SUMMARY_CHARS = 600

SYSTEM_PROMPT = (
    "You summarize AI coding assistant sessions. Given a transcript, reply with "
    "a 1-3 sentence plain-text summary: what the user was trying to do, what "
    "was done, and the outcome or open issue. Do not use markdown, bullet "
    "points, or a preamble like 'This session'. Do not repeat secrets or "
    "credentials."
)

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def is_local_url(url: str) -> bool:
    """True if ``url`` points at this machine (so no data leaves it)."""
    host = (urlparse(url).hostname or "").lower()
    return host in _LOCAL_HOSTS or host.endswith(".localhost")


def build_transcript(conn, session_id: str, max_chars: int = MAX_TRANSCRIPT_CHARS) -> str:
    """Return a redacted, size-bounded transcript of a session.

    When the conversation is longer than ``max_chars`` the beginning (the ask)
    and the end (the outcome) are kept and the middle is elided.
    """
    rows = conn.execute(
        "SELECT role, content FROM messages WHERE session_id = ? ORDER BY timestamp",
        (session_id,),
    ).fetchall()

    parts: list[str] = []
    for r in rows:
        content = (r["content"] or "").strip()
        if not content:
            continue
        if len(content) > MAX_MESSAGE_CHARS:
            content = content[:MAX_MESSAGE_CHARS] + " [...]"
        parts.append(f"[{r['role'] or 'unknown'}] {content}")

    if not parts:
        return ""

    total = sum(len(p) + 1 for p in parts)
    if total > max_chars:
        head_budget = int(max_chars * 0.4)
        tail_budget = max_chars - head_budget
        head: list[str] = []
        used = 0
        for p in parts:
            if used + len(p) > head_budget and head:
                break
            head.append(p)
            used += len(p) + 1
        tail: list[str] = []
        used = 0
        for p in reversed(parts[len(head):]):
            if used + len(p) > tail_budget and tail:
                break
            tail.append(p)
            used += len(p) + 1
        tail.reverse()
        omitted = len(parts) - len(head) - len(tail)
        if omitted > 0:
            head.append(f"[... {omitted} messages omitted ...]")
        parts = head + tail

    redacted, _hits = redact_text("\n".join(parts))
    return redacted


def clean_summary(text: str) -> str:
    """Normalize model output into a single short plain-text paragraph."""
    text = (text or "").strip()
    # Some models wrap output in quotes or emit reasoning blocks.
    if "</think>" in text:
        text = text.split("</think>", 1)[1].strip()
    text = " ".join(text.split())
    text = text.strip("\"'`")
    if len(text) > MAX_SUMMARY_CHARS:
        cut = text[:MAX_SUMMARY_CHARS]
        # Prefer ending on a sentence boundary if there is one reasonably late.
        idx = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        text = cut[: idx + 1] if idx > MAX_SUMMARY_CHARS // 2 else cut.rstrip() + "..."
    return text


BACKENDS = ("openai", "anthropic", "bedrock")
DEFAULT_BACKEND = os.environ.get("SPOOLING_SUMMARY_BACKEND", "openai")
DEFAULT_ANTHROPIC_MODEL = "claude-haiku-4-5"
MAX_OUTPUT_TOKENS = 300


def is_local_backend(backend: str, base_url: str) -> bool:
    """True if the transcript stays on this machine for this backend."""
    return backend == "openai" and is_local_url(base_url)


def resolve_model(backend: str, model: str | None) -> str:
    """Pick the model id, with a per-backend default where one is safe."""
    if model:
        return model
    if backend == "openai":
        return DEFAULT_MODEL
    if backend == "anthropic":
        return os.environ.get("SPOOLING_SUMMARY_MODEL") or DEFAULT_ANTHROPIC_MODEL
    # Bedrock ids are region/account specific (inference profiles like
    # "us.anthropic....-v1:0"), so there is no default that is safe everywhere.
    env = os.environ.get("SPOOLING_SUMMARY_MODEL")
    if env:
        return env
    raise ValueError(
        "bedrock needs a model id: pass --model (e.g. an inference profile id like "
        "us.anthropic.claude-haiku-4-5-20251001-v1:0) or set SPOOLING_SUMMARY_MODEL"
    )


def _user_prompt(transcript: str) -> str:
    return f"Transcript:\n\n{transcript}\n\nSummary:"


def _complete_openai(transcript, *, model, base_url, api_key, timeout, client) -> str:
    api_key = api_key or os.environ.get("SPOOLING_SUMMARY_API_KEY")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {
        "model": model,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _user_prompt(transcript)},
        ],
    }
    url = base_url.rstrip("/") + "/chat/completions"

    owns_client = client is None
    client = client or httpx.Client(timeout=timeout)
    try:
        resp = client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    finally:
        if owns_client:
            client.close()

    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise ValueError(f"unexpected response from {url}: {str(data)[:200]}") from e


@functools.lru_cache(maxsize=4)
def _anthropic_client(api_key: str | None, timeout: float):
    import anthropic

    return anthropic.Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"), timeout=timeout)


def _complete_anthropic(transcript, *, model, api_key, timeout) -> str:
    client = _anthropic_client(api_key, timeout)
    resp = client.messages.create(
        model=model,
        max_tokens=MAX_OUTPUT_TOKENS,
        temperature=0.2,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _user_prompt(transcript)}],
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


@functools.lru_cache(maxsize=4)
def _bedrock_client(region: str | None):
    try:
        import boto3
    except ImportError as e:  # boto3 normally ships via strands-agents
        raise RuntimeError("the bedrock backend needs boto3: pip install -U boto3") from e
    # boto3 itself only reads AWS_DEFAULT_REGION; honor AWS_REGION too, since
    # that is what Bedrock API keys and most AWS tooling document.
    region = region or os.environ.get("AWS_REGION")
    kwargs = {"region_name": region} if region else {}
    return boto3.client("bedrock-runtime", **kwargs)


def _complete_bedrock(transcript, *, model, region, timeout) -> str:
    # boto3 reads AWS_BEARER_TOKEN_BEDROCK (Bedrock API keys) as well as the
    # usual profile / SSO / env credentials; nothing to wire up here.
    client = _bedrock_client(region)
    resp = client.converse(
        modelId=model,
        system=[{"text": SYSTEM_PROMPT}],
        messages=[{"role": "user", "content": [{"text": _user_prompt(transcript)}]}],
        inferenceConfig={"maxTokens": MAX_OUTPUT_TOKENS, "temperature": 0.2},
    )
    try:
        blocks = resp["output"]["message"]["content"]
    except (KeyError, TypeError) as e:
        raise ValueError(f"unexpected bedrock response: {str(resp)[:200]}") from e
    return "".join(b.get("text", "") for b in blocks)


def generate_summary(
    transcript: str,
    *,
    model: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    api_key: str | None = None,
    backend: str = "openai",
    region: str | None = None,
    timeout: float = 120.0,
    client: httpx.Client | None = None,
) -> str:
    """Ask the model for a summary of ``transcript``. Raises on failure."""
    if backend not in BACKENDS:
        raise ValueError(f"unknown backend {backend!r}; choose from {', '.join(BACKENDS)}")
    model = resolve_model(backend, model)

    if backend == "openai":
        content = _complete_openai(transcript, model=model, base_url=base_url,
                                   api_key=api_key, timeout=timeout, client=client)
    elif backend == "anthropic":
        content = _complete_anthropic(transcript, model=model, api_key=api_key, timeout=timeout)
    else:
        content = _complete_bedrock(transcript, model=model, region=region, timeout=timeout)

    summary = clean_summary(content)
    if not summary:
        raise ValueError("model returned an empty summary")
    # The model could echo something from the transcript that slipped past the
    # first pass; scrub the output too.
    summary, _hits = redact_text(summary)
    return summary


def select_sessions(
    conn,
    *,
    session_ids: list[str] | None = None,
    project: str | None = None,
    provider: str | None = None,
    days: int | None = None,
    min_messages: int = 4,
    force: bool = False,
    limit: int = 20,
) -> list[dict]:
    """Pick sessions that need a summary, newest first.

    A session needs one when it has none, or when a summary that *we*
    generated is stale (the session has grown since). A provider-supplied
    summary (``summary_message_count IS NULL``) is never replaced unless
    ``force`` is set.
    """
    wheres: list[str] = []
    params: list = []

    if session_ids:
        wheres.append("id IN (%s)" % ",".join("?" * len(session_ids)))
        params.extend(session_ids)
    else:
        wheres.append("message_count >= ?")
        params.append(min_messages)
    if project:
        wheres.append("LOWER(project) = LOWER(?)")
        params.append(project)
    if provider:
        wheres.append("provider_id = ?")
        params.append(provider)
    if days:
        from spooling.stats import _days_ago

        wheres.append("started_at >= ?")
        params.append(_days_ago(days))
    if not force:
        wheres.append(
            "(summary IS NULL OR summary = '' OR "
            "(summary_message_count IS NOT NULL AND message_count > summary_message_count))"
        )

    params.append(limit)
    rows = conn.execute(
        f"""SELECT id, provider_id, project, title, message_count, summary
            FROM sessions WHERE {' AND '.join(wheres)}
            ORDER BY started_at DESC NULLS LAST LIMIT ?""",
        tuple(params),
    ).fetchall()
    return [dict(r) for r in rows]


def store_summary(conn, session_id: str, summary: str, message_count: int) -> None:
    conn.execute(
        "UPDATE sessions SET summary = ?, summary_message_count = ? WHERE id = ?",
        (summary, message_count, session_id),
    )
    conn.commit()


_FATAL_NAMES = {
    # anthropic SDK
    "APIConnectionError", "AuthenticationError", "PermissionDeniedError",
    # botocore
    "NoCredentialsError", "EndpointConnectionError", "NoRegionError",
    "PartialCredentialsError", "CredentialRetrievalError", "TokenRetrievalError",
}
_FATAL_AWS_CODES = {
    "AccessDeniedException", "UnrecognizedClientException", "ExpiredTokenException",
    "InvalidSignatureException", "ResourceNotFoundException", "ValidationException",
}


def is_fatal_error(exc: BaseException) -> bool:
    """True for errors that will repeat for every session (bad credentials,
    unreachable server, unknown model), so the run should stop instead of
    failing once per session."""
    if isinstance(exc, httpx.ConnectError):
        return True
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (401, 403, 404):
        return True
    if type(exc).__name__ in _FATAL_NAMES:
        return True
    code = (getattr(exc, "response", None) or {}).get("Error", {}).get("Code") \
        if isinstance(getattr(exc, "response", None), dict) else None
    return code in _FATAL_AWS_CODES
