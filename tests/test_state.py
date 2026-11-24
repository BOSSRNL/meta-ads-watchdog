import json
import os
import time
from datetime import date

import pytest

from meta_ads_watchdog.state import (LockedError, PausedCampaign, PausedList, TeamLock,
                                     TeamState, is_numeric_id)

TODAY = date(2026, 11, 18)


def test_daily_parts_reset_on_a_new_day_but_budgets_stay(tmp_path):
    path = tmp_path / "state.json"
    state = TeamState(path, date(2026, 11, 17))
    state.announced = True
    state.first_alert_today("111:x")
    state.account_issues["act_1"] = 3
    state.remember_budget("111", 500.0)
    state.save()
    later = TeamState(path, TODAY)
    assert not later.announced and later.alerted == set() and later.account_issues == {}
    assert later.previous_budget("111") == 500.0
    same_day = TeamState(path, date(2026, 11, 17))
    assert same_day.announced and not same_day.first_alert_today("111:x")


def test_old_memory_is_forgotten(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "budgets": {"1": {"budget": 5, "seen": "2026-10-01"}, "2": {"budget": 5, "seen": "2026-11-10"},
                    "3": {"budget": 5, "seen": "garbage"}, "4": "junk"},
        "security_paused": {"7": "2026-11-01", "8": "2026-11-15"}}), encoding="utf-8")
    state = TeamState(path, TODAY)
    assert state.previous_budget("4") is None
    state.save()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert list(data["budgets"]) == ["2"] and list(data["security_paused"]) == ["8"]


@pytest.mark.parametrize("content", ["{broken", "[1, 2]", "\udcff".encode("utf-8", "surrogatepass")])
def test_damaged_state_is_set_aside_not_deleted(tmp_path, content):
    path = tmp_path / "state.json"
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    state = TeamState(path, TODAY)
    assert state.budgets == {} and not path.exists()
    assert len(list(tmp_path.glob("state.json.damaged-*"))) == 1


def test_wrong_types_inside_state_are_ignored(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"day": TODAY.isoformat(), "announced": "yes", "alerted": 5,
                                "account_issues": [], "budgets": None}), encoding="utf-8")
    state = TeamState(path, TODAY)
    assert (state.announced, state.alerted, state.account_issues, state.budgets) == (
        False, set(), {}, {})


def test_paused_list_round_trip_dedupes_and_flags_bad_lines(tmp_path):
    paused = PausedList(tmp_path / "list.jsonl")
    assert paused.read() == ([], [])
    first = PausedCampaign("111", "A / b", "act_1", "pause_overspent", "2026-11-17")
    paused.add(first)
    paused.add(PausedCampaign("111", "A / b", "act_1", "pause_escalation", "2026-11-18"))
    with paused.path.open("a", encoding="utf-8") as handle:
        handle.write("\nnot json\n" + json.dumps({**first.__dict__, "campaign_id": "abc"}) + "\n"
                     + json.dumps({**first.__dict__, "paused_on": "yesterday"}) + "\n"
                     + json.dumps({"campaign_id": "1"}) + "\n")
    records, bad = paused.read()
    assert [r.reason for r in records] == ["pause_escalation"] and len(bad) == 4
    paused.replace(records, [])
    assert paused.read() == (records, [])
    paused.replace([], [])
    assert not paused.path.exists()


def test_lock_blocks_a_second_run_and_is_released(tmp_path):
    with TeamLock(tmp_path):
        with pytest.raises(LockedError):
            with TeamLock(tmp_path):
                pass
    assert not (tmp_path / "run.lock").exists()


def test_stale_lock_is_taken_over(tmp_path):
    lock = tmp_path / "run.lock"
    lock.write_text("123 old", encoding="utf-8")
    old = time.time() - 4 * 3600
    os.utime(lock, (old, old))
    with TeamLock(tmp_path):
        assert str(os.getpid()) in lock.read_text(encoding="utf-8")
    assert not lock.exists()


def test_lock_is_released_after_an_error(tmp_path):
    with pytest.raises(RuntimeError):
        with TeamLock(tmp_path):
            raise RuntimeError("boom")
    assert not (tmp_path / "run.lock").exists()


def test_lock_owner_and_heartbeat(tmp_path):
    lock_path = tmp_path / "run.lock"
    with TeamLock(tmp_path) as lock:
        old = time.time() - 30 * 60
        os.utime(lock_path, (old, old))
        lock.touch()  # a live run refreshes its lock
        assert time.time() - lock_path.stat().st_mtime < 60
        with pytest.raises(LockedError):
            with TeamLock(tmp_path):
                pass
    stranger = TeamLock(tmp_path)
    lock_path.write_text("someone-else 2026-01-01", encoding="utf-8")
    stranger.touch()
    stranger.__exit__(None, None, None)
    assert lock_path.exists()  # never touch or remove a lock you do not own


def test_lock_waits_for_the_other_run(tmp_path):
    lock_path = tmp_path / "run.lock"
    lock_path.write_text("other now", encoding="utf-8")
    waits = []

    def sleep(seconds):
        waits.append(seconds)
        if len(waits) == 2:
            lock_path.unlink()  # the other run finishes

    with TeamLock(tmp_path, wait_seconds=60, sleep=sleep):
        assert lock_path.exists()
    assert waits == [5.0, 5.0]


def test_paused_list_remove_and_reason_filter(tmp_path):
    paused = PausedList(tmp_path / "list.jsonl")
    keep = PausedCampaign("111", "a", "act_1", "pause_overspent", "2026-11-17")
    paused.add(keep)
    paused.add(PausedCampaign("222", "b", "act_1", "pause_escalation", "2026-11-17"))
    paused.add(PausedCampaign("333", "c", "act_1", "pause_spike", "2026-11-17"))
    records, bad = paused.read()
    assert [r.campaign_id for r in records] == ["111", "222"] and len(bad) == 1
    assert paused.remove("222") is True and paused.remove("999") is False
    records, bad = paused.read()
    assert records == [keep] and len(bad) == 1  # the unreadable line is kept, not dropped


@pytest.mark.parametrize("value, ok", [("123", True), ("0" * 30, True), ("", False),
                                       ("12a", False), (chr(0x661) * 3, False), ("1\n", False),
                                       (123, False), ("0" * 31, False)])
def test_is_numeric_id(value, ok):
    assert is_numeric_id(value) is ok
