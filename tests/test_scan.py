import json
from datetime import timedelta

from conftest import ACCOUNT, NORMAL_NOW, PAYDAY_NOW, WEBHOOK as WEBHOOK_URL, campaign, make_config

from meta_ads_watchdog.graph import GraphError
from meta_ads_watchdog.scan import find_actor, run_scan
from meta_ads_watchdog.state import PAUSED_FILE, STATE_FILE, PausedList


def scan(tmp_path, graph, notifier, now=NORMAL_NOW, dry_run=False, **config):
    cfg = make_config(tmp_path, **config)
    result = run_scan(cfg, graph, lambda team: notifier, now, cfg.teams, dry_run)
    return cfg, result


def paused_list(cfg):
    records, bad = PausedList(cfg.state_dir / "team-a" / PAUSED_FILE).read()
    assert bad == []
    return records


def test_overspent_campaign_is_paused_listed_and_alerted(tmp_path, graph, notifier):
    graph.add_campaign(campaign("111", daily=500), spend=530)
    graph.add_campaign(campaign("222", daily=500), spend=529)
    cfg, result = scan(tmp_path, graph, notifier)
    assert graph.posts == [("/111", {"status": "PAUSED"})]
    assert result.paused == 1 and result.errors == 0
    assert [(r.campaign_id, r.reason, r.paused_on) for r in paused_list(cfg)] == [
        ("111", "pause_overspent", "2026-11-18")]
    assert notifier.titles() == ["🔴 **Overspent, paused**"]
    assert notifier.sent[0][1] is False  # main channel only


def test_second_run_does_not_pause_or_list_again(tmp_path, graph, notifier):
    graph.add_campaign(campaign("111", daily=500), spend=530)
    cfg, _ = scan(tmp_path, graph, notifier)
    scan(tmp_path, graph, notifier)
    assert len(graph.posts) == 1 and len(paused_list(cfg)) == 1 and len(notifier.sent) == 1


def test_payday_announces_once_and_alerts_overspend_once_without_pausing(tmp_path, graph, notifier):
    graph.add_campaign(campaign("111", daily=500), spend=700)
    scan(tmp_path, graph, notifier, now=PAYDAY_NOW)
    scan(tmp_path, graph, notifier, now=PAYDAY_NOW + timedelta(minutes=30))
    assert graph.posts == []
    assert notifier.titles() == ["🟢 **Sale-day mode is ON today (Payday (Nov 15))**",
                                 "🟡 **Overspent but still running (sale day)**"]


def test_payday_escalation_pauses_at_twice_the_budget(tmp_path, graph, notifier):
    graph.add_campaign(campaign("111", daily=500), spend=1000)
    cfg, _ = scan(tmp_path, graph, notifier, now=PAYDAY_NOW)
    assert graph.posts == [("/111", {"status": "PAUSED"})]
    assert paused_list(cfg)[0].reason == "pause_escalation"
    assert "Escalation" in notifier.titles()[1]


def test_announcement_can_be_turned_off(tmp_path, graph, notifier):
    scan(tmp_path, graph, notifier, now=PAYDAY_NOW, sale_days={"announce": False})
    assert notifier.sent == []


def test_hard_limit_and_anomaly_pause_but_are_not_listed(tmp_path, graph, notifier):
    graph.add_campaign(campaign("111", daily=30000), spend=40000)
    graph.add_campaign(campaign("222", daily=45000), spend=0)
    cfg, result = scan(tmp_path, graph, notifier)
    assert sorted(p for p, _ in graph.posts) == ["/111", "/222"]
    assert result.paused == 2 and paused_list(cfg) == []
    titles = notifier.titles()
    assert any("Emergency pause" in t for t in titles) and any("anomaly" in t for t in titles)
    assert all(is_issue for _, is_issue in notifier.sent)


