"""Files the watchdog keeps between runs, one folder per team.

state.json              what was already alerted today, last seen budgets, held campaigns
paused-campaigns.jsonl  campaigns the reactivator turns back on the next day
needs-attention.jsonl   list entries the reactivator gave up on; a person must look
run.lock                stops two runs for the same team from overlapping
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

STATE_FILE = "state.json"
PAUSED_FILE = "paused-campaigns.jsonl"
ATTENTION_FILE = "needs-attention.jsonl"
LOCK_FILE = "run.lock"
QUEUE_FILE = "discord-queue.jsonl"
BUDGET_MEMORY_DAYS = 30
HELD_MEMORY_DAYS = 30
SECURITY_MEMORY_DAYS = 7
# A running scan refreshes its lock after every ad account, so a lock this old
# belongs to a run that died (crash, reboot, killed task).
STALE_LOCK = timedelta(minutes=20)
LOCK_POLL_SECONDS = 5.0
REACTIVATABLE_REASONS = frozenset({"pause_overspent", "pause_escalation"})
NUMERIC_ID = re.compile(r"[0-9]{1,30}")


def is_numeric_id(value: object) -> bool:
    """ASCII digits only ("١٢٣" and "123\\n" are rejected)."""
    return isinstance(value, str) and NUMERIC_ID.fullmatch(value) is not None


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def set_aside(path: Path, why: str) -> None:
    """Keeps a damaged file for inspection instead of deleting it."""
    target = path.with_name(f"{path.name}.damaged-{datetime.now():%Y%m%d-%H%M%S}")
    path.replace(target)
    log.warning("%s could not be read (%s); kept it as %s and started fresh",
                path.name, why, target.name)


class TeamState:
    def __init__(self, path: Path, today: date):
        self.path = path
        self.today = today.isoformat()
        data = self._load()
        same_day = data.get("day") == self.today
        self.announced: bool = data.get("announced") is True and same_day
        self.alerted: set[str] = {str(key) for key in _list(data, "alerted")} if same_day else set()
        self.account_issues: dict[str, Any] = _dict(data, "account_issues") if same_day else {}
        self.budgets: dict[str, Any] = _dict(data, "budgets")
        self.security_paused: dict[str, Any] = _dict(data, "security_paused")
        # Campaigns paused for a reason that needs a person (security, anomaly,
        # spend limit), with the day it happened. The reactivator never undoes these.
        self.held: dict[str, Any] = _dict(data, "held")

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not a JSON object")
            return data
        except (ValueError, RecursionError) as error:  # bad JSON, not UTF-8, absurd nesting
            set_aside(self.path, str(error))
            return {}

    def previous_budget(self, campaign_id: str) -> float | None:
        entry = self.budgets.get(campaign_id)
        value = entry.get("budget") if isinstance(entry, dict) else None
        return float(value) if isinstance(value, (int, float)) else None

    def remember_budget(self, campaign_id: str, budget: float) -> None:
        self.budgets[campaign_id] = {"budget": budget, "seen": self.today}

    def held_since(self, campaign_id: str) -> str | None:
        value = self.held.get(campaign_id)
        return value if isinstance(value, str) else None

    def first_alert_today(self, key: str) -> bool:
        """True the first time `key` is seen today, then False."""
        if key in self.alerted:
            return False
        self.alerted.add(key)
        return True

    def save(self) -> None:
        self._forget_old()
        data = {"day": self.today, "announced": self.announced, "alerted": sorted(self.alerted),
                "account_issues": self.account_issues, "budgets": self.budgets,
                "security_paused": self.security_paused, "held": self.held}
        write_atomic(self.path, json.dumps(data, ensure_ascii=False, indent=2))

    def _forget_old(self) -> None:
        today = date.fromisoformat(self.today)

        def recent(day: Any, keep_days: int) -> bool:
            try:
                return today - date.fromisoformat(str(day)) <= timedelta(days=keep_days)
            except ValueError:
                return False

        self.budgets = {cid: entry for cid, entry in self.budgets.items()
                        if isinstance(entry, dict) and recent(entry.get("seen"), BUDGET_MEMORY_DAYS)}
        self.security_paused = {cid: day for cid, day in self.security_paused.items()
                                if recent(day, SECURITY_MEMORY_DAYS)}
        self.held = {cid: day for cid, day in self.held.items() if recent(day, HELD_MEMORY_DAYS)}


def _list(data: dict[str, Any], key: str) -> list[Any]:
    value = data.get(key)
    return value if isinstance(value, list) else []


def _dict(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    return dict(value) if isinstance(value, dict) else {}


@dataclass(frozen=True)
class PausedCampaign:
    campaign_id: str
    campaign_name: str
    account_id: str
    reason: str
    paused_on: str  # YYYY-MM-DD

    def to_line(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


class PausedList:
    """The campaigns the reactivator should turn back on."""

    def __init__(self, path: Path):
        self.path = path

    def add(self, record: PausedCampaign) -> None:
        """Appends the entry unless the same campaign is already listed for that day
        (a pause that keeps failing is retried every run)."""
        records, _ = self.read()
        if any(r.campaign_id == record.campaign_id and r.paused_on == record.paused_on
               for r in records):
            return
        append_line(self.path, record.to_line())

    def read(self) -> tuple[list[PausedCampaign], list[str]]:
        """Returns the unique records, and the lines that could not be read.
        A file that is not UTF-8 (saved by another editor) is set aside, so it
        can never stop a pause."""
        if not self.path.exists():
            return [], []
        try:
            text = self.path.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            set_aside(self.path, str(error))
            return [], []
        records: dict[str, PausedCampaign] = {}
        bad: list[str] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            record = _paused_from_line(line)
            if record is None:
                bad.append(line)
            else:
                records[record.campaign_id] = record  # the latest line wins
        return list(records.values()), bad

    def remove(self, campaign_id: str) -> bool:
        """Drops every entry for a campaign. Returns True if there was one."""
        records, bad = self.read()
        kept = [record for record in records if record.campaign_id != campaign_id]
        if len(kept) == len(records):
            return False
        self.replace(kept, bad)
        return True

    def replace(self, records: list[PausedCampaign], bad_lines: list[str]) -> None:
        if not records and not bad_lines:
            self.path.unlink(missing_ok=True)
            return
        lines = [record.to_line() for record in records] + bad_lines
        write_atomic(self.path, "".join(line + "\n" for line in lines))


def _paused_from_line(line: str) -> PausedCampaign | None:
    try:
        data = json.loads(line)
        record = PausedCampaign(**{key: str(data[key]) for key in PausedCampaign.__dataclass_fields__})
    except (ValueError, TypeError, KeyError, RecursionError):  # deeply nested JSON recurses
        return None
    if not is_numeric_id(record.campaign_id) or record.reason not in REACTIVATABLE_REASONS:
        return None
    try:
        date.fromisoformat(record.paused_on)
    except ValueError:
        return None
    return record


class LockedError(RuntimeError):
    """Another run for the same team is still going."""


class TeamLock:
    """One run per team at a time. The owner refreshes it with `touch()`."""

    def __init__(self, folder: Path, wait_seconds: float = 0,
                 sleep: Callable[[float], None] | None = None):
        self.path = folder / LOCK_FILE
        self.owner = f"{os.getpid()}-{uuid.uuid4().hex}"
        self._wait_seconds = wait_seconds
        self._sleep = sleep or time.sleep

    def __enter__(self) -> "TeamLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        waited = 0.0
        while not self._acquire():
            if waited >= self._wait_seconds:
                raise LockedError(f"{self.path} is held by another run")
            self._sleep(LOCK_POLL_SECONDS)
            waited += LOCK_POLL_SECONDS
        return self

    def __exit__(self, *exc: object) -> None:
        if self._is_mine():
            self.path.unlink(missing_ok=True)

    def touch(self) -> bool:
        """Tells other runs this lock's owner is still alive. False means another
        run took the lock over: stop before changing anything else."""
        if not self._is_mine():
            log.error("lost the lock %s to another run", self.path)
            return False
        os.utime(self.path)
        return True

    def _acquire(self) -> bool:
        if self._try_create():
            return True
        if not self._is_stale():
            return False
        log.warning("taking over a lock not refreshed for %s: %s", STALE_LOCK, self.path)
        self.path.unlink(missing_ok=True)
        # If two runs take over the same stale lock at the same instant, both may
        # run once. Every state write is atomic, so that costs a duplicate alert
        # at worst; the next runs see a fresh lock again.
        return self._try_create()

    def _try_create(self) -> bool:
        try:
            handle = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(f"{self.owner} {datetime.now().isoformat(timespec='seconds')}\n")
        return True

    def _is_mine(self) -> bool:
        try:
            return self.path.read_text(encoding="utf-8").startswith(self.owner + " ")
        except (FileNotFoundError, UnicodeDecodeError):
            return False

    def _is_stale(self) -> bool:
        try:
            age = time.time() - self.path.stat().st_mtime
        except FileNotFoundError:
            return True
        return age > STALE_LOCK.total_seconds()
