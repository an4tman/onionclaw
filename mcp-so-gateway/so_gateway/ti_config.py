"""TI enrichment configuration: which providers are enabled, TTLs, rate limits.

The ENABLED-PROVIDER SET is the privacy/cost throttle (kb/security/threat-intel-
enrichment): each lookup ships an IOC to a third party, so a provider is only
ever spawned when it is enabled. Keyed providers self-disable when their key is
absent from the environment, so dropping a key from ti.env is sufficient to turn
a provider off.

Env vars (keyed providers read keys from the mounted ti.env, mirroring so.env):
  TI_OTX_API_KEY          AlienVault OTX            (IP, domain)
  TI_ABUSEIPDB_API_KEY    AbuseIPDB                 (IP)
  TI_VT_API_KEY           VirusTotal v3             (IP, domain, hash)

Keyless feeds (no env needed; toggle off with TI_ENABLE_FEEDS=false):
  Tor exit list · Feodo Tracker C2 · Spamhaus DROP · DShield/ISC · blocklist.de

Tunables:
  TI_CACHE_DB             default /data/ti-cache.sqlite
  TI_TTL_SECONDS          default 21600 (6h) for keyed-provider records
  TI_FEED_TTL_SECONDS     default 21600 (6h) for the keyless feed snapshots
  TI_ENABLE_FEEDS         default true
  TI_HTTP_TIMEOUT         default 20 (seconds per request)
"""

import os


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        v = int(raw)
        return v if v > 0 else default
    except ValueError:
        return default


# A polite User-Agent for the keyless feeds (DShield asks for contact info).
# Set TI_USER_AGENT in ti.env to include a real contact for your deployment.
USER_AGENT = os.environ.get(
    "TI_USER_AGENT", "soc-agent-suite-ti-enrichment/1.0 (set TI_USER_AGENT)"
)

CACHE_DB = os.environ.get("TI_CACHE_DB", "/data/ti-cache.sqlite")
TTL_SECONDS = _int("TI_TTL_SECONDS", 6 * 3600)
FEED_TTL_SECONDS = _int("TI_FEED_TTL_SECONDS", 6 * 3600)
HTTP_TIMEOUT = _int("TI_HTTP_TIMEOUT", 20)
ENABLE_FEEDS = _bool("TI_ENABLE_FEEDS", True)

OTX_API_KEY = os.environ.get("TI_OTX_API_KEY") or None
ABUSEIPDB_API_KEY = os.environ.get("TI_ABUSEIPDB_API_KEY") or None
VT_API_KEY = os.environ.get("TI_VT_API_KEY") or None
