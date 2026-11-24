"""Turns Meta's budget fields into amounts in the account's currency.

Meta returns budgets in the currency's smallest unit (cents for USD, centavos
for PHP). Eleven currencies have no smaller unit and use an offset of 1:
https://developers.facebook.com/docs/marketing-api/currencies
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

OFFSET_ONE_CURRENCIES = frozenset(
    {"CLP", "COP", "CRC", "HUF", "ISK", "IDR", "JPY", "KRW", "PYG", "TWD", "VND"})


@dataclass(frozen=True)
class Budgets:
    daily: float | None          # total daily budget, None when the campaign has none
    lifetime: float | None       # total lifetime budget, None when the campaign has none
    largest_daily: float | None  # the biggest single daily budget (campaign or one ad set)

    @property
    def effective(self) -> float:
        """The amount used to spot sudden budget jumps."""
        return self.daily or self.lifetime or 0.0


def to_major(raw: Any, currency: str) -> float | None:
    """'150000' with PHP -> 1500.0. Returns None for missing, zero or invalid values."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return value if currency.upper() in OFFSET_ONE_CURRENCIES else value / 100


def campaign_budgets(campaign: Mapping[str, Any], ad_sets: Iterable[Mapping[str, Any]],
                     currency: str) -> Budgets:
    """Campaign budget (CBO) if set, otherwise the sum of its active ad sets (ABO)."""
    daily = to_major(campaign.get("daily_budget"), currency)
    lifetime = to_major(campaign.get("lifetime_budget"), currency)
    if daily or lifetime:
        return Budgets(daily, lifetime, daily)
    dailies = [v for v in (to_major(s.get("daily_budget"), currency) for s in ad_sets) if v]
    lifetimes = [v for v in (to_major(s.get("lifetime_budget"), currency) for s in ad_sets) if v]
    return Budgets(
        daily=sum(dailies) or None,
        lifetime=sum(lifetimes) or None,
        largest_daily=max(dailies, default=None),
    )


def to_spend(raw: Any) -> float:
    """Insights report spend already in major units, as a string like '123.45'."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) and value > 0 else 0.0
