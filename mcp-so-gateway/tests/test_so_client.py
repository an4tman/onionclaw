"""Tests for SoClient against the verified Security Onion 2.4 API contract.

Verified 2026-06-01 against live box + so-agent HARs
(so-guided-analysis-fresh-2026-01-05.har and siblings).
"""

import json

import httpx
import pytest
import respx

from so_gateway.config import Config
from so_gateway.so_client import (
    SoClient,
    _project_event,
    _substitute,
    _unresolved_placeholders,
)

CFG = Config(
    url="https://so.test",
    email="agent@test.local",
    password="pw",
    ssl_skip_verify=True,
)

# Shared mock data — Ory Kratos BROWSER flow + X-Srv-Token
# (verified 2026-06-01: the API/Bearer flow only works for GETs; POSTs need
# the browser-flow cookies plus an X-Srv-Token header).
LOGIN_BROWSER_URL = "https://so.test/auth/self-service/login/browser"
LOGIN_SUBMIT_URL = "https://so.test/auth/self-service/login"
INFO_URL = "https://so.test/api/info"
FLOW_ID = "flow123"
CSRF_VAL = "CSRFVAL"
SESSION_COOKIE = "sess123"
SRV_TOKEN = "eyJTEST"

# A minimal realistic playbook list (one playbook, two questions) matching the
# HAR-confirmed shape: list of objects, each with a "questions" list.
PLAYBOOK_LIST = [
    {
        "name": "Sigma - Category - file_event",
        "id": "1600005",
        "description": "Baseline Playbook for file events.",
        "questions": [
            {
                "question": "What file creation event triggered this alert?",
                "context": "Review the filename and location.\n",
                "range": None,
                "answer_sources": ["alert"],
                "query": (
                    "aggregation: false\n"
                    "logsource:\n"
                    "  category: alert\n"
                    "detection:\n"
                    "  selection:\n"
                    "    document_id: '{soc_id}'\n"
                    "  condition: selection\n"
                    "fields:\n"
                    "  - hostname\n"
                    "  - User\n"
                    "  - file.path\n"
                ),
            },
            {
                "question": "What process created this file?",
                "context": "Pivoting off the ProcessGuid shows the process chain.\n",
                "range": "+/-1h",
                "answer_sources": ["process_creation"],
                "query": (
                    "aggregation: false\n"
                    "logsource:\n"
                    "  category: process_creation\n"
                    "detection:\n"
                    "  selection:\n"
                    "    ProcessGuid: '{event_data.process.entity_id}'\n"
                    "  condition: selection\n"
                    "fields:\n"
                    "  - hostname\n"
                    "  - User\n"
                    "  - Image\n"
                    "  - CommandLine\n"
                ),
            },
        ],
    }
]

ALERT_FIELDS = {
    "soc_id": "Kl7yjpsBEEV8H2qfvz4w",
    "event_data.process.entity_id": "BaeaYNZvesMW0Yk6WK1QEw",
}

SUBSTITUTED_YAMLS = [
    (
        "aggregation: false\n"
        "logsource:\n"
        "  category: alert\n"
        "detection:\n"
        "  selection:\n"
        "    document_id: 'Kl7yjpsBEEV8H2qfvz4w'\n"
        "  condition: selection\n"
        "fields:\n"
        "  - hostname\n"
        "  - User\n"
        "  - file.path\n"
    ),
    (
        "aggregation: false\n"
        "logsource:\n"
        "  category: process_creation\n"
        "detection:\n"
        "  selection:\n"
        "    ProcessGuid: 'BaeaYNZvesMW0Yk6WK1QEw'\n"
        "  condition: selection\n"
        "fields:\n"
        "  - hostname\n"
        "  - User\n"
        "  - Image\n"
        "  - CommandLine\n"
    ),
]

CONVERT_RESPONSE = [
    {
        "query": "tags:alert AND _id:Kl7yjpsBEEV8H2qfvz4w | table @timestamp host.name user.name file.path",
        "fields": ["host.name", "user.name", "file.path"],
    },
    {
        "query": (
            "(event.category:process AND event.type:start) AND "
            "process.entity_id:BaeaYNZvesMW0Yk6WK1QEw | table @timestamp "
            "host.name user.name process.executable process.command_line"
        ),
        "fields": ["host.name", "user.name", "process.executable", "process.command_line"],
    },
]

TEST_RANGE = "2026/01/05 10:14:20 AM - 2026/01/05 12:14:20 PM"


