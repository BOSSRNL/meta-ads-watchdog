import json
from datetime import date
from pathlib import Path

import pytest

from conftest import TOKEN, WEBHOOK

from meta_ads_watchdog.config import ConfigError, Rules, load_config, parse_config

ENV = {"META_ACCESS_TOKEN": TOKEN, "HOOK": WEBHOOK}
TEAM = {"name": "Team A", "ad_accounts": ["act_1234567"], "discord_webhook_env": "HOOK"}


def parse(raw, env=ENV, base=Path("/base")):
    return parse_config(raw, env, base_dir=base)


def test_minimal_config_uses_safe_defaults():
    config = parse({"teams": [TEAM], "timezone": "Asia/Manila"})
    assert config.dry_run is True and config.language == "en"
    assert config.rules == Rules() and config.graph_version == "v25.0"
    assert config.state_dir == Path("/base/state")
    team = config.teams[0]
    assert (team.slug, team.webhook, team.issues_webhook) == ("team-a", WEBHOOK, None)


def test_full_config():
    config = parse({
        "teams": [TEAM, {"name": "Team B!", "ad_accounts": ["act_7654321"]}],
        "rules": {"hard_spend_limit": 500, "overspend_tolerance": [[0, 1], [50, 2.5]],
                  "sale_day_pause_multiplier": 1.5},
        "sale_days": {"payday_days": [10, 25], "double_days": False,
                      "extra_dates": ["2026-11-25"], "announce": False},
        "language": "tl", "dry_run": False, "timezone": "Asia/Manila",
        "state_dir": "C:/watchdog-state" if Path("C:/").exists() else "/var/watchdog",
        "graph_version": "v24.0"})
    assert config.rules.hard_spend_limit == 500 and config.rules.anomaly_budget == 45000
    assert config.rules.overspend_tolerance == ((0, 1), (50, 2.5))
    assert config.sale_days.extra_dates == {date(2026, 11, 25)}
    assert config.teams[1].slug == "team-b" and config.state_dir.is_absolute()
    assert str(config.timezone) == "Asia/Manila" and config.language == "tl"


@pytest.mark.parametrize("raw, env, message", [
    ({"teams": [TEAM]}, {"HOOK": WEBHOOK}, "META_ACCESS_TOKEN is not set"),
    ([], ENV, "config must be a JSON object"),
    ({"teams": [TEAM], "dryrun": True}, ENV, "unknown key in config: dryrun"),
    ({"teams": []}, ENV, "teams must be a non-empty list"),
    ({"teams": [{**TEAM, "ad_accounts": ["1234567"]}]}, ENV, 'must look like "act_'),
    ({"teams": [{**TEAM, "ad_accounts": []}]}, ENV, "ad_accounts must be a non-empty list"),
    ({"teams": [{**TEAM, "name": " "}]}, ENV, "name must be a non-empty string"),
    ({"teams": [{**TEAM, "name": "!!"}]}, ENV, "needs at least one letter"),
    ({"teams": [TEAM, {**TEAM, "ad_accounts": ["act_9999999"]}]}, ENV, "same name"),
    ({"teams": [TEAM, {**TEAM, "name": "B"}]}, ENV, "listed more than once"),
    ({"teams": [{**TEAM, "discord_webhook_env": "lower"}]}, ENV, "environment variable name"),
    ({"teams": [{**TEAM, "discord_webhook_env": "MISSING"}]}, ENV, "MISSING (from"),
    ({"teams": [TEAM]}, {**ENV, "HOOK": "https://evil.example/hook"}, "not a Discord webhook URL"),
    ({"teams": [TEAM], "language": "fr"}, ENV, "language must be one of en, tl"),
    ({"teams": [TEAM], "graph_version": "25"}, ENV, "graph_version"),
    ({"teams": [TEAM], "dry_run": "no"}, ENV, "dry_run must be true or false"),
    ({"teams": [TEAM], "timezone": "Mars/Base"}, ENV, "not a known time zone"),
    ({"teams": [TEAM], "rules": {"hard_spend_limit": 0}}, ENV, "greater than 0"),
    ({"teams": [TEAM], "rules": {"hard_spend_limit": "1"}}, ENV, "must be a number"),
    ({"teams": [TEAM], "rules": {"hard_spend_limit": True}}, ENV, "must be a number"),
    ({"teams": [TEAM], "rules": {"spike_multiplier": 1}}, ENV, "greater than 1"),
    ({"teams": [TEAM], "rules": {"overspend_tolerance": [[10, 1]]}}, ENV, "must start at 0"),
    ({"teams": [TEAM], "rules": {"overspend_tolerance": [[0, 1], [0, 2]]}}, ENV, "budgets must go up"),
    ({"teams": [TEAM], "rules": {"overspend_tolerance": [[0]]}}, ENV, "each tier"),
    ({"teams": [TEAM], "rules": {"overspend_tolerance": []}}, ENV, "non-empty list"),
    ({"teams": [TEAM], "rules": {"overspend_tolerance": [[0, -1]]}}, ENV, "at least 0"),
    ({"teams": [TEAM], "sale_days": {"payday_days": [0]}}, ENV, "days from 1 to 31"),
    ({"teams": [TEAM], "sale_days": {"payday_days": [True]}}, ENV, "days from 1 to 31"),
    ({"teams": [TEAM], "sale_days": {"extra_dates": "2026-11-25"}}, ENV, "must be a list"),
    ({"teams": [TEAM], "sale_days": {"extra_dates": ["11/25/2026"]}}, ENV, "is not a"),
    ({"teams": [TEAM], "sale_days": {"payday": [15]}}, ENV, "unknown key in sale_days"),
    ({"teams": [TEAM]}, ENV, "timezone is required"),
    ({"teams": [TEAM], "timezone": "Asia"}, ENV, "not a known time zone"),
    ({"teams": [{**TEAM, "ad_accounts": ["act_" + chr(0x661) * 7]}]}, ENV, 'must look like "act_'),
    ({"teams": [{**TEAM, "ad_accounts": ["act_1234567\n"]}]}, ENV, 'must look like "act_'),
    ({"teams": [TEAM]}, {**ENV, "HOOK": WEBHOOK + "/../x"}, "not a Discord webhook URL"),
])
def test_invalid_configs_are_rejected_with_a_clear_message(raw, env, message):
    with pytest.raises(ConfigError, match=None) as caught:
        parse(raw, env)
    assert message in str(caught.value)


def test_load_config_file_errors(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.json", ENV)
    broken = tmp_path / "broken.json"
    broken.write_text("{nope", encoding="utf-8")
    with pytest.raises(ConfigError, match="cannot read config file"):
        load_config(broken, ENV)
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"teams": [TEAM], "timezone": "UTC"}), encoding="utf-8")
    assert load_config(good, ENV).state_dir == tmp_path.resolve() / "state"


def test_example_config_in_the_repo_is_valid():
    example = Path(__file__).resolve().parents[1] / "config.example.json"
    raw = json.loads(example.read_text(encoding="utf-8"))
    env = {"META_ACCESS_TOKEN": TOKEN, **{
        team[key]: WEBHOOK for team in raw["teams"]
        for key in ("discord_webhook_env", "discord_issues_webhook_env") if key in team}}
    config = parse(raw, env)
    assert config.dry_run is True and config.rules == Rules()
