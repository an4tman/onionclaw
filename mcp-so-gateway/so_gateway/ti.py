"""Threat-intel enrichment for the SOC agent.

Tiered, READ-ONLY, external-IPs-only IOC reputation. Design:
kb/security/threat-intel-enrichment + kb/projects/soc-agent-roadmap (Component 2).

Pipeline (all in-process; no sub-agent spawning needed at this scale):
  IOC extractor (deterministic; drops RFC1918/reserved IPs)
    -> per ENABLED provider: cache-check -> rate-limit -> lookup -> normalize
    -> deterministic aggregator -> one reputation summary per IOC

The ENABLED-PROVIDER SET is the privacy/cost throttle: a provider with no key is
never called. The cache short-circuits repeat lookups across runs (the primary
defense against hammering VirusTotal's 4 req/min free tier).

SAFETY / prompt-injection hygiene: IOCs come from untrusted alert data. We only
ever send the indicator VALUE to a provider's API lookup endpoint. We never fetch
an attacker-controlled URL in a browser-like way, never follow redirects into
attacker infrastructure, and never execute anything from a TI response. Provider
responses are treated as data, normalized into a fixed schema, and never
interpreted as instructions.

Verdict taxonomy (per design): malicious | suspicious | benign | unknown.
"""

import ipaddress
import re
import threading
import time

import httpx

from . import ti_config as cfg
from .ti_cache import TiCache

# --------------------------------------------------------------------------- #
# IOC extraction / typing
# --------------------------------------------------------------------------- #

_MD5 = re.compile(r"^[a-fA-F0-9]{32}$")
_SHA1 = re.compile(r"^[a-fA-F0-9]{40}$")
_SHA256 = re.compile(r"^[a-fA-F0-9]{64}$")
_DOMAIN = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$", re.IGNORECASE
)


def is_external_ip(value: str) -> bool:
    """True iff *value* is a routable, public IP (drops RFC1918/loopback/etc).

    This is the external-IPs-only gate: private, loopback, link-local, reserved,
    multicast and unspecified addresses are dropped so we never leak internal
    addressing to a third-party TI provider.
    """
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def classify_ioc(value: str) -> str | None:
    """Return the IOC kind for *value*: 'ip' | 'domain' | 'hash', or None.

    IPs are only classified as 'ip' if EXTERNAL (RFC1918 etc. -> None, dropped).
    """
    v = value.strip()
    if not v:
        return None
    try:
        ipaddress.ip_address(v)
        return "ip" if is_external_ip(v) else None
    except ValueError:
        pass
    if _MD5.match(v) or _SHA1.match(v) or _SHA256.match(v):
        return "hash"
    if _DOMAIN.match(v):
        return "domain"
    return None


def extract_iocs(values: list[str]) -> dict:
    """Normalize + dedupe + type a list of indicator strings.

    Returns {"ips": [...], "domains": [...], "hashes": [...], "dropped": [...]}.
    'dropped' carries values that were RFC1918/internal or unrecognized, so the
    caller can SEE that the RFC1918 filter fired (audit/test evidence).
    """
    ips, domains, hashes, dropped = set(), set(), set(), []
    for raw in values:
        v = (raw or "").strip()
        if not v:
            continue
        kind = classify_ioc(v)
        if kind == "ip":
            ips.add(v)
        elif kind == "domain":
            domains.add(v.lower())
        elif kind == "hash":
            hashes.add(v.lower())
        else:
            dropped.append(v)
    return {
        "ips": sorted(ips),
        "domains": sorted(domains),
        "hashes": sorted(hashes),
        "dropped": dropped,
    }


# --------------------------------------------------------------------------- #
# Per-provider rate limiting (process-global, thread-safe)
# --------------------------------------------------------------------------- #


class _RateLimiter:
    """Simple min-interval throttle per provider key. Thread-safe (sync)."""

    def __init__(self) -> None:
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, key: str, min_interval_s: float) -> None:
        with self._lock:
            now = time.monotonic()
            last = self._last.get(key, 0.0)
            delta = now - last
            if delta < min_interval_s:
                time.sleep(min_interval_s - delta)
            self._last[key] = time.monotonic()