def _register_login_mocks(respx_mock):
    """Register the Ory Kratos browser-flow login routes + /api/info.

    Returns the browser-flow GET route (handy for call-count assertions).

    1. GET /auth/self-service/login/browser → flow id + csrf_token node;
       sets the csrf_token cookie.
    2. POST /auth/self-service/login?flow=<id> → sets ory_kratos_session cookie.
    3. GET /api/info → srvToken (JWT).
    """
    flow_route = respx_mock.get(LOGIN_BROWSER_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": FLOW_ID,
                "ui": {
                    "nodes": [
                        {"attributes": {"name": "csrf_token", "value": CSRF_VAL}},
                        {"attributes": {"name": "identifier"}},
                    ]
                },
            },
            headers={"Set-Cookie": f"csrf_token={CSRF_VAL}; Path=/; HttpOnly"},
        )
    )
    submit_route = respx_mock.post(LOGIN_SUBMIT_URL).mock(
        return_value=httpx.Response(
            200,
            json={"session": {"active": True}},
            headers={"Set-Cookie": f"ory_kratos_session={SESSION_COOKIE}; Path=/; HttpOnly"},
        )
    )
    respx_mock.get(INFO_URL).mock(
        return_value=httpx.Response(200, json={"srvToken": SRV_TOKEN})
    )
    # Stash the submit route on the flow route so tests can reach it without
    # re-registering (which would shadow the Set-Cookie response).
    flow_route._submit_route = submit_route
    return flow_route


# ---------------------------------------------------------------------------
# login — browser flow + X-Srv-Token (verified 2026-06-01)
# ---------------------------------------------------------------------------


@respx.mock
def test_login_sets_srv_token_header():
    _register_login_mocks(respx.mock)

    client = SoClient(CFG)
    client.login()

    # X-Srv-Token must be set on the client for all subsequent requests
    assert client._client.headers["X-Srv-Token"] == SRV_TOKEN
    assert client._authenticated is True


@respx.mock
def test_login_submits_form_encoded_credentials_with_csrf():
    flow_route = _register_login_mocks(respx.mock)

    client = SoClient(CFG)
    client.login()

    # The credential POST must be form-encoded and carry the csrf_token + flow param
    req = flow_route._submit_route.calls.last.request
    assert req.url.params["flow"] == FLOW_ID
    assert req.headers["content-type"].startswith("application/x-www-form-urlencoded")
    body = req.content.decode()
    assert "identifier=agent%40test.local" in body
    assert f"csrf_token={CSRF_VAL}" in body
    assert "method=password" in body


@respx.mock
def test_login_raises_without_session_cookie():
    """If the credential POST does not set ory_kratos_session, login raises."""
    respx.mock.get(LOGIN_BROWSER_URL).mock(
        return_value=httpx.Response(
            200,
            json={"id": FLOW_ID, "ui": {"nodes": [
                {"attributes": {"name": "csrf_token", "value": CSRF_VAL}}]}},
        )
    )
    # POST returns 200 but NO session cookie
    respx.mock.post(LOGIN_SUBMIT_URL).mock(
        return_value=httpx.Response(200, json={})
    )

    client = SoClient(CFG)
    with pytest.raises(RuntimeError, match="ory_kratos_session"):
        client.login()


# ---------------------------------------------------------------------------
# get_detection
# ---------------------------------------------------------------------------


@respx.mock
def test_get_detection_logs_in_and_returns():
    _register_login_mocks(respx.mock)
    detection_route = respx.mock.get("https://so.test/api/detection/abc-123").mock(
        return_value=httpx.Response(
            200,
            json={"publicId": "2009207", "id": "abc-123", "title": "X"},
        )
    )

    client = SoClient(CFG)
    result = client.get_detection("abc-123")

    assert result == {"publicId": "2009207", "id": "abc-123", "title": "X"}

    assert detection_route.called
    # Requests ride the browser-flow cookies + the X-Srv-Token header (no Bearer)
    sent = detection_route.calls.last.request.headers.get("x-srv-token")
    assert sent == SRV_TOKEN


@respx.mock
def test_get_detection_raises_when_both_lookups_404():
    """A genuine miss -- 404 on BOTH the ES-_id and the publicId endpoints -- raises."""
    _register_login_mocks(respx.mock)
    respx.mock.get("https://so.test/api/detection/bad-id").mock(
        return_value=httpx.Response(404)
    )
    respx.mock.get("https://so.test/api/detection/public/bad-id").mock(
        return_value=httpx.Response(404)
    )

    client = SoClient(CFG)
    with pytest.raises(httpx.HTTPStatusError):
        client.get_detection("bad-id")


@respx.mock
def test_get_detection_falls_back_to_public_id_on_404():
    """A publicId (sid / Sigma rule.uuid) 404s the ES-_id endpoint, so the client
    transparently retries GET /api/detection/public/{id} and returns that object.

    Reproduces the live 2026-06-08 trap: an agent has only the publicId, the
    ES-_id endpoint returns a bare 404/empty, and the fallback resolves it.
    """
    _register_login_mocks(respx.mock)
    public_id = "71158e3f-df67-472b-930e-7d287acaa3e1"
    by_id_route = respx.mock.get(
        f"https://so.test/api/detection/{public_id}"
    ).mock(return_value=httpx.Response(404))
    by_public_route = respx.mock.get(
        f"https://so.test/api/detection/public/{public_id}"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"id": "oD_kqJcBPiDhvlxwpwvM", "publicId": public_id, "title": "X"},
        )
    )

    client = SoClient(CFG)
    result = client.get_detection(public_id)

    assert result == {
        "id": "oD_kqJcBPiDhvlxwpwvM",
        "publicId": public_id,
        "title": "X",
    }
    assert by_id_route.called
    assert by_public_route.called


