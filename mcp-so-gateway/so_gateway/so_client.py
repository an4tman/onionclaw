"""httpx client for the Security Onion Core API: Kratos login + read-only reads.

Verified 2026-06-01 against live box + so-agent HARs
(so-guided-analysis-fresh-2026-01-05.har and siblings).
"""

import os
import re
from datetime import datetime, timedelta

import httpx

from so_gateway.config import Config

# HTTP statuses that, on a WRITE, plausibly mean the browser-flow session /
# X-Srv-Token went stale (Kratos session expired or CSRF rejected). SO returns
# 400 for a stale X-Srv-Token on PUT /api/detection (observed 2026-06-02), and
# 401/403 for an expired/again-rejected session -- all three trigger one
# forced re-login + retry.
_REAUTH_STATUSES = frozenset({400, 401, 403})


class SoWriteError(RuntimeError):
    """A SO write failed and could not be self-healed by re-auth.

    Carries the SO HTTP status + response body so the failure is never silent
    or opaque (the caller surfaces this; the tuning token stays re-appliable).
    """

# SO range-string format matches the HAR captures: "YYYY/MM/DD HH:MM:SS AM - YYYY/MM/DD HH:MM:SS PM"
# %I gives zero-padded 12-hour clock (01-12), which matches the zero-padded hours
# seen in every captured HAR range (e.g. "04:16:47 AM", "10:14:20 AM").
_SO_RANGE_FMT = "%Y/%m/%d %I:%M:%S %p"


def _env_int(name: str, default: int) -> int:
    """Read a positive int from the environment, falling back to *default*.

    A missing, empty, or unparseable value yields *default* so a bad env var
    never crashes the gateway.
    """
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        val = int(raw)
    except ValueError:
        return default
    return val if val > 0 else default


# How many events to ASK the SO API for per query. Lowered from the old 500:
# the guided-analysis path projects + caps the response anyway, and a 500-row
# raw Elastic pull is what produced the multi-megabyte payloads (fix-queue H-1).
# Override with SO_EVENT_LIMIT.
_DEFAULT_EVENT_LIMIT = _env_int("SO_EVENT_LIMIT", 50)

# How many (projected) events to RETURN per guided-analysis question. The full
# count is still reported as ``total`` so the caller knows how many matched.
# Override with SO_MAX_EVENTS_PER_QUESTION.
_MAX_EVENTS_PER_QUESTION = _env_int("SO_MAX_EVENTS_PER_QUESTION", 20)

# Always-included identifiers, on top of the question's projected ``fields``,
# so a trimmed event is still pivotable.
_IDENTIFIER_FIELDS = ("@timestamp", "_id", "source.ip", "destination.ip")

# Verbose fields that must never appear in a trimmed event: packet base64, the
# full rule text, and the raw message JSON dwarf everything else.
_VERBOSE_FIELDS = frozenset({"packet", "rule.rule", "message"})


def _substitute(query: str, alert_fields: dict) -> str:
    """Single-pass substitution of ``{key}`` placeholders over the ORIGINAL query.

    A single regex pass ensures alert field VALUES are never re-scanned for
    further placeholders (so a value like ``"{b}"`` is inserted verbatim, not
    re-substituted).  Keys may contain dots (e.g. ``{source.ip}``).  Placeholders
    whose key is absent from *alert_fields* are left unchanged.

    SECURITY: alert field values are substituted verbatim with no escaping --
    intentional for the current read-only scope; revisit if a write-path ever
    sends these queries.
    """

    def _repl(m: re.Match) -> str:
        return str(alert_fields[m.group(1)]) if m.group(1) in alert_fields else m.group(0)

    return re.sub(r"\{([^}]+)\}", _repl, query)


def _unresolved_placeholders(substituted: str) -> list[str]:
    """Return the sorted unique ``{key}`` placeholder names still present.

    Used both to decide whether a question is fully resolved AND to tell the
    caller which alert_fields are still missing (fix-queue M-3).
    """
    return sorted(set(re.findall(r"\{([^}]+)\}", substituted)))


