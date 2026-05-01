"""Subscription-cost estimation for AI coding tools.

The per-token "Cost (API)" column reflects what a metered API customer
would pay for the same workload. Most users instead pay a flat monthly
subscription, so the API-equivalent number can be 10-50x what hits their
card. This module estimates real subscription spend.

Data fetched from each provider's published pricing page on 2026-04-30:

- Claude:           https://claude.com/pricing
- GitHub Copilot:   https://github.com/features/copilot/plans
- Cursor:           https://cursor.com/pricing
- Windsurf:         https://windsurf.com/pricing
- Gemini Code Assist: https://cloud.google.com/products/gemini/code-assist
- ChatGPT (Codex):  https://chatgpt.com/pricing
- Kiro:             https://kiro.dev (preview, free)
- Antigravity:      https://antigravity.google (preview, free)

Tiers are listed cheapest-paid first. For each tier we record whether it
unlocks "premium" models (Opus, GPT-5, Gemini Pro) so we can detect the
user's plan from the models actually used in their sessions.

Refreshing: this is a static table. Re-run `WebFetch` on each pricing
page periodically and update.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

# (tier_name, monthly_usd, unlocks_premium_models)
PROVIDER_PLAN_TIERS: dict[str, list[tuple[str, float, bool]]] = {
    "claude-code": [
        ("Free",   0.0,  False),
        ("Pro",    20.0, False),  # Sonnet/Haiku
        ("Max 5x", 100.0, True),  # Adds Opus
        ("Max 20x", 200.0, True),
    ],
    "copilot": [
        ("Free",  0.0,  False),
        ("Pro",   10.0, False),
        ("Pro+",  39.0, True),
    ],
    "cursor": [
        ("Hobby", 0.0,  False),
        ("Pro",   20.0, False),
        ("Pro+",  60.0, False),
        ("Ultra", 200.0, True),
    ],
    "windsurf": [
        ("Free", 0.0,   False),
        ("Pro",  20.0,  False),
        ("Max",  200.0, True),
    ],
    "gemini": [
        ("Free",       0.0,  False),
        ("Standard",   19.0, False),
        ("Enterprise", 45.0, True),
    ],
    "codex": [
        ("Free", 0.0,   False),
        ("Plus", 20.0,  False),
        ("Pro",  200.0, True),
    ],
    "kiro":        [("Preview", 0.0, False)],
    "antigravity": [("Preview", 0.0, False)],
}


# Substrings that mark a model as "premium" (only the higher-paid tier
# unlocks it). Uses lowercase containment so dated/regional variants
# match without enumeration.
_PREMIUM_MODEL_MARKERS = (
    "opus",            # Claude Opus
    "gpt-5",           # OpenAI GPT-5 family
    "gemini-3-pro",    # Gemini 3 Pro
    "gemini-2.5-pro",  # Gemini 2.5 Pro
    "o3",              # OpenAI reasoning models
)


def is_premium_model(model: str | None) -> bool:
    if not model:
        return False
    m = model.lower()
    return any(marker in m for marker in _PREMIUM_MODEL_MARKERS)


def detect_plan_tier(
    provider_id: str,
    models_used: Iterable[str | None],
) -> tuple[str, float]:
    """Pick the cheapest plan tier that supports every model seen.

    If any premium model (Opus, GPT-5, etc.) appears, returns the
    cheapest premium-unlocked tier. Otherwise returns the cheapest paid
    tier. Free preview providers (Kiro, Antigravity) always return their
    single free tier.
    """
    tiers = PROVIDER_PLAN_TIERS.get(provider_id)
    if not tiers:
        return ("Unknown", 20.0)

    needs_premium = any(is_premium_model(m) for m in models_used)
    if needs_premium:
        for name, price, premium in tiers:
            if premium:
                return (name, price)

    paid = [(n, p) for n, p, _ in tiers if p > 0]
    if paid:
        return paid[0]
    return (tiers[0][0], tiers[0][1])


def subscription_cost_for_range(
    provider_id: str,
    models_used: Iterable[str | None],
    started_at: datetime | None,
    ended_at: datetime | None,
) -> dict:
    """Estimate flat-rate subscription cost over a date range.

    Returns ``{"plan": str, "monthly_usd": float, "months": float, "cost_usd": float}``.
    A range of less than one month still bills the full month — flat
    subscriptions don't prorate for short usage windows. Use the call
    site to decide if you want ceil-to-month or fractional.
    """
    plan, monthly = detect_plan_tier(provider_id, models_used)
    if not started_at or not ended_at:
        return {"plan": plan, "monthly_usd": monthly, "months": 0.0, "cost_usd": 0.0}
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    if ended_at.tzinfo is None:
        ended_at = ended_at.replace(tzinfo=timezone.utc)

    delta_days = max(0.0, (ended_at - started_at).total_seconds() / 86400)
    # Charge in whole months, rounded up: any usage in a calendar month
    # means the full month was paid for.
    months = max(1.0, delta_days / 30.0) if delta_days > 0 else 0.0
    return {
        "plan": plan,
        "monthly_usd": monthly,
        "months": round(months, 2),
        "cost_usd": round(monthly * months, 2),
    }
