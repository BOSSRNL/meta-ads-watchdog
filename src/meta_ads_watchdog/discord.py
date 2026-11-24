"""Discord webhook alerts with an offline queue.

A message that cannot be sent (no internet, Discord down) is saved to a queue
file and sent at the start of the next run, so no alert is lost.
"""

from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Callable

import requests

from .state import set_aside, write_atomic

log = logging.getLogger(__name__)

MAX_CONTENT = 2000           # Discord's limit for one message
MAX_QUEUE = 200              # oldest queued messages are dropped past this
MAX_QUEUE_AGE = timedelta(days=7)
MAX_429_WAIT_SECONDS = 30.0
TIMEOUT_SECONDS = 15
NAME_LIMIT = 100
# Inline Markdown, plus < > so <https://...>, <@id> and <#id> do not render.
MARKDOWN = re.compile(r"([\\*_~`|\[\]<>])")
HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})  # controls, bidi overrides, line breaks
ZERO_WIDTH_SPACE = "\u200b"


def plain(text: object, limit: int = NAME_LIMIT) -> str:
    """Outside text (campaign names, API errors) as one short line without
    control or invisible formatting characters. Safe for logs and terminals."""
    flat = "".join(" " if unicodedata.category(char) in HIDDEN_CATEGORIES else char
                   for char in str(text))
    flat = " ".join(flat.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def clean(text: object, limit: int = NAME_LIMIT) -> str:
    """`plain()`, then made inert inside a Discord message: no Markdown, no
    clickable links, no mention or channel rendering."""
    escaped = MARKDOWN.sub(r"\\\1", plain(text, limit))
    escaped = escaped.replace("://", ":" + ZERO_WIDTH_SPACE + "//")
    return escaped.replace("@", "@" + ZERO_WIDTH_SPACE)


def fit(content: str) -> str:
    return content if len(content) <= MAX_CONTENT else content[: MAX_CONTENT - 1] + "…"


class Delivery(Enum):
    SENT = "sent"
    LATER = "later"        # network trouble or Discord busy: keep it for the next run
    REJECTED = "rejected"  # Discord refused it (bad webhook, bad message): retrying won't help


class Webhook:
    def __init__(self, url: str | None, session: requests.Session,
                 sleep: Callable[[float], None] = time.sleep):
        self.url = url
        self._session = session
        self._sleep = sleep

    def post(self, content: str) -> Delivery:
        if not self.url:
            return Delivery.SENT
        payload = {"content": fit(content), "allowed_mentions": {"parse": []}}
        for _ in range(2):
            try:
                response = self._session.post(self.url, json=payload, timeout=TIMEOUT_SECONDS,
                                              allow_redirects=False)
            except requests.RequestException as error:
                log.warning("Discord unreachable: %s", type(error).__name__)
                return Delivery.LATER
            if response.status_code == 429:
                self._sleep(_retry_after(response))
                continue
            if response.status_code < 300:
                return Delivery.SENT
            log.error("Discord refused the message: HTTP %s", response.status_code)
            return Delivery.LATER if response.status_code >= 500 else Delivery.REJECTED
        return Delivery.LATER


class Notifier:
    """Sends every alert to the main webhook and issue alerts to the issues webhook too."""

    def __init__(self, main: Webhook, issues: Webhook, queue_file: Path, prefix: str = ""):
        self._main = main
        self._issues = issues
        self._queue_file = queue_file
        self._prefix = prefix
        self.rejected = 0  # alerts Discord refused for good (deleted webhook...): a failed run

    def send(self, content: str, is_issue: bool = False) -> None:
        """Never raises: trouble with alerts must not stop a pause."""
        log.info("ALERT %s", content.splitlines()[0] if content else "")
        message = self._prefix + content
        targets = [self._main] + ([self._issues] if is_issue and self._issues.url else [])
        for webhook in targets:
            delivery = webhook.post(message)
            if delivery is Delivery.REJECTED:
                self.rejected += 1
            elif delivery is Delivery.LATER:
                try:
                    self._queue(webhook, message)
                except OSError as error:
                    log.error("could not queue a Discord message: %s", error)

    def flush_queue(self) -> None:
        """Sends queued messages in order and stops at the first one that still
        cannot go out, so an offline run does not wait on every message."""
        records = self._read_queue()
        if not records:
            return
        cutoff = datetime.now() - MAX_QUEUE_AGE
        pending: list[dict] = []
        sent = 0
        for index, record in enumerate(records):
            if _queued_at(record) < cutoff:
                log.warning("dropped a queued Discord message older than %s", MAX_QUEUE_AGE)
                continue
            webhook = self._issues if record.get("target") == "issues" else self._main
            late = f"*(late message from {record.get('queued_at', '?')})*\n"
            delivery = webhook.post(late + str(record.get("content", "")))
            if delivery is Delivery.LATER:
                pending = records[index:]
                break
            sent += delivery is Delivery.SENT
            self.rejected += delivery is Delivery.REJECTED
        self._write_queue(pending)
        if sent:
            log.info("sent %s queued Discord message(s)", sent)

    def _queue(self, webhook: Webhook, content: str) -> None:
        records = self._read_queue()
        records.append({"target": "issues" if webhook is self._issues else "main",
                        "content": content,
                        "queued_at": datetime.now().isoformat(timespec="seconds")})
        if len(records) > MAX_QUEUE:
            log.warning("Discord queue is full, dropping %s oldest message(s)",
                        len(records) - MAX_QUEUE)
            records = records[-MAX_QUEUE:]
        self._write_queue(records)
        log.warning("Discord message queued; it will be sent on the next run")

    def _read_queue(self) -> list[dict]:
        if not self._queue_file.exists():
            return []
        try:
            text = self._queue_file.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            set_aside(self._queue_file, str(error))
            return []
        records = []
        for line in text.splitlines():
            try:
                record = json.loads(line)
            except (ValueError, RecursionError):
                log.warning("skipped a damaged line in %s", self._queue_file.name)
                continue
            if isinstance(record, dict):
                records.append(record)
        return records

    def _write_queue(self, records: list[dict]) -> None:
        if not records:
            self._queue_file.unlink(missing_ok=True)
            return
        write_atomic(self._queue_file,
                     "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))


def _queued_at(record: dict) -> datetime:
    try:
        stamp = datetime.fromisoformat(str(record.get("queued_at")))
    except ValueError:
        return datetime.min
    # Hand-edited stamps may carry a time zone; compare everything as local time.
    return stamp.astimezone().replace(tzinfo=None) if stamp.tzinfo else stamp


def _retry_after(response: requests.Response) -> float:
    try:
        seconds = float(response.json().get("retry_after", 1))
    except (ValueError, AttributeError, TypeError):
        seconds = 1.0
    return max(0.0, min(seconds, MAX_429_WAIT_SECONDS))
