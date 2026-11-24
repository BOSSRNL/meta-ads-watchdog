"""The reactivator: turns back on the campaigns the watchdog paused for overspending.

Only campaigns paused before today are turned on, so it is safe to run at any
hour (hourly is fine). Before turning a campaign on it checks that:
- the list entry is an overspend pause (security, anomaly and spend-limit
  pauses are never listed, and are refused if someone adds them by hand);
- the campaign belongs to one of the team's ad accounts;
- the watchdog has not paused it again for a reason that needs a person.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Callable

from .config import Config, Team
from .discord import Notifier, clean, plain
from .graph import GraphClient, GraphError
from .messages import Messages, rule_values
from .scan import NotifierFactory, local_today
from .state import (ATTENTION_FILE, PAUSED_FILE, STATE_FILE, LockedError, PausedCampaign,
                    PausedList, TeamLock, TeamState, append_line)

log = logging.getLogger(__name__)

BACKUP_FOLDER = "backup"
BACKUPS_KEPT = 30
LIST_LIMIT = 15
LOCK_WAIT_SECONDS = 600       # wait for a scan of the same team to finish
GIVE_UP_AFTER = timedelta(days=7)


class LockLost(Exception):
    """Another run took over this team's lock (this run was too slow)."""


class Outcome(Enum):
    ON = "on"          # turned on, or already on
    RETRY = "retry"    # failed for now, stays on the list
    MANUAL = "manual"  # will not be turned on automatically; moved to needs-attention.jsonl


@dataclass
class ReactivateResult:
    turned_on: int = 0
    retry: int = 0
    manual: int = 0
    errors: int = 0
    skipped_teams: list[str] = field(default_factory=list)
    aborted: bool = False


def run_reactivate(config: Config, graph: GraphClient, make_notifier: NotifierFactory,
                   now: datetime, teams: tuple[Team, ...], dry_run: bool) -> ReactivateResult:
    result = ReactivateResult()
    today = local_today(config, now)
    for team in teams:
        try:
            _reactivate_team(config, team, graph, make_notifier, today, dry_run, result)
        except LockedError as error:
            log.warning("skipped team %s: %s", team.name, error)
            result.skipped_teams.append(team.name)
        except Exception:  # one team's bug or disk error must not stop the other teams
            log.exception("team %s failed", team.name)
            result.errors += 1
        if result.aborted:
            break
    return result


def _reactivate_team(config: Config, team: Team, graph: GraphClient,
                     make_notifier: NotifierFactory, today: date, dry_run: bool,
                     result: ReactivateResult) -> None:
    folder = config.state_dir / team.slug
    with TeamLock(folder, wait_seconds=LOCK_WAIT_SECONDS) as lock:
        notifier = make_notifier(team)
        try:
            notifier.flush_queue()
        except Exception:  # trouble with old alerts must not stop today's reactivation
            log.exception("could not send the queued Discord messages of %s", team.name)
            result.errors += 1
        try:
            TeamReactivation(config, team, graph, notifier, folder, today, dry_run,
                             result, lock.touch).run()
        finally:
            result.errors += notifier.rejected


