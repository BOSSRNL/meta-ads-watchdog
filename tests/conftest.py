"""Shared fakes: a fake Graph API, a fake notifier and a config builder. No network."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from meta_ads_watchdog.config import Config, parse_config
from meta_ads_watchdog.graph import GraphError

TOKEN = "EAAtesttoken1234567890"
WEBHOOK = "https://discord.com/api/webhooks/123456/abcDEF-ghi"
ISSUES_WEBHOOK = "https://discord.com/api/webhooks/654321/xyz_ABC"
ACCOUNT = "act_1000001"
# A normal day: not a payday, not a double day.
NORMAL_NOW = datetime(2026, 11, 18, 4, 0, tzinfo=timezone.utc)   # Nov 18 in Manila
PAYDAY_NOW = datetime(2026, 11, 15, 4, 0, tzinfo=timezone.utc)   # Nov 15 in Manila


def make_config(tmp_path: Path, **overrides: Any) -> Config:
    raw: dict[str, Any] = {
        "teams": [{"name": "Team A", "ad_accounts": [ACCOUNT],
                   "discord_webhook_env": "HOOK_A", "discord_issues_webhook_env": "HOOK_A_ISSUES"}],
        "timezone": "Asia/Manila",
        "dry_run": False,
    }
    raw.update(overrides)
    env = {"META_ACCESS_TOKEN": TOKEN, "HOOK_A": WEBHOOK, "HOOK_A_ISSUES": ISSUES_WEBHOOK}
    return parse_config(raw, env, base_dir=tmp_path)


def campaign(cid: str, daily: float | None = None, lifetime: float | None = None,
             name: str | None = None, created: str = "2026-10-01T08:00:00+0800") -> dict[str, Any]:
    """Budgets in pesos; stored the way Meta returns them (centavos, as strings)."""
    row: dict[str, Any] = {"id": cid, "name": name or f"Campaign {cid}", "created_time": created}
    if daily is not None:
        row["daily_budget"] = str(int(daily * 100))
    if lifetime is not None:
        row["lifetime_budget"] = str(int(lifetime * 100))
    return row


class FakeGraph:
    def __init__(self) -> None:
        self.accounts: dict[str, dict[str, Any]] = {}
        self.campaigns: dict[str, list[dict[str, Any]]] = {}
        self.ad_sets: dict[str, list[dict[str, Any]]] = {}
        self.spend: dict[str, dict[str, float]] = {}
        self.activities: dict[str, list[dict[str, Any]]] = {}
        self.statuses: dict[str, str] = {}
        self.owners: dict[str, str] = {}   # campaign id -> ad account id (default ACCOUNT)
        self.stuck: set[str] = set()       # campaigns that stay PAUSED after a POST
        self.errors: dict[str, GraphError] = {}  # "GET /path" or "POST /path" -> error
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.posts: list[tuple[str, dict[str, Any]]] = []

    def add_account(self, account: str = ACCOUNT, status: int = 1, currency: str = "PHP",
                    disable_reason: int = 0) -> None:
        self.accounts[account] = {"name": f"Shop {account[-2:]}", "account_status": status,
                                  "disable_reason": disable_reason, "currency": currency}
        self.campaigns.setdefault(account, [])
        self.spend.setdefault(account, {})

    def add_campaign(self, row: dict[str, Any], spend: float = 0, account: str = ACCOUNT) -> None:
        self.campaigns[account].append(row)
        self.spend[account][row["id"]] = spend
        self.statuses[row["id"]] = "ACTIVE"
        self.owners[row["id"]] = account

    def _fail(self, key: str) -> None:
        if key in self.errors:
            raise self.errors[key]

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append(("GET", path, dict(params or {})))
        self._fail(f"GET {path}")
        if path == "/me":
            return {"id": "42"}
        key = path[1:]
        if key in self.accounts:
            return self.accounts[key]
        if key in self.statuses:
            owner = self.owners.get(key, ACCOUNT)
            return {"id": key, "status": self.statuses[key], "account_id": owner[len("act_"):]}
        raise GraphError("[100] Unsupported get request", code=100)

    def get_all(self, path: str, params: dict[str, Any] | None = None,
                max_pages: int = 50) -> list[dict[str, Any]]:
        self.calls.append(("GET_ALL", path, dict(params or {})))
        self._fail(f"GET {path}")
        account, edge = path[1:].split("/")
        if edge == "campaigns":
            return [c for c in self.campaigns[account] if self.statuses.get(c["id"]) == "ACTIVE"]
        if edge == "adsets":
            return list(self.ad_sets.get(account, []))
        if edge == "insights":
            return [{"campaign_id": cid, "spend": f"{value:.2f}"}
                    for cid, value in self.spend[account].items()]
        if edge == "activities":
            return list(self.activities.get(account, []))
        raise AssertionError(f"unexpected edge {edge}")

    def post(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        self.posts.append((path, dict(data)))
        self.calls.append(("POST", path, dict(data)))
        self._fail(f"POST {path}")
        cid = path[1:]
        if cid not in self.stuck:
            self.statuses[cid] = data["status"]
        return {"success": True}


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, bool]] = []
        self.flushed = 0
        self.rejected = 0

    def send(self, content: str, is_issue: bool = False) -> None:
        self.sent.append((content, is_issue))

    def flush_queue(self) -> None:
        self.flushed += 1

    def titles(self) -> list[str]:
        return [content.splitlines()[0] for content, _ in self.sent]


@pytest.fixture
def graph() -> FakeGraph:
    fake = FakeGraph()
    fake.add_account()
    return fake


@pytest.fixture
def notifier() -> FakeNotifier:
    return FakeNotifier()
