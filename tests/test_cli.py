import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import TOKEN, WEBHOOK, FakeGraph, campaign

from meta_ads_watchdog import cli
from meta_ads_watchdog.graph import GraphError
from meta_ads_watchdog.state import PAUSED_FILE, PausedCampaign, PausedList

ACCOUNT = "act_1000001"


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A config folder with a .env, and a clean environment."""
    for key in ("META_ACCESS_TOKEN", "HOOK_A"):
        monkeypatch.delenv(key, raising=False)
    config = {"teams": [{"name": "Team A", "ad_accounts": [ACCOUNT], "discord_webhook_env": "HOOK_A"},
                        {"name": "Team B", "ad_accounts": ["act_1000002"]}],
              "dry_run": False, "timezone": "Asia/Manila"}
    (tmp_path / "watchdog.config.json").write_text(json.dumps(config), encoding="utf-8")
    (tmp_path / ".env").write_text(f"META_ACCESS_TOKEN={TOKEN}\nHOOK_A={WEBHOOK}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    yield tmp_path
    logging.getLogger("meta_ads_watchdog").handlers.clear()


@pytest.fixture
def fake(monkeypatch):
    graph = FakeGraph()
    graph.add_account(ACCOUNT)
    graph.add_account("act_1000002")
    sent = []

    class Session:
        def post(self, url, json=None, **kwargs):
            sent.append(json["content"])
            return type("R", (), {"status_code": 204})()

    monkeypatch.setattr(cli, "_clients", lambda config: (graph, Session()))
    graph.sent = sent
    return graph


def test_config_error_exits_2(project, capsys):
    (project / ".env").write_text("", encoding="utf-8")
    assert cli.main(["check-config"]) == cli.EXIT_CONFIG
    assert "META_ACCESS_TOKEN is not set" in capsys.readouterr().err


def test_unknown_team_exits_2(project, capsys):
    assert cli.main(["scan", "--team", "Nope"]) == cli.EXIT_CONFIG
    assert "no team named nope" in capsys.readouterr().err


def test_check_config_offline(project, capsys):
    assert cli.main(["check-config"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "Config OK. 2 team(s), 2 ad account(s)." in out and "LIVE (pauses for real)" in out
    assert "- Team B: 1 account(s), Discord OFF" in out


def test_check_config_online(project, fake, capsys):
    assert cli.main(["check-config", "--online"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "Token OK." in out and f"{ACCOUNT}: Shop 01 | status 1 | PHP" in out
    fake.errors["GET /act_1000002"] = GraphError("[200] no permission", code=200)
    assert cli.main(["check-config", "--online"]) == cli.EXIT_FAILED
    fake.errors["GET /me"] = GraphError("[190] bad token", code=190)
    assert cli.main(["check-config", "--online"]) == cli.EXIT_FAILED
    assert "Token FAILED" in capsys.readouterr().out


def test_scan_pauses_and_writes_a_redacted_log(project, fake, capsys):
    fake.add_campaign(campaign("111", daily=500), spend=600)
    fake.errors["GET /act_1000002"] = GraphError(f"[1] oops {TOKEN}", code=1)
    assert cli.main(["scan"]) == cli.EXIT_FAILED  # one account failed
    assert fake.posts == [("/111", {"status": "PAUSED"})]
    assert "paused 1, errors 1" in capsys.readouterr().out
    log_text = (project / "state" / "scan.log").read_text(encoding="utf-8")
    assert "PAUSE_OVERSPENT" in log_text and TOKEN not in log_text and "***" in log_text


def test_scan_dry_run_flag_overrides_the_config(project, fake):
    fake.add_campaign(campaign("111", daily=500), spend=600)
    assert cli.main(["scan", "--dry-run", "--team", "team a"]) == cli.EXIT_OK
    assert fake.posts == [] and fake.sent[0].startswith("🧪 **DRY RUN**")


def test_reactivate_command(project, fake, capsys):
    PausedList(project / "state" / "team-a" / PAUSED_FILE).add(
        PausedCampaign("111", "C", ACCOUNT, "pause_overspent", "2020-01-01"))
    fake.statuses["111"] = "PAUSED"
    assert cli.main(["reactivate"]) == cli.EXIT_OK
    assert fake.statuses["111"] == "ACTIVE"
    assert "on 1, retry 0" in capsys.readouterr().out
    fake.errors["GET /me"] = GraphError("[190] x", code=190)
    PausedList(project / "state" / "team-a" / PAUSED_FILE).add(
        PausedCampaign("222", "C", ACCOUNT, "pause_overspent", "2020-01-01"))
    assert cli.main(["reactivate"]) == cli.EXIT_FAILED


def test_unexpected_crash_is_logged_without_the_token(project, fake, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError(f"disk full near {TOKEN}")
    monkeypatch.setattr(cli, "run_scan", boom)
    assert cli.main(["scan"]) == cli.EXIT_FAILED
    log_text = (project / "state" / "scan.log").read_text(encoding="utf-8")
    assert "RuntimeError: disk full near ***" in log_text and TOKEN not in log_text


def test_webhook_urls_are_redacted_from_logs_too(project, fake, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError(f"could not reach {WEBHOOK}")
    monkeypatch.setattr(cli, "run_reactivate", boom)
    assert cli.main(["reactivate", "--team", "Team A"]) == cli.EXIT_FAILED
    log_text = (project / "state" / "reactivate-team-a.log").read_text(encoding="utf-8")
    assert WEBHOOK not in log_text and "could not reach ***" in log_text


def test_skipped_team_exits_3(project, fake, capsys):
    lock = project / "state" / "team-b" / "run.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("someone-else now", encoding="utf-8")
    assert cli.main(["scan"]) == cli.EXIT_SKIPPED
    assert "skipped (another run still going): Team B" in capsys.readouterr().out
    assert lock.exists()  # someone else's lock is never removed


def test_log_file_cannot_be_written(project, fake, capsys):
    (project / "state").write_text("a file where the folder should be", encoding="utf-8")
    assert cli.main(["scan"]) == cli.EXIT_FAILED
    assert "Cannot write the log" in capsys.readouterr().err


def test_module_entry_point_runs():
    src = Path(__file__).resolve().parents[1] / "src"
    done = subprocess.run([sys.executable, "-m", "meta_ads_watchdog", "--version"],
                          capture_output=True, text=True, cwd=src)
    assert done.returncode == 0 and "meta-ads-watchdog 1.0.0" in done.stdout