def test_budget_spike_names_who_changed_it(tmp_path, graph, notifier):
    graph.add_campaign(campaign("111", daily=100), spend=0)
    cfg, _ = scan(tmp_path, graph, notifier)
    graph.campaigns[ACCOUNT][0]["daily_budget"] = "100000"  # 100 -> 1,000
    graph.activities[ACCOUNT] = [
        {"object_id": "111", "event_type": "update_campaign_budget", "actor_name": "Mallory",
         "event_time": "2026-11-18T11:50:00+0800"},
        {"object_id": "111", "event_type": "update_campaign_name", "actor_name": "Alice",
         "event_time": "2026-11-18T11:55:00+0800"}]
    scan(tmp_path, graph, notifier, now=NORMAL_NOW + timedelta(minutes=30))
    content, is_issue = notifier.sent[0]
    assert "Security alert" in content and is_issue
    assert "Budget: 100 → 1,000" in content and "Mallory (Nov 18, 11:50 AM)" in content
    assert paused_list(cfg) == []


def test_spike_paused_campaign_turned_back_on_by_a_person_is_left_alone(tmp_path, graph, notifier):
    graph.add_campaign(campaign("111", daily=100), spend=0)
    scan(tmp_path, graph, notifier)
    graph.campaigns[ACCOUNT][0]["daily_budget"] = "100000"
    scan(tmp_path, graph, notifier)
    assert graph.statuses["111"] == "PAUSED"
    graph.statuses["111"] = "ACTIVE"  # the owner checked it and turned it back on
    scan(tmp_path, graph, notifier)
    assert len(graph.posts) == 1


