import json

import pytest
import requests

from meta_ads_watchdog.graph import GraphClient, GraphError

TOKEN = "EAAsecret123456"


class Response:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def _next(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url, **kwargs):
        return self._next("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._next("POST", url, **kwargs)


def client(*responses):
    session = Session(*responses)
    sleeps = []
    return GraphClient(TOKEN, "v25.0", session, sleeps.append), session, sleeps


def test_get_encodes_lists_and_sends_the_token():
    graph, session, _ = client(Response(200, {"id": "1"}))
    assert graph.get("/act_1/campaigns", {"effective_status": ["ACTIVE"], "limit": 200,
                                          "flag": True}) == {"id": "1"}
    method, url, kwargs = session.calls[0]
    assert url == "https://graph.facebook.com/v25.0/act_1/campaigns"
    assert kwargs["params"] == {"effective_status": json.dumps(["ACTIVE"]), "limit": "200",
                                "flag": "true", "access_token": TOKEN}
    assert kwargs["timeout"] == 30


def test_post_sends_form_data():
    graph, session, _ = client(Response(200, {"success": True}))
    graph.post("/123", {"status": "PAUSED"})
    assert session.calls[0][2]["data"] == {"status": "PAUSED", "access_token": TOKEN}


def test_get_all_follows_paging_and_stops_at_foreign_hosts():
    next_url = f"https://graph.facebook.com/v25.0/act_1/campaigns?after=x&access_token={TOKEN}"
    graph, session, _ = client(
        Response(200, {"data": [{"id": "1"}], "paging": {"next": next_url}}),
        Response(200, {"data": [{"id": "2"}, "junk"], "paging": {"next": "https://evil.example/x"}}))
    assert graph.get_all("/act_1/campaigns") == [{"id": "1"}, {"id": "2"}]
    assert session.calls[1][1] == next_url and session.calls[1][2]["params"] is None


def test_get_all_page_cap():
    page = {"data": [{"id": "1"}], "paging": {"next": "https://graph.facebook.com/v25.0/n?a=1"}}
    graph, session, _ = client(*[Response(200, page) for _ in range(3)])
    assert len(graph.get_all("/x", max_pages=3)) == 3 and len(session.calls) == 3
    assert session.calls[1][2]["params"] == {"access_token": TOKEN}  # next URL without a token


def test_throttling_is_retried_with_backoff():
    throttled = Response(400, {"error": {"code": 80004, "message": "too many calls"}})
    graph, _, sleeps = client(throttled, throttled, Response(200, {"ok": 1}))
    assert graph.get("/x") == {"ok": 1}
    assert sleeps == [2.0, 4.0]


@pytest.mark.parametrize("failure", [
    Response(500, ValueError("not json")), Response(429, {}),
    Response(400, {"error": {"code": 2, "message": "temporary"}}),
    Response(400, {"error": {"code": 100, "message": "x", "is_transient": True}}),
    requests.ConnectionError("down"),
])
def test_transient_failures_are_retried(failure):
    graph, _, sleeps = client(failure, Response(200, {"ok": 1}))
    assert graph.get("/x") == {"ok": 1} and sleeps == [2.0]


def test_gives_up_after_four_tries():
    busy = Response(400, {"error": {"code": 17, "message": "User request limit reached"}})
    graph, session, sleeps = client(busy, busy, busy, busy)
    with pytest.raises(GraphError) as caught:
        graph.get("/x")
    assert caught.value.code == 17 and "gave up after 4 tries" in str(caught.value)
    assert len(session.calls) == 4 and sleeps == [2.0, 4.0, 8.0]


def test_permanent_errors_are_not_retried():
    graph, session, _ = client(Response(400, {"error": {"code": 190, "error_subcode": 463,
                                                        "message": "Session has expired"}}))
    with pytest.raises(GraphError) as caught:
        graph.get("/x")
    error = caught.value
    assert error.is_token_problem and error.subcode == 463 and error.status == 400
    assert len(session.calls) == 1


def test_plain_http_errors_and_redirects_are_errors():
    graph, session, _ = client(Response(403, ValueError("html")),
                               Response(302, ValueError("redirect body")))
    with pytest.raises(GraphError) as first:
        graph.get("/x")
    assert str(first.value) == "HTTP 403" and first.value.code is None
    with pytest.raises(GraphError) as second:
        graph.post("/x", {"status": "PAUSED"})
    assert str(second.value) == "HTTP 302"
    assert all(call[2]["allow_redirects"] is False for call in session.calls)


def test_long_error_is_redacted_before_it_is_shortened():
    message = "x" * 290 + TOKEN + "y" * 50
    graph, _, _ = client(Response(400, {"error": {"code": 100, "message": message}}))
    with pytest.raises(GraphError) as caught:
        graph.get("/x")
    assert "EAA" not in str(caught.value) and len(str(caught.value)) <= 300


def test_token_never_appears_in_errors():
    leak = requests.ConnectionError(f"Max retries exceeded with url: /x?access_token={TOKEN}")
    graph, _, _ = client(leak, leak, leak, leak)
    with pytest.raises(GraphError) as caught:
        graph.get("/x")
    assert TOKEN not in str(caught.value) and "***" in str(caught.value)
    graph2, _, _ = client(Response(400, {"error": {"code": 100, "message": f"bad {TOKEN}"}}))
    with pytest.raises(GraphError) as caught2:
        graph2.get("/x")
    assert TOKEN not in str(caught2.value)


def test_ok_status_with_error_body_is_an_error():
    graph, _, _ = client(Response(200, {"error": {"code": 100, "message": "nope"}}))
    with pytest.raises(GraphError):
        graph.get("/x")