_LIMITER = _RateLimiter()

# Conservative min-intervals (seconds) — well under each free tier.
_RATE = {
    "OTX": 0.2,         # ~10 req/s allowed; be gentle
    "AbuseIPDB": 1.0,   # 1000/day
    "VirusTotal": 16.0, # 4 req/min free tier -> >=15s between calls
    "feeds": 2.0,       # the keyless bulk lists
}


# --------------------------------------------------------------------------- #
# Keyed providers (each returns a normalized record dict)
# --------------------------------------------------------------------------- #
#
# Normalized record schema:
#   {provider, ioc, kind, verdict, score(0-100|None), categories[list],
#    evidence(str), ttl, error(str|None)}
# Endpoints/auth VERIFIED current 2026-06-02 (see kb / task notes).


def _rec(provider, ioc, kind, *, verdict="unknown", score=None,
         categories=None, evidence="", error=None):
    return {
        "provider": provider,
        "ioc": ioc,
        "kind": kind,
        "verdict": verdict,
        "score": score,
        "categories": categories or [],
        "evidence": evidence,
        "error": error,
    }


def _client() -> httpx.Client:
    return httpx.Client(
        timeout=cfg.HTTP_TIMEOUT,
        follow_redirects=False,  # injection hygiene: never chase a redirect
        headers={"User-Agent": cfg.USER_AGENT},
    )


def otx_lookup(ioc: str, kind: str) -> dict:
    """AlienVault OTX. GET /api/v1/indicators/{IPv4|domain}/{ioc}/general,
    header X-OTX-API-KEY. Verdict from pulse_count."""
    name = "OTX"
    if kind == "ip":
        path = f"IPv4/{ioc}/general"
    elif kind == "domain":
        path = f"domain/{ioc}/general"
    else:
        return _rec(name, ioc, kind, error="OTX does not support hashes here")
    _LIMITER.wait(name, _RATE[name])
    url = f"https://otx.alienvault.com/api/v1/indicators/{path}"
    try:
        with _client() as c:
            r = c.get(url, headers={"X-OTX-API-KEY": cfg.OTX_API_KEY})
        if r.status_code == 404:
            return _rec(name, ioc, kind, verdict="unknown", score=0,
                        evidence="not found in OTX")
        if r.status_code != 200:
            return _rec(name, ioc, kind, error=f"HTTP {r.status_code}")
        data = r.json()
        pulses = data.get("pulse_info", {}) or {}
        n = pulses.get("count", 0) or 0
        names = [p.get("name", "") for p in (pulses.get("pulses") or [])][:5]
        if n == 0:
            verdict, score = "benign", 0
        elif n >= 3:
            verdict, score = "malicious", min(100, 50 + n * 10)
        else:
            verdict, score = "suspicious", 50 + n * 10
        return _rec(name, ioc, kind, verdict=verdict, score=score,
                    categories=names,
                    evidence=f"{n} OTX pulse(s)" + (f": {names}" if names else ""))
    except httpx.HTTPError as e:
        return _rec(name, ioc, kind, error=f"network: {e}")


def abuseipdb_lookup(ioc: str, kind: str) -> dict:
    """AbuseIPDB. GET /api/v2/check?ipAddress=, header Key. IPs only."""
    name = "AbuseIPDB"
    if kind != "ip":
        return _rec(name, ioc, kind, error="AbuseIPDB supports IPs only")
    _LIMITER.wait(name, _RATE[name])
    try:
        with _client() as c:
            r = c.get(
                "https://api.abuseipdb.com/api/v2/check",
                headers={"Key": cfg.ABUSEIPDB_API_KEY, "Accept": "application/json"},
                params={"ipAddress": ioc, "maxAgeInDays": "90"},
            )
        if r.status_code != 200:
            return _rec(name, ioc, kind, error=f"HTTP {r.status_code}")
        d = (r.json() or {}).get("data", {}) or {}
        score = 0 if d.get("isWhitelisted") else int(d.get("abuseConfidenceScore", 0) or 0)
        reports = d.get("totalReports", 0) or 0
        # Drive the verdict off AbuseIPDB's own confidence score, not the raw
        # crowd-report count: well-known infra (e.g. 8.8.8.8) accrues many
        # low-quality reports but keeps confidence ~0, which is NOT suspicious.
        if score >= 50:
            verdict = "malicious"
        elif score >= 25:
            verdict = "suspicious"
        else:
            verdict = "benign"
        cats = []
        if d.get("usageType"):
            cats.append(d["usageType"])
        if d.get("isTor"):
            cats.append("tor")
        return _rec(name, ioc, kind, verdict=verdict, score=score, categories=cats,
                    evidence=f"abuseConfidence={score}, reports={reports}, "
                             f"country={d.get('countryCode')}, isp={d.get('isp')}")
    except httpx.HTTPError as e:
        return _rec(name, ioc, kind, error=f"network: {e}")