@respx.mock
def test_login_called_once():
    login_flow_route = _register_login_mocks(respx.mock)
    respx.mock.get("https://so.test/api/detection/abc-123").mock(
        return_value=httpx.Response(200, json={"id": "abc-123"})
    )

    client = SoClient(CFG)
    client.get_detection("abc-123")
    client.get_detection("abc-123")

    # Login flow GET should only have been called once despite two get_detection calls
    assert login_flow_route.call_count == 1


# ---------------------------------------------------------------------------
# get_playbook — verified 2026-06-01: returns a JSON LIST, not a dict.
# Caller passes the publicId (rule.uuid), NOT the ES _id.
# ---------------------------------------------------------------------------


@respx.mock
def test_get_playbook_logs_in_and_returns_list():
    _register_login_mocks(respx.mock)
    playbook_route = respx.mock.get(
        "https://so.test/api/playbook/detection/02773bed-83bf-469f-b7ff-e676e7d78bab"
    ).mock(
        return_value=httpx.Response(200, json=PLAYBOOK_LIST)
    )

    client = SoClient(CFG)
    result = client.get_playbook("02773bed-83bf-469f-b7ff-e676e7d78bab")

    # Return value is the list, not a dict
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["name"] == "Sigma - Category - file_event"
    assert len(result[0]["questions"]) == 2

    assert playbook_route.called
    sent = playbook_route.calls.last.request.headers.get("x-srv-token")
    assert sent == SRV_TOKEN


@respx.mock
def test_get_playbook_raises_on_error():
    _register_login_mocks(respx.mock)
    respx.mock.get(
        "https://so.test/api/playbook/detection/bad-uuid"
    ).mock(return_value=httpx.Response(404))

    client = SoClient(CFG)
    with pytest.raises(httpx.HTTPStatusError):
        client.get_playbook("bad-uuid")


# ---------------------------------------------------------------------------
# convert_queries — verified 2026-06-01:
# POST body is a JSON ARRAY of Sigma-YAML strings.
# Response is a JSON ARRAY (parallel) of {query, fields} objects.
# ---------------------------------------------------------------------------


@respx.mock
def test_convert_queries_posts_array_and_returns_array():
    _register_login_mocks(respx.mock)
    convert_route = respx.mock.post("https://so.test/api/playbook/convert").mock(
        return_value=httpx.Response(200, json=CONVERT_RESPONSE)
    )

    client = SoClient(CFG)
    result = client.convert_queries(SUBSTITUTED_YAMLS)

    # Returns the parallel list of {query, fields} objects
    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0]["query"] == "tags:alert AND _id:Kl7yjpsBEEV8H2qfvz4w | table @timestamp host.name user.name file.path"
    assert result[0]["fields"] == ["host.name", "user.name", "file.path"]

    # Request body must be a JSON array of strings
    assert convert_route.called
    body = json.loads(convert_route.calls.last.request.content)
    assert isinstance(body, list)
    assert body == SUBSTITUTED_YAMLS


@respx.mock
def test_convert_queries_raises_on_error():
    _register_login_mocks(respx.mock)
    respx.mock.post("https://so.test/api/playbook/convert").mock(
        return_value=httpx.Response(500)
    )

    client = SoClient(CFG)
    with pytest.raises(httpx.HTTPStatusError):
        client.convert_queries(["some sigma yaml"])


# ---------------------------------------------------------------------------
# get_events — verified 2026-06-01:
# GET /api/events/ with query, range, format, zone, metricLimit, eventLimit.
# ---------------------------------------------------------------------------


@respx.mock
def test_get_events_sends_all_required_params():
    _register_login_mocks(respx.mock)
    events_route = respx.mock.get("https://so.test/api/events/").mock(
        return_value=httpx.Response(
            200, json={"events": [{"_id": "1", "host.name": "tars"}]}
        )
    )

    client = SoClient(CFG)
    result = client.get_events(
        "tags:alert AND _id:Kl7yjpsBEEV8H2qfvz4w | table @timestamp host.name",
        range=TEST_RANGE,
    )

    assert result == {"events": [{"_id": "1", "host.name": "tars"}]}
    assert events_route.called

    params = events_route.calls.last.request.url.params
    assert params["query"] == "tags:alert AND _id:Kl7yjpsBEEV8H2qfvz4w | table @timestamp host.name"
    assert params["range"] == TEST_RANGE
    assert params["format"] == "2006/01/02 3:04:05 PM"
    assert params["zone"] == "America/New_York"
    # Default limit lowered from 500 to a small configurable value (fix-queue H-1).
    assert params["metricLimit"] == "50"
    assert params["eventLimit"] == "50"


