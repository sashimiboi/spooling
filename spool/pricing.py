"""Model pricing lookup backed by LiteLLM's open price table.

LiteLLM maintains the most complete community price list I know of for
LLM APIs, covering 500+ models across Anthropic, OpenAI, Google, Mistral,
Bedrock, Vertex and more. The file lives at a stable GitHub raw URL and
is plain JSON, so we can fetch it on demand, cache it on disk, and look
up rates without adding a runtime dependency.

Source: https://github.com/BerriAI/litellm (MIT)
Rate card: model_prices_and_context_window.json

The table gives per-token costs in USD as fractional floats:
    input_cost_per_token
    output_cost_per_token
    cache_creation_input_token_cost
    cache_read_input_token_cost

Spool's formula charges these four components separately, which is more
accurate than the old derived-from-input-rate approach (Anthropic's
actual cache write rate is 1.25x input for 5-minute TTL but 2x for
1-hour TTL; the published table encodes whichever Anthropic bills, so
we don't have to guess).
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
import urllib.error
from dataclasses import dataclass
from pathlib import Path

from spool.config import MODEL_PRICING, DEFAULT_PRICING

# "chat-gemini-3-0-flash-preview-free-tier" -> "gemini-3-flash-preview".
# Gemini Code Assist reports modelID with a "chat-" prefix, a tier suffix
# ("-free-tier" / "-paid-tier"), and an extra ".0" minor version segment
# that LiteLLM's keys don't carry. Normalizing all three lands us on a
# real LiteLLM key.
_GEMINI_CHAT_PREFIX = re.compile(r"^chat-")
_GEMINI_TIER_SUFFIX = re.compile(r"-(?:free|paid)-tier$")
_GEMINI_ZERO_MINOR = re.compile(r"^(gemini-\d+)-0(-)")

PRICING_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)

CACHE_DIR = Path(os.getenv("SPOOL_CACHE_DIR", str(Path.home() / ".spool")))
CACHE_FILE = CACHE_DIR / "model_prices.json"
CACHE_TTL_SECONDS = 7 * 24 * 3600  # refresh weekly


@dataclass(frozen=True)
class ModelRates:
    """Per-token USD costs for one model.

    All four components are separate so we don't have to assume cache
    writes are 1.25x input or cache reads are 0.10x input — the LiteLLM
    table encodes whatever Anthropic / OpenAI actually bills.
    """

    input: float
    output: float
    cache_write: float
    cache_read: float

    def cost(
        self,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_write_tokens: int = 0,
        cache_read_tokens: int = 0,
    ) -> float:
        return round(
            input_tokens * self.input
            + output_tokens * self.output
            + cache_write_tokens * self.cache_write
            + cache_read_tokens * self.cache_read,
            6,
        )


_table: dict | None = None
_table_loaded_from: str | None = None


def _fetch_remote() -> dict:
    req = urllib.request.Request(
        PRICING_URL,
        headers={"User-Agent": "spool-pricing/1"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


def _read_cache() -> dict | None:
    if not CACHE_FILE.exists():
        return None
    try:
        age = time.time() - CACHE_FILE.stat().st_mtime
    except OSError:
        return None
    if age > CACHE_TTL_SECONDS:
        return None
    try:
        with CACHE_FILE.open("r") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(data: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_FILE.with_suffix(".tmp")
    with tmp.open("w") as f:
        json.dump(data, f)
    tmp.replace(CACHE_FILE)


def refresh() -> dict:
    """Force-fetch the remote pricing table, write to cache, return it."""
    global _table, _table_loaded_from
    data = _fetch_remote()
    _write_cache(data)
    _table = data
    _table_loaded_from = "remote"
    return data


def _get_table() -> dict:
    global _table, _table_loaded_from
    if _table is not None:
        return _table
    cached = _read_cache()
    if cached is not None:
        _table = cached
        _table_loaded_from = "cache"
        return cached
    # Cache missing or stale — try remote. Fall back to an empty dict
    # on network failure so callers can still resolve via hard-coded
    # MODEL_PRICING.
    try:
        return refresh()
    except (urllib.error.URLError, TimeoutError, OSError):
        _table = {}
        _table_loaded_from = "fallback-empty"
        return _table


def _candidate_keys(model: str) -> list[str]:
    """Candidate LiteLLM keys to try for a given Spool model name.

    LiteLLM sometimes keys entries as ``claude-opus-4-6`` and sometimes
    prefixes them with a provider like ``anthropic/claude-opus-4-6``.
    Spool's ingests store the raw model slug reported by the SDK, which
    may or may not carry a date suffix. Try a handful of variants so we
    get a hit even on mildly mismatched names.
    """
    if not model:
        return []
    m = model.strip()
    variants = [
        m,
        f"anthropic/{m}",
        m.split("/", 1)[-1] if "/" in m else m,
    ]
    # Strip trailing date suffix: claude-sonnet-4-20250514 -> claude-sonnet-4
    parts = m.split("-")
    if parts and parts[-1].isdigit() and len(parts[-1]) == 8:
        variants.append("-".join(parts[:-1]))
        variants.append(f"anthropic/{'-'.join(parts[:-1])}")

    # Gemini Code Assist normalization: chat-gemini-3-0-flash-preview-free-tier
    # -> gemini-3-flash-preview, plus its gemini/ and vertex_ai/ variants.
    g = _GEMINI_CHAT_PREFIX.sub("", m)
    g = _GEMINI_TIER_SUFFIX.sub("", g)
    g = _GEMINI_ZERO_MINOR.sub(r"\1\2", g)
    if g != m:
        variants.extend([g, f"gemini/{g}", f"vertex_ai/{g}"])
    # De-dupe while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for v in variants:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def get_rates(model: str | None) -> ModelRates:
    """Return per-token USD rates for a model, falling back to DEFAULT_PRICING.

    Resolution order:
    1. LiteLLM table (fetched/cached from GitHub)
    2. Hard-coded MODEL_PRICING in spool.config (input/output only, no cache info)
    3. DEFAULT_PRICING (Sonnet-ish rates)
    """
    table = _get_table()
    for key in _candidate_keys(model or ""):
        entry = table.get(key)
        if not entry:
            continue
        inp = entry.get("input_cost_per_token")
        out = entry.get("output_cost_per_token")
        if inp is None or out is None:
            continue
        cw = entry.get("cache_creation_input_token_cost")
        cr = entry.get("cache_read_input_token_cost")
        # LiteLLM rows without cache pricing get the Anthropic 5-min
        # cache defaults (1.25x / 0.10x input) as a reasonable fallback.
        if cw is None:
            cw = inp * 1.25
        if cr is None:
            cr = inp * 0.10
        return ModelRates(input=inp, output=out, cache_write=cw, cache_read=cr)

    # Fallback — use the hard-coded MODEL_PRICING in $/Mtok.
    fallback = MODEL_PRICING.get(model or "", DEFAULT_PRICING)
    in_per_m, out_per_m = fallback
    return ModelRates(
        input=in_per_m / 1_000_000,
        output=out_per_m / 1_000_000,
        cache_write=in_per_m / 1_000_000 * 1.25,
        cache_read=in_per_m / 1_000_000 * 0.10,
    )


def table_status() -> dict:
    """Introspection for the CLI / GUI — where did pricing come from?"""
    _get_table()
    return {
        "source": _table_loaded_from,
        "entry_count": len(_table) if _table else 0,
        "cache_path": str(CACHE_FILE),
        "cache_exists": CACHE_FILE.exists(),
        "cache_age_seconds": (
            time.time() - CACHE_FILE.stat().st_mtime if CACHE_FILE.exists() else None
        ),
        "url": PRICING_URL,
    }
