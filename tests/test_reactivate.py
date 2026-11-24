import json
from datetime import timedelta

from conftest import ACCOUNT, NORMAL_NOW, make_config

from meta_ads_watchdog.graph import GraphError
from meta_ads_watchdog.reactivate import run_reactivate
from meta_ads_watchdog.state import (ATTENTION_FILE, PAUSED_FILE, STATE_FILE, PausedCampaign,
                                     PausedList, TeamLock, TeamState)

YESTERDAY = "2026-11-17"
TODAY = "2026-11-18"


def setup(tmp_path, graph, records, bad_lines=(), **config):
    cfg = make_config(tmp_path, **config)
    paused = PausedList(cfg.state_dir / "team-a" / PAUSED_FILE)
    paused.path.parent.mkdir(parents=True, exist_ok=True)
    for cid, day, status in records:
        paused.add(PausedCampaign(cid, f"Campaign {cid}", ACCOUNT, "pause_overspent", day))
        if status is not None:
            graph.statuses[cid] = status
    if bad_lines:
        with paused.path.open("a", encoding="utf-8") as handle:
            for line in bad_lines:
                handle.write(line + "\n")
    return cfg, paused


def run(cfg, graph, notifier, dry_run=False, now=NORMAL_NOW):
    return run_reactivate(cfg, graph, lambda team: notifier, now, cfg.teams, dry_run)


def attention(cfg):
    path = cfg.state_dir / "team-a" / ATTENTION_FILE
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def test_turns_on_yesterdays_campaigns_and_keeps_todays(tmp_path, graph, notifier):
    cfg, paused = setup(tmp_path, graph, [("111", YESTERDAY, "PAUSED"), ("222", TODAY, "PAUSED")])
    result = run(cfg, graph, notifier)
    assert graph.posts == [("/111", {"status": "ACTIVE"})]
    assert graph.statuses == {"111": "ACTIVE", "222": "PAUSED"}
    assert [r.campaign_id for r in paused.read()[0]] == ["222"]
    assert result.turned_on == 1 and not result.aborted
    assert len(notifier.sent) == 1
    summary, is_issue = notifier.sent[0]
    assert summary.startswith("✅ **Reactivation done**") and not is_issue
    assert "Paused today, waiting for tomorrow: 1" in summary
    assert "**Turned back on:**" in summary and "Campaign 111 (111): turned on" in summary
    assert len(list((cfg.state_dir / "team-a" / "backup").iterdir())) == 1


def test_running_twice_is_safe(tmp_path, graph, notifier):
    cfg, paused = setup(tmp_path, graph, [("111", YESTERDAY, "PAUSED")])
    run(cfg, graph, notifier)
    run(cfg, graph, notifier)
    assert len(graph.posts) == 1 and not paused.path.exists() and len(notifier.sent) == 1


def test_already_active_campaign_is_not_touched(tmp_path, graph, notifier):
    cfg, _ = setup(tmp_path, graph, [("111", YESTERDAY, "ACTIVE")])
    result = run(cfg, graph, notifier)
    assert graph.posts == [] and result.turned_on == 1


def test_archived_and_unreadable_entries_move_to_needs_attention(tmp_path, graph, notifier):
    hand_added = json.dumps({"campaign_id": "333", "campaign_name": "x", "account_id": ACCOUNT,
                             "reason": "pause_spike", "paused_on": "2026-01-01"})
    cfg, paused = setup(tmp_path, graph, [("111", YESTERDAY, "ARCHIVED")],
                        bad_lines=["not json", hand_added])
    graph.statuses["333"] = "PAUSED"
    result = run(cfg, graph, notifier)
    assert graph.posts == [] and result.manual == 3 and not paused.path.exists()
    kept = attention(cfg)
    assert len(kept) == 3 and json.loads(kept[0])["campaign_id"] == "111"
    assert kept[1:] == ["not json", hand_added]
    summary, is_issue = notifier.sent[0]
    assert "Reactivation issue" in summary and is_issue and "status is ARCHIVED" in summary
    assert "not json: unreadable or not an overspend entry" in summary