def virustotal_lookup(ioc: str, kind: str) -> dict:
    """VirusTotal v3. GET /api/v3/{ip_addresses|domains|files}/{ioc},
    header x-apikey. Verdict from last_analysis_stats."""
    name = "VirusTotal"
    seg = {"ip": "ip_addresses", "domain": "domains", "hash": "files"}.get(kind)
    if not seg:
        return _rec(name, ioc, kind, error="unsupported kind")
    _LIMITER.wait(name, _RATE[name])
    try:
        with _client() as c:
            r = c.get(f"https://www.virustotal.com/api/v3/{seg}/{ioc}",
                      headers={"x-apikey": cfg.VT_API_KEY})
        if r.status_code == 404:
            return _rec(name, ioc, kind, verdict="unknown", score=0,
                        evidence="not found in VT")
        if r.status_code == 429:
            return _rec(name, ioc, kind, error="rate limited (429)")
        if r.status_code != 200:
            return _rec(name, ioc, kind, error=f"HTTP {r.status_code}")
        attrs = (r.json() or {}).get("data", {}).get("attributes", {}) or {}
        stats = attrs.get("last_analysis_stats", {}) or {}
        mal = stats.get("malicious", 0) or 0
        sus = stats.get("suspicious", 0) or 0
        total = sum(v for v in stats.values() if isinstance(v, int)) or 0
        score = round(((mal + 0.5 * sus) / total) * 100, 1) if total else 0
        if mal >= 3:
            verdict = "malicious"
        elif mal >= 1 or sus >= 2:
            verdict = "suspicious"
        else:
            verdict = "benign"
        cats = []
        if attrs.get("as_owner"):
            cats.append(attrs["as_owner"])
        if attrs.get("type_description"):
            cats.append(attrs["type_description"])
        return _rec(name, ioc, kind, verdict=verdict, score=score, categories=cats,
                    evidence=f"{mal} malicious / {sus} suspicious of {total} engines "
                             f"(reputation={attrs.get('reputation')})")
    except httpx.HTTPError as e:
        return _rec(name, ioc, kind, error=f"network: {e}")


# --------------------------------------------------------------------------- #
# Keyless feeds (membership tests against cached bulk lists). IPs only.
# Endpoints VERIFIED current 2026-06-02.
# --------------------------------------------------------------------------- #

_FEEDS = {
    # name: (url, parser_key, verdict_if_member, category)
    "TorExitList": (
        "https://check.torproject.org/torbulkexitlist",
        "lines_ip", "suspicious", "tor-exit-node"),
    "FeodoTracker": (
        "https://feodotracker.abuse.ch/downloads/ipblocklist_recommended.txt",
        "hash_comment_ip", "malicious", "botnet-c2"),
    "SpamhausDROP": (
        "https://www.spamhaus.org/drop/drop.txt",
        "spamhaus_cidr", "malicious", "spamhaus-drop"),
    "DShield": (
        None,  # per-IP API, handled specially below
        "dshield_ip", "suspicious", "dshield-attacker"),
    "BlocklistDE": (
        "https://lists.blocklist.de/lists/all.txt",
        "lines_ip", "suspicious", "blocklist.de-attacker"),
}

# In-process snapshot cache for the bulk lists: name -> (expires_at, payload).
_feed_lock = threading.Lock()
_feed_snapshots: dict[str, tuple[float, object]] = {}