@respx.mock
def test_get_events_custom_zone_and_limit():
    _register_login_mocks(respx.mock)
    events_route = respx.mock.get("https://so.test/api/events/").mock(
        return_value=httpx.Response(200, json={"events": []})
    )

    client = SoClient(CFG)
    client.get_events("q", range=TEST_RANGE, zone="UTC", limit=5)

    params = events_route.calls.last.request.url.params
    assert params["zone"] == "UTC"
    assert params["metricLimit"] == "5"
    assert params["eventLimit"] == "5"


@respx.mock
def test_get_events_raises_on_error():
    _register_login_mocks(respx.mock)
    respx.mock.get("https://so.test/api/events/").mock(
        return_value=httpx.Response(403)
    )

    client = SoClient(CFG)
    with pytest.raises(httpx.HTTPStatusError):
        client.get_events("q", range=TEST_RANGE)


# ---------------------------------------------------------------------------
# run_guided_analysis — verified 2026-06-01:
# - Accepts public_id (rule.uuid) and alert_fields (must include soc_id).
# - Fetches playbook list, collects all questions across all playbooks.
# - Performs ONE batched convert_queries call (all substituted YAMLs).
# - Calls get_events per question, using the provided or default range.
# - Returns {question, context, query, fields, events} per question.
# - {soc_id} placeholder in Sigma YAML is substituted before the convert call.
# ---------------------------------------------------------------------------


@respx.mock
def test_run_guided_analysis_soc_id_substituted_in_convert_body():
    """soc_id must be substituted into the Sigma YAML before the convert call."""
    _register_login_mocks(respx.mock)

    respx.mock.get(
        "https://so.test/api/playbook/detection/02773bed-83bf-469f-b7ff-e676e7d78bab"
    ).mock(return_value=httpx.Response(200, json=PLAYBOOK_LIST))

    convert_route = respx.mock.post("https://so.test/api/playbook/convert").mock(
        return_value=httpx.Response(200, json=CONVERT_RESPONSE)
    )

    respx.mock.get("https://so.test/api/events/").mock(
        return_value=httpx.Response(200, json={"events": []})
    )

    client = SoClient(CFG)
    client.run_guided_analysis(
        "02773bed-83bf-469f-b7ff-e676e7d78bab",
        ALERT_FIELDS,
        range=TEST_RANGE,
    )

    # The convert request body must be the substituted YAMLs (not the raw templates)
    body = json.loads(convert_route.calls.last.request.content)
    assert isinstance(body, list)
    assert len(body) == 2
    # First question: {soc_id} → "Kl7yjpsBEEV8H2qfvz4w"
    assert "Kl7yjpsBEEV8H2qfvz4w" in body[0]
    assert "{soc_id}" not in body[0]
    # Second question: {event_data.process.entity_id} → "BaeaYNZvesMW0Yk6WK1QEw"
    assert "BaeaYNZvesMW0Yk6WK1QEw" in body[1]
    assert "{event_data.process.entity_id}" not in body[1]


@respx.mock
def test_run_guided_analysis_one_batched_convert_call():
    """convert_queries must be called exactly ONCE for all questions (batched)."""
    _register_login_mocks(respx.mock)

    respx.mock.get(
        "https://so.test/api/playbook/detection/02773bed-83bf-469f-b7ff-e676e7d78bab"
    ).mock(return_value=httpx.Response(200, json=PLAYBOOK_LIST))

    convert_route = respx.mock.post("https://so.test/api/playbook/convert").mock(
        return_value=httpx.Response(200, json=CONVERT_RESPONSE)
    )

    respx.mock.get("https://so.test/api/events/").mock(
        return_value=httpx.Response(200, json={"events": []})
    )

    client = SoClient(CFG)
    client.run_guided_analysis(
        "02773bed-83bf-469f-b7ff-e676e7d78bab",
        ALERT_FIELDS,
        range=TEST_RANGE,
    )

    assert convert_route.call_count == 1


@respx.mock
def test_run_guided_analysis_returns_correct_shape():
    """Top-level summary dict with one results item per resolved question."""
    _register_login_mocks(respx.mock)

    respx.mock.get(
        "https://so.test/api/playbook/detection/02773bed-83bf-469f-b7ff-e676e7d78bab"
    ).mock(return_value=httpx.Response(200, json=PLAYBOOK_LIST))

    respx.mock.post("https://so.test/api/playbook/convert").mock(
        return_value=httpx.Response(200, json=CONVERT_RESPONSE)
    )

    events_data = [{"_id": "evt1"}, {"_id": "evt2"}]
    respx.mock.get("https://so.test/api/events/").mock(
        return_value=httpx.Response(200, json={"events": events_data})
    )

    client = SoClient(CFG)
    out = client.run_guided_analysis(
        "02773bed-83bf-469f-b7ff-e676e7d78bab",
        ALERT_FIELDS,
        range=TEST_RANGE,
    )

    # New shape: top-level summary dict (fix-queue M-3)
    assert isinstance(out, dict)
    assert out["resolved"] == 2
    assert out["skipped"] == 0
    assert out["missing_fields"] == []
    assert out["skipped_questions"] == []

    results = out["results"]
    assert len(results) == 2

    item0 = results[0]
    assert item0["question"] == "What file creation event triggered this alert?"
    assert item0["context"] == "Review the filename and location.\n"
    assert item0["query"] == CONVERT_RESPONSE[0]["query"]
    assert item0["fields"] == CONVERT_RESPONSE[0]["fields"]
    assert item0["total"] == 2
    # Events are projected to identifiers + fields; _id survives as an identifier.
    assert {e["_id"] for e in item0["events"]} == {"evt1", "evt2"}

    item1 = results[1]
    assert item1["question"] == "What process created this file?"
    assert item1["query"] == CONVERT_RESPONSE[1]["query"]


