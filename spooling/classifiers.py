"""Classify tool spans into vendor + category.

`vendor` = which service/product the tool talks to (linear, github, slack,
anthropic, filesystem, shell...). `category` = what kind of thing the tool
does (issue-tracker, vcs, chat, docs, web, filesystem, shell, planning,
llm, search, exec).

Classification is prefix-based, biased toward MCP tool-name conventions
(`mcp__<server>__<action>`). Unknown
names fall through to ("unknown", "other"). Callers can add custom entries
via `register_classifier` at import time.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolClass:
    vendor: str
    category: str


_MCP_VENDORS: dict[str, ToolClass] = {
    "linear":     ToolClass("linear",     "issue-tracker"),
    "github":     ToolClass("github",     "vcs"),
    "gitlab":     ToolClass("gitlab",     "vcs"),
    "bitbucket":  ToolClass("bitbucket",  "vcs"),
    "slack":      ToolClass("slack",      "chat"),
    "discord":    ToolClass("discord",    "chat"),
    "teams":      ToolClass("teams",      "chat"),
    "notion":     ToolClass("notion",     "docs"),
    "confluence": ToolClass("confluence", "docs"),
    "jira":       ToolClass("jira",       "issue-tracker"),
    "atlassian":  ToolClass("atlassian",  "issue-tracker"),
    "stripe":     ToolClass("stripe",     "payments"),
    "paypal":     ToolClass("paypal",     "payments"),
    "vercel":     ToolClass("vercel",     "hosting"),
    "netlify":    ToolClass("netlify",    "hosting"),
    "cloudflare": ToolClass("cloudflare", "hosting"),
    "aws":        ToolClass("aws",        "cloud"),
    "gcp":        ToolClass("gcp",        "cloud"),
    "azure":      ToolClass("azure",      "cloud"),
    "snowflake":  ToolClass("snowflake",  "database"),
    "bigquery":   ToolClass("bigquery",   "database"),
    "postgres":   ToolClass("postgres",   "database"),
    "mysql":      ToolClass("mysql",      "database"),
    "mongodb":    ToolClass("mongodb",    "database"),
    "redis":      ToolClass("redis",      "database"),
    "supabase":   ToolClass("supabase",   "database"),
    "neon":       ToolClass("neon",       "database"),
    "sentry":     ToolClass("sentry",     "observability"),
    "datadog":    ToolClass("datadog",    "observability"),
    "grafana":    ToolClass("grafana",    "observability"),
    "honeycomb":  ToolClass("honeycomb",  "observability"),
    "openai":     ToolClass("openai",     "llm"),
    "anthropic":  ToolClass("anthropic",  "llm"),
    "gmail":      ToolClass("gmail",      "email"),
    "calendar":   ToolClass("calendar",   "calendar"),
    "drive":      ToolClass("drive",      "storage"),
    "dropbox":    ToolClass("dropbox",    "storage"),
    "figma":      ToolClass("figma",      "design"),
    "shopify":    ToolClass("shopify",    "commerce"),
}

# Built-in tools → local-execution vendors.
_BUILTIN: dict[str, ToolClass] = {
    # Filesystem
    "Read":         ToolClass("filesystem", "filesystem"),
    "Write":        ToolClass("filesystem", "filesystem"),
    "Edit":         ToolClass("filesystem", "filesystem"),
    "NotebookEdit": ToolClass("filesystem", "filesystem"),
    "Glob":         ToolClass("filesystem", "filesystem"),
    # Shell
    "Bash":         ToolClass("shell",      "shell"),
    "exec":         ToolClass("shell",      "shell"),
    # Search
    "Grep":         ToolClass("search",     "search"),
    "ToolSearch":   ToolClass("search",     "search"),
    # Web
    "WebFetch":     ToolClass("web",        "web"),
    "WebSearch":    ToolClass("web",        "web"),
    # Agents / planning
    "Task":         ToolClass("agent",      "agent"),
    "Agent":        ToolClass("agent",      "agent"),
    "TaskCreate":   ToolClass("planning",   "planning"),
    "TaskUpdate":   ToolClass("planning",   "planning"),
    "TaskList":     ToolClass("planning",   "planning"),
    "TaskGet":      ToolClass("planning",   "planning"),
    "TaskStop":     ToolClass("planning",   "planning"),
    "TaskOutput":   ToolClass("planning",   "planning"),
    # Process / monitor
    "Monitor":      ToolClass("shell",      "shell"),
    # Git / GitHub via gh CLI typically appears as Bash(gh ...); leave alone.
    # Notebook
    "Jupyter":      ToolClass("filesystem", "filesystem"),
}

UNKNOWN = ToolClass("unknown", "other")


def register_classifier(tool_name: str, vendor: str, category: str) -> None:
    """Register or override classification for a specific tool name."""
    _BUILTIN[tool_name] = ToolClass(vendor, category)


def register_mcp_vendor(prefix: str, vendor: str, category: str) -> None:
    """Register or override classification for an MCP vendor prefix."""
    _MCP_VENDORS[prefix] = ToolClass(vendor, category)


def classify(tool_name: str | None) -> ToolClass:
    """Return (vendor, category) for a tool_name. Never raises."""
    if not tool_name:
        return UNKNOWN

    # MCP: mcp__<vendor>__<action> or mcp_<vendor>_<action>
    low = tool_name.lower()
    if low.startswith("mcp__") or low.startswith("mcp_"):
        rest = low.split("mcp__", 1)[-1] if "mcp__" in low else low.split("mcp_", 1)[-1]
        # Next segment up to next __ or _ is the server name, which we match
        # against our vendor table as a substring (so "linear_server" → linear).
        head = rest.split("__", 1)[0].split("_", 1)[0]
        for prefix, cls in _MCP_VENDORS.items():
            if head.startswith(prefix) or prefix in head:
                return cls
        return ToolClass(head or "mcp", "mcp")

    # Exact match on builtins
    cls = _BUILTIN.get(tool_name)
    if cls is not None:
        return cls

    # Case-insensitive builtin fallback
    for k, v in _BUILTIN.items():
        if k.lower() == low:
            return v

    # Heuristic: plain vendor prefix like "linear.create_issue" or "slack-send"
    for sep in (".", "-", ":", "_"):
        if sep in tool_name:
            head = tool_name.split(sep, 1)[0].lower()
            if head in _MCP_VENDORS:
                return _MCP_VENDORS[head]

    return UNKNOWN