def _lookup_field(doc: dict, field: str) -> object:
    """Best-effort read of *field* from an SO event payload.

    SO event payloads use flat dotted keys (e.g. ``payload["source.ip"]``,
    ``payload["@timestamp"]``), but some docs nest (``payload["source"]["ip"]``).
    Try the flat key first, then walk the dotted path. Returns ``None`` when
    absent (and ``None`` values are dropped by the caller).
    """
    if field in doc:
        return doc[field]
    cur: object = doc
    for part in field.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _project_event(event: dict, fields: list[str] | None) -> dict:
    """Project a raw SO event down to identifiers + the question's *fields*.

    Verified 2026-06-02 against the live box: the SO ``/api/events/`` API wraps
    each document — the real field values live under ``event["payload"]`` (flat
    dotted keys like ``source.ip`` / ``@timestamp``) and the ES ``_id`` is at
    ``event["id"]``. This projects out of the payload, keeps a few identifiers,
    drops verbose fields (packet base64, full rule text, raw message JSON), and
    returns a small dict an LLM SOC agent can consume (fix-queue H-1). Non-dict
    events are returned unchanged.
    """
    if not isinstance(event, dict):
        return event

    # The document fields are under "payload"; fall back to the event itself so
    # the simpler {dotted: value} shape used in unit tests also works.
    payload = event.get("payload")
    doc = payload if isinstance(payload, dict) else event

    wanted: list[str] = list(_IDENTIFIER_FIELDS)
    for f in fields or []:
        if f not in wanted:
            wanted.append(f)

    projected: dict = {}
    for f in wanted:
        if f in _VERBOSE_FIELDS:
            continue
        val = _lookup_field(doc, f)
        # The SO wrapper carries the ES _id at the top level as "id"; surface it
        # as "_id" when the payload itself has no _id.
        if val is None and f == "_id":
            val = event.get("id")
        if val is not None:
            projected[f] = val
    return projected


def _default_range() -> str:
    """Return a 24-hour range string ending now, in SO's format.

    Format: "YYYY/MM/DD h:MM:SS AM - YYYY/MM/DD h:MM:SS PM"
    Uses the platform's local time (same as what the SO UI shows).
    """
    now = datetime.now()
    start = now - timedelta(hours=24)
    return f"{start.strftime(_SO_RANGE_FMT)} - {now.strftime(_SO_RANGE_FMT)}"


