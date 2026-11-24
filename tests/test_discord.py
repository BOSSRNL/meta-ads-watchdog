import json
from datetime import datetime, timedelta

import requests

from meta_ads_watchdog.discord import MAX_CONTENT, MAX_QUEUE, Notifier, Webhook, clean, fit, plain

MAIN = "https://discord.com/api/webhooks/1/main"
ISSUES = "https://discord.com/api/webhooks/2/issues"
ZW = chr(0x200B)
NL = chr(10)


class Response:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.posts = []

    def post(self, url, **kwargs):
        assert kwargs["allow_redirects"] is False
        self.posts.append((url, kwargs["json"]))
        item = self.responses.pop(0) if self.responses else Response(204)
        if isinstance(item, Exception):
            raise item
        return item


def notifier(tmp_path, session, prefix=""):
    sleeps = []
    return Notifier(Webhook(MAIN, session, sleeps.append), Webhook(ISSUES, session, sleeps.append),
                    tmp_path / "queue.jsonl", prefix), sleeps


def queued(content, target="main", age=timedelta(hours=1)):
    stamp = (datetime.now() - age).isoformat(timespec="seconds")
    return json.dumps({"target": target, "content": content, "queued_at": stamp})


def queue_contents(tmp_path):
    return [json.loads(line)["content"]
            for line in (tmp_path / "queue.jsonl").read_text("utf-8").splitlines()]


def test_alert_goes_to_main_and_issue_alerts_to_both(tmp_path):
    session = Session()
    sender, _ = notifier(tmp_path, session, prefix="🧪 ")
    sender.send("normal")
    sender.send("problem", is_issue=True)
    assert [url for url, _ in session.posts] == [MAIN, MAIN, ISSUES]
    assert session.posts[0][1] == {"content": "🧪 normal", "allowed_mentions": {"parse": []}}


def test_failed_alert_is_queued_and_sent_next_run(tmp_path):
    session = Session(requests.ConnectionError("offline"), Response(500))
    sender, _ = notifier(tmp_path, session)
    sender.send("first", is_issue=True)  # main: offline, issues: 500 -> both queued
    queued_targets = [json.loads(line)["target"]
                      for line in (tmp_path / "queue.jsonl").read_text("utf-8").splitlines()]
    assert queued_targets == ["main", "issues"]
    sender.flush_queue()
    assert not (tmp_path / "queue.jsonl").exists()
    assert session.posts[-2][1]["content"].startswith("*(late message from ")
    assert session.posts[-1][0] == ISSUES


def test_queue_keeps_what_still_fails_and_skips_damaged_lines(tmp_path):
    (tmp_path / "queue.jsonl").write_text(NL.join([queued("a"), "broken", "[1]"]) + NL,
                                          encoding="utf-8")
    sender, _ = notifier(tmp_path, Session(Response(500)))
    sender.flush_queue()
    assert queue_contents(tmp_path) == ["a"]


def test_flush_stops_at_the_first_message_that_still_cannot_go(tmp_path):
    (tmp_path / "queue.jsonl").write_text(NL.join(queued(n) for n in "abcd") + NL,
                                          encoding="utf-8")
    session = Session(Response(204), Response(404), requests.ConnectionError("offline"))
    sender, _ = notifier(tmp_path, session)
    sender.flush_queue()
    assert len(session.posts) == 3  # a sent, b refused for good (dropped), c failed: stop
    assert queue_contents(tmp_path) == ["c", "d"]


def test_old_or_undated_queued_messages_expire(tmp_path):
    old = queued("old", age=timedelta(days=8))
    (tmp_path / "queue.jsonl").write_text(old + NL + json.dumps({"content": "no date"}) + NL,
                                          encoding="utf-8")
    session = Session()
    sender, _ = notifier(tmp_path, session)
    sender.flush_queue()
    assert session.posts == [] and not (tmp_path / "queue.jsonl").exists()


def test_refused_message_is_not_queued(tmp_path):
    sender, _ = notifier(tmp_path, Session(Response(400)))
    sender.send("bad")
    assert not (tmp_path / "queue.jsonl").exists()


def test_queue_is_capped(tmp_path):
    session = Session(*[Response(500)] * (MAX_QUEUE + 5))
    sender, _ = notifier(tmp_path, session)
    for i in range(MAX_QUEUE + 5):
        sender.send(f"m{i}")
    contents = queue_contents(tmp_path)
    assert len(contents) == MAX_QUEUE and contents[0] == "m5"


def test_rate_limited_message_waits_and_retries_once(tmp_path):
    session = Session(Response(429, {"retry_after": 99}), Response(204))
    sender, sleeps = notifier(tmp_path, session)
    sender.send("x")
    assert sleeps == [30.0] and len(session.posts) == 2
    assert not (tmp_path / "queue.jsonl").exists()


def test_rate_limited_twice_is_queued(tmp_path):
    sender, sleeps = notifier(tmp_path, Session(Response(429), Response(429)))
    sender.send("x")
    assert sleeps == [1.0, 1.0] and (tmp_path / "queue.jsonl").exists()


def test_no_webhook_means_log_only(tmp_path):
    session = Session()
    sender = Notifier(Webhook(None, session), Webhook(None, session), tmp_path / "q.jsonl")
    sender.send("x", is_issue=True)
    sender.flush_queue()
    assert session.posts == []


def test_clean_blocks_markdown_links_mentions_and_hidden_characters():
    assert clean("a" + chr(13) + NL + "b" + chr(9) + "c") == "a b c"
    assert clean("[link](https://x) *b* `c` ~d~ |e| _f_ " + "\\") == (
        "\\[link\\](https:" + ZW + "//x) \\*b\\* \\`c\\` \\~d\\~ \\|e\\| \\_f\\_ \\\\")
    assert clean("<@123> <#456> @everyone") == (
        "\\<@" + ZW + "123\\> \\<#456\\> @" + ZW + "everyone")
    hidden = "a" + chr(0x202E) + "b" + chr(0x2028) + "c" + chr(0x85) + "d" + chr(0xFEFF) + "e"
    assert clean(hidden) == "a b c d e"


def test_plain_and_fit():
    assert plain("x" * 200, 10) == "x" * 9 + "…" and plain(" a  b ") == "a b"
    assert plain("bad" + chr(27) + "[31m") == "bad [31m"  # no terminal escape codes
    assert fit("y" * 5000) == "y" * (MAX_CONTENT - 1) + "…" and fit("ok") == "ok"


def test_queue_stamp_with_a_time_zone_is_understood(tmp_path):
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")  # "...+08:00"
    (tmp_path / "queue.jsonl").write_text(
        json.dumps({"target": "main", "content": "z", "queued_at": stamp}) + NL, encoding="utf-8")
    session = Session()
    sender, _ = notifier(tmp_path, session)
    sender.flush_queue()
    assert len(session.posts) == 1
