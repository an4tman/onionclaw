import os

from mcp.server.fastmcp import FastMCP

from . import ti
from .config import load_config
from .so_client import SoClient
from .tuning_service import TuningService
from .tuning_store import TuningStore

_MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
_MCP_PORT = int(os.environ.get("MCP_PORT", "8080"))

# Audit/undo DB. Lives on a mounted volume so it survives a container recreate.
_AUDIT_DB_PATH = os.environ.get("SO_AUDIT_DB", "/data/tuning-audit.sqlite")

mcp = FastMCP("so-gateway", host=_MCP_HOST, port=_MCP_PORT)

_client: SoClient | None = None
_tuning_service: TuningService | None = None


def _get_client() -> SoClient:
    global _client
    if _client is None:
        _client = SoClient(load_config())
    return _client


def _get_tuning_service() -> TuningService:
    global _tuning_service
    if _tuning_service is None:
        _tuning_service = TuningService(_get_client(), TuningStore(_AUDIT_DB_PATH))
    return _tuning_service


def ping() -> str:
    return "Ready"


def get_detection(detection_id: str) -> dict:
    """Fetch one detection object. Accepts EITHER the ``publicId`` OR the ES ``_id``.

    *detection_id*: normally the alert's ``publicId`` / ``rule.uuid`` (an ET sid
    like ``"2009205"`` or a Sigma UUID like ``"71158e3f-df67-472b-930e-7d287acaa3e1"``)
    -- that is what alerts carry. The gateway resolves it by trying the ES-``_id``
    endpoint first and falling back to the publicId endpoint, so you do NOT need
    the short ES ``_id`` to look a detection up. Returns the full detection object
    (``publicId``, ``title``, ``engine``, ``language``, ``isEnabled``, ``overrides``,
    the Sigma/Suricata ``content``, …). To TUNE a detection, pass the same
    ``publicId`` to ``propose_tuning`` (it works for Sigma/elastalert UUIDs too).
    """
    return _get_client().get_detection(detection_id)


def get_playbook(public_id: str) -> list:
    """Return the playbook list for *public_id* (the alert's ``rule.uuid``).

    Calls GET /api/playbook/detection/{public_id} and returns the JSON array.
    Each element contains a ``questions`` list with Sigma-YAML query templates.
    """
    return _get_client().get_playbook(public_id)


def run_guided_analysis(
    public_id: str,
    alert_fields: dict,
    range: str | None = None,
) -> dict:
    """Run the full guided-analysis workflow for an alert.

    *public_id*: the alert's ``rule.uuid`` (UUID string).
    *alert_fields*: dict of field values to substitute into question queries;
        must include ``soc_id`` (the ES ``_id`` of the triggering alert event).
    *range*: optional SO range string "YYYY/MM/DD h:MM:SS AM - YYYY/MM/DD h:MM:SS PM";
        defaults to the last 24 hours when omitted.

    Returns a summary dict ``{resolved, skipped, missing_fields, results,
    skipped_questions}``. Each ``results`` entry carries the question, converted
    query, projected ``fields``, the full ``total`` match count, and a capped
    list of TRIMMED ``events`` (projected to the question's fields + a few
    identifiers; verbose packet/rule/message data dropped) so the payload stays
    small enough for an LLM SOC agent to consume. ``skipped_questions`` lists any
    question whose query still had ``unresolved_placeholders`` (missing
    alert_fields), so the caller knows what to supply.
    """
    return _get_client().run_guided_analysis(public_id, alert_fields, range)


# ---------------------------------------------------------------------------
# WRITE tools — the human-gated, reversible tuning surface (spec §4).
#
# SAFETY: propose_tuning is read-only (validate + preview + token). apply_tuning
# is the ONLY tool that writes a tuning, and it requires a single-use token from
# a prior propose. The agent WORKFLOW layers the human-approval gate on top:
#   * Claude Code: the native permission prompt on apply_tuning is the gate.
#   * OpenClaw: the skill protocol forbids calling apply_tuning until the
#     operator affirms the surfaced token + blast radius.
# disable/modify proposals come back flagged double_gated for a louder confirm.
# Every applied write is audited + revertible via revert_tuning.
# ---------------------------------------------------------------------------


def propose_tuning(
    public_id: str,
    override_type: str,
    scope: dict,
    rationale: str,
    review_horizon_days: int | None = 90,
) -> dict:
    """Validate + preview a tuning and issue a single-use token. NO WRITE.

    *public_id*: the detection's publicId (e.g. the ET sid ``"2009205"``).
    *override_type*: one of ``suppress`` / ``threshold`` / ``modify`` / ``disable``.
    *scope*: type-specific params:
        suppress  -> {"track": "by_src|by_dst|by_either", "ip": "<host or CIDR>"}
        threshold -> {"thresholdType": "limit|threshold|both", "track": ...,
                      "count": <int>, "seconds": <int>}
        modify    -> {"regex": "<re>", "value": "<replacement>"}
        disable   -> {} (flips the detection's isEnabled to false)
    *rationale*: human-readable reason, recorded on the override + audit log.
    *review_horizon_days*: advisory only; nothing auto-expires.

    Returns ``{token, override, detection, blast_radius, double_gated,
    review_horizon_days}``. This call is read-only and injection-safe: malformed
    scope is rejected here before any token exists. To actually apply, a human
    must approve and then ``apply_tuning(token)`` is called.
    """
    return _get_tuning_service().propose_tuning(
        public_id=public_id,
        override_type=override_type,
        scope=scope,
        rationale=rationale,
        review_horizon_days=review_horizon_days,
    )


