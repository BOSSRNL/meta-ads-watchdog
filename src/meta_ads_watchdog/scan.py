"""The watchdog run: checks every active campaign of every team's ad accounts."""

from __future__ import annotations

import dataclasses
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .budgets import campaign_budgets, to_major, to_spend
from .config import Config, Team
from .discord import Notifier, clean, plain
from .graph import GraphClient, GraphError
from .messages import ISSUE_ACTIONS, CampaignAlert, Messages, rule_values
from .rules import Action, CampaignFacts, decide
from .sale_days import DayMode, day_mode
from .state import (PAUSED_FILE, STATE_FILE, LockedError, PausedCampaign, PausedList,
                    TeamLock, TeamState, is_numeric_id)

log = logging.getLogger(__name__)

PAGE_SIZE = 200
ACTIVITY_LOOKBACK = timedelta(days=2)
ACTIVITY_PAGES = 2
CREATED_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
ACCOUNT_FIELDS = "name,account_status,disable_reason,currency,timezone_name"

NotifierFactory = Callable[[Team], Notifier]


@dataclass
class RunResult:
    errors: int = 0
    paused: int = 0
    skipped_teams: list[str] = field(default_factory=list)
    token_failed: bool = False


def local_today(config: Config, now: datetime) -> date:
    return now.astimezone(config.timezone).date()


def run_scan(config: Config, graph: GraphClient, make_notifier: NotifierFactory,
             now: datetime, teams: tuple[Team, ...], dry_run: bool) -> RunResult:
    result = RunResult()
    today = local_today(config, now)
    mode = day_mode(today, config.sale_days)
    log.info("scan started: %s%s%s", mode.kind, f" ({mode.reason})" if mode.reason else "",
             ", DRY RUN" if dry_run else "")
    for team in teams:
        try:
            _scan_team(config, team, graph, make_notifier, mode, now, dry_run, result)
        except LockedError as error:
            log.warning("skipped team %s: %s", team.name, error)
            result.skipped_teams.append(team.name)
        except Exception:  # one team's bug or disk error must not stop the other teams
            log.exception("team %s failed", team.name)
            result.errors += 1
        if result.token_failed:
            break
    return result


def _scan_team(config: Config, team: Team, graph: GraphClient, make_notifier: NotifierFactory,
               mode: DayMode, now: datetime, dry_run: bool, result: RunResult) -> None:
    folder = config.state_dir / team.slug
    with TeamLock(folder) as lock:
        notifier = make_notifier(team)
        try:
            notifier.flush_queue()
        except Exception:  # trouble with old alerts must never stop today's pauses
            log.exception("could not send the queued Discord messages of %s", team.name)
            result.errors += 1
        state = TeamState(folder / STATE_FILE, local_today(config, now))
        scan = TeamScan(config, team, graph, notifier, state, PausedList(folder / PAUSED_FILE),
                        mode, now, dry_run, result, lock.touch)
        try:
            scan.run()
        finally:
            if lock.touch():  # never overwrite the state of a run that took the lock over
                state.save()
            result.errors += notifier.rejected


