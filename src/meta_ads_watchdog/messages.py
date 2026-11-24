"""Discord alert texts in English ("en") and Taglish ("tl").

Every alert title keeps an English keyword in both languages, so a team can
search Discord for it: security, anomaly, emergency pause, escalation, issue.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Rules
from .discord import clean
from .rules import Action

ACCOUNT_STATUS = {1: "ACTIVE", 2: "DISABLED", 3: "UNSETTLED", 7: "PENDING_RISK_REVIEW",
                  8: "PENDING_SETTLEMENT", 9: "IN_GRACE_PERIOD", 100: "PENDING_CLOSURE",
                  101: "CLOSED", 201: "ANY_ACTIVE", 202: "ANY_CLOSED"}
DISABLE_REASON = {0: "NONE", 1: "ADS_INTEGRITY_POLICY", 2: "ADS_IP_REVIEW", 3: "RISK_PAYMENT",
                  4: "GRAY_ACCOUNT_SHUT_DOWN", 5: "ADS_AFC_REVIEW", 6: "BUSINESS_INTEGRITY_RAR",
                  7: "PERMANENT_CLOSE", 8: "UNUSED_RESELLER_ACCOUNT", 9: "UNUSED_ACCOUNT",
                  10: "UMBRELLA_AD_ACCOUNT", 11: "BUSINESS_MANAGER_INTEGRITY_POLICY",
                  12: "MISREPRESENTED_AD_ACCOUNT", 13: "AOAB_DESHARE_LEGAL_ENTITY",
                  14: "CTX_THREAD_REVIEW", 15: "COMPROMISED_AD_ACCOUNT"}

TEXT: dict[str, dict[str, str]] = {
    "en": {
        "dry_run": "🧪 **DRY RUN**, nothing was paused or turned on\n",
        "sale_on": "🟢 **Sale-day mode is ON today ({reason})**\nTeam: {team}\n"
                   "Overspent campaigns stay on today; I alert once per campaign. "
                   "A campaign is paused only when spend reaches {multiplier}x its budget, "
                   "and it goes on tomorrow's reactivation list.\n"
                   "The {hard_limit} spend limit and the {anomaly} budget check stay on.",
        Action.PAUSE_OVERSPENT.value: "🔴 **Overspent, paused**",
        Action.PAUSE_ESCALATION.value: "🟠 **Escalation: spend reached {multiplier}x the budget "
                                       "on a sale day, paused**",
        Action.ALERT_SALE_OVERSPENT.value: "🟡 **Overspent but still running (sale day)**",
        Action.PAUSE_HARD_LIMIT.value: "🚨 **Emergency pause: spend reached {hard_limit}**",
        Action.PAUSE_ANOMALY.value: "⚠️ **Budget anomaly: a budget of {anomaly} or more, paused**",
        Action.PAUSE_SPIKE.value: "🛑 **Security alert: budget jumped x{spike} or more, paused**",
        Action.PAUSE_NEW_BIG.value: "🛑 **Security alert: new campaign with a {new_budget}+ "
                                    "budget, paused**",
        "back_tomorrow": "On tomorrow's reactivation list.",
        "sale_left_on": "Left on because today is a sale day. Paused only at {multiplier}x "
                        "the budget or {hard_limit} spend.",
        "check_first": "Not on the reactivation list. Check it before turning it back on.",
        "maybe_hacked": "Not on the reactivation list. Check right away whether your team "
                        "did this; it could be a hacked account.",
        "changed_by": "Changed by: {actor}",
        "no_actor": "not in the activity log yet, check Ads Manager > Activity history",
        "account_issue": "⚠️ **Account issue: {status}**\nTeam: {team}\nAccount: {name} "
                         "({account})\nReason: {reason}\nThis account is skipped until it is "
                         "active again.",
        "pause_failed": "⚠️ **Pause issue: could not pause a campaign**\nTeam: {team}\n"
                        "Account: {account}\nCampaign: {campaign} ({campaign_id})\n"
                        "Planned: {planned}\nError: {error}\nPause it by hand in Ads Manager.",
        "scan_failed": "⚠️ **Scan issue: could not check an ad account**\nTeam: {team}\n"
                       "Account: {account}\nError: {error}\nI will try again on the next run.",
        "token_problem": "🚨 **Token issue: Meta rejected the access token**\nTeam: {team}\n"
                         "Error: {error}\nNothing was checked. Make a new token and update .env.",
        "react_on": "Turned back on",
        "react_done": "✅ **Reactivation done**",
        "react_partial": "⚠️ **Reactivation issue: some campaigns were not turned on**",
        "react_counts": "Back on: {ok} | Retry next run: {retry} | Check by hand: {manual} | "
                        "Paused today, waiting for tomorrow: {waiting}",
        "react_retry": "Not turned on (retried next run)",
        "react_manual": "Check by hand (moved to needs-attention.jsonl)",
        "list_failed": "⚠️ **List issue: could not update the reactivation list**\nTeam: {team}\n"
                       "Campaign: {campaign} ({campaign_id})\nError: {error}\n"
                       "Check the disk. Turn this campaign back on by hand if it should run.",
        "react_abort": "🚨 **Reactivation issue: did not run ({title})**\nTeam: {team}\n"
                       "Reason: {detail}\nThe list was not changed. Run it again when this is fixed.",
        "and_more": "...and {count} more",
    },
    "tl": {
        "dry_run": "🧪 **DRY RUN**, walang na-pause o na-on\n",
        "sale_on": "🟢 **Sale-day mode is ON today ({reason})**\nTeam: {team}\n"
                   "Heads up, hindi muna io-off ang overspent campaigns today. Isang alert "
                   "lang per campaign. Io-off ko lang pag umabot na sa {multiplier}x ng budget "
                   "ang spend, tapos ilalagay sa list para ma-on ulit bukas.\n"
                   "Naka-on pa rin ang {hard_limit} spend limit at ang {anomaly} budget check.",
        Action.PAUSE_OVERSPENT.value: "🔴 **Overspent, turned off**",
        Action.PAUSE_ESCALATION.value: "🟠 **Escalation: {multiplier}x na ng budget ang spend, "
                                       "turned off (sale day)**",
        Action.ALERT_SALE_OVERSPENT.value: "🟡 **Overspent pero naka-on pa (sale day)**",
        Action.PAUSE_HARD_LIMIT.value: "🚨 **Emergency pause: umabot na sa {hard_limit} spend**",
        Action.PAUSE_ANOMALY.value: "⚠️ **Budget anomaly: {anomaly} pataas ang budget, "
                                    "paused muna**",
        Action.PAUSE_SPIKE.value: "🛑 **Security alert: biglang na-x{spike} ang budget, paused**",
        Action.PAUSE_NEW_BIG.value: "🛑 **Security alert: bagong campaign na {new_budget}+ agad "
                                    "ang budget, paused**",
        "back_tomorrow": "Nasa list na, i-o-on ulit bukas.",
        "sale_left_on": "Hindi ko muna io-off kasi sale day today. Io-off ko lang pag "
                        "umabot na sa {multiplier}x ng budget o {hard_limit} spend.",
        "check_first": "Hindi kasama sa reactivation. Pa-check muna bago i-on ulit.",
        "maybe_hacked": "Hindi kasama sa reactivation. Pa-check agad kung kayo ang gumawa "
                        "nito, baka hacker.",
        "changed_by": "Ginawa ni: {actor}",
        "no_actor": "wala pa sa activity log, pa-check sa Ads Manager > Activity history",
        "account_issue": "⚠️ **Account issue: {status}**\nTeam: {team}\nAccount: {name} "
                         "({account})\nReason: {reason}\nSkip muna ang account na ito hanggang "
                         "maging active ulit.",
        "pause_failed": "⚠️ **Pause issue: hindi ma-pause ang campaign**\nTeam: {team}\n"
                        "Account: {account}\nCampaign: {campaign} ({campaign_id})\n"
                        "Dapat sana: {planned}\nError: {error}\nPaki-pause manually sa Ads Manager.",
        "scan_failed": "⚠️ **Scan issue: hindi ma-check ang ad account**\nTeam: {team}\n"
                       "Account: {account}\nError: {error}\nUulitin ko sa next run.",
        "token_problem": "🚨 **Token issue: ayaw tanggapin ni Meta ang access token**\n"
                         "Team: {team}\nError: {error}\nWalang na-check. Gumawa ng bagong token "
                         "at i-update ang .env.",
        "react_on": "Na-on ulit",
        "react_done": "✅ **Reactivation done**",
        "react_partial": "⚠️ **Reactivation issue: may hindi na-on**",
        "react_counts": "Back on: {ok} | Retry next run: {retry} | Check manually: {manual} | "
                        "Na-off today, bukas pa: {waiting}",
        "react_retry": "Hindi na-on (uulitin sa next run)",
        "react_manual": "Check manually (nilipat sa needs-attention.jsonl)",
        "list_failed": "⚠️ **List issue: hindi ma-update ang reactivation list**\nTeam: {team}\n"
                       "Campaign: {campaign} ({campaign_id})\nError: {error}\n"
                       "Pa-check ang disk. I-on manually ang campaign kung dapat siyang tumakbo.",
        "react_abort": "🚨 **Reactivation issue: hindi tumakbo ({title})**\nTeam: {team}\n"
                       "Reason: {detail}\nWalang nagalaw sa list. Paki-run ulit pag okay na.",
        "and_more": "...at {count} pa",
    },
}

FOLLOW_UP = {
    Action.PAUSE_OVERSPENT: "back_tomorrow",
    Action.PAUSE_ESCALATION: "back_tomorrow",
    Action.ALERT_SALE_OVERSPENT: "sale_left_on",
    Action.PAUSE_HARD_LIMIT: "check_first",
    Action.PAUSE_ANOMALY: "check_first",
    Action.PAUSE_SPIKE: "maybe_hacked",
    Action.PAUSE_NEW_BIG: "maybe_hacked",
}
ISSUE_ACTIONS = frozenset({Action.PAUSE_HARD_LIMIT, Action.PAUSE_ANOMALY,
                           Action.PAUSE_SPIKE, Action.PAUSE_NEW_BIG})


def amount(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"


@dataclass(frozen=True)
class CampaignAlert:
    team: str
    account_id: str
    account_name: str
    campaign_id: str
    campaign_name: str
    budget: float | None
    spend: float
    previous_budget: float | None = None
    actor: str | None = None


class Messages:
    def __init__(self, language: str, rules_values: dict[str, str]):
        self._text = TEXT[language]
        self._values = rules_values

    def get(self, key: str, **values: object) -> str:
        return self._text[key].format(**self._values, **values)

    def campaign(self, action: Action, alert: CampaignAlert) -> str:
        budget = amount(alert.budget)
        if alert.previous_budget is not None:
            budget = f"{amount(alert.previous_budget)} → {budget}"
        lines = [
            self.get(action.value),
            f"Team: {clean(alert.team)}",
            f"Ad account: {clean(alert.account_name)} ({alert.account_id})",
            f"Campaign: {clean(alert.campaign_name)}",
            f"Campaign ID: {alert.campaign_id}",
            f"Budget: {budget} | Spend today: {amount(alert.spend)}",
        ]
        if action.is_security:
            lines.append(self.get("changed_by", actor=clean(alert.actor or self.get("no_actor"))))
        lines.append(self.get(FOLLOW_UP[action]))
        return "\n".join(lines)

    def account_issue(self, team: str, account_id: str, name: str, status: object,
                      reason: object) -> str:
        return self.get("account_issue", team=clean(team), account=account_id, name=clean(name),
                        status=ACCOUNT_STATUS.get(_int(status), f"status {clean(status, 20)}"),
                        reason=DISABLE_REASON.get(_int(reason), f"code {clean(reason, 20)}"))


def _int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def rule_values(rules: Rules) -> dict[str, str]:
    """The numbers alert texts mention, formatted once."""
    return {
        "multiplier": amount(rules.sale_day_pause_multiplier),
        "hard_limit": amount(rules.hard_spend_limit),
        "anomaly": amount(rules.anomaly_budget),
        "spike": amount(rules.spike_multiplier),
        "new_budget": amount(rules.new_campaign_budget),
    }