class TeamReactivation:
    def __init__(self, config: Config, team: Team, graph: GraphClient, notifier: Notifier,
                 folder: Path, today: date, dry_run: bool, result: ReactivateResult,
                 heartbeat: Callable[[], bool] = lambda: True):
        self.team, self.graph, self.notifier, self.folder = team, graph, notifier, folder
        self.today, self.dry_run, self.result = today, dry_run, result
        self.heartbeat = heartbeat
        self.messages = Messages(config.language, rule_values(config.rules))
        self.paused = PausedList(folder / PAUSED_FILE)
        self.state = TeamState(folder / STATE_FILE, today)

    def run(self) -> None:
        records, bad_lines = self.paused.read()
        today = self.today.isoformat()
        due = [record for record in records if record.paused_on < today]
        waiting = [record for record in records if record.paused_on >= today]
        if not due and not bad_lines:
            log.info("team %s: nothing to turn on (%s paused today)", self.team.name, len(waiting))
            return
        try:
            self.graph.get("/me", {"fields": "id"})
        except GraphError as error:
            self._abort("token problem" if error.is_token_problem else "Meta unreachable", error)
            return
        try:
            outcomes = [(record, *self._give_up_if_old(record, *self.turn_on(record)))
                        for record in due]
        except GraphError as error:  # only a token problem gets here
            self._abort("token problem", error)
            return
        except LockLost:
            self.result.errors += 1
            log.error("team %s: another run took the lock; stopped without saving", self.team.name)
            return
        self._count(outcomes, len(bad_lines))
        summary = self._summary(outcomes, bad_lines, len(waiting))
        if self.dry_run:
            log.info("DRY RUN summary:\n%s", summary)
            return
        if not self.heartbeat():  # another run owns the team now: write nothing
            self.result.errors += 1
            return
        self._save(outcomes, waiting, bad_lines)
        self._report(summary, outcomes, bool(bad_lines))

    def _report(self, summary: str, outcomes: list[tuple[PausedCampaign, Outcome, str]],
                has_bad_lines: bool) -> None:
        """One message per run. A run where nothing changed (only retries) is
        reported once a day, so an hourly schedule does not repeat itself."""
        only_retries = all(o is Outcome.RETRY for _, o, _ in outcomes) and not has_bad_lines
        if only_retries and not self.state.first_alert_today("reactivation-retry"):
            log.info("only retries again; already reported today")
            return
        self.state.save()
        self.notifier.send(summary, is_issue=any(o is not Outcome.ON for _, o, _ in outcomes)
                           or has_bad_lines)

    def turn_on(self, record: PausedCampaign) -> tuple[Outcome, str]:
        refused = self._refusal(record)
        if refused:
            return Outcome.MANUAL, refused
        path = f"/{record.campaign_id}"
        try:
            info = self.graph.get(path, {"fields": "status,account_id"})
            owner = info.get("account_id")
            if not owner:
                return Outcome.RETRY, "Meta did not say which ad account owns it"
            if f"act_{owner}" not in self.team.ad_accounts:
                return Outcome.MANUAL, "belongs to an ad account outside this team"
            status = info.get("status")
            if status == "ACTIVE":
                return Outcome.ON, "already on"
            if status != "PAUSED":
                return Outcome.MANUAL, f"status is {plain(status, 20)}"
            if self.dry_run:
                return Outcome.ON, "would be turned on"
            if not self.heartbeat():  # another run owns the team now: change nothing
                raise LockLost
            self.graph.post(path, {"status": "ACTIVE"})
            status = self.graph.get(path, {"fields": "status"}).get("status")
        except GraphError as error:
            if error.is_token_problem:
                raise
            return Outcome.RETRY, str(error)
        if status == "ACTIVE":
            log.info("turned on %s %s", record.campaign_id, plain(record.campaign_name))
            return Outcome.ON, "turned on"
        return Outcome.RETRY, f"still {plain(status, 20)} after turning on"

    def _refusal(self, record: PausedCampaign) -> str | None:
        if record.account_id not in self.team.ad_accounts:
            return "listed under an ad account outside this team"
        held = self.state.held_since(record.campaign_id)
        if held and held >= record.paused_on:
            return f"paused again on {held} for a reason that needs a person"
        return None

    def _give_up_if_old(self, record: PausedCampaign, outcome: Outcome,
                        detail: str) -> tuple[Outcome, str]:
        paused_on = date.fromisoformat(record.paused_on)
        if outcome is Outcome.RETRY and self.today - paused_on > GIVE_UP_AFTER:
            return Outcome.MANUAL, f"{detail} (still failing after {GIVE_UP_AFTER.days} days)"
        return outcome, detail

    def _save(self, outcomes: list[tuple[PausedCampaign, Outcome, str]],
              waiting: list[PausedCampaign], bad_lines: list[str]) -> None:
        """Keeps today's and retryable entries; moves the rest where a person will see them.
        A run with only retries changes nothing, so it writes nothing and makes no backup."""
        if all(outcome is Outcome.RETRY for _, outcome, _ in outcomes) and not bad_lines:
            return
        self._backup()
        retry = [record for record, outcome, _ in outcomes if outcome is Outcome.RETRY]
        manual = [record.to_line() for record, outcome, _ in outcomes if outcome is Outcome.MANUAL]
        for line in manual + bad_lines:
            append_line(self.folder / ATTENTION_FILE, line)
        self.paused.replace(waiting + retry, [])

    def _abort(self, title: str, error: GraphError) -> None:
        self.result.aborted = True
        log.error("reactivation stopped (%s): %s", title, error)
        self.notifier.send(self.messages.get("react_abort", title=title, team=self.team.name,
                                             detail=clean(error, 300)), is_issue=True)

    def _backup(self) -> None:
        folder = self.folder / BACKUP_FOLDER
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.paused.path, folder / f"{datetime.now():%Y%m%d-%H%M%S}-{PAUSED_FILE}")
        for old in sorted(folder.glob(f"*-{PAUSED_FILE}"))[:-BACKUPS_KEPT]:
            old.unlink()

    def _count(self, outcomes: list[tuple[PausedCampaign, Outcome, str]], bad: int) -> None:
        for _, outcome, _ in outcomes:
            if outcome is Outcome.ON:
                self.result.turned_on += 1
            elif outcome is Outcome.RETRY:
                self.result.retry += 1
            else:
                self.result.manual += 1
        self.result.manual += bad

    def _summary(self, outcomes: list[tuple[PausedCampaign, Outcome, str]],
                 bad_lines: list[str], waiting: int) -> str:
        on = [(r, detail) for r, o, detail in outcomes if o is Outcome.ON]
        retry = [(r, detail) for r, o, detail in outcomes if o is Outcome.RETRY]
        manual = [(r, detail) for r, o, detail in outcomes if o is Outcome.MANUAL]
        title = "react_done" if not retry and not manual and not bad_lines else "react_partial"
        lines = [self.messages.get(title), f"Team: {clean(self.team.name)}",
                 self.messages.get("react_counts", ok=len(on), retry=len(retry),
                                   manual=len(manual) + len(bad_lines), waiting=waiting)]
        groups = (("react_on", [_line(r, d) for r, d in on]),
                  ("react_retry", [_line(r, d) for r, d in retry]),
                  ("react_manual", [_line(r, d) for r, d in manual]
                   + [f"• {clean(line, 80)}: unreadable or not an overspend entry"
                      for line in bad_lines]))
        for label, items in groups:
            if items:
                lines.append(f"\n**{self.messages.get(label)}:**")
                lines.extend(items[:LIST_LIMIT])
                if len(items) > LIST_LIMIT:
                    lines.append(self.messages.get("and_more", count=len(items) - LIST_LIMIT))
        return "\n".join(lines)


def _line(record: PausedCampaign, detail: str) -> str:
    return f"• {clean(record.campaign_name)} ({record.campaign_id}): {clean(detail, 120)}"
