"""A small Meta Graph API client: retries, throttling, paging, and no token leaks.

Error codes follow https://developers.facebook.com/docs/marketing-api/overview/rate-limiting
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Mapping

import requests

log = logging.getLogger(__name__)

GRAPH_HOST = "https://graph.facebook.com"
THROTTLE_CODES = frozenset({4, 17, 32, 613, 80000, 80003, 80004, 80014})
TRANSIENT_CODES = frozenset({1, 2})
TOKEN_CODES = frozenset({190})
MAX_ATTEMPTS = 4
BASE_DELAY_SECONDS = 2.0
MAX_DELAY_SECONDS = 60.0
TIMEOUT_SECONDS = 30
MAX_PAGES = 50


class GraphError(Exception):
    """A Graph API call failed for good (after any retries)."""

    def __init__(self, message: str, code: int | None = None, subcode: int | None = None,
                 status: int | None = None):
        super().__init__(message)
        self.code = code
        self.subcode = subcode
        self.status = status

    @property
    def is_token_problem(self) -> bool:
        return self.code in TOKEN_CODES


class GraphClient:
    def __init__(self, token: str, version: str, session: requests.Session | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self._token = token
        self._base = f"{GRAPH_HOST}/{version}"
        self._session = session or requests.Session()
        self._sleep = sleep

    def redact(self, text: str) -> str:
        return text.replace(self._token, "***") if self._token else text

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self._call("GET", self._base + path, _encode(params))

    def post(self, path: str, data: Mapping[str, Any]) -> dict[str, Any]:
        return self._call("POST", self._base + path, _encode(data))

    def get_all(self, path: str, params: Mapping[str, Any] | None = None,
                max_pages: int = MAX_PAGES) -> list[dict[str, Any]]:
        """Follows `paging.next` and returns every row of `data`."""
        rows: list[dict[str, Any]] = []
        body = self.get(path, params)
        for page in range(max_pages):
            rows.extend(item for item in body.get("data", []) if isinstance(item, dict))
            next_url = (body.get("paging") or {}).get("next")
            if not isinstance(next_url, str) or not next_url.startswith(GRAPH_HOST + "/"):
                return rows
            if page + 1 == max_pages:
                log.warning("stopped after %s pages of %s", max_pages, path)
                return rows
            body = self._call("GET", next_url, None)
        return rows

    def _call(self, method: str, url: str, fields: dict[str, str] | None) -> dict[str, Any]:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return self._once(method, url, fields)
            except _Retry as retry:
                if attempt == MAX_ATTEMPTS:
                    raise GraphError(f"gave up after {MAX_ATTEMPTS} tries: {retry}",
                                     retry.code, retry.subcode, retry.status) from None
                delay = min(BASE_DELAY_SECONDS * 2 ** (attempt - 1), MAX_DELAY_SECONDS)
                log.warning("Meta API busy or unreachable (%s), retrying in %.0fs", retry, delay)
                self._sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    def _once(self, method: str, url: str, fields: dict[str, str] | None) -> dict[str, Any]:
        auth = {"access_token": self._token}
        try:
            if method == "GET":
                # A `paging.next` URL (fields is None) already carries its own token.
                has_token = fields is None and "access_token=" in url
                params = None if has_token else {**(fields or {}), **auth}
                response = self._session.get(url, params=params, timeout=TIMEOUT_SECONDS,
                                             allow_redirects=False)
            else:
                response = self._session.post(url, data={**(fields or {}), **auth},
                                              timeout=TIMEOUT_SECONDS, allow_redirects=False)
        except requests.RequestException as error:
            raise _Retry(self.redact(f"{type(error).__name__}: {error}")) from None
        return self._parse(response)

    def _parse(self, response: requests.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError:
            body = None
        error = body.get("error") if isinstance(body, dict) else None
        if response.status_code < 400 and isinstance(body, dict) and not error:
            return body
        if not isinstance(error, dict):
            message = f"HTTP {response.status_code}"
            if response.status_code >= 500 or response.status_code == 429:
                raise _Retry(message, status=response.status_code)
            raise GraphError(message, status=response.status_code)
        code = _int(error.get("code"))
        subcode = _int(error.get("error_subcode"))
        # Redact before shortening, so a cut can never leave half a token behind.
        message = self.redact(f"[{code}] {error.get('message', 'unknown error')}")[:300]
        if (code in THROTTLE_CODES or code in TRANSIENT_CODES or error.get("is_transient") is True
                or response.status_code >= 500):
            raise _Retry(message, code, subcode, response.status_code)
        raise GraphError(message, code, subcode, response.status_code)


class _Retry(Exception):
    def __init__(self, message: str, code: int | None = None, subcode: int | None = None,
                 status: int | None = None):
        super().__init__(message)
        self.code = code
        self.subcode = subcode
        self.status = status


def _encode(values: Mapping[str, Any] | None) -> dict[str, str]:
    """Graph API wants lists and objects as JSON strings."""
    encoded: dict[str, str] = {}
    for key, value in (values or {}).items():
        if isinstance(value, (list, dict)):
            encoded[key] = json.dumps(value)
        elif isinstance(value, bool):
            encoded[key] = "true" if value else "false"
        else:
            encoded[key] = str(value)
    return encoded


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
