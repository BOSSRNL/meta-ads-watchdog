"""Decides whether today is a normal day or a sale day (payday, double-day, extra date)."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date

from .config import SaleDays

NORMAL = "normal"
SALE = "sale"


@dataclass(frozen=True)
class DayMode:
    kind: str  # NORMAL or SALE
    reason: str = ""

    @property
    def is_sale(self) -> bool:
        return self.kind == SALE


def day_mode(today: date, sale_days: SaleDays) -> DayMode:
    """A payday past the end of the month (30 in February) falls on its last day."""
    if today in sale_days.extra_dates:
        return DayMode(SALE, f"Extra sale date ({today:%b %d})")
    if sale_days.double_days and today.day == today.month:
        return DayMode(SALE, f"Double-day sale ({today.month}.{today.day})")
    last_day = calendar.monthrange(today.year, today.month)[1]
    if today.day in {min(day, last_day) for day in sale_days.payday_days}:
        return DayMode(SALE, f"Payday ({today:%b %d})")
    return DayMode(NORMAL)