def test_errors_are_retried_not_dropped(tmp_path, graph, notifier):
    cfg, paused = setup(tmp_path, graph, [("111", YESTERDAY, "PAUSED"), ("222", YESTERDAY, "PAUSED"),
                                          ("444", YESTERDAY, None)])
    graph.errors["POST /111"] = GraphError("[613] Calls to this api have exceeded the rate limit.",
                                           code=613)
    graph.stuck.add("222")
    result = run(cfg, graph, notifier)  # 444: Meta answers code 100 (missing or no permission)
    assert result.retry == 3 and result.manual == 0 and attention(cfg) == []
    assert sorted(r.campaign_id for r in paused.read()[0]) == ["111", "222", "444"]
    assert "still PAUSED after turning on" in notifier.sent[0][0]


def test_gives_up_after_a_week_and_asks_a_person(tmp_path, graph, notifier):
    cfg, paused = setup(tmp_path, graph, [("444", "2026-11-10", None), ("555", "2026-11-11", None)])
    result = run(cfg, graph, notifier)
    assert (result.manual, result.retry) == (1, 1)
    assert [r.campaign_id for r in paused.read()[0]] == ["555"]
    assert json.loads(attention(cfg)[0])["campaign_id"] == "444"
    assert "still failing after 7 days" in notifier.sent[0][0]


def test_campaign_from_another_account_is_refused(tmp_path, graph, notifier):
    cfg, paused = setup(tmp_path, graph, [("111", YESTERDAY, "PAUSED")])
    graph.owners["111"] = "act_9999999"  # the list says our account, Meta says otherwise
    foreign = PausedCampaign("222", "x", "act_9999999", "pause_overspent", YESTERDAY)
    paused.add(foreign)
    graph.statuses["222"] = "PAUSED"
    result = run(cfg, graph, notifier)
    assert graph.posts == [] and result.manual == 2
    assert "outside this team" in notifier.sent[0][0]


def test_campaign_held_by_a_later_pause_is_refused(tmp_path, graph, notifier):
    cfg, _ = setup(tmp_path, graph, [("111", "2026-11-16", "PAUSED"), ("222", "2026-11-16", "PAUSED")])
    state = TeamState(cfg.state_dir / "team-a" / STATE_FILE, NORMAL_NOW.date())
    state.held["111"] = YESTERDAY      # paused for the spend limit after the overspend pause
    state.held["222"] = "2026-11-10"   # an older hold, before this overspend pause
    state.save()
    result = run(cfg, graph, notifier)
    assert graph.posts == [("/222", {"status": "ACTIVE"})] and result.manual == 1
    assert "paused again on 2026-11-17" in notifier.sent[0][0]


def test_token_problem_aborts_without_touching_the_list(tmp_path, graph, notifier):
    cfg, paused = setup(tmp_path, graph, [("111", YESTERDAY, "PAUSED")])
    before = paused.path.read_text(encoding="utf-8")
    graph.errors["GET /me"] = GraphError("[190] expired", code=190)
    result = run(cfg, graph, notifier)
    assert result.aborted and graph.posts == []
    assert paused.path.read_text(encoding="utf-8") == before
    assert "token problem" in notifier.sent[0][0] and notifier.sent[0][1]


def test_token_dying_midway_keeps_the_whole_list(tmp_path, graph, notifier):
    cfg, paused = setup(tmp_path, graph, [("111", YESTERDAY, "PAUSED"), ("222", YESTERDAY, "PAUSED")])
    graph.errors["POST /222"] = GraphError("[190] expired", code=190)
    result = run(cfg, graph, notifier)
    assert result.aborted and len(paused.read()[0]) == 2
    del graph.errors["POST /222"]  # new token in place
    rerun = run(cfg, graph, notifier)
    assert rerun.turned_on == 2 and not paused.path.exists()
    assert graph.posts.count(("/111", {"status": "ACTIVE"})) == 1  # already on, not posted again


def test_meta_unreachable_aborts(tmp_path, graph, notifier):
    cfg, _ = setup(tmp_path, graph, [("111", YESTERDAY, "PAUSED")])
    graph.errors["GET /me"] = GraphError("gave up after 4 tries: ConnectionError")
    result = run(cfg, graph, notifier)
    assert result.aborted and "Meta unreachable" in notifier.sent[0][0]