class SoClient:
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        # follow_redirects=True is REQUIRED: the credential POST returns a 303
        # that sets the ory_kratos_session cookie. Cookies are kept by default
        # (httpx.Client maintains a cookie jar across requests).
        self._client = httpx.Client(
            base_url=cfg.url,
            verify=not cfg.ssl_skip_verify,
            timeout=30.0,
            follow_redirects=True,
        )
        self._authenticated = False

    def login(self) -> None:
        """Authenticate via the Ory Kratos BROWSER flow + X-Srv-Token.

        The API/Bearer flow only works for GETs; SO's CSRF protection rejects
        POSTs (e.g. /api/playbook/convert -> 400) unless the request rides the
        browser-flow cookies AND carries an X-Srv-Token header.

        Verified 2026-06-01 against the live box (convert returned 200):
          1. GET /auth/self-service/login/browser -> sets csrf_token cookie;
             body has the flow id and the csrf_token UI node value.
          2. POST /auth/self-service/login?flow=<id> (form-encoded) -> 303 sets
             the ory_kratos_session cookie.
          3. GET /api/info -> srvToken (a JWT).
          4. Set X-Srv-Token on the client so every later request carries it.
        Idempotent: only runs once (subsequent calls are no-ops).
        """
        if self._authenticated:
            return

        # Step 1: initiate the browser login flow (sets csrf_token cookie,
        # returns the flow id and the CSRF token value in the UI nodes).
        flow_resp = self._client.get(
            "/auth/self-service/login/browser",
            headers={"Accept": "application/json"},
        )
        flow_resp.raise_for_status()
        data = flow_resp.json()
        flow = data["id"]
        csrf = ""
        for node in data["ui"]["nodes"]:
            attrs = node.get("attributes", {})
            if attrs.get("name") == "csrf_token":
                csrf = attrs.get("value", "")
                break

        # Step 2: POST credentials FORM-ENCODED (use data=, not json=).
        # The 303 redirect is followed and sets the ory_kratos_session cookie.
        # SECURITY: this request body carries the password -- never add httpx event hooks or logging that capture the request body.
        creds_resp = self._client.post(
            "/auth/self-service/login",
            params={"flow": flow},
            data={
                "identifier": self._cfg.email,
                "password": self._cfg.password,
                "csrf_token": csrf,
                "method": "password",
            },
        )
        creds_resp.raise_for_status()
        if "ory_kratos_session" not in self._client.cookies:
            raise RuntimeError(
                "login failed: ory_kratos_session cookie not set after credential POST"
            )

        # Step 3: fetch the server token (JWT) required for CSRF-protected POSTs.
        info = self._client.get(
            "/api/info", headers={"Accept": "application/json"}
        ).json()
        srv = info["srvToken"]

        # Step 4: set X-Srv-Token on the client for ALL subsequent requests.
        self._client.headers["X-Srv-Token"] = srv
        self._authenticated = True

    def _force_relogin(self) -> None:
        """Drop the stale session so the next login() re-authenticates."""
        self._authenticated = False
        self._client.headers.pop("X-Srv-Token", None)
        self._client.cookies.clear()
        self.login()

    def _session_expired(self, resp: httpx.Response) -> bool:
        """True if *resp* shows the browser-flow session went stale.

        Two shapes, both meaning "authenticate again":
          1. An auth/CSRF status: 400 (stale X-Srv-Token on a write), 401, 403.
          2. A FOLLOWED redirect to the Kratos login. The client runs with
             ``follow_redirects=True`` (required so the credential POST's 303 is
             followed), so an expired session is NOT surfaced as a 302 to the
             caller -- httpx follows it to the login page and the ``/api/...``
             request lands (usually 200) on a non-``/api`` login URL. Then
             ``raise_for_status()`` passes and ``.json()`` throws "Expecting
             value: line 1 column 1". Detect it by a non-empty redirect history
             whose final URL left ``/api`` for the login flow.
        """
        if resp.status_code in _REAUTH_STATUSES:
            return True
        if resp.history:
            final = str(resp.url)
            if "/auth/" in final or "/login" in final:
                return True
        return False

    def _request_authed(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Issue an authenticated request, self-healing ONE stale-session failure.

        Ensure a login, issue the request, and -- if the session had expired (an
        auth status OR a followed redirect to the login page) -- force a fresh
        login and retry once. Returns the (possibly second) response WITHOUT
        raising; callers apply their own ``raise_for_status()`` / 404 handling.
        """
        self.login()  # idempotent: authenticates once
        resp = self._client.request(method, url, **kwargs)
        if self._session_expired(resp):
            self._force_relogin()
            resp = self._client.request(method, url, **kwargs)
        return resp

    def _write_with_reauth(self, method: str, url: str, **kwargs) -> dict:
        """CSRF-protected write that self-heals one stale-session failure.

        Issue the write; on an expired session (an auth/CSRF status 400/401/403
        OR a followed redirect to the login page) force a fresh login() and retry
        once. On any non-2xx after that, raise SoWriteError with the SO status +
        body -- never a silent 400.
        """
        resp = self._request_authed(method, url, **kwargs)
        if not resp.is_success:
            raise SoWriteError(
                f"SO write {method} {url} failed: HTTP {resp.status_code} "
                f"(after re-auth retry). Response body: {resp.text[:1000]}"
            )
        return resp.json()

    def get_detection(self, detection_id: str) -> dict:
        """Fetch a detection by EITHER its Elasticsearch ``_id`` OR its ``publicId``.

        SO exposes two single-detection lookups:
          * ``GET /api/detection/{id}``        -> by ES ``_id`` (short, e.g.
            ``oD_kqJcBPiDhvlxwpwvM``)
          * ``GET /api/detection/public/{id}`` -> by ``publicId`` (an ET sid like
            ``2009205`` or a Sigma UUID -- the alert's ``rule.uuid``)

        A SOC alert carries the ``publicId``/``rule.uuid``, NOT the ES ``_id`` --
        and SO returns a bare **404 with an empty body** when a publicId is handed
        to the ``/{id}`` endpoint (verified live 2026-06-08 against the Sigma rule
        ``71158e3f-...`` -> ``/api/detection/{UUID}`` 404 empty,
        ``/api/detection/public/{UUID}`` 200 full object). So an agent that only
        has the publicId could not fetch the detection and would wrongly conclude
        the gateway "can't handle Sigma rules". To make the natural input just
        work, this tries the ES-``_id`` endpoint first and, on a 404, transparently
        falls back to the publicId endpoint. A genuine miss (both 404) or any
        other non-2xx still raises ``httpx.HTTPStatusError``.
        """
        resp = self._request_authed("GET", f"/api/detection/{detection_id}")
        if resp.status_code == 404:
            # Not an ES _id -- retry as a publicId (sid / rule.uuid).
            resp = self._request_authed("GET", f"/api/detection/public/{detection_id}")
        resp.raise_for_status()
        return resp.json()

    def get_detection_by_public_id(self, public_id: str) -> dict:
        """Fetch a detection by its *publicId* (e.g. the ET sid ``2009205``).

        GET /api/detection/public/{publicId} -> detection object. This is the
        lookup the tuning-write path uses: a SOC alert carries the ``publicId``
        (``rule.uuid``), and PUT /api/detection needs the FULL object (incl. the
        ES ``_id`` under ``id``) which this returns.

        Verified 2026-06-02 against the live box (200) + the
        so-get-single-detection-request.har capture.
        """
        resp = self._request_authed("GET", f"/api/detection/public/{public_id}")
        resp.raise_for_status()
        return resp.json()

    def put_detection(self, detection: dict) -> dict:
        """PUT a FULL detection object back to SO -- the tuning WRITE.

        PUT /api/detection with the entire detection as the JSON body (NOT a
        partial patch); SO replaces the stored detection. The body must include
        ``id``/``publicId`` and the desired ``overrides`` / ``isEnabled``.
        Returns the updated detection.

        This is a REAL state-changing write. Callers MUST have captured the
        prior state (for the audit/undo record) before calling. Rides the
        browser-flow cookies + X-Srv-Token (CSRF-protected POST/PUT path).

        Verified 2026-06-02 against the so-tune-detection-*-request.har captures
        (PUT https://securityonion.local/api/detection -> 200, body = full object).
        """
        return self._write_with_reauth("PUT", "/api/detection", json=detection)

    def disposition_alerts(
        self,
        rule_uuid: str,
        date_range: str,
        acknowledge: bool = True,
        escalate: bool = False,
        search_filter: str = "NOT event.acknowledged:true AND tags:alert",
        timezone: str = "America/New_York",
    ) -> dict:
        """Disposition (acknowledge/close or escalate) alerts for *rule_uuid*.

        POST /api/events/ack -- bulk-marks matching alert events acknowledged
        (close) and/or escalated. *date_range* is an SO range string
        "YYYY/MM/DD h:MM:SS AM - YYYY/MM/DD h:MM:SS PM". This is a real write but
        is fully reversible by re-calling with ``acknowledge=False`` (the SO UI
        "undo acknowledge"); the server records it in the audit log.

        Verified 2026-06-02 against the so-disable-detection-workflow.har ack
        request shape.
        """
        self.login()  # idempotent: authenticates once
        body = {
            "searchFilter": search_filter,
            "eventFilter": {"rule.uuid": rule_uuid},
            "dateRange": date_range,
            "dateRangeFormat": "2006/01/02 3:04:05 PM",
            "timezone": timezone,
            "escalate": escalate,
            "acknowledge": acknowledge,
        }
        return self._write_with_reauth("POST", "/api/events/ack", json=body)

    def get_playbook(self, public_id: str) -> list:
        """Fetch the playbook list for *public_id* (the alert's ``rule.uuid``).

        GET /api/playbook/detection/{public_id} -> 200 JSON **array** of playbook
        objects, each containing a ``questions`` list.

        Verified 2026-06-01 against live box + so-agent HARs.
        """
        resp = self._request_authed("GET", f"/api/playbook/detection/{public_id}")
        resp.raise_for_status()
        return resp.json()

    def convert_queries(self, sigma_yamls: list[str]) -> list[dict]:
        """Convert a batch of Sigma-YAML strings to SO query objects.

        POST /api/playbook/convert with a JSON **array** of Sigma-YAML strings ->
        200 JSON **array** (parallel to input) of ``{"query": <str>, "fields": [...]}``
        objects.

        Verified 2026-06-01 against live box + so-agent HARs.
        """
        resp = self._request_authed("POST", "/api/playbook/convert", json=sigma_yamls)
        resp.raise_for_status()
        return resp.json()

    def get_events(
        self,
        query: str,
        range: str,
        zone: str = "America/New_York",
        limit: int = _DEFAULT_EVENT_LIMIT,
    ) -> dict:
        """Fetch events matching *query* from the SO API.

        GET /api/events/ with params: query, range, format, zone, metricLimit,
        eventLimit -> returns resp.json().

        ``range`` must be in SO's format: "YYYY/MM/DD h:MM:SS AM - YYYY/MM/DD h:MM:SS PM".
        ``format`` is fixed at "2006/01/02 3:04:05 PM" (Go reference time).
        ``limit`` defaults to the configurable ``SO_EVENT_LIMIT`` (default 50,
        down from the old hard-coded 500 that produced multi-megabyte payloads).

        Verified 2026-06-01 against live box + so-agent HARs.
        """
        resp = self._request_authed(
            "GET",
            "/api/events/",
            params={
                "query": query,
                "range": range,
                "format": "2006/01/02 3:04:05 PM",
                "zone": zone,
                "metricLimit": limit,
                "eventLimit": limit,
            },
        )
        resp.raise_for_status()
        return resp.json()

    def count_matching_alerts(
        self,
        public_id: str,
        override: dict,
        range: str | None = None,
    ) -> int:
        """Blast-radius probe: count recent alerts a tuning *would* silence.

        Read-only. Queries the SO event store for alert events from this rule
        (``rule.uuid:{public_id}``), narrowed by the override's scope when it has
        an ``ip`` (suppress) -- an advisory "how many recent alerts match" the
        human reviews before approving. Defaults to the last 24h.

        Returns the SO ``totalEvents`` when present, else the length of the
        returned event list. Best-effort -- the caller treats this as advisory.
        """
        query = f"tags:alert AND rule.uuid:{public_id}"
        ip = override.get("ip")
        if ip:
            host = ip.split("/")[0]
            track = override.get("track", "by_either")
            if track == "by_src":
                query += f" AND source.ip:{host}"
            elif track == "by_dst":
                query += f" AND destination.ip:{host}"
            else:
                query += f" AND (source.ip:{host} OR destination.ip:{host})"
        raw = self.get_events(query, range=range or _default_range())
        if isinstance(raw, dict):
            if isinstance(raw.get("totalEvents"), int):
                return raw["totalEvents"]
            events = raw.get("events", [])
            return len(events) if isinstance(events, list) else 0
        return len(raw) if isinstance(raw, list) else 0

    def run_guided_analysis(
        self,
        public_id: str,
        alert_fields: dict,
        range: str | None = None,
    ) -> dict:
        """Run the SO playbook questions for *public_id*, substituting
        *alert_fields* into each question's Sigma-YAML query, converting the
        resolved batch in one call, then fetching + TRIMMING events per question.

        *public_id* is the alert's ``rule.uuid`` (a UUID string), NOT the ES _id.
        *alert_fields* must include ``soc_id`` (the ES _id / ``_id`` field) so
        that ``{soc_id}`` placeholders in the first "alert" question are resolved.

        Returns a top-level summary dict (fix-queue H-1 + M-3)::

            {
                "resolved": <int>,          # questions that fully resolved
                "skipped": <int>,           # questions with missing alert_fields
                "missing_fields": [<str>],  # union of unresolved placeholders
                "results": [                # one per RESOLVED question
                    {
                        "question": <str>,
                        "context": <str | None>,
                        "query": <converted SO query str>,
                        "fields": <list[str] | None>,
                        "total": <int>,     # full match count from SO
                        "events": [<trimmed dict>],  # capped + projected
                    },
                ],
                "skipped_questions": [      # one per SKIPPED question
                    {
                        "question": <str>,
                        "unresolved_placeholders": [<str>],
                    },
                ],
            }

        Each returned event is projected to the question's ``fields`` plus a few
        identifiers (@timestamp, _id, source.ip, destination.ip), with verbose
        fields (packet base64, full rule text, raw message) dropped and the list
        capped at SO_MAX_EVENTS_PER_QUESTION -- so an LLM SOC agent can actually
        consume the result. ``total`` preserves the real match count.

        Verified 2026-06-01 against live box + so-agent HARs.
        """
        playbooks = self.get_playbook(public_id)

        # Collect all questions from all returned playbooks
        questions: list[dict] = []
        for pb in playbooks:
            questions.extend(pb.get("questions", []))

        # Substitute each question's Sigma-YAML. Keep fully-resolved ones for the
        # convert batch; record the rest with their unresolved placeholders so
        # the caller knows which alert_fields to supply (fix-queue M-3). A
        # leftover ``{placeholder}`` would otherwise 400 the WHOLE convert batch.
        resolved_questions: list[dict] = []
        substituted_yamls: list[str] = []
        skipped_questions: list[dict] = []
        missing_fields: set[str] = set()
        for q in questions:
            sub = _substitute(q["query"], alert_fields)
            unresolved = _unresolved_placeholders(sub)
            if unresolved:
                missing_fields.update(unresolved)
                skipped_questions.append(
                    {
                        "question": q.get("question"),
                        "unresolved_placeholders": unresolved,
                    }
                )
                continue
            resolved_questions.append(q)
            substituted_yamls.append(sub)

        results: list[dict] = []
        if substituted_yamls:
            # One batched convert call -- results are parallel to substituted_yamls
            converted = self.convert_queries(substituted_yamls)
            effective_range = range or _default_range()

            for q, conv in zip(resolved_questions, converted):
                raw = self.get_events(conv["query"], range=effective_range)
                events = raw.get("events", raw) if isinstance(raw, dict) else raw
                if not isinstance(events, list):
                    # Unexpected shape -- pass through untrimmed but still capped.
                    events = [events]
                total = len(events)
                fields = conv.get("fields")
                trimmed = [
                    _project_event(e, fields)
                    for e in events[:_MAX_EVENTS_PER_QUESTION]
                ]
                results.append(
                    {
                        "question": q.get("question"),
                        "context": q.get("context"),
                        "query": conv["query"],
                        "fields": fields,
                        "total": total,
                        "events": trimmed,
                    }
                )

        return {
            "resolved": len(results),
            "skipped": len(skipped_questions),
            "missing_fields": sorted(missing_fields),
            "results": results,
            "skipped_questions": skipped_questions,
        }
