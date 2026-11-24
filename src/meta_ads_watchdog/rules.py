"""The decision for one campaign. Pure: no network, no files, no clock."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .budgets import Budgets
from .config import Rules
from .sale_days import DayMode


class Action(Enum):
    KEEP = "keep"
    PAUSE_SPIKE = "pause_spike"                # security: budget jumped xN
    PAUSE_NEW_BIG = "pause_new_big"            # security: new campaign with a big budget
    PAUSE_ANOMALY = "pause_anomaly"            # a single budget is suspiciously large
    PAUSE_HARD_LIMIT = "pause_hard_limit"      # today's spend reached the hard limit
    PAUSE_OVERSPENT = "pause_overspent"        # normal day, spend went past budget + tolerance
    PAUSE_ESCALATION = "pause_escalation"      # sale day, spend reached budget x multiplier
    ALERT_SALE_OVERSPENT = "alert_sale_overspent"  # sale day, overspent but left running

    @property
    def reactivate_tomorrow(self) -> bool:
        """Only plain overspend pauses go on the list the reactivator turns back on."""
        return self in (Action.PAUSE_OVERSPENT, Action.PAUSE_ESCALATION)

    @property
    def is_security(self) -> bool:
        return self in (Action.PAUSE_SPIKE, Action.PAUSE_NEW_BIG)


@dataclass(frozen=True)
class CampaignFacts:
    budgets: Budgets
    spend: float
    previous_budget: float | None  # effective budget seen on an earlier run
    is_new: bool                   # created within rules.new_campaign_hours
    security_cleared: bool         # paused for security before and turned back on by a person


def tolerance_for(daily_budget: float, rules: Rules) -> float:
    allowed = rules.overspend_tolerance[0][1]
    for minimum, tolerance in rules.overspend_tolerance:
        if daily_budget >= minimum:
            allowed = tolerance
    return allowed


def decide(facts: CampaignFacts, mode: DayMode, rules: Rules) -> Action:
    security = _security_check(facts, rules)
    if security is not Action.KEEP:
        return security
    largest = facts.budgets.largest_daily
    if largest is not None and largest >= rules.anomaly_budget:
        return Action.PAUSE_ANOMALY
    if facts.spend >= rules.hard_spend_limit:
        return Action.PAUSE_HARD_LIMIT
    daily = facts.budgets.daily
    if daily is None:
        return Action.KEEP  # lifetime budgets have no daily limit to compare with
    if facts.spend < daily + tolerance_for(daily, rules):
        return Action.KEEP
    if not mode.is_sale:
        return Action.PAUSE_OVERSPENT
    if facts.spend >= daily * rules.sale_day_pause_multiplier:
        return Action.PAUSE_ESCALATION
    return Action.ALERT_SALE_OVERSPENT


def _security_check(facts: CampaignFacts, rules: Rules) -> Action:
    effective = facts.budgets.effective
    previous = facts.previous_budget
    if previous and previous > 0 and effective >= previous * rules.spike_multiplier:
        return Action.PAUSE_SPIKE
    # Daily budgets only: a lifetime budget of the same amount is spread over many days.
    daily = facts.budgets.daily
    if (facts.is_new and not facts.security_cleared and daily is not None
            and daily >= rules.new_campaign_budget):
        return Action.PAUSE_NEW_BIG
    return Action.KEEP
