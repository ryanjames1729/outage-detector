"""Service checks.

Every check returns a CheckResult whose status is one of:

* ``up``       – working normally
* ``degraded`` – working, but slow or the vendor reports partial problems
* ``down``     – not working
* ``unknown``  – we could not determine the status (e.g. a vendor's status
                 page itself was unreachable). Unknown never triggers alerts
                 on its own, so a flaky status page doesn't page you.
"""

from __future__ import annotations

import json
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from . import __version__

UP, DEGRADED, DOWN, UNKNOWN = "up", "degraded", "down", "unknown"
SEVERITY = {UP: 0, UNKNOWN: 0, DEGRADED: 1, DOWN: 2}
USER_AGENT = f"outage-detector/{__version__}"


@dataclass
class CheckResult:
    status: str
    summary: str
    details: list[str] = field(default_factory=list)
    latency_ms: float | None = None


def worst(statuses: list[str]) -> str:
    return max(statuses, key=lambda s: SEVERITY[s], default=UP)


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------

def _request(url: str, *, method: str = "GET", timeout: float = 10,
             headers: dict[str, str] | None = None, verify_tls: bool = True):
    """Return (status_code, body_bytes, latency_ms). Raises on network errors."""
    req = urllib.request.Request(url, method=method, headers={
        "User-Agent": USER_AGENT, **(headers or {})})
    ctx = None
    if url.lower().startswith("https") and not verify_tls:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            body = resp.read(2_000_000)
            code = resp.status
    except urllib.error.HTTPError as e:  # non-2xx still means "server answered"
        body = e.read(2_000_000) if e.fp else b""
        code = e.code
    return code, body, (time.monotonic() - start) * 1000


def _fetch_json(url: str, timeout: float) -> Any:
    code, body, _ = _request(url, timeout=timeout,
                             headers={"Accept": "application/json"})
    if code >= 400:
        raise RuntimeError(f"HTTP {code} from {url}")
    return json.loads(body)


def _describe_error(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.URLError):
        exc = exc.reason if isinstance(exc.reason, BaseException) else exc
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "timed out"
    if isinstance(exc, ssl.SSLError):
        return f"TLS/SSL error: {exc}"
    if isinstance(exc, socket.gaierror):
        return f"DNS lookup failed: {exc}"
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused"
    return f"{type(exc).__name__}: {exc}"


def _expected_codes(spec: Any) -> Callable[[int], bool]:
    if spec is None:
        return lambda c: 200 <= c < 400
    if isinstance(spec, int):
        return lambda c: c == spec
    return lambda c: c in set(spec)


# --------------------------------------------------------------------------
# Check types
# --------------------------------------------------------------------------

def check_http(svc: dict) -> CheckResult:
    """Probe a URL directly (your SIS, LMS login page, website, VPN portal...)."""
    url = svc["url"]
    try:
        code, body, latency = _request(
            url,
            method=svc.get("method", "GET"),
            timeout=svc.get("timeout", 10),
            headers=svc.get("headers"),
            verify_tls=svc.get("verify_tls", True),
        )
    except Exception as e:  # noqa: BLE001 - any failure to connect = down
        return CheckResult(DOWN, f"Unreachable: {_describe_error(e)}", [url])

    if not _expected_codes(svc.get("expected_status"))(code):
        return CheckResult(DOWN, f"Unexpected HTTP status {code}", [url], latency)

    keyword = svc.get("keyword")
    if keyword and keyword.encode() not in body:
        return CheckResult(DOWN, f"Page loaded (HTTP {code}) but expected text "
                                 f"'{keyword}' was missing", [url], latency)

    slow = svc.get("degraded_latency_ms")
    if slow and latency > slow:
        return CheckResult(DEGRADED, f"Slow response: {latency:.0f} ms "
                                     f"(threshold {slow} ms)", [url], latency)
    return CheckResult(UP, f"HTTP {code} in {latency:.0f} ms", [url], latency)


def check_tcp(svc: dict) -> CheckResult:
    """Confirm a port accepts connections (mail server, RDP gateway, DB...)."""
    host, port = svc["host"], int(svc["port"])
    start = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=svc.get("timeout", 10)):
            pass
    except Exception as e:  # noqa: BLE001
        return CheckResult(DOWN, f"Cannot connect to {host}:{port}: "
                                 f"{_describe_error(e)}")
    latency = (time.monotonic() - start) * 1000
    return CheckResult(UP, f"{host}:{port} accepted connection in "
                           f"{latency:.0f} ms", latency_ms=latency)