def _fetch_feed_payload(name: str) -> object:
    url, parser, _, _ = _FEEDS[name]
    _LIMITER.wait("feeds", _RATE["feeds"])
    with _client() as c:
        r = c.get(url)
        r.raise_for_status()
        text = r.text
    if parser == "lines_ip":
        return frozenset(
            ln.strip() for ln in text.splitlines()
            if ln.strip() and not ln.startswith("#")
        )
    if parser == "hash_comment_ip":
        return frozenset(
            ln.strip() for ln in text.splitlines()
            if ln.strip() and not ln.startswith("#")
        )
    if parser == "spamhaus_cidr":
        nets = []
        for ln in text.splitlines():
            ln = ln.strip()
            if not ln or ln.startswith(";"):
                continue
            cidr = ln.split(";")[0].strip()
            try:
                nets.append(ipaddress.ip_network(cidr, strict=False))
            except ValueError:
                continue
        return nets
    return frozenset()


def _feed_payload(name: str) -> object:
    """Return a (cached) snapshot of feed *name*, refreshing on TTL expiry."""
    with _feed_lock:
        snap = _feed_snapshots.get(name)
        if snap and snap[0] > time.time():
            return snap[1]
    payload = _fetch_feed_payload(name)
    with _feed_lock:
        _feed_snapshots[name] = (time.time() + cfg.FEED_TTL_SECONDS, payload)
    return payload


def feed_lookup(name: str, ioc: str, kind: str) -> dict:
    """Membership test of an IP against keyless feed *name*."""
    url, parser, hit_verdict, category = _FEEDS[name]
    if kind != "ip":
        return _rec(name, ioc, kind, error=f"{name} supports IPs only")
    try:
        if name == "DShield":
            _LIMITER.wait("feeds", _RATE["feeds"])
            with _client() as c:
                r = c.get(f"https://isc.sans.edu/api/ip/{ioc}?json")
            if r.status_code != 200:
                return _rec(name, ioc, kind, error=f"HTTP {r.status_code}")
            ipd = (r.json() or {}).get("ip", {}) or {}
            count = int(ipd.get("count") or 0)
            attacks = int(ipd.get("attacks") or 0)
            if count > 0 or attacks > 0:
                return _rec(name, ioc, kind, verdict="suspicious",
                            score=min(100, 40 + count),
                            categories=[category],
                            evidence=f"DShield count={count}, attacks={attacks}, "
                                     f"as={ipd.get('asname')}")
            return _rec(name, ioc, kind, verdict="benign", score=0,
                        evidence="no DShield reports")
        payload = _feed_payload(name)
        member = False
        if parser == "spamhaus_cidr":
            ip = ipaddress.ip_address(ioc)
            member = any(ip in net for net in payload)
        else:
            member = ioc in payload
        if member:
            return _rec(name, ioc, kind, verdict=hit_verdict, score=80,
                        categories=[category],
                        evidence=f"listed on {name}")
        return _rec(name, ioc, kind, verdict="benign", score=0,
                    evidence=f"not on {name}")
    except httpx.HTTPError as e:
        return _rec(name, ioc, kind, error=f"network: {e}")
    except ValueError as e:
        return _rec(name, ioc, kind, error=f"bad ioc: {e}")


# --------------------------------------------------------------------------- #
# Provider registry — enabled set = privacy/cost throttle
# --------------------------------------------------------------------------- #


def enabled_providers() -> list[tuple[str, str, object]]:
    """Return [(name, tier, callable(ioc, kind))] for every ENABLED provider.

    Keyed providers self-disable without a key. Keyless feeds toggle as a group
    via TI_ENABLE_FEEDS. The returned list IS the privacy/cost throttle.
    """
    out: list[tuple[str, str, object]] = []
    if cfg.OTX_API_KEY:
        out.append(("OTX", "keyed", otx_lookup))
    if cfg.ABUSEIPDB_API_KEY:
        out.append(("AbuseIPDB", "keyed", abuseipdb_lookup))
    if cfg.VT_API_KEY:
        out.append(("VirusTotal", "keyed", virustotal_lookup))
    if cfg.ENABLE_FEEDS:
        for fname in _FEEDS:
            out.append((fname, "keyless",
                        (lambda n: (lambda ioc, kind: feed_lookup(n, ioc, kind)))(fname)))
    return out