@respx.mock
def test_run_guided_analysis_uses_provided_range_in_events_call():
    """The events call must use the range passed to run_guided_analysis."""
    _register_login_mocks(respx.mock)

    respx.mock.get(
        "https://so.test/api/playbook/detection/test-uuid"
    ).mock(return_value=httpx.Response(200, json=[
        {"questions": [{"question": "Q", "context": None, "range": None,
                        "query": "aggregation: false\nlogsource:\n  category: alert\ndetection:\n  selection:\n    document_id: '{soc_id}'\n  condition: selection\nfields:\n  - hostname\n"}]}
    ]))

    respx.mock.post("https://so.test/api/playbook/convert").mock(
        return_value=httpx.Response(200, json=[{"query": "converted_q", "fields": ["hostname"]}])
    )

    events_route = respx.mock.get("https://so.test/api/events/").mock(
        return_value=httpx.Response(200, json={"events": []})
    )

    client = SoClient(CFG)
    custom_range = "2026/01/05 09:00:00 AM - 2026/01/05 05:00:00 PM"
    client.run_guided_analysis("test-uuid", {"soc_id": "abc"}, range=custom_range)

    assert events_route.calls.last.request.url.params["range"] == custom_range


@respx.mock
def test_run_guided_analysis_default_range_when_none():
    """When range=None a non-empty default range is computed and passed to events."""
    _register_login_mocks(respx.mock)

    respx.mock.get(
        "https://so.test/api/playbook/detection/test-uuid"
    ).mock(return_value=httpx.Response(200, json=[
        {"questions": [{"question": "Q", "context": None, "range": None,
                        "query": "aggregation: false\nlogsource:\n  category: alert\ndetection:\n  selection:\n    document_id: '{soc_id}'\n  condition: selection\nfields:\n  - hostname\n"}]}
    ]))

    respx.mock.post("https://so.test/api/playbook/convert").mock(
        return_value=httpx.Response(200, json=[{"query": "q", "fields": []}])
    )

    events_route = respx.mock.get("https://so.test/api/events/").mock(
        return_value=httpx.Response(200, json={"events": []})
    )

    client = SoClient(CFG)
    client.run_guided_analysis("test-uuid", {"soc_id": "abc"})

    sent_range = events_route.calls.last.request.url.params.get("range", "")
    # Default range must be non-empty and contain the SO separator " - "
    assert " - " in sent_range
    assert len(sent_range) > 10


@respx.mock
def test_run_guided_analysis_empty_playbook_list():
    """Playbook endpoint returning an empty list yields an empty result."""
    _register_login_mocks(respx.mock)
    respx.mock.get("https://so.test/api/playbook/detection/empty").mock(
        return_value=httpx.Response(200, json=[])
    )

    client = SoClient(CFG)
    out = client.run_guided_analysis("empty", {"soc_id": "x"})
    assert out["resolved"] == 0
    assert out["results"] == []
    assert out["skipped"] == 0


@respx.mock
def test_run_guided_analysis_playbook_with_no_questions():
    """A playbook with an empty questions list yields an empty result."""
    _register_login_mocks(respx.mock)
    respx.mock.get("https://so.test/api/playbook/detection/empty-q").mock(
        return_value=httpx.Response(200, json=[{"name": "pb", "questions": []}])
    )

    client = SoClient(CFG)
    out = client.run_guided_analysis("empty-q", {"soc_id": "x"})
    assert out["resolved"] == 0
    assert out["results"] == []