def test_dry_run_changes_nothing(tmp_path, graph, notifier):
    cfg, paused = setup(tmp_path, graph, [("111", YESTERDAY, "PAUSED")], bad_lines=["junk"])
    before = paused.path.read_text(encoding="utf-8")
    result = run(cfg, graph, notifier, dry_run=True)
    assert graph.posts == [] and notifier.sent == [] and result.turned_on == 1
    assert paused.path.read_text(encoding="utf-8") == before and attention(cfg) == []
    assert not (cfg.state_dir / "team-a" / "backup").exists()


def test_empty_list_does_nothing(tmp_path, graph, notifier):
    cfg = make_config(tmp_path)
    result = run(cfg, graph, notifier)
    assert graph.calls == [] and notifier.sent == [] and result.turned_on == 0


def test_long_lists_are_shortened_in_discord(tmp_path, graph, notifier):
    records = [(str(1000 + i), YESTERDAY, "ARCHIVED") for i in range(20)]
    cfg, _ = setup(tmp_path, graph, records)
    run(cfg, graph, notifier)
    assert notifier.sent[0][0].count("...and 5 more") == 1


def test_old_backups_are_pruned(tmp_path, graph, notifier):
    cfg, _ = setup(tmp_path, graph, [("111", YESTERDAY, "PAUSED")])
    folder = cfg.state_dir / "team-a" / "backup"
    folder.mkdir(parents=True)
    for i in range(35):
        (folder / f"2026010{i:02d}-000000-{PAUSED_FILE}").write_text("x", encoding="utf-8")
    run(cfg, graph, notifier, now=NORMAL_NOW + timedelta(seconds=1))
    assert len(list(folder.iterdir())) == 30


def test_waits_for_a_running_scan_to_finish(tmp_path, graph, notifier, monkeypatch):
    cfg, _ = setup(tmp_path, graph, [("111", YESTERDAY, "PAUSED")])
    lock = cfg.state_dir / "team-a" / "run.lock"
    lock.write_text("scan-in-progress now", encoding="utf-8")
    waits = []

    def fake_sleep(seconds):
        waits.append(seconds)
        lock.unlink()  # the scan finishes while the reactivator waits

    monkeypatch.setattr("meta_ads_watchdog.state.time.sleep", fake_sleep)
    result = run(cfg, graph, notifier)
    assert waits == [5.0] and result.turned_on == 1 and result.skipped_teams == []


def test_waits_for_a_running_scan_then_gives_up(tmp_path, graph, notifier, monkeypatch):
    cfg, paused = setup(tmp_path, graph, [("111", YESTERDAY, "PAUSED")])
    monkeypatch.setattr("meta_ads_watchdog.reactivate.LOCK_WAIT_SECONDS", 0)
    with TeamLock(cfg.state_dir / "team-a"):
        result = run(cfg, graph, notifier)
    assert result.skipped_teams == ["Team A"] and graph.posts == []


def test_a_crash_in_one_team_does_not_stop_the_next(tmp_path, graph, notifier):
    teams = [{"name": "Team A", "ad_accounts": [ACCOUNT]},
             {"name": "Team B", "ad_accounts": ["act_1000002"]}]
    cfg, _ = setup(tmp_path, graph, [("111", YESTERDAY, "PAUSED")], teams=teams)
    (cfg.state_dir / "team-a" / STATE_FILE).mkdir()  # unreadable as a file
    team_b = PausedList(cfg.state_dir / "team-b" / PAUSED_FILE)
    team_b.add(PausedCampaign("222", "B", "act_1000002", "pause_overspent", YESTERDAY))
    graph.statuses["222"] = "PAUSED"
    graph.owners["222"] = "act_1000002"
    result = run(cfg, graph, notifier)
    assert result.errors == 1 and graph.statuses["222"] == "ACTIVE"


def test_retries_are_reported_once_a_day_on_an_hourly_schedule(tmp_path, graph, notifier):
    cfg, _ = setup(tmp_path, graph, [("111", YESTERDAY, "PAUSED")])
    graph.stuck.add("111")
    for minutes in (0, 60, 120):
        run(cfg, graph, notifier, now=NORMAL_NOW + timedelta(minutes=minutes))
    assert len(graph.posts) == 3 and len(notifier.sent) == 1
    graph.stuck.clear()
    run(cfg, graph, notifier, now=NORMAL_NOW + timedelta(minutes=180))
    assert len(notifier.sent) == 2 and "turned on" in notifier.sent[1][0]


