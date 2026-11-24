from datetime import date

import pytest

from meta_ads_watchdog.budgets import Budgets, campaign_budgets, to_major, to_spend
from meta_ads_watchdog.config import SaleDays
from meta_ads_watchdog.sale_days import NORMAL, SALE, day_mode

DEFAULT = SaleDays()


@pytest.mark.parametrize("day, kind, reason", [
    (date(2026, 11, 11), SALE, "Double-day sale (11.11)"),
    (date(2026, 1, 1), SALE, "Double-day sale (1.1)"),
    (date(2026, 11, 15), SALE, "Payday (Nov 15)"),
    (date(2026, 11, 30), SALE, "Payday (Nov 30)"),
    (date(2026, 12, 30), SALE, "Payday (Dec 30)"),
    (date(2026, 12, 31), NORMAL, ""),
    (date(2026, 2, 28), SALE, "Payday (Feb 28)"),
    (date(2028, 2, 29), SALE, "Payday (Feb 29)"),
    (date(2028, 2, 28), NORMAL, ""),
    (date(2026, 11, 18), NORMAL, ""),
])
def test_default_sale_days(day, kind, reason):
    mode = day_mode(day, DEFAULT)
    assert (mode.kind, mode.reason) == (kind, reason)
    assert mode.is_sale is (kind == SALE)


def test_extra_dates_and_switches():
    sale = SaleDays(payday_days=(31,), double_days=False,
                    extra_dates=frozenset({date(2026, 11, 25)}))
    assert day_mode(date(2026, 11, 25), sale).reason == "Extra sale date (Nov 25)"
    assert day_mode(date(2026, 11, 11), sale).kind == NORMAL
    assert day_mode(date(2026, 11, 30), sale).reason == "Payday (Nov 30)"  # 31 -> last day
    assert day_mode(date(2026, 12, 31), sale).kind == SALE


@pytest.mark.parametrize("raw, currency, expected", [
    ("150000", "PHP", 1500.0), (150000, "usd", 1500.0), ("5000", "JPY", 5000.0),
    ("5000", "IDR", 5000.0), ("0", "PHP", None), (None, "PHP", None), ("abc", "PHP", None),
    ("-100", "PHP", None), ("inf", "PHP", None), ("nan", "PHP", None),
])
def test_to_major(raw, currency, expected):
    assert to_major(raw, currency) == expected


@pytest.mark.parametrize("raw, expected", [("123.45", 123.45), (None, 0.0), ("x", 0.0),
                                           ("-5", 0.0), ("nan", 0.0)])
def test_to_spend(raw, expected):
    assert to_spend(raw) == expected


def test_campaign_budget_wins_over_ad_sets():
    budgets = campaign_budgets({"daily_budget": "100000"}, [{"daily_budget": "999999"}], "PHP")
    assert budgets == Budgets(1000.0, None, 1000.0)


def test_ad_set_budgets_are_added_up():
    sets = [{"daily_budget": "50000"}, {"daily_budget": "30000"}, {"lifetime_budget": "1000000"}]
    budgets = campaign_budgets({}, sets, "PHP")
    assert budgets == Budgets(800.0, 10000.0, 500.0)
    assert budgets.effective == 800.0


def test_no_budget_anywhere():
    budgets = campaign_budgets({"daily_budget": "0"}, [], "PHP")
    assert budgets == Budgets(None, None, None)
    assert budgets.effective == 0.0


def test_lifetime_only_effective_budget():
    assert campaign_budgets({"lifetime_budget": "500000"}, [], "PHP").effective == 5000.0