@respx.mock
def test_run_guided_analysis_skips_questions_with_unresolved_placeholder():
    """Questions whose query still has an unsubstituted {placeholder} after
    substitution are SKIPPED (not sent to convert) — otherwise a partial
    alert_fields would 400 the WHOLE convert batch.

    Two-question playbook: one fully resolved, one with an unprovided
    placeholder → only the resolved one is converted and returned.
    """
    _register_login_mocks(respx.mock)
    respx.mock.get("https://so.test/api/playbook/detection/abc-uuid").mock(
        return_value=httpx.Response(
            200,
            json=[{
                "questions": [
                    {
                        "question": "Resolved question",
                        "context": None,
                        "range": None,
                        "query": "aggregation: false\ndetection:\n  selection:\n    document_id: '{soc_id}'\n",
                    },
                    {
                        "question": "Unresolved question",
                        "context": None,
                        "range": None,
                        "query": "aggregation: false\ndetection:\n  selection:\n    dst.ip: '{destination.ip}'\n",
                    },
                ]
            }],
        )
    )

    convert_route = respx.mock.post("https://so.test/api/playbook/convert").mock(
        return_value=httpx.Response(200, json=[{"query": "resolved_q", "fields": []}])
    )
    respx.mock.get("https://so.test/api/events/").mock(
        return_value=httpx.Response(200, json={"events": []})
    )

    client = SoClient(CFG)
    out = client.run_guided_analysis(
        "abc-uuid", {"soc_id": "1.2.3.4"}, range=TEST_RANGE
    )

    # Only the resolved question reaches convert (batch of 1, no placeholder)
    body = json.loads(convert_route.calls.last.request.content)
    assert len(body) == 1
    assert "{destination.ip}" not in body[0]
    assert "1.2.3.4" in body[0]

    # Only one result returned, for the resolved question
    assert out["resolved"] == 1
    assert out["results"][0]["question"] == "Resolved question"

    # The skipped question surfaces with its unresolved placeholder (fix-queue M-3)
    assert out["skipped"] == 1
    assert out["missing_fields"] == ["destination.ip"]
    sq = out["skipped_questions"]
    assert len(sq) == 1
    assert sq[0]["question"] == "Unresolved question"
    assert sq[0]["unresolved_placeholders"] == ["destination.ip"]


@respx.mock
def test_run_guided_analysis_all_unresolved_returns_empty():
    """If every question has an unresolved placeholder, no convert call is made."""
    _register_login_mocks(respx.mock)
    respx.mock.get("https://so.test/api/playbook/detection/abc-uuid").mock(
        return_value=httpx.Response(
            200,
            json=[{
                "questions": [{
                    "question": "Unresolved",
                    "context": None,
                    "range": None,
                    "query": "aggregation: false\ndetection:\n  selection:\n    dst.ip: '{destination.ip}'\n",
                }]
            }],
        )
    )
    convert_route = respx.mock.post("https://so.test/api/playbook/convert").mock(
        return_value=httpx.Response(200, json=[])
    )

    client = SoClient(CFG)
    out = client.run_guided_analysis("abc-uuid", {"soc_id": "x"}, range=TEST_RANGE)

    assert out["resolved"] == 0
    assert out["results"] == []
    assert not convert_route.called
    # Every question skipped -> feedback still surfaces the missing field (M-3)
    assert out["skipped"] == 1
    assert out["missing_fields"] == ["destination.ip"]
    assert out["skipped_questions"][0]["unresolved_placeholders"] == ["destination.ip"]


def test_substitute_no_cascading_substitution():
    """A field value containing a {placeholder} must NOT be re-substituted.

    With alert_fields {"soc_id": "{b}", "b": "X"} and a template using {soc_id},
    the single-pass substitution must yield the literal "{b}" — NOT "X".

    (Tested at the _substitute level: run_guided_analysis would now SKIP such a
    query because the resulting "{b}" is an unresolved placeholder.)
    """
    out = _substitute("document_id: '{soc_id}'", {"soc_id": "{b}", "b": "X"})
    assert out == "document_id: '{b}'"
    assert "X" not in out


@respx.mock
def test_run_guided_analysis_multiple_playbooks_all_questions_collected():
    """Questions from multiple returned playbooks are all included."""
    _register_login_mocks(respx.mock)

    multi_playbooks = [
        {"questions": [{"question": "Q1", "context": None, "range": None,
                        "query": "aggregation: false\ndetection:\n  selection:\n    id: '{soc_id}'\n"}]},
        {"questions": [{"question": "Q2", "context": None, "range": None,
                        "query": "aggregation: false\ndetection:\n  selection:\n    id: '{soc_id}'\n"}]},
    ]
    respx.mock.get("https://so.test/api/playbook/detection/multi-uuid").mock(
        return_value=httpx.Response(200, json=multi_playbooks)
    )

    convert_route = respx.mock.post("https://so.test/api/playbook/convert").mock(
        return_value=httpx.Response(200, json=[
            {"query": "q1_converted", "fields": []},
            {"query": "q2_converted", "fields": []},
        ])
    )

    respx.mock.get("https://so.test/api/events/").mock(
        return_value=httpx.Response(200, json={"events": []})
    )

    client = SoClient(CFG)
    out = client.run_guided_analysis("multi-uuid", {"soc_id": "abc"}, range=TEST_RANGE)

    results = out["results"]
    assert len(results) == 2
    assert results[0]["question"] == "Q1"
    assert results[1]["question"] == "Q2"
    # One batched call with both YAMLs
    assert convert_route.call_count == 1
    body = json.loads(convert_route.calls.last.request.content)
    assert len(body) == 2


