"""Client-side secret redactor for the spooling push pipeline.

Runs over message content before it leaves the developer's machine. Replaces
matched secrets with placeholders like ``[REDACTED:SNOWFLAKE_PAT]`` so the
conversation still reads naturally in the cloud GUI.

Two layers, in order:
  1. Vendor-specific regexes — high precision, low false-positive rate.
  2. Sensitive env-var heuristic — KEY=VALUE lines where KEY contains a
     sensitive substring (PASSWORD, SECRET, TOKEN, KEY, PAT, etc.) get the
     value scrubbed. Benign env vars like ``SNOWFLAKE_DATABASE=BACKYARD``
     are left alone because ``DATABASE`` isn't on the sensitive list.

We deliberately avoid generic high-entropy detection here — too many false
positives in coding-assistant transcripts (UUIDs, hashes, base64'd IDs,
test fixtures). If you need it, add it as an opt-in flag later.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Tuple


# --- Vendor-specific patterns ----------------------------------------------

# (label, pattern). label is what shows up in [REDACTED:label].
# Patterns must capture the entire secret in group 0 so we can blank it out
# without touching surrounding context.
VENDOR_PATTERNS: List[Tuple[str, re.Pattern]] = [
    # Snowflake PAT (programmatic access token) — JWT-shaped, prefix eyJ
    # We match the JWT shape directly so it's caught even outside KEY=VALUE.
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),

    # AWS access key (AKIA / ASIA / AGPA / ANPA / etc. — 20 char base32-ish)
    ("AWS_ACCESS_KEY", re.compile(r"\b(?:AKIA|ASIA|AGPA|ANPA|AIDA|AROA|AIPA|ANVA|ABIA|ACCA)[0-9A-Z]{16}\b")),

    # AWS secret access key — 40 chars base64-ish, hard to match without context.
    # Only match when adjacent to "aws_secret" or "AWS_SECRET" to avoid FPs.
    ("AWS_SECRET_KEY", re.compile(
        r"(?i)aws[_\-]?secret[_\-]?(?:access[_\-]?)?key\s*[:=]\s*['\"]?([A-Za-z0-9/+=]{40})['\"]?"
    )),

    # GitHub tokens
    ("GITHUB_TOKEN", re.compile(r"\bghp_[A-Za-z0-9]{36,}\b")),
    ("GITHUB_APP_TOKEN", re.compile(r"\b(?:ghs|gho|ghu|ghr)_[A-Za-z0-9]{36,}\b")),
    ("GITHUB_PAT", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{82,}\b")),

    # Anthropic — listed before OpenAI so its more-specific match wins ties.
    ("ANTHROPIC_KEY", re.compile(r"\bsk-ant-(?:api\d{2}|sid\d{2})-[A-Za-z0-9_\-]{40,}\b")),

    # OpenAI — explicit negative lookahead so it doesn't catch sk-ant-... too.
    ("OPENAI_KEY", re.compile(r"\bsk-(?!ant-)(?:proj-)?[A-Za-z0-9_\-]{20,}\b")),

    # Google API key
    ("GOOGLE_API_KEY", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),

    # Slack
    ("SLACK_TOKEN", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b")),

    # Stripe
    ("STRIPE_KEY", re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{20,}\b")),

    # PEM private key blocks — multiline. Match the whole block.
    ("PRIVATE_KEY", re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED |PGP )?PRIVATE KEY-----"
        r"[\s\S]*?"
        r"-----END (?:RSA |EC |OPENSSH |DSA |ENCRYPTED |PGP )?PRIVATE KEY-----",
        re.MULTILINE,
    )),

    # Generic JWT (catch-all for non-Snowflake JWTs after vendor matches)
    # Already covered by the JWT pattern at top, but kept here as a label.
]


# --- Sensitive env-var heuristic -------------------------------------------

# Substrings (case-insensitive) that indicate the env var holds a secret.
SENSITIVE_KEY_SUBSTRINGS = [
    "password", "passwd", "secret", "token", "api_key", "apikey",
    "access_key", "private_key", "auth", "credential", "session_key",
    "encryption", "signing_key", "client_secret", "refresh_token",
    "bearer", "salt", "_pat", "pat=", "dsn",  # connection strings often carry creds
]

# Substrings that DEFINITELY mean it's NOT a secret, even if a sensitive
# substring matched. Order matters: we check this first.
BENIGN_KEY_SUBSTRINGS = [
    "_url", "_uri", "_host", "_port", "_user", "_username",
    "_database", "_schema", "_table", "_region", "_account_name",
    "_id", "_path", "_version", "_role", "_warehouse",
    "_method", "_type", "_name",
]

# KEY=VALUE pattern. Matches lines like:
#   FOO_TOKEN=abc123
#   FOO_TOKEN="abc123"
#   FOO_TOKEN: abc123      (yaml-ish)
# Captures: 1=key, 2=separator, 3=optional-quote, 4=value
ENV_LINE_RE = re.compile(
    r"""(?m)
    ^                             # start of line
    (\s*[A-Z][A-Z0-9_]{2,64})     # KEY
    (\s*[:=]\s*)                  # separator
    (['"])?                       # optional quote
    ([^\n'"\s][^\n'"]{2,})         # VALUE (≥3 chars)
    (?:\3)?                       # closing quote (matches opening if any)
    \s*$
    """,
    re.VERBOSE,
)


def _key_is_sensitive(key: str) -> bool:
    k = key.lower()
    for benign in BENIGN_KEY_SUBSTRINGS:
        if benign in k:
            return False
    return any(s in k for s in SENSITIVE_KEY_SUBSTRINGS)


# --- Public API ------------------------------------------------------------

@dataclass
class RedactionHit:
    label: str  # e.g. "SNOWFLAKE_PAT", "AWS_ACCESS_KEY", "ENV:SNOWFLAKE_PAT"
    span: Tuple[int, int]


def redact_text(text: str) -> Tuple[str, List[RedactionHit]]:
    """Scrub secrets from ``text``. Returns (redacted_text, hits).

    Each hit's ``span`` refers to the *original* text. Hits are reported in
    discovery order, not necessarily sorted.
    """
    if not text:
        return text, []

    hits: List[RedactionHit] = []

    # Pass 1: vendor patterns.
    # We can't naively re.sub because some patterns capture the secret in a
    # subgroup (AWS_SECRET_KEY) while others match the whole secret directly.
    # We collect (start, end, replacement) tuples then apply right-to-left.
    edits: List[Tuple[int, int, str]] = []

    for label, pat in VENDOR_PATTERNS:
        for m in pat.finditer(text):
            # If the pattern has a subgroup, redact only that group; otherwise
            # redact the whole match.
            if m.groups() and m.group(1):
                start, end = m.span(1)
            else:
                start, end = m.span(0)
            edits.append((start, end, f"[REDACTED:{label}]"))
            hits.append(RedactionHit(label=label, span=(start, end)))

    # Pass 2: sensitive KEY=VALUE lines. Skip ranges already redacted.
    redacted_ranges = [(s, e) for s, e, _ in edits]

    def overlaps(start: int, end: int) -> bool:
        for rs, re_ in redacted_ranges:
            if not (end <= rs or start >= re_):
                return True
        return False

    for m in ENV_LINE_RE.finditer(text):
        key = m.group(1).strip()
        if not _key_is_sensitive(key):
            continue
        v_start, v_end = m.span(4)
        if overlaps(v_start, v_end):
            continue
        edits.append((v_start, v_end, f"[REDACTED:ENV:{key}]"))
        hits.append(RedactionHit(label=f"ENV:{key}", span=(v_start, v_end)))

    if not edits:
        return text, hits

    # Two patterns can match the same span (e.g. ANTHROPIC_KEY is a stricter
    # subset of OPENAI_KEY). Keep one edit per region: sort by (start, longer-
    # match-first) and drop any later edit that overlaps a kept one.
    edits.sort(key=lambda x: (x[0], -(x[1] - x[0])))
    kept: List[Tuple[int, int, str]] = []
    last_end = -1
    for start, end, repl in edits:
        if start < last_end:
            continue  # overlaps the previous kept edit; skip
        kept.append((start, end, repl))
        last_end = end

    # Apply right-to-left so earlier offsets stay valid.
    kept.sort(key=lambda x: x[0], reverse=True)
    out = text
    for start, end, repl in kept:
        out = out[:start] + repl + out[end:]
    return out, hits


def redact_messages(messages: list[dict]) -> Tuple[list[dict], int]:
    """Redact every message's ``content`` field in place.

    Returns (messages, total_hits). The list is the same one passed in
    (mutated). Returns total_hits so callers can report a summary like
    "redacted 4 secrets in this push".
    """
    total = 0
    for m in messages:
        content = m.get("content")
        if not isinstance(content, str) or not content:
            continue
        new_content, hits = redact_text(content)
        if hits:
            m["content"] = new_content
            total += len(hits)
    return messages, total


def redact_value(v):
    """Recursively walk arbitrary JSON, redact strings.

    Returns (new_value, total_hits). Containers (dict/list) are returned as
    fresh objects so callers can decide whether to swap in or mutate. Strings
    that don't match anything are returned unchanged.
    """
    if isinstance(v, str):
        new, hits = redact_text(v)
        return new, len(hits)
    if isinstance(v, list):
        total = 0
        out = []
        for item in v:
            new_item, n = redact_value(item)
            out.append(new_item)
            total += n
        return out, total
    if isinstance(v, dict):
        total = 0
        out = {}
        for k, item in v.items():
            new_item, n = redact_value(item)
            out[k] = new_item
            total += n
        return out, total
    return v, 0


# Span fields that can carry secrets. tool_input is the big one — for an
# `edit` tool call it holds {file_path, old_string, new_string}, which is
# exactly where the leaked .env diffs lived in the screenshot. agent_prompt
# is the system/user prompt of a sub-agent. attrs is a free-form dict the
# OTel layer may stuff arbitrary strings into.
SPAN_REDACT_FIELDS = ("tool_input", "tool_output", "agent_prompt", "attrs")


def redact_span(span: dict) -> int:
    """Mutate a span dict in place, return hit count."""
    total = 0
    for field in SPAN_REDACT_FIELDS:
        if field in span and span[field] is not None:
            new, n = redact_value(span[field])
            if n:
                span[field] = new
                total += n
    for ev in span.get("events") or []:
        attrs = ev.get("attrs")
        if attrs is None:
            continue
        new, n = redact_value(attrs)
        if n:
            ev["attrs"] = new
            total += n
    return total


def redact_traces(traces: list[dict]) -> Tuple[list[dict], int]:
    """Mutate each trace's spans in place, return (traces, total_hits)."""
    total = 0
    for t in traces:
        for s in t.get("spans") or []:
            total += redact_span(s)
    return traces, total