_SP_COMPONENT = {
    "operational": UP,
    "under_maintenance": UP,
    "degraded_performance": DEGRADED,
    "partial_outage": DEGRADED,
    "major_outage": DOWN,
}
_SP_INDICATOR = {"none": UP, "maintenance": UP, "minor": DEGRADED,
                 "major": DOWN, "critical": DOWN}


def check_statuspage(svc: dict) -> CheckResult:
    """Read a vendor's Atlassian Statuspage (used by Zoom, Canvas, GitHub,
    Dropbox, Atlassian, Discord, and thousands of other SaaS vendors)."""
    base = svc["url"].rstrip("/")
    api = base if base.endswith(".json") else f"{base}/api/v2/summary.json"
    try:
        data = _fetch_json(api, svc.get("timeout", 15))
    except Exception as e:  # noqa: BLE001
        return CheckResult(UNKNOWN, f"Could not read status page: "
                                    f"{_describe_error(e)}", [api])

    wanted = [c.lower() for c in svc.get("components", [])]
    details: list[str] = []
    if wanted:
        comps = [c for c in data.get("components", [])
                 if c.get("name", "").lower() in wanted]
        missing = set(wanted) - {c["name"].lower() for c in comps}
        if missing:
            details.append("Components not found on status page: "
                           + ", ".join(sorted(missing)))
        statuses = [_SP_COMPONENT.get(c.get("status"), DEGRADED) for c in comps]
        for c in comps:
            if c.get("status") != "operational":
                details.append(f"{c['name']}: {c.get('status', '?').replace('_', ' ')}")
        status = worst(statuses)
        impacted = sum(1 for s in statuses if s != UP)
        summary = ("All monitored components operational" if status == UP
                   else f"{impacted} monitored component(s) impacted")
    else:
        ind = data.get("status", {})
        status = _SP_INDICATOR.get(ind.get("indicator", "none"), DEGRADED)
        summary = ind.get("description") or "No description"

    for inc in data.get("incidents", []):
        if inc.get("status") in ("resolved", "postmortem"):
            continue
        line = f"Incident: {inc.get('name')} [{inc.get('status')}, impact {inc.get('impact')}]"
        if inc.get("shortlink"):
            line += f" {inc['shortlink']}"
        details.append(line)
    details.append(f"Status page: {base if not base.endswith('.json') else api}")
    return CheckResult(status, summary, details)


_GOOGLE_IMPACT = {"SERVICE_OUTAGE": DOWN, "SERVICE_DISRUPTION": DEGRADED,
                  "SERVICE_INFORMATION": UP}
GOOGLE_FEED = "https://www.google.com/appsstatus/dashboard/incidents.json"


def check_google_workspace(svc: dict) -> CheckResult:
    """Read Google's Workspace Status Dashboard (Gmail, Drive, Classroom...)."""
    url = svc.get("url", GOOGLE_FEED)
    try:
        incidents = _fetch_json(url, svc.get("timeout", 15))
    except Exception as e:  # noqa: BLE001
        return CheckResult(UNKNOWN, f"Could not read Google status feed: "
                                    f"{_describe_error(e)}", [url])
    wanted = [p.lower() for p in svc.get("products", [])]
    statuses, details = [], []
    for inc in incidents:
        if inc.get("end"):
            continue  # resolved
        products = [p.get("title", "") for p in inc.get("affected_products", [])]
        if wanted and not any(p.lower() in wanted for p in products):
            continue
        s = _GOOGLE_IMPACT.get(inc.get("status_impact"), DEGRADED)
        statuses.append(s)
        if s != UP:
            desc = (inc.get("most_recent_update") or {}).get("text") \
                or inc.get("external_desc", "")
            desc = " ".join(desc.split())[:300]
            details.append(f"{', '.join(products)}: {desc}")
    status = worst(statuses)
    summary = ("No active incidents" if status == UP
               else f"{len(details)} active incident(s)")
    details.append("Status page: https://www.google.com/appsstatus/dashboard/")
    return CheckResult(status, summary, details)


CHECKS: dict[str, Callable[[dict], CheckResult]] = {
    "http": check_http,
    "tcp": check_tcp,
    "statuspage": check_statuspage,
    "google_workspace": check_google_workspace,
}


def run_check(svc: dict) -> CheckResult:
    try:
        return CHECKS[svc["type"]](svc)
    except Exception as e:  # noqa: BLE001 - a bug in one check must not kill the run
        return CheckResult(UNKNOWN, f"Check crashed: {_describe_error(e)}")
