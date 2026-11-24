"""Loads and validates the watchdog config file and secrets from the environment.

The config file holds settings only. Secrets (the Meta token and Discord
webhook URLs) come from environment variables, usually through a `.env` file,
so the config file can be shared or committed without leaking anything.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

TOKEN_ENV = "META_ACCESS_TOKEN"
DEFAULT_GRAPH_VERSION = "v25.0"
LANGUAGES = ("en", "tl")

# Used with fullmatch and [0-9]: ASCII digits only, and no trailing newline slips through.
AD_ACCOUNT_ID = re.compile(r"act_[0-9]{5,20}")
GRAPH_VERSION = re.compile(r"v[0-9]{2}\.[0-9]")
ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
WEBHOOK_URL = re.compile(r"https://(?:discord|discordapp)\.com/api/webhooks/[0-9]+/[A-Za-z0-9_-]+")


class ConfigError(ValueError):
    """The config file or the environment is missing something or is invalid."""


@dataclass(frozen=True)
class Rules:
    """Thresholds, in the ad account's currency (major units, e.g. pesos)."""

    # (minimum daily budget, allowed overspend) pairs, sorted by budget.
    overspend_tolerance: tuple[tuple[float, float], ...] = (
        (0, 10), (100, 30), (1000, 150), (10000, 250))
    hard_spend_limit: float = 40000
    anomaly_budget: float = 45000
    spike_multiplier: float = 10
    new_campaign_budget: float = 1000
    new_campaign_hours: float = 24
    sale_day_pause_multiplier: float = 2


@dataclass(frozen=True)
class SaleDays:
    payday_days: tuple[int, ...] = (15, 30)
    double_days: bool = True
    extra_dates: frozenset[date] = frozenset()
    announce: bool = True


@dataclass(frozen=True)
class Team:
    name: str
    slug: str
    ad_accounts: tuple[str, ...]
    webhook: str | None
    issues_webhook: str | None


@dataclass(frozen=True)
class Config:
    teams: tuple[Team, ...]
    access_token: str
    state_dir: Path
    rules: Rules = field(default_factory=Rules)
    sale_days: SaleDays = field(default_factory=SaleDays)
    language: str = "en"
    dry_run: bool = True
    timezone: ZoneInfo = ZoneInfo("UTC")
    graph_version: str = DEFAULT_GRAPH_VERSION


TOP_KEYS = {"teams", "rules", "sale_days", "language", "dry_run", "timezone",
            "state_dir", "graph_version"}
TEAM_KEYS = {"name", "ad_accounts", "discord_webhook_env", "discord_issues_webhook_env"}
RULE_KEYS = {"overspend_tolerance", "hard_spend_limit", "anomaly_budget", "spike_multiplier",
             "new_campaign_budget", "new_campaign_hours", "sale_day_pause_multiplier"}
SALE_KEYS = {"payday_days", "double_days", "extra_dates", "announce"}