class TeamScan:
    def __init__(self, config: Config, team: Team, graph: GraphClient, notifier: Notifier,
                 state: TeamState, paused: PausedList, mode: DayMode, now: datetime,
                 dry_run: bool, result: RunResult, heartbeat: Callable[[], bool] = lambda: True):
        self.config, self.team, self.graph = config, team, graph
        self.notifier, self.state, self.paused, self.mode = notifier, state, paused, mode
        self.now, self.dry_run, self.result = now, dry_run, result
        self.heartbeat = heartbeat
        self.messages = Messages(config.language, rule_values(config.rules))
        self._activities: dict[str, list[dict[str, Any]]] = {}

    def run(self) -> None:
        if self.mode.is_sale and self.config.sale_days.announce and not self.state.announced:
            self.notifier.send(self.messages.get("sale_on", reason=self.mode.reason,
                                                 team=self.team.name))
            self.state.announced = True
        for account in self.team.ad_accounts:
            try:
                self.scan_account(account)
            except GraphError as error:
                if error.is_token_problem:
                    self._token_failed(error)
                    return
                self.result.errors += 1
                log.error("account %s could not be checked: %s", account, error)
                if self.state.first_alert_today(f"scan-error:{account}"):
                    self.notifier.send(self.messages.get(
                        "scan_failed", team=self.team.name, account=account,
                        error=clean(error, 300)), is_issue=True)
            if not self.heartbeat():
                self.result.errors += 1
                return
            self.state.save()

    def _token_failed(self, error: GraphError) -> None:
        self.result.token_failed = True
        self.result.errors += 1
        log.error("Meta rejected the access token: %s", error)
        if self.state.first_alert_today("token-problem"):
            self.notifier.send(self.messages.get("token_problem", team=self.team.name,
                                                 error=clean(error, 300)), is_issue=True)

    def scan_account(self, account: str) -> None:
        log.info("checking %s", account)
        info = self.graph.get(f"/{account}", {"fields": ACCOUNT_FIELDS})
        name = str(info.get("name") or account)
        if not self._account_is_active(account, name, info):
            return
        self._check_timezone(account, info.get("timezone_name"))
        currency = str(info.get("currency") or "")
        campaigns = self.graph.get_all(f"/{account}/campaigns", {
            "fields": "id,name,daily_budget,lifetime_budget,created_time",
            "effective_status": ["ACTIVE"], "limit": PAGE_SIZE})
        if not campaigns:
            log.info("%s has no active campaigns", account)
            return
        ad_sets = self._ad_sets(account, campaigns, currency)
        spend = self._spend_today(account)
        for campaign in campaigns:
            campaign_id = campaign.get("id")
            if not is_numeric_id(campaign_id):
                log.warning("skipped a campaign without a valid id in %s", account)
                continue
            self.check_campaign(account, name, currency, campaign,
                                ad_sets.get(campaign_id, []), spend.get(campaign_id, 0.0))

    def _account_is_active(self, account: str, name: str, info: dict[str, Any]) -> bool:
        status = info.get("account_status")
        if status == 1:
            self.state.account_issues.pop(account, None)
            return True
        log.warning("%s is not active (status %s)", account, plain(status, 20))
        if self.state.account_issues.get(account) != status:
            self.notifier.send(self.messages.account_issue(
                self.team.name, account, name, status, info.get("disable_reason")), is_issue=True)
            self.state.account_issues[account] = status
        return False

    def _check_timezone(self, account: str, zone: Any) -> None:
        if (isinstance(zone, str) and zone and not same_offset(zone, self.config.timezone, self.now)
                and self.state.first_alert_today(f"timezone:{account}")):
            log.warning("%s uses time zone %s but the config says %s; days and sale days "
                        "follow the config", account, plain(zone, 40), self.config.timezone)

    def _ad_sets(self, account: str, campaigns: list[dict[str, Any]],
                 currency: str) -> dict[str, list[dict[str, Any]]]:
        """Ad set budgets are only needed for campaigns without a campaign budget."""
        if all(to_major(c.get("daily_budget"), currency) or to_major(c.get("lifetime_budget"), currency)
               for c in campaigns):
            return {}
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for ad_set in self.graph.get_all(f"/{account}/adsets", {
                "fields": "campaign_id,daily_budget,lifetime_budget",
                "effective_status": ["ACTIVE"], "limit": PAGE_SIZE}):
            grouped[str(ad_set.get("campaign_id"))].append(ad_set)
        return grouped

    def _spend_today(self, account: str) -> dict[str, float]:
        rows = self.graph.get_all(f"/{account}/insights", {
            "level": "campaign", "fields": "campaign_id,spend", "date_preset": "today",
            "limit": PAGE_SIZE})
        return {str(row.get("campaign_id")): to_spend(row.get("spend")) for row in rows}

    def check_campaign(self, account: str, account_name: str, currency: str,
                       campaign: dict[str, Any], ad_sets: list[dict[str, Any]],
                       spend: float) -> None:
        campaign_id = str(campaign["id"])
        budgets = campaign_budgets(campaign, ad_sets, currency)
        previous = self.state.previous_budget(campaign_id)
        facts = CampaignFacts(budgets, spend, previous, self._is_new(campaign),
                              campaign_id in self.state.security_paused)
        action = decide(facts, self.mode, self.config.rules)
        alert = CampaignAlert(
            team=self.team.name, account_id=account, account_name=account_name,
            campaign_id=campaign_id, campaign_name=str(campaign.get("name", "")),
            budget=budgets.daily if budgets.daily is not None else budgets.lifetime, spend=spend,
            previous_budget=previous if action is Action.PAUSE_SPIKE else None)
        log.info("%s %s | budget %s | spend %s", action.value.upper(),
                 plain(alert.campaign_name), alert.budget, spend)
        if not action.is_security:
            self.state.remember_budget(campaign_id, budgets.effective)
        if action is Action.KEEP:
            return
        if action is Action.ALERT_SALE_OVERSPENT:
            if self.state.first_alert_today(f"{campaign_id}:{action.value}"):
                self.notifier.send(self.messages.campaign(action, alert))
            return
        if self._pause(action, alert) and action.is_security:
            # Only a pause that really happened counts. After a failed pause or a
            # dry run the old budget stays, so the next run checks it again.
            self.state.remember_budget(campaign_id, budgets.effective)
            self.state.security_paused[campaign_id] = self.state.today

    def _pause(self, action: Action, alert: CampaignAlert) -> bool:
        """Pauses the campaign and alerts. True only when Meta confirmed the pause."""
        if self.dry_run:
            if self.state.first_alert_today(f"{alert.campaign_id}:{action.value}"):
                self._pause_alert(action, alert)
            return False
        self._update_list(action, alert)
        try:
            self.graph.post(f"/{alert.campaign_id}", {"status": "PAUSED"})
        except GraphError as error:
            if error.is_token_problem:
                raise
            self._pause_failed(action, alert, error)
            return False
        if not action.reactivate_tomorrow:
            self.state.held[alert.campaign_id] = self.state.today
        self.result.paused += 1
        self._pause_alert(action, alert)
        return True

    def _pause_alert(self, action: Action, alert: CampaignAlert) -> None:
        # The activity log is read after the pause, so a hijacked campaign stops first.
        if action.is_security:
            alert = dataclasses.replace(alert, actor=self._actor(alert.account_id,
                                                                 alert.campaign_id, action))
        self.notifier.send(self.messages.campaign(action, alert),
                           is_issue=action in ISSUE_ACTIONS)

    def _update_list(self, action: Action, alert: CampaignAlert) -> None:
        """Runs before the pause: an overspend pause is listed for tomorrow, any
        other pause drops older entries so the reactivator cannot undo it.
        Any trouble with the list is reported, and the pause still happens."""
        try:
            if action.reactivate_tomorrow:
                self.paused.add(PausedCampaign(alert.campaign_id, alert.campaign_name,
                                               alert.account_id, action.value, self.state.today))
            elif self.paused.remove(alert.campaign_id):
                log.info("removed %s from the reactivation list", alert.campaign_id)
        except Exception as error:  # a broken list file must never block a pause
            self.result.errors += 1
            log.error("could not update %s: %s", self.paused.path.name, error)
            self.notifier.send(self.messages.get(
                "list_failed", team=self.team.name, campaign=clean(alert.campaign_name),
                campaign_id=alert.campaign_id, error=clean(error, 200)), is_issue=True)

    def _pause_failed(self, action: Action, alert: CampaignAlert, error: GraphError) -> None:
        self.result.errors += 1
        log.error("could not pause %s: %s", alert.campaign_id, error)
        if self.state.first_alert_today(f"pause-failed:{alert.campaign_id}:{action.value}"):
            self.notifier.send(self.messages.get(
                "pause_failed", team=self.team.name, account=alert.account_id,
                campaign=clean(alert.campaign_name), campaign_id=alert.campaign_id,
                planned=action.value, error=clean(error, 300)), is_issue=True)

    def _is_new(self, campaign: dict[str, Any]) -> bool:
        try:
            created = datetime.strptime(str(campaign.get("created_time")), CREATED_TIME_FORMAT)
        except ValueError:
            return False
        age = self.now - created
        return timedelta(0) <= age <= timedelta(hours=self.config.rules.new_campaign_hours)

    def _actor(self, account: str, campaign_id: str, action: Action) -> str | None:
        if account not in self._activities:
            try:
                self._activities[account] = self.graph.get_all(f"/{account}/activities", {
                    "fields": "event_type,event_time,actor_name,object_id",
                    "since": int((self.now - ACTIVITY_LOOKBACK).timestamp()),
                    "limit": PAGE_SIZE}, max_pages=ACTIVITY_PAGES)
            except GraphError as error:
                # The pause already happened; "who did it" is a nice-to-have. Even a
                # token error is only logged here: the next Graph call raises it again
                # and the run handles it there, after this alert and its state are kept.
                log.warning("activity log of %s unavailable: %s", account, error)
                self._activities[account] = []
        keyword = "budget" if action is Action.PAUSE_SPIKE else "create"
        return find_actor(self._activities[account], campaign_id, keyword)


def same_offset(zone_name: str, zone: ZoneInfo, now: datetime) -> bool:
    """Compares UTC offsets, so aliases like Asia/Calcutta and Asia/Kolkata match."""
    try:
        other = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return zone_name == str(zone)
    return now.astimezone(other).utcoffset() == now.astimezone(zone).utcoffset()


def find_actor(rows: list[dict[str, Any]], campaign_id: str, keyword: str) -> str | None:
    """Who last changed the campaign, preferring events that match `keyword`."""
    mine = [row for row in rows if str(row.get("object_id")) == campaign_id]
    if not mine:
        return None
    matching = [row for row in mine if keyword in str(row.get("event_type", ""))] or mine
    latest = max(matching, key=lambda row: str(row.get("event_time", "")))
    actor = str(latest.get("actor_name") or "Unknown user")
    when = _short_time(latest.get("event_time"))
    return f"{actor} ({when})" if when else actor


def _short_time(value: Any) -> str:
    try:
        return datetime.strptime(str(value), CREATED_TIME_FORMAT).strftime("%b %d, %I:%M %p")
    except ValueError:
        return ""