def apply_tuning(token: str) -> dict:
    """Apply a previously proposed tuning. REAL SO WRITE. Single-use token.

    *token*: the one-time token returned by ``propose_tuning``. Requires that a
    human has approved the proposal (CC permission prompt / OpenClaw operator
    affirmation) BEFORE this is called. PUTs the computed override to SO, records
    an undo record, and returns ``{handle, status, public_id, override_type}``.
    The token cannot be reused; the ``handle`` reverts the change.
    """
    return _get_tuning_service().apply_tuning(token)


def revert_tuning(handle: str) -> dict:
    """Revert a previously applied tuning, restoring the captured prior state.

    *handle*: the undo handle returned by ``apply_tuning`` (or listed by
    ``list_tunings``). Re-fetches the live detection, restores the prior
    ``overrides``/``isEnabled``, and marks the record reverted.
    """
    return _get_tuning_service().revert_tuning(handle)


def list_tunings() -> list:
    """List currently-applied tunings + their undo handles (excludes reverted)."""
    return _get_tuning_service().list_tunings()


def list_pending_proposals() -> list:
    """List proposals awaiting operator approval (token + summary). Read-only.

    Use this to resolve a bare "approve" (exactly one pending proposal means
    that's the one) or to re-show outstanding proposals. Pending proposals are
    in-memory: a gateway restart clears them (re-propose).
    """
    return _get_tuning_service().list_pending()


def disposition_alerts(
    rule_uuid: str,
    date_range: str,
    acknowledge: bool = True,
    escalate: bool = False,
) -> dict:
    """Disposition (acknowledge/close or escalate) alerts for a rule. REAL WRITE.

    *rule_uuid*: the alert's ``rule.uuid`` / publicId.
    *date_range*: SO range string "YYYY/MM/DD h:MM:SS AM - YYYY/MM/DD h:MM:SS PM".
    *acknowledge*: mark matching alerts acknowledged (close). Reversible by
        re-calling with ``acknowledge=False``.
    *escalate*: escalate matching alerts.

    Audited like a tuning. The agent workflow gates this the same way as apply.
    """
    return _get_tuning_service().disposition_alerts(
        rule_uuid=rule_uuid,
        date_range=date_range,
        acknowledge=acknowledge,
        escalate=escalate,
    )


# ---------------------------------------------------------------------------
# THREAT-INTEL ENRICHMENT tools (Component 2). READ-ONLY.
#
# Tiered IOC reputation: external IPs / domains / hashes are enriched against the
# ENABLED provider set (the privacy/cost throttle — a provider with no key is
# never called), results cached + rate-limited, then deterministically merged
# into one reputation summary per IOC. RFC1918/internal IPs are dropped by the
# extractor and never leave the network.
#
# SAFETY: IOCs come from untrusted alert data (prompt injection). These tools
# only send the indicator VALUE to a provider's lookup API; they never fetch an
# attacker-controlled URL in a browser-like way, never follow redirects, and
# never execute anything from a TI response.
# ---------------------------------------------------------------------------


def enrich_iocs(indicators: list[str], use_cache: bool = True) -> dict:
    """Enrich external IOCs (IPs/domains/hashes) and return reputation summaries.

    *indicators*: raw indicator strings pulled from an alert (IPs, domains,
        md5/sha1/sha256 hashes). Internal/RFC1918 IPs and unrecognized values are
        dropped by the extractor (reported under ``dropped_internal_or_invalid``)
        and are never sent to any provider.
    *use_cache*: check the on-disk TI cache before any external call (default
        True). The cache short-circuits repeat lookups across runs and is the
        primary defense against hammering rate-limited providers.

    Returns ``{extracted, dropped_internal_or_invalid, enabled_providers,
    summaries, cache_stats}``. Each ``summaries`` entry is one IOC's reputation:
    ``{ioc, kind, consensus_verdict (malicious|suspicious|benign|unknown),
    max_score, malicious_sources, suspicious_sources, benign_sources, conflict,
    providers_queried, providers_errored, any_cached, records}``. READ-ONLY.
    """
    return ti.enrich_indicators(indicators, use_cache=use_cache)


def ti_provider_status() -> dict:
    """Show which TI providers are enabled (the privacy/cost throttle). No secrets.

    Returns the keyed-provider enablement (OTX/AbuseIPDB/VirusTotal — true iff a
    key is present in the environment), whether the keyless feeds are enabled and
    which ones, the cache DB path, and the TTLs. Never returns key material.
    """
    return ti.provider_status()


def extract_iocs(indicators: list[str]) -> dict:
    """Normalize/dedupe/type indicator strings WITHOUT any external lookup.

    Deterministic. Returns ``{ips, domains, hashes, dropped}`` where ``dropped``
    holds RFC1918/internal/invalid values that the external-IPs-only filter
    removed. Useful to preview what WOULD be enriched (and confirm the RFC1918
    drop) before any provider call.
    """
    return ti.extract_iocs(indicators)


mcp.tool()(ping)
mcp.tool()(get_detection)
mcp.tool()(get_playbook)
mcp.tool()(run_guided_analysis)
mcp.tool()(propose_tuning)
mcp.tool()(apply_tuning)
mcp.tool()(revert_tuning)
mcp.tool()(list_tunings)
mcp.tool()(list_pending_proposals)
mcp.tool()(disposition_alerts)
mcp.tool()(enrich_iocs)
mcp.tool()(ti_provider_status)
mcp.tool()(extract_iocs)

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
