import pytest

from meta_ads_watchdog.budgets import Budgets
from meta_ads_watchdog.config import Rules
from meta_ads_watchdog.rules import Action, CampaignFacts, decide, tolerance_for
from meta_ads_watchdog.sale_days import DayMode, NORMAL, SALE

RULES = Rules()
NORMAL_DAY = DayMode(NORMAL)
SALE_DAY = DayMode(SALE, "Payday")


def facts(daily=None, spend=0.0, lifetime=None, previous=None, is_new=False, cleared=False,
          largest=None):
    budgets = Budgets(daily, lifetime, largest if largest is not None else daily)
    return CampaignFacts(budgets, spend, previous, is_new, cleared)


@pytest.mark.parametrize("budget, allowed", [
    (0, 10), (99.99, 10), (100, 30), (999, 30), (1000, 150), (9999, 150), (10000, 250), (44000, 250)])
def test_tolerance_tiers_match_the_original_watchdog(budget, allowed):
    assert tolerance_for(budget, RULES) == allowed


@pytest.mark.parametrize("daily, spend, expected", [
    (50, 59.99, Action.KEEP), (50, 60, Action.PAUSE_OVERSPENT),
    (500, 529, Action.KEEP), (500, 530, Action.PAUSE_OVERSPENT),
    (5000, 5149, Action.KEEP), (5000, 5150, Action.PAUSE_OVERSPENT),
    (20000, 20249, Action.KEEP), (20000, 20250, Action.PAUSE_OVERSPENT),
])
def test_normal_day_pauses_at_budget_plus_tolerance(daily, spend, expected):
    assert decide(facts(daily, spend), NORMAL_DAY, RULES) is expected


def test_sale_day_alerts_but_keeps_running_below_twice_the_budget():
    assert decide(facts(500, 999), SALE_DAY, RULES) is Action.ALERT_SALE_OVERSPENT


def test_sale_day_pauses_at_twice_the_budget():
    assert decide(facts(500, 1000), SALE_DAY, RULES) is Action.PAUSE_ESCALATION


def test_sale_day_under_budget_is_kept():
    assert decide(facts(500, 400), SALE_DAY, RULES) is Action.KEEP


def test_hard_limit_wins_over_sale_day():
    assert decide(facts(30000, 40000), SALE_DAY, RULES) is Action.PAUSE_HARD_LIMIT


def test_anomaly_uses_the_largest_single_budget():
    assert decide(facts(45000, 0), NORMAL_DAY, RULES) is Action.PAUSE_ANOMALY
    # ABO: ten ad sets of 5,000 add up to 50,000 but none is suspicious on its own.
    assert decide(facts(50000, 0, largest=5000), NORMAL_DAY, RULES) is Action.KEEP


def test_lifetime_budget_has_no_daily_overspend_check():
    assert decide(facts(None, 30000, lifetime=10000), NORMAL_DAY, RULES) is Action.KEEP
    assert decide(facts(None, 40000, lifetime=100000), NORMAL_DAY, RULES) is Action.PAUSE_HARD_LIMIT


def test_budget_spike_is_a_security_pause():
    assert decide(facts(1000, 0, previous=100), NORMAL_DAY, RULES) is Action.PAUSE_SPIKE
    assert decide(facts(999, 0, previous=100), NORMAL_DAY, RULES) is Action.KEEP


def test_spike_needs_a_previous_budget_above_zero():
    assert decide(facts(1000, 0, previous=0), NORMAL_DAY, RULES) is Action.KEEP


def test_new_big_campaign_is_a_security_pause_until_cleared():
    assert decide(facts(1000, 0, is_new=True), NORMAL_DAY, RULES) is Action.PAUSE_NEW_BIG
    assert decide(facts(999, 0, is_new=True), NORMAL_DAY, RULES) is Action.KEEP
    assert decide(facts(1000, 0, is_new=True, cleared=True), NORMAL_DAY, RULES) is Action.KEEP


def test_security_comes_before_everything_else():
    assert decide(facts(50000, 45000, previous=100), SALE_DAY, RULES) is Action.PAUSE_SPIKE


def test_only_overspend_pauses_are_reactivated():
    assert {a for a in Action if a.reactivate_tomorrow} == {
        Action.PAUSE_OVERSPENT, Action.PAUSE_ESCALATION}
    assert {a for a in Action if a.is_security} == {Action.PAUSE_SPIKE, Action.PAUSE_NEW_BIG}


def test_new_campaign_check_ignores_lifetime_budgets():
    lifetime_only = facts(None, 0, lifetime=30000, is_new=True)
    assert decide(lifetime_only, NORMAL_DAY, RULES) is Action.KEEP


def test_custom_rules_change_the_thresholds():
    rules = Rules(overspend_tolerance=((0, 5),), hard_spend_limit=100,
                  sale_day_pause_multiplier=3)
    assert decide(facts(50, 55), NORMAL_DAY, rules) is Action.PAUSE_OVERSPENT
    assert decide(facts(50, 99), SALE_DAY, rules) is Action.ALERT_SALE_OVERSPENT
    assert decide(facts(80, 100), SALE_DAY, rules) is Action.PAUSE_HARD_LIMIT