def provider_status() -> dict:
    """Human-readable view of which providers are enabled (no secrets)."""
    return {
        "keyed": {
            "OTX": bool(cfg.OTX_API_KEY),
            "AbuseIPDB": bool(cfg.ABUSEIPDB_API_KEY),
            "VirusTotal": bool(cfg.VT_API_KEY),
        },
        "keyless_feeds_enabled": cfg.ENABLE_FEEDS,
        "keyless_feeds": list(_FEEDS.keys()),
        "cache_db": cfg.CACHE_DB,
        "ttl_seconds": cfg.TTL_SECONDS,
        "feed_ttl_seconds": cfg.FEED_TTL_SECONDS,
    }


# --------------------------------------------------------------------------- #
# Deterministic aggregator (NOT an agent) -> one reputation summary per IOC
# --------------------------------------------------------------------------- #

_VERDICT_RANK = {"malicious": 3, "suspicious": 2, "benign": 1, "unknown": 0}
_RANK_VERDICT = {v: k for k, v in _VERDICT_RANK.items()}


def _aggregate(ioc: str, kind: str, records: list[dict]) -> dict:
    """Merge per-provider records for one IOC into a single reputation summary."""
    usable = [r for r in records if not r.get("error")]
    errored = [r for r in records if r.get("error")]
    if not usable:
        consensus = "unknown"
    else:
        consensus = _RANK_VERDICT[max(_VERDICT_RANK[r["verdict"]] for r in usable)]
    mal = [r["provider"] for r in usable if r["verdict"] == "malicious"]
    sus = [r["provider"] for r in usable if r["verdict"] == "suspicious"]
    scores = [r["score"] for r in usable if isinstance(r.get("score"), (int, float))]
    # conflict = at least one malicious/suspicious AND at least one benign
    benign = [r["provider"] for r in usable if r["verdict"] == "benign"]
    conflict = bool((mal or sus) and benign)
    return {
        "ioc": ioc,
        "kind": kind,
        "consensus_verdict": consensus,
        "max_score": max(scores) if scores else None,
        "malicious_sources": mal,
        "suspicious_sources": sus,
        "benign_sources": benign,
        "conflict": conflict,
        "providers_queried": [r["provider"] for r in records],
        "providers_errored": [
            {"provider": r["provider"], "error": r["error"]} for r in errored
        ],
        "any_cached": any(r.get("cached") for r in records),
        "records": records,
    }


def enrich_indicators(values: list[str], use_cache: bool = True) -> dict:
    """Enrich a list of indicator strings; return per-IOC reputation summaries.

    External IPs only (RFC1918/internal dropped by the extractor). Each enabled
    provider is cache-checked, rate-limited, queried, and normalized; results are
    deterministically aggregated. READ-ONLY.
    """
    extracted = extract_iocs(values)
    cache = TiCache(cfg.CACHE_DB)
    providers = enabled_providers()
    summaries = []

    typed = (
        [(ip, "ip") for ip in extracted["ips"]]
        + [(d, "domain") for d in extracted["domains"]]
        + [(h, "hash") for h in extracted["hashes"]]
    )

    for ioc, kind in typed:
        records = []
        for pname, tier, fn in providers:
            cached = cache.get(ioc, pname) if use_cache else None
            if cached is not None:
                records.append(cached)
                continue
            rec = fn(ioc, kind)
            # cache only clean (non-error) results; feeds use feed_ttl
            if not rec.get("error"):
                ttl = cfg.FEED_TTL_SECONDS if tier == "keyless" else cfg.TTL_SECONDS
                cache.put(ioc, pname, rec, ttl)
            records.append(rec)
        summaries.append(_aggregate(ioc, kind, records))

    return {
        "extracted": extracted,
        "dropped_internal_or_invalid": extracted["dropped"],
        "enabled_providers": [p[0] for p in providers],
        "summaries": summaries,
        "cache_stats": cache.stats(),
    }
