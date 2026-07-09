"""Tests for SoClient WRITE-path methods against the verified SO 2.4 contract.

Verified 2026-06-02 against the live box + the so-tune-detection-*-request.har
and so-disable-detection-workflow.har captures:

  GET  /api/detection/public/{publicId}  -> detection object (lookup by publicId)
  PUT  /api/detection                    -> body = full detection; returns updated
  POST /api/events/ack                   -> disposition (acknowledge/escalate)

All ride the same browser-flow cookies + X-Srv-Token header the read path uses.
"""

import json

import httpx
import pytest
import respx

from so_gateway.config import Config
from so_gateway.so_client import SoClient, SoWriteError

CFG = Config(url="https://so.test", email="a@test.local", password="pw", ssl_skip_verify=True)

LOGIN_BROWSER_URL = "https://so.test/auth/self-service/login/browser"
LOGIN_SUBMIT_URL = "https://so.test/auth/self-service/login"
INFO_URL = "https://so.test/api/info"
SRV_TOKEN = "SRVTOKEN252"


def _register_login_mocks(m):
    m.get(LOGIN_BROWSER_URL).mock(
        return_value=httpx.Response(
            200,
            json={"id": "flow1", "ui": {"nodes": [
                {"attributes": {"name": "csrf_token", "value": "C"}}]}},
            headers={"Set-Cookie": "csrf_token=C; Path=/"},
        )
    )
    m.post(LOGIN_SUBMIT_URL).mock(
        return_value=httpx.Response(
            200, json={"session": {"active": True}},
            headers={"Set-Cookie": "ory_kratos_session=S; Path=/"},
        )
    )
    m.get(INFO_URL).mock(return_value=httpx.Response(200, json={"srvToken": SRV_TOKEN}))


DETECTION = {
    "id": "Pj_lqJcBPiDhvlxwuZTf",
    "publicId": "2009205",
    "title": "ET MALWARE KEYPLUG",
    "isEnabled": True,
    "overrides": [],
}


# ---------------------------------------------------------------------------
# get_detection_by_public_id
# ---------------------------------------------------------------------------


@respx.mock
def test_get_detection_by_public_id():
    _register_login_mocks(respx.mock)
    route = respx.mock.get("https://so.test/api/detection/public/2009205").mock(
        return_value=httpx.Response(200, json=DETECTION)
    )
    client = SoClient(CFG)
    out = client.get_detection_by_public_id("2009205")
    assert out["publicId"] == "2009205"
    assert route.called
    assert route.calls.last.request.headers.get("x-srv-token") == SRV_TOKEN


@respx.mock
def test_get_detection_by_public_id_404():
    _register_login_mocks(respx.mock)
    respx.mock.get("https://so.test/api/detection/public/9999999").mock(
        return_value=httpx.Response(404)
    )
    client = SoClient(CFG)
    with pytest.raises(httpx.HTTPStatusError):
        client.get_detection_by_public_id("9999999")


# ---------------------------------------------------------------------------
# put_detection — the WRITE
# ---------------------------------------------------------------------------


@respx.mock
def test_put_detection_sends_full_object_as_json_body():
    _register_login_mocks(respx.mock)
    updated = dict(DETECTION)
    updated["overrides"] = [{"type": "suppress", "ip": "1.2.3.4/32"}]
    route = respx.mock.put("https://so.test/api/detection").mock(
        return_value=httpx.Response(200, json=updated)
    )

    client = SoClient(CFG)
    body_in = dict(DETECTION)
    body_in["overrides"] = [{"type": "suppress", "ip": "1.2.3.4/32"}]
    out = client.put_detection(body_in)

    assert out["overrides"][0]["ip"] == "1.2.3.4/32"
    assert route.called
    req = route.calls.last.request
    # JSON content-type + X-Srv-Token (the CSRF-protected write path)
    assert req.headers["content-type"].startswith("application/json")
    assert req.headers.get("x-srv-token") == SRV_TOKEN
    sent = json.loads(req.content)
    # The FULL detection object is sent (id + publicId + overrides), per the HAR.
    assert sent["id"] == "Pj_lqJcBPiDhvlxwuZTf"
    assert sent["publicId"] == "2009205"
    assert sent["overrides"] == [{"type": "suppress", "ip": "1.2.3.4/32"}]


@respx.mock
def test_put_detection_raises_on_403_permission_denied():
    """A least-privilege account without detection-write must surface the error."""
    _register_login_mocks(respx.mock)
    respx.mock.put("https://so.test/api/detection").mock(
        return_value=httpx.Response(403, text="forbidden")
    )
    client = SoClient(CFG)
    with pytest.raises(SoWriteError) as exc:
        client.put_detection(DETECTION)
    assert "403" in str(exc.value)


@respx.mock
def test_put_detection_reauths_and_retries_on_stale_400():
    """F5: a stale-session 400 forces one re-login + retry, then succeeds."""
    _register_login_mocks(respx.mock)
    calls = {"n": 0}

    def _put(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(400, text="stale X-Srv-Token")
        return httpx.Response(200, json=DETECTION)

    respx.mock.put("https://so.test/api/detection").mock(side_effect=_put)
    client = SoClient(CFG)
    out = client.put_detection(DETECTION)
    assert out["publicId"] == "2009205"
    assert calls["n"] == 2  # first 400, retried once after forced re-auth


@respx.mock
def test_put_detection_raises_sowriteerror_with_body_after_retry():
    """F5: if the write still fails after re-auth, raise loudly with the SO body."""
    _register_login_mocks(respx.mock)
    respx.mock.put("https://so.test/api/detection").mock(
        return_value=httpx.Response(400, text="still-bad-detection-body")
    )
    client = SoClient(CFG)
    with pytest.raises(SoWriteError) as exc:
        client.put_detection(DETECTION)
    assert "still-bad-detection-body" in str(exc.value)


# ---------------------------------------------------------------------------
# disposition_alerts — POST /api/events/ack (acknowledge / escalate / close)
# ---------------------------------------------------------------------------


@respx.mock
def test_disposition_alerts_acknowledge_body_shape():
    _register_login_mocks(respx.mock)
    route = respx.mock.post("https://so.test/api/events/ack").mock(
        return_value=httpx.Response(200, json={"updated": 3})
    )

    client = SoClient(CFG)
    out = client.disposition_alerts(
        rule_uuid="2009208",
        date_range="2026/01/03 03:02:00 AM - 2026/01/04 03:02:00 PM",
        acknowledge=True,
        escalate=False,
    )
    assert out == {"updated": 3}
    assert route.called
    req = route.calls.last.request
    assert req.headers.get("x-srv-token") == SRV_TOKEN
    body = json.loads(req.content)
    # Shape verified from so-disable-detection-workflow.har ack request.
    assert body["eventFilter"] == {"rule.uuid": "2009208"}
    assert body["acknowledge"] is True
    assert body["escalate"] is False
    assert body["dateRange"] == "2026/01/03 03:02:00 AM - 2026/01/04 03:02:00 PM"
    assert body["dateRangeFormat"] == "2006/01/02 3:04:05 PM"
    assert "searchFilter" in body