# ---------------------------------------------------------------------------
# Payload trimming (fix-queue H-1): per-event projection, verbose-field drop,
# per-question cap + total. Verified against the new SoClient contract.
# ---------------------------------------------------------------------------


def test_project_event_keeps_identifiers_plus_fields_drops_the_rest():
    """A trimmed event keeps identifiers + projected fields, nothing else."""
    raw = {
        "@timestamp": "2026-06-01T00:00:00Z",
        "_id": "evt1",
        "source.ip": "10.0.0.19",
        "destination.ip": "10.0.0.255",
        "network.community_id": "1:abc=",
        "some.other.field": "noise",
    }
    out = _project_event(raw, ["network.community_id"])
    assert out == {
        "@timestamp": "2026-06-01T00:00:00Z",
        "_id": "evt1",
        "source.ip": "10.0.0.19",
        "destination.ip": "10.0.0.255",
        "network.community_id": "1:abc=",
    }
    # Unprojected noise field is dropped.
    assert "some.other.field" not in out


def test_project_event_drops_verbose_fields():
    """packet base64, full rule text, and raw message must never survive."""
    raw = {
        "_id": "evt1",
        "source.ip": "1.1.1.1",
        "packet": "QUFBQQ==" * 1000,
        "rule.rule": "alert tcp any any -> any any (...)" * 100,
        "message": "{\"big\": \"json\"}" * 100,
    }
    # Even if a verbose field is explicitly listed in fields, it is dropped.
    out = _project_event(raw, ["packet", "rule.rule", "message", "source.ip"])
    assert "packet" not in out
    assert "rule.rule" not in out
    assert "message" not in out
    assert out["source.ip"] == "1.1.1.1"


def test_project_event_absent_field_is_omitted():
    """Fields that are not present (None) are omitted, not set to null."""
    out = _project_event({"_id": "e"}, ["source.ip", "not.there"])
    assert out == {"_id": "e"}


def test_project_event_handles_nested_payloads():
    """Dotted fields resolve via nested-dict traversal when not a flat key."""
    raw = {"_id": "e", "source": {"ip": "9.9.9.9"}}
    out = _project_event(raw, ["source.ip"])
    assert out["source.ip"] == "9.9.9.9"


def test_project_event_unwraps_so_payload_and_id():
    """The live SO /api/events/ wrapper: fields under payload, _id under id.

    Verified 2026-06-02: SO wraps each event as
    {"id": <es_id>, "payload": {<flat dotted fields>}, ...wrapper keys...}.
    The projector must read fields out of payload and surface the top-level
    "id" as "_id".
    """
    wrapped = {
        "id": "HOeZhp4BoSZ7N5loDVzo",
        "timestamp": "2026-06-02T04:29:20.650Z",
        "type": "conn",
        "score": 0.0,
        "payload": {
            "@timestamp": "2026-06-02T04:29:20.650Z",
            "source.ip": "10.0.0.19",
            "destination.ip": "10.0.0.255",
            "destination.port": 57621,
            "message": "{\"big\": \"json\"}" * 100,
            "noise": "drop me",
        },
    }
    out = _project_event(wrapped, ["destination.port"])
    assert out["_id"] == "HOeZhp4BoSZ7N5loDVzo"
    assert out["@timestamp"] == "2026-06-02T04:29:20.650Z"
    assert out["source.ip"] == "10.0.0.19"
    assert out["destination.ip"] == "10.0.0.255"
    assert out["destination.port"] == 57621
    # Verbose + unprojected payload keys are dropped.
    assert "message" not in out
    assert "noise" not in out


def test_unresolved_placeholders_helper():
    assert _unresolved_placeholders("a '{x}' b '{y.z}' c") == ["x", "y.z"]
    assert _unresolved_placeholders("fully resolved 1.2.3.4") == []


@respx.mock
def test_run_guided_analysis_trims_events_to_projected_shape():
    """Returned events carry only identifiers + the question's fields."""
    _register_login_mocks(respx.mock)

    respx.mock.get("https://so.test/api/playbook/detection/trim-uuid").mock(
        return_value=httpx.Response(200, json=[
            {"questions": [{"question": "Q", "context": None, "range": None,
                            "query": "aggregation: false\ndetection:\n  selection:\n    id: '{soc_id}'\n"}]}
        ])
    )
    respx.mock.post("https://so.test/api/playbook/convert").mock(
        return_value=httpx.Response(200, json=[{"query": "q", "fields": ["network.community_id"]}])
    )

    # A fat raw event with verbose fields the trimmer must drop.
    fat_event = {
        "@timestamp": "2026-06-01T00:00:00Z",
        "_id": "evt1",
        "source.ip": "10.0.0.19",
        "destination.ip": "10.0.0.255",
        "network.community_id": "1:abc=",
        "packet": "QUFBQQ==" * 5000,
        "rule.rule": "alert ..." * 5000,
        "message": "{\"x\": 1}" * 5000,
        "junk": "drop me",
    }
    respx.mock.get("https://so.test/api/events/").mock(
        return_value=httpx.Response(200, json={"events": [fat_event]})
    )

    client = SoClient(CFG)
    out = client.run_guided_analysis("trim-uuid", {"soc_id": "abc"}, range=TEST_RANGE)

    ev = out["results"][0]["events"][0]
    assert set(ev.keys()) == {
        "@timestamp", "_id", "source.ip", "destination.ip", "network.community_id",
    }
    assert "packet" not in ev
    assert "rule.rule" not in ev
    assert "message" not in ev
    assert "junk" not in ev