def load_config(path: Path, env: Mapping[str, str]) -> Config:
    """Reads `path` and the secrets in `env`. Raises ConfigError on any problem."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigError(f"cannot read config file {path}: {error}") from None
    return parse_config(raw, env, base_dir=path.resolve().parent)


def parse_config(raw: Any, env: Mapping[str, str], base_dir: Path) -> Config:
    data = _object(raw, "config", TOP_KEYS)
    token = env.get(TOKEN_ENV, "").strip()
    if not token:
        raise ConfigError(f"{TOKEN_ENV} is not set. Put it in your .env file.")
    teams = _teams(data.get("teams"), env)
    language = data.get("language", "en")
    if language not in LANGUAGES:
        raise ConfigError(f"language must be one of {', '.join(LANGUAGES)}")
    graph_version = data.get("graph_version", DEFAULT_GRAPH_VERSION)
    if not isinstance(graph_version, str) or not GRAPH_VERSION.fullmatch(graph_version):
        raise ConfigError('graph_version must look like "v25.0"')
    state_dir = Path(_string(data.get("state_dir", "state"), "state_dir"))
    return Config(
        teams=teams,
        access_token=token,
        state_dir=state_dir if state_dir.is_absolute() else base_dir / state_dir,
        rules=_rules(data.get("rules", {})),
        sale_days=_sale_days(data.get("sale_days", {})),
        language=language,
        dry_run=_bool(data.get("dry_run", True), "dry_run"),
        timezone=_timezone(data.get("timezone")),
        graph_version=graph_version,
    )


def _object(value: Any, where: str, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{where} must be a JSON object")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ConfigError(f"unknown key in {where}: {', '.join(unknown)}")
    return value


def _string(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where} must be a non-empty string")
    return value.strip()


def _bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{where} must be true or false")
    return value


def _number(value: Any, where: str, minimum: float = 0, strict: bool = True) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where} must be a number")
    if value < minimum or (strict and value == minimum):
        raise ConfigError(f"{where} must be {'greater than' if strict else 'at least'} {minimum:g}")
    return float(value)


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not slug:
        raise ConfigError(f'team name "{name}" needs at least one letter or digit')
    return slug


def _teams(value: Any, env: Mapping[str, str]) -> tuple[Team, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError("teams must be a non-empty list")
    teams = tuple(_team(item, index, env) for index, item in enumerate(value))
    slugs = [team.slug for team in teams]
    if len(set(slugs)) != len(slugs):
        raise ConfigError("two teams have the same name (after removing symbols)")
    accounts = [account for team in teams for account in team.ad_accounts]
    if len(set(accounts)) != len(accounts):
        raise ConfigError("an ad account is listed more than once")
    return teams


def _team(value: Any, index: int, env: Mapping[str, str]) -> Team:
    where = f"teams[{index}]"
    data = _object(value, where, TEAM_KEYS)
    name = _string(data.get("name"), f"{where}.name")
    accounts = data.get("ad_accounts")
    if not isinstance(accounts, list) or not accounts:
        raise ConfigError(f"{where}.ad_accounts must be a non-empty list")
    for account in accounts:
        if not isinstance(account, str) or not AD_ACCOUNT_ID.fullmatch(account):
            raise ConfigError(f'{where}.ad_accounts: "{account}" must look like "act_1234567890"')
    return Team(
        name=name,
        slug=_slug(name),
        ad_accounts=tuple(accounts),
        webhook=_webhook(data.get("discord_webhook_env"), f"{where}.discord_webhook_env", env),
        issues_webhook=_webhook(data.get("discord_issues_webhook_env"),
                                f"{where}.discord_issues_webhook_env", env),
    )


def _webhook(env_name: Any, where: str, env: Mapping[str, str]) -> str | None:
    if env_name is None:
        return None
    if not isinstance(env_name, str) or not ENV_NAME.fullmatch(env_name):
        raise ConfigError(f"{where} must be an environment variable name like DISCORD_WEBHOOK_TEAM_A")
    url = env.get(env_name, "").strip()
    if not url:
        raise ConfigError(f"{env_name} (from {where}) is not set. Put it in your .env file.")
    if not WEBHOOK_URL.fullmatch(url):
        raise ConfigError(f"{env_name} is not a Discord webhook URL")
    return url


def _rules(value: Any) -> Rules:
    data = _object(value, "rules", RULE_KEYS)
    defaults = Rules()
    numbers = {key: _number(data[key], f"rules.{key}")
               for key in RULE_KEYS - {"overspend_tolerance"} if key in data}
    for key in ("spike_multiplier", "sale_day_pause_multiplier"):
        if key in numbers and numbers[key] <= 1:
            raise ConfigError(f"rules.{key} must be greater than 1")
    tolerance = (_tolerance(data["overspend_tolerance"]) if "overspend_tolerance" in data
                 else defaults.overspend_tolerance)
    return Rules(overspend_tolerance=tolerance,
                 **{**{k: getattr(defaults, k) for k in RULE_KEYS - {"overspend_tolerance"}},
                    **numbers})


def _tolerance(value: Any) -> tuple[tuple[float, float], ...]:
    where = "rules.overspend_tolerance"
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{where} must be a non-empty list of [min_budget, allowed_overspend]")
    tiers = []
    for item in value:
        if not isinstance(item, list) or len(item) != 2:
            raise ConfigError(f"{where}: each tier must be [min_budget, allowed_overspend]")
        tiers.append((_number(item[0], where, strict=False), _number(item[1], where, strict=False)))
    budgets = [budget for budget, _ in tiers]
    if budgets[0] != 0 or budgets != sorted(set(budgets)):
        raise ConfigError(f"{where}: the first tier must start at 0 and budgets must go up")
    return tuple(tiers)


def _sale_days(value: Any) -> SaleDays:
    data = _object(value, "sale_days", SALE_KEYS)
    defaults = SaleDays()
    paydays = data.get("payday_days", list(defaults.payday_days))
    if not isinstance(paydays, list) or any(
            isinstance(day, bool) or not isinstance(day, int) or not 1 <= day <= 31
            for day in paydays):
        raise ConfigError("sale_days.payday_days must be a list of days from 1 to 31")
    extra = data.get("extra_dates", [])
    if not isinstance(extra, list):
        raise ConfigError('sale_days.extra_dates must be a list of "YYYY-MM-DD" dates')
    return SaleDays(
        payday_days=tuple(paydays),
        double_days=_bool(data.get("double_days", defaults.double_days), "sale_days.double_days"),
        extra_dates=frozenset(_date(item) for item in extra),
        announce=_bool(data.get("announce", defaults.announce), "sale_days.announce"),
    )


def _date(value: Any) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ConfigError(f'sale_days.extra_dates: "{value}" is not a "YYYY-MM-DD" date') from None


def _timezone(value: Any) -> ZoneInfo:
    if value is None:
        raise ConfigError('timezone is required, for example "Asia/Manila". '
                          "Use the time zone of your ad accounts.")
    try:
        return ZoneInfo(_string(value, "timezone"))
    except (ZoneInfoNotFoundError, ValueError, OSError):  # Windows: "Asia" is a folder
        raise ConfigError(f'timezone "{value}" is not a known time zone like "Asia/Manila"') from None