def test_new_big_campaign_is_paused_once_then_trusted(tmp_path, graph, notifier):
    created = (NORMAL_NOW - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S+0000")
    graph.add_campaign(campaign("111", daily=1000, created=created), spend=0)
    scan(tmp_path, graph, notifier)
    assert "new campaign" in notifier.titles()[0]
    assert "not in the activity log yet" in notifier.sent[0][0]
    graph.statuses["111"] = "ACTIVE"
    scan(tmp_path, graph, notifier)
    assert len(graph.posts) == 1


def test_old_or_unparsable_created_time_is_not_new(tmp_path, graph, notifier):
    graph.add_campaign(campaign("111", daily=5000, created="2026-01-01T00:00:00+0000"))
    graph.add_campaign(campaign("222", daily=5000, created="garbage"))
    scan(tmp_path, graph, notifier)
    assert graph.posts == []


def test_abo_campaign_uses_its_ad_set_budgets(tmp_path, graph, notifier):
    graph.add_campaign(campaign("111"), spend=830)  # 800 budget + 30 tolerance
    graph.add_campaign(campaign("222"), spend=829)
    graph.ad_sets[ACCOUNT] = [{"campaign_id": "111", "daily_budget": "40000"},
                              {"campaign_id": "111", "daily_budget": "40000"},
                              {"campaign_id": "222", "daily_budget": "80000"}]
    scan(tmp_path, graph, notifier)
    assert graph.posts == [("/111", {"status": "PAUSED"})]


def test_ad_sets_are_only_fetched_when_needed(tmp_path, graph, notifier):
    graph.add_campaign(campaign("111", daily=500), spend=0)
    scan(tmp_path, graph, notifier)
    assert not any(path.endswith("/adsets") for _, path, _ in graph.calls)


def test_requests_ask_meta_for_the_right_things(tmp_path, graph, notifier):
    graph.add_campaign(campaign("111", daily=500), spend=0)
    scan(tmp_path, graph, notifier)
    calls = {path: params for _, path, params in graph.calls}
    assert calls[f"/{ACCOUNT}/campaigns"]["effective_status"] == ["ACTIVE"]
    assert calls[f"/{ACCOUNT}/insights"] == {"level": "campaign", "fields": "campaign_id,spend",
                                             "date_preset": "today", "limit": 200}


def test_lifetime_budget_campaign_is_only_hard_limited(tmp_path, graph, notifier):
    graph.add_campaign(campaign("111", lifetime=5000), spend=4000)
    scan(tmp_path, graph, notifier)
    assert graph.posts == []


def test_zero_decimal_currency(tmp_path, graph, notifier):
    graph.add_account(currency="JPY")
    graph.add_campaign({"id": "111", "name": "Yen", "daily_budget": "5000",
                        "created_time": "2026-01-01T00:00:00+0000"}, spend=5100)
    scan(tmp_path, graph, notifier)
    assert graph.posts == []  # 5,000 yen budget + 150 tolerance > 5,100


def test_inactive_account_alerts_once_a_day_and_is_skipped(tmp_path, graph, notifier):
    graph.add_account(status=3, disable_reason=3)
    graph.add_campaign(campaign("111", daily=500), spend=0)
    scan(tmp_path, graph, notifier)
    scan(tmp_path, graph, notifier)
    assert not any(path.endswith("/campaigns") for _, path, _ in graph.calls)
    assert len(notifier.sent) == 1
    content, is_issue = notifier.sent[0]
    assert is_issue and "Account issue: UNSETTLED" in content and "RISK_PAYMENT" in content
    graph.accounts[ACCOUNT]["account_status"] = 1
    scan(tmp_path, graph, notifier)
    graph.accounts[ACCOUNT]["account_status"] = 3
    scan(tmp_path, graph, notifier)
    assert len(notifier.sent) == 2  # alerted again after it recovered and broke again


def test_unknown_status_codes_are_shown_raw(tmp_path, graph, notifier):
    graph.add_account(status=55, disable_reason=99)
    scan(tmp_path, graph, notifier)
    assert "status 55" in notifier.sent[0][0] and "code 99" in notifier.sent[0][0]


def test_one_broken_account_does_not_stop_the_others(tmp_path, graph, notifier):
    second = "act_1000002"
    graph.add_account(second)
    graph.add_campaign(campaign("111", daily=500), spend=600)
    graph.add_campaign(campaign("222", daily=500), spend=600, account=second)
    graph.errors[f"GET /{ACCOUNT}/insights"] = GraphError("[2] Service unavailable", code=2)
    teams = [{"name": "Team A", "ad_accounts": [ACCOUNT, second]}]
    _, result = scan(tmp_path, graph, notifier, teams=teams)
    _, result2 = scan(tmp_path, graph, notifier, teams=teams)
    assert graph.posts == [("/222", {"status": "PAUSED"})]
    assert result.errors == 1 and result2.errors == 1
    scan_alerts = [c for c, issue in notifier.sent if "Scan issue" in c]
    assert len(scan_alerts) == 1  # once a day, not every 30 minutes


def test_token_problem_stops_the_run_and_alerts_once(tmp_path, graph, notifier):
    graph.errors[f"GET /{ACCOUNT}"] = GraphError("[190] Error validating access token", code=190)
    _, result = scan(tmp_path, graph, notifier)
    scan(tmp_path, graph, notifier)
    assert result.token_failed and result.errors == 1
    assert len(notifier.sent) == 1 and "Token issue" in notifier.sent[0][0]


def test_token_problem_during_pause_stops_the_run(tmp_path, graph, notifier):
    graph.add_campaign(campaign("111", daily=500), spend=600)
    graph.errors["POST /111"] = GraphError("[190] expired", code=190)
    cfg, result = scan(tmp_path, graph, notifier)
    assert result.token_failed and graph.statuses["111"] == "ACTIVE"
    # Listed before the pause; the reactivator finds it ACTIVE and only says "already on".
    assert [r.campaign_id for r in paused_list(cfg)] == ["111"]


def test_failed_overspend_pause_is_reported_once_and_retried(tmp_path, graph, notifier):
    graph.add_campaign(campaign("111", daily=500), spend=600)
    graph.errors["POST /111"] = GraphError("[2] Service temporarily unavailable", code=2)
    cfg, result = scan(tmp_path, graph, notifier)
    scan(tmp_path, graph, notifier)
    assert result.errors == 1 and result.paused == 0 and len(graph.posts) == 2
    assert len(notifier.sent) == 1
    content, is_issue = notifier.sent[0]
    assert "Pause issue" in content and is_issue and "pause_overspent" in content
    del graph.errors["POST /111"]
    scan(tmp_path, graph, notifier)
    assert graph.statuses["111"] == "PAUSED" and len(paused_list(cfg)) == 1


def test_failed_security_pause_is_retried_next_run(tmp_path, graph, notifier):
    created = (NORMAL_NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S+0000")
    graph.add_campaign(campaign("111", daily=5000, created=created), spend=0)
    graph.add_campaign(campaign("222", daily=100), spend=0)
    graph.errors["POST /111"] = GraphError("[2] Service temporarily unavailable", code=2)
    scan(tmp_path, graph, notifier)  # 111: the pause fails; 222: budget 100 remembered
    graph.errors["POST /222"] = GraphError("[2] Service temporarily unavailable", code=2)
    graph.campaigns[ACCOUNT][1]["daily_budget"] = "100000"  # 100 -> 1,000 spike
    scan(tmp_path, graph, notifier)  # both pauses fail
    assert graph.statuses == {"111": "ACTIVE", "222": "ACTIVE"}
    del graph.errors["POST /111"], graph.errors["POST /222"]
    scan(tmp_path, graph, notifier)  # Meta is back: both are checked again and paused
    assert graph.statuses == {"111": "PAUSED", "222": "PAUSED"}
    assert sum("Pause issue" in content for content, _ in notifier.sent) == 2  # once each


def test_dry_run_does_not_mark_campaigns_as_cleared(tmp_path, graph, notifier):
    created = (NORMAL_NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S+0000")
    graph.add_campaign(campaign("111", daily=5000, created=created), spend=0)
    graph.add_campaign(campaign("222", daily=100), spend=0)
    scan(tmp_path, graph, notifier, dry_run=True)
    graph.campaigns[ACCOUNT][1]["daily_budget"] = "100000"
    scan(tmp_path, graph, notifier, dry_run=True)
    assert graph.posts == []
    scan(tmp_path, graph, notifier)  # going live
    assert graph.statuses == {"111": "PAUSED", "222": "PAUSED"}


def test_dry_run_changes_nothing_and_alerts_once(tmp_path, graph, notifier):
    graph.add_campaign(campaign("111", daily=500), spend=600)
    cfg, result = scan(tmp_path, graph, notifier, dry_run=True)
    scan(tmp_path, graph, notifier, dry_run=True)
    assert graph.posts == [] and paused_list(cfg) == [] and result.paused == 0
    assert notifier.titles() == ["🔴 **Overspent, paused**"]


def test_later_pause_needing_a_person_removes_the_old_list_entry(tmp_path, graph, notifier):
    graph.add_campaign(campaign("111", daily=30000), spend=31000)
    cfg, _ = scan(tmp_path, graph, notifier)
    assert [r.reason for r in paused_list(cfg)] == ["pause_overspent"]
    graph.statuses["111"] = "ACTIVE"   # the owner turned it back on the same day
    graph.spend[ACCOUNT]["111"] = 40000
    scan(tmp_path, graph, notifier)
    assert paused_list(cfg) == [] and graph.statuses["111"] == "PAUSED"
    data = json.loads((cfg.state_dir / "team-a" / STATE_FILE).read_text(encoding="utf-8"))
    assert data["held"] == {"111": "2026-11-18"}


def test_list_write_failure_still_pauses_and_alerts(tmp_path, graph, notifier, monkeypatch):
    graph.add_campaign(campaign("111", daily=500), spend=600)

    def broken(self, record):
        raise OSError("disk full")
    monkeypatch.setattr(PausedList, "add", broken)
    _, result = scan(tmp_path, graph, notifier)
    assert graph.statuses["111"] == "PAUSED" and result.errors == 1
    titles = notifier.titles()
    assert any("List issue" in t for t in titles) and "🔴 **Overspent, paused**" in titles


def test_a_crash_in_one_team_does_not_stop_the_next(tmp_path, graph, notifier):
    second = "act_1000002"
    graph.add_account(second)
    graph.add_campaign(campaign("222", daily=500), spend=600, account=second)
    teams = [{"name": "Team A", "ad_accounts": [ACCOUNT]}, {"name": "Team B", "ad_accounts": [second]}]
    cfg = make_config(tmp_path, teams=teams)
    (cfg.state_dir / "team-a" / STATE_FILE).mkdir(parents=True)
    _, result = scan(tmp_path, graph, notifier, teams=teams)
    assert result.errors == 1 and graph.statuses["222"] == "PAUSED"


def test_timezone_mismatch_is_logged_once_a_day(tmp_path, graph, notifier, caplog):
    graph.accounts[ACCOUNT]["timezone_name"] = "America/Los_Angeles"
    scan(tmp_path, graph, notifier)
    scan(tmp_path, graph, notifier)
    assert sum("uses time zone America/Los_Angeles" in r.message for r in caplog.records) == 1


def test_heartbeat_refreshes_the_lock_after_each_account(tmp_path, graph, notifier, monkeypatch):
    beats = []
    monkeypatch.setattr("meta_ads_watchdog.scan.TeamLock.touch", lambda self: beats.append(1))
    scan(tmp_path, graph, notifier, teams=[{"name": "Team A",
                                            "ad_accounts": [ACCOUNT, "act_1000002"]}])
    assert len(beats) == 2


def test_campaign_without_numeric_id_is_skipped(tmp_path, graph, notifier):
    graph.add_campaign({"id": "abc", "name": "odd"}, spend=9999)
    _, result = scan(tmp_path, graph, notifier)
    assert graph.posts == [] and result.errors == 0


def test_campaign_names_cannot_inject_markdown_or_extra_lines(tmp_path, graph, notifier):
    graph.add_campaign(campaign("111", daily=500,
                                name="**Fake**\nBudget: 0 @everyone <https://evil.example>\u202e"),
                       spend=600)
    scan(tmp_path, graph, notifier)
    content = notifier.sent[0][0]
    assert ("Campaign: \\*\\*Fake\\*\\* Budget: 0 @\u200beveryone "
            "\\<https:\u200b//evil.example\\>\n") in content
    assert [line for line in content.splitlines() if line.startswith("Budget:")] == [
        "Budget: 500 | Spend today: 600"]


def test_state_is_saved_and_budgets_remembered(tmp_path, graph, notifier):
    graph.add_campaign(campaign("111", daily=500), spend=0)
    cfg, _ = scan(tmp_path, graph, notifier)
    data = json.loads((cfg.state_dir / "team-a" / STATE_FILE).read_text(encoding="utf-8"))
    assert data["budgets"]["111"] == {"budget": 500.0, "seen": "2026-11-18"}


def test_locked_team_is_skipped(tmp_path, graph, notifier):
    cfg = make_config(tmp_path)
    lock = cfg.state_dir / "team-a" / "run.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("1 now", encoding="utf-8")
    _, result = scan(tmp_path, graph, notifier)
    assert result.skipped_teams == ["Team A"] and graph.calls == []


def test_find_actor_falls_back_to_any_event_for_the_campaign():
    rows = [{"object_id": "1", "event_type": "update_campaign_name", "actor_name": "",
             "event_time": "bad"}]
    assert find_actor(rows, "1", "budget") == "Unknown user"
    assert find_actor(rows, "2", "budget") is None


def test_unreadable_list_file_never_blocks_a_pause(tmp_path, graph, notifier):
    graph.add_campaign(campaign("111", daily=30000), spend=40000)
    cfg = make_config(tmp_path)
    listed = cfg.state_dir / "team-a" / PAUSED_FILE
    listed.parent.mkdir(parents=True)
    listed.write_text('{"campaign_id": "111"}', encoding="utf-16")  # saved by another editor
    _, result = scan(tmp_path, graph, notifier)
    assert graph.statuses["111"] == "PAUSED" and result.paused == 1
    assert len(list(listed.parent.glob(PAUSED_FILE + ".damaged-*"))) == 1


def test_unreadable_discord_queue_never_blocks_a_scan(tmp_path, graph, notifier):
    from meta_ads_watchdog.discord import Notifier, Webhook
    graph.add_campaign(campaign("111", daily=500), spend=600)
    cfg = make_config(tmp_path)
    queue = cfg.state_dir / "team-a" / "discord-queue.jsonl"
    queue.parent.mkdir(parents=True)
    queue.write_text('{"content": "x"}', encoding="utf-16")
    real = Notifier(Webhook(None, None), Webhook(None, None), queue)
    result = run_scan(cfg, graph, lambda team: real, NORMAL_NOW, cfg.teams, False)
    assert graph.statuses["111"] == "PAUSED" and result.errors == 0


def test_security_pause_happens_before_the_activity_lookup(tmp_path, graph, notifier):
    created = (NORMAL_NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S+0000")
    graph.add_campaign(campaign("111", daily=5000, created=created), spend=0)
    scan(tmp_path, graph, notifier)
    order = [(method, path) for method, path, _ in graph.calls if method != "GET"]
    assert order[-2:] == [("POST", "/111"), ("GET_ALL", f"/{ACCOUNT}/activities")]


def test_retried_overspend_pause_is_listed_once(tmp_path, graph, notifier):
    graph.add_campaign(campaign("111", daily=500), spend=600)
    graph.errors["POST /111"] = GraphError("[2] Service temporarily unavailable", code=2)
    cfg, _ = scan(tmp_path, graph, notifier)
    scan(tmp_path, graph, notifier)
    scan(tmp_path, graph, notifier)
    lines = (cfg.state_dir / "team-a" / PAUSED_FILE).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


def test_timezone_aliases_are_not_flagged(tmp_path, graph, notifier, caplog):
    graph.accounts[ACCOUNT]["timezone_name"] = "Asia/Singapore"  # same offset as Manila
    scan(tmp_path, graph, notifier)
    assert not any("uses time zone" in r.message for r in caplog.records)


def test_losing_the_lock_stops_the_team_without_saving(tmp_path, graph, notifier, monkeypatch):
    graph.add_campaign(campaign("111", daily=500), spend=0)
    monkeypatch.setattr("meta_ads_watchdog.scan.TeamLock.touch", lambda self: False)
    cfg, result = scan(tmp_path, graph, notifier)
    assert result.errors == 1 and not (cfg.state_dir / "team-a" / STATE_FILE).exists()


def test_discord_rejections_count_as_errors(tmp_path, graph):
    from meta_ads_watchdog.discord import Notifier, Webhook

    class Refusing:
        def post(self, url, **kwargs):
            return type("R", (), {"status_code": 404})()

    graph.add_campaign(campaign("111", daily=500), spend=600)
    cfg = make_config(tmp_path)
    session = Refusing()
    real = Notifier(Webhook(WEBHOOK_URL, session), Webhook(None, session),
                    cfg.state_dir / "team-a" / "discord-queue.jsonl")
    result = run_scan(cfg, graph, lambda team: real, NORMAL_NOW, cfg.teams, False)
    assert graph.statuses["111"] == "PAUSED" and result.errors == 1


def test_any_list_error_never_blocks_a_pause(tmp_path, graph, notifier, monkeypatch):
    graph.add_campaign(campaign("111", daily=30000), spend=40000)

    def broken(self, campaign_id):
        raise ValueError("unexpected content")
    monkeypatch.setattr(PausedList, "remove", broken)
    _, result = scan(tmp_path, graph, notifier)
    assert graph.statuses["111"] == "PAUSED" and result.errors == 1


def test_queued_alert_trouble_never_blocks_a_scan(tmp_path, graph, notifier, monkeypatch):
    graph.add_campaign(campaign("111", daily=500), spend=600)

    def broken():
        raise RuntimeError("queue trouble")
    monkeypatch.setattr(notifier, "flush_queue", broken)
    _, result = scan(tmp_path, graph, notifier)
    assert graph.statuses["111"] == "PAUSED" and result.errors == 1


def test_token_dying_right_after_a_security_pause_keeps_its_alert_and_state(
        tmp_path, graph, notifier):
    created = (NORMAL_NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S+0000")
    graph.add_campaign(campaign("111", daily=5000, created=created), spend=0)
    graph.errors[f"GET /{ACCOUNT}/activities"] = GraphError("[190] expired", code=190)
    cfg, _ = scan(tmp_path, graph, notifier)
    assert graph.statuses["111"] == "PAUSED"
    assert any("Security alert" in content for content, _ in notifier.sent)
    data = json.loads((cfg.state_dir / "team-a" / STATE_FILE).read_text(encoding="utf-8"))
    assert data["security_paused"] == {"111": "2026-11-18"}