def test_hourly_retries_make_no_backups(tmp_path, graph, notifier):
    cfg, _ = setup(tmp_path, graph, [("111", YESTERDAY, "PAUSED")])
    graph.stuck.add("111")
    for minutes in (0, 60):
        run(cfg, graph, notifier, now=NORMAL_NOW + timedelta(minutes=minutes))
    assert not (cfg.state_dir / "team-a" / "backup").exists()


def test_missing_owner_from_meta_is_retried(tmp_path, graph, notifier, monkeypatch):
    cfg, paused = setup(tmp_path, graph, [("111", YESTERDAY, "PAUSED")])
    real_get = graph.get
    monkeypatch.setattr(graph, "get", lambda path, params=None: {
        k: v for k, v in real_get(path, params).items() if k != "account_id"})
    result = run(cfg, graph, notifier)
    assert result.retry == 1 and graph.posts == []
    assert "Meta did not say which ad account owns it" in notifier.sent[0][0]


def test_losing_the_lock_mid_run_changes_nothing(tmp_path, graph, notifier):
    from meta_ads_watchdog.reactivate import ReactivateResult, TeamReactivation
    cfg, paused = setup(tmp_path, graph, [("111", YESTERDAY, "PAUSED")])
    before = paused.path.read_text(encoding="utf-8")
    result = ReactivateResult()
    TeamReactivation(cfg, cfg.teams[0], graph, notifier, cfg.state_dir / "team-a",
                     NORMAL_NOW.date(), False, result, heartbeat=lambda: False).run()
    assert graph.posts == [] and result.errors == 1
    assert paused.path.read_text(encoding="utf-8") == before


def test_deeply_nested_line_is_just_an_unreadable_line(tmp_path, graph, notifier):
    cfg, _ = setup(tmp_path, graph, [], bad_lines=["[" * 100000 + "]" * 100000])
    result = run(cfg, graph, notifier)
    assert result.manual == 1 and graph.posts == []


def test_discord_rejections_count_as_errors(tmp_path, graph):
    from conftest import WEBHOOK
    from meta_ads_watchdog.discord import Notifier, Webhook

    class Refusing:
        def post(self, url, **kwargs):
            return type("R", (), {"status_code": 404})()

    cfg, _ = setup(tmp_path, graph, [("111", YESTERDAY, "PAUSED")])
    real = Notifier(Webhook(WEBHOOK, Refusing()), Webhook(None, None),
                    cfg.state_dir / "team-a" / "discord-queue.jsonl")
    result = run_reactivate(cfg, graph, lambda team: real, NORMAL_NOW, cfg.teams, False)
    assert graph.statuses["111"] == "ACTIVE" and result.errors == 1


def test_lost_lock_before_saving_leaves_the_list_alone(tmp_path, graph, notifier):
    from meta_ads_watchdog.reactivate import ReactivateResult, TeamReactivation
    cfg, paused = setup(tmp_path, graph, [("111", YESTERDAY, "ACTIVE")])  # no POST needed
    before = paused.path.read_text(encoding="utf-8")
    TeamReactivation(cfg, cfg.teams[0], graph, notifier, cfg.state_dir / "team-a",
                     NORMAL_NOW.date(), False, ReactivateResult(), heartbeat=lambda: False).run()
    assert paused.path.read_text(encoding="utf-8") == before


def test_queue_trouble_does_not_stop_reactivation(tmp_path, graph, notifier, monkeypatch):
    cfg, _ = setup(tmp_path, graph, [("111", YESTERDAY, "PAUSED")])

    def broken():
        raise TypeError("odd queue")
    monkeypatch.setattr(notifier, "flush_queue", broken)
    result = run(cfg, graph, notifier)
    assert graph.statuses["111"] == "ACTIVE" and result.errors == 1


def test_lost_lock_before_saving_writes_no_state_and_sends_nothing(tmp_path, graph, notifier):
    from meta_ads_watchdog.reactivate import ReactivateResult, TeamReactivation
    cfg, _ = setup(tmp_path, graph, [("111", YESTERDAY, "ACTIVE")])
    result = ReactivateResult()
    TeamReactivation(cfg, cfg.teams[0], graph, notifier, cfg.state_dir / "team-a",
                     NORMAL_NOW.date(), False, result, heartbeat=lambda: False).run()
    assert not (cfg.state_dir / "team-a" / STATE_FILE).exists()
    assert notifier.sent == [] and result.errors == 1