@respx.mock
def test_run_guided_analysis_caps_events_and_reports_total():
    """events list is capped while total reflects the full match count."""
    _register_login_mocks(respx.mock)

    respx.mock.get("https://so.test/api/playbook/detection/cap-uuid").mock(
        return_value=httpx.Response(200, json=[
            {"questions": [{"question": "Q", "context": None, "range": None,
                            "query": "aggregation: false\ndetection:\n  selection:\n    id: '{soc_id}'\n"}]}
        ])
    )
    respx.mock.post("https://so.test/api/playbook/convert").mock(
        return_value=httpx.Response(200, json=[{"query": "q", "fields": []}])
    )

    # 100 events returned by SO; the per-question cap (default 20) must apply.
    many = [{"_id": f"evt{i}", "source.ip": "1.1.1.1"} for i in range(100)]
    respx.mock.get("https://so.test/api/events/").mock(
        return_value=httpx.Response(200, json={"events": many})
    )

    client = SoClient(CFG)
    out = client.run_guided_analysis("cap-uuid", {"soc_id": "abc"}, range=TEST_RANGE)

    result = out["results"][0]
    assert result["total"] == 100
    assert len(result["events"]) == 20  # _MAX_EVENTS_PER_QUESTION default


# ---------------------------------------------------------------------------
# session-expiry self-heal (2026-06-13): a guest-clock step after a QEMU pause
# expires the SO browser-flow session. Because the client uses
# follow_redirects=True, an expired session is FOLLOWED to the Kratos login page
# (200 HTML) instead of surfacing as a 302 -- which used to make every tool 500
# with "Expecting value: line 1 column 1" until a manual container restart.
# ---------------------------------------------------------------------------


def test_session_expired_classifies_responses():
    """_session_expired flags auth statuses AND a followed redirect to login,
    but not a clean /api 200."""
    client = SoClient(CFG)

    for code in (400, 401, 403):
        r = httpx.Response(code, request=httpx.Request("GET", "https://so.test/api/x"))
        assert client._session_expired(r) is True

    # followed redirect: final URL landed on the Kratos login flow
    redirect = httpx.Response(302, request=httpx.Request("GET", "https://so.test/api/x"))
    landed = httpx.Response(
        200,
        request=httpx.Request("GET", LOGIN_BROWSER_URL),
        history=[redirect],
    )
    assert client._session_expired(landed) is True

    # a clean /api 200 with no redirect is NOT expired
    ok = httpx.Response(200, request=httpx.Request("GET", "https://so.test/api/x"))
    assert client._session_expired(ok) is False


@respx.mock
def test_read_reauths_on_session_expiry_redirect():
    """A read whose session has expired is redirected to the Kratos login
    (followed to 200 because follow_redirects=True). The client must detect the
    login-page landing, force a fresh login, and retry once -- returning the real
    object rather than choking on the login page.

    Reproduces the live 2026-06-13 trap (post-pause clock step expired the SO
    session and the gateway 500'd until a manual restart).
    """
    flow_route = _register_login_mocks(respx.mock)
    detection_route = respx.mock.get("https://so.test/api/detection/abc-123").mock(
        side_effect=[
            # 1st call: session expired -> SO redirects to the login (followed).
            httpx.Response(302, headers={"Location": LOGIN_BROWSER_URL}),
            # 2nd call (after forced re-login): the real object.
            httpx.Response(
                200, json={"id": "abc-123", "publicId": "2009207", "title": "X"}
            ),
        ]
    )

    client = SoClient(CFG)
    result = client.get_detection("abc-123")

    assert result == {"id": "abc-123", "publicId": "2009207", "title": "X"}
    # hit twice: the expired attempt, then the post-reauth retry
    assert detection_route.call_count == 2
    # the browser-flow login ran more than once (initial + forced re-login)
    assert flow_route.call_count >= 2


@respx.mock
def test_write_reauths_on_session_expiry_redirect():
    """The CSRF-protected write path also self-heals a followed login redirect."""
    _register_login_mocks(respx.mock)
    put_route = respx.mock.put("https://so.test/api/detection").mock(
        side_effect=[
            httpx.Response(302, headers={"Location": LOGIN_BROWSER_URL}),
            httpx.Response(200, json={"id": "abc-123", "isEnabled": False}),
        ]
    )

    client = SoClient(CFG)
    result = client.put_detection({"id": "abc-123", "isEnabled": False})

    assert result == {"id": "abc-123", "isEnabled": False}
    assert put_route.call_count == 2
