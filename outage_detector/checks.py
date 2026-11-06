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
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
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


def _norm(name: str) -> str:
    """Case/whitespace-insensitive and ignore ™ ® © so config names are easy to type."""
    return " ".join(re.sub(r"[™®©]", "", name).lower().split())


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

    wanted = {_norm(c) for c in svc.get("components", [])}
    details: list[str] = []
    watched_ids: set[str] = set()
    if wanted:
        all_comps = data.get("components", [])
        matched = [c for c in all_comps if _norm(c.get("name", "")) in wanted]
        missing = wanted - {_norm(c["name"]) for c in matched}
        if missing:
            details.append("Components not found on status page: "
                           + ", ".join(sorted(missing)))
        # Naming a group (e.g. "School Websites") watches everything in it, so
        # the alert names the exact product that's affected.
        comps = []
        for c in matched:
            children = c.get("components") if c.get("group") else None
            if children:
                comps += [x for x in all_comps if x.get("id") in children]
            else:
                comps.append(c)
        comps = list({id(c): c for c in comps}.values())  # de-duplicate
        watched_ids = {c["id"] for c in comps if "id" in c}
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
        affected = {c.get("id") for c in inc.get("components", [])}
        if watched_ids and affected and not affected & watched_ids:
            continue  # incident is about components we don't watch
        line = f"Incident: {inc.get('name')} [{inc.get('status')}, impact {inc.get('impact')}]"
        if inc.get("shortlink"):
            line += f" {inc['shortlink']}"
        details.append(line)
    details.append(f"Status page: {base if not base.endswith('.json') else api}")
    return CheckResult(status, summary, details)


_GOOGLE_IMPACT = {"SERVICE_OUTAGE": DOWN, "SERVICE_DISRUPTION": DEGRADED,
                  "SERVICE_INFORMATION": UP}
GOOGLE_WORKSPACE_FEED = "https://www.google.com/appsstatus/dashboard/incidents.json"
GOOGLE_CLOUD_FEED = "https://status.cloud.google.com/incidents.json"


def _check_google(svc: dict, default_url: str, page: str) -> CheckResult:
    url = svc.get("url", default_url)
    try:
        incidents = _fetch_json(url, svc.get("timeout", 15))
    except Exception as e:  # noqa: BLE001
        return CheckResult(UNKNOWN, f"Could not read Google status feed: "
                                    f"{_describe_error(e)}", [url])
    wanted = {_norm(p) for p in svc.get("products", [])}
    statuses, details = [], []
    for inc in incidents:
        if inc.get("end"):
            continue  # resolved
        products = [p.get("title", "") for p in inc.get("affected_products", [])]
        if wanted and not any(_norm(p) in wanted for p in products):
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
    details.append(f"Status page: {page}")
    return CheckResult(status, summary, details)


def check_google_workspace(svc: dict) -> CheckResult:
    """Google Workspace Status Dashboard (Gmail, Drive, Classroom, Meet...)."""
    return _check_google(svc, GOOGLE_WORKSPACE_FEED,
                         "https://www.google.com/appsstatus/dashboard/")


def check_google_cloud(svc: dict) -> CheckResult:
    """Google Cloud status dashboard (Compute Engine, Cloud Storage...)."""
    return _check_google(svc, GOOGLE_CLOUD_FEED, "https://status.cloud.google.com/")


# --------------------------------------------------------------------------
# Microsoft 365 (tenant-specific, via Microsoft Graph service health)
# --------------------------------------------------------------------------

# https://learn.microsoft.com/graph/api/resources/servicehealthissue
_M365_UP = {"serviceOperational", "serviceRestored", "falsePositive", "resolved",
            "resolvedExternal", "mitigated", "mitigatedExternal",
            "postIncidentReviewPublished", "investigationSuspended"}
_M365_DOWN = {"serviceInterruption"}
GRAPH = "https://graph.microsoft.com/v1.0"


def _m365_token(svc: dict) -> str:
    tenant = os.environ.get("M365_TENANT_ID") or svc.get("tenant_id")
    client = os.environ.get("M365_CLIENT_ID") or svc.get("client_id")
    secret = os.environ.get("M365_CLIENT_SECRET")
    if not (tenant and client and secret):
        raise LookupError("Microsoft 365 check needs M365_TENANT_ID, "
                          "M365_CLIENT_ID and M365_CLIENT_SECRET (see README)")
    body = urllib.parse.urlencode({
        "client_id": client, "client_secret": secret, "grant_type": "client_credentials",
        "scope": "https://graph.microsoft.com/.default"}).encode()
    req = urllib.request.Request(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        data=body, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=svc.get("timeout", 15)) as r:
            return json.loads(r.read())["access_token"]
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read() or b"{}").get("error_description", "")
        except ValueError:
            err = ""
        raise RuntimeError(f"Microsoft sign-in failed (HTTP {e.code}): "
                           f"{err.splitlines()[0] if err else ''}") from None


def check_microsoft365(svc: dict) -> CheckResult:
    """Microsoft 365 Service health for *your* tenant (Exchange, Teams,
    SharePoint, OneDrive, Entra ID, Intune...). This is the same data as the
    admin center's Service health page, which is more accurate than any
    public Microsoft status page."""
    try:
        token = _m365_token(svc)
        url = (f"{GRAPH}/admin/serviceAnnouncement/issues"
               "?$filter=isResolved%20eq%20false&$top=100")
        issues: list[dict] = []
        while url:
            code, body, _ = _request(url, timeout=svc.get("timeout", 15), headers={
                "Authorization": f"Bearer {token}", "Accept": "application/json"})
            if code >= 400:
                raise RuntimeError(f"Graph API HTTP {code}: {body[:200]!r}")
            page = json.loads(body)
            issues += page.get("value", [])
            url = page.get("@odata.nextLink")
    except LookupError as e:
        return CheckResult(UNKNOWN, str(e))
    except Exception as e:  # noqa: BLE001
        return CheckResult(UNKNOWN, f"Could not read Microsoft 365 service health: "
                                    f"{_describe_error(e)}")

    wanted = {_norm(s) for s in svc.get("services", [])}
    advisories = svc.get("include_advisories", False)
    statuses, details = [], []
    for i in issues:
        if wanted and _norm(i.get("service", "")) not in wanted:
            continue
        if i.get("classification") != "incident" and not advisories:
            continue
        st = i.get("status", "")
        s = UP if st in _M365_UP else DOWN if st in _M365_DOWN else DEGRADED
        statuses.append(s)
        if s != UP:
            impact = " ".join((i.get("impactDescription") or "").split())[:300]
            details.append(f"{i.get('service')} - {i.get('title')} [{i.get('id')}, "
                           f"{st}]: {impact}")
    status = worst(statuses)
    summary = ("No active incidents" if status == UP
               else f"{len(details)} active incident(s)")
    details.append("Admin center: https://admin.microsoft.com/#/servicehealth")
    return CheckResult(status, summary, details)


# --------------------------------------------------------------------------
# AWS public Health Dashboard
# --------------------------------------------------------------------------

AWS_FEED = "https://health.aws.amazon.com/public/currentevents"
# Status codes used by the public dashboard: 0 resolved, 1 informational,
# 2 degradation, 3 disruption.
_AWS_STATUS = {"0": UP, "1": DEGRADED, "2": DEGRADED, "3": DOWN}


def check_aws(svc: dict) -> CheckResult:
    """AWS Health Dashboard: current public events. Filter with `regions`
    (e.g. "us-east-1") and/or `services` (e.g. "Amazon CloudFront", "ec2")."""
    url = svc.get("url", AWS_FEED)
    try:
        code, body, _ = _request(url, timeout=svc.get("timeout", 15))
        if code >= 400:
            raise RuntimeError(f"HTTP {code}")
        # The feed has been served as UTF-16 at times; handle either encoding.
        if body[:2] in (b"\xff\xfe", b"\xfe\xff"):
            text = body.decode("utf-16")
        else:
            text = body.decode("utf-8-sig")
        events = json.loads(text) if text.strip() else []
    except Exception as e:  # noqa: BLE001
        return CheckResult(UNKNOWN, f"Could not read AWS Health Dashboard: "
                                    f"{_describe_error(e)}", [url])

    regions = {r.lower() for r in svc.get("regions", [])}
    services = {_norm(s) for s in svc.get("services", [])}

    def svc_match(key: str, name: str) -> bool:
        key = key.lower()
        return any(s == _norm(name) or s == key or key.startswith(s + "-")
                   for s in services)

    statuses, details = [], []
    for ev in events:
        key = (ev.get("service") or "").lower()          # e.g. "ec2-us-east-1"
        name = ev.get("service_name") or key
        region = (ev.get("region_name") or "").lower()
        if regions and not any(r in key or r == region for r in regions):
            continue
        status_code = str(ev.get("status"))
        if services and not svc_match(key, name):
            # "Multiple services" events list what's affected separately.
            hits = {k: v for k, v in (ev.get("impacted_services") or {}).items()
                    if svc_match(k, v.get("service_name", ""))}
            if not hits:
                continue
            name = ", ".join(sorted(v.get("service_name", k) for k, v in hits.items()))
            status_code = max((str(v.get("current", "1")) for v in hits.values()),
                              key=lambda c: SEVERITY[_AWS_STATUS.get(c, DEGRADED)])
        summary = ev.get("summary") or ""
        if summary.upper().startswith("[RESOLVED]"):
            continue
        s = _AWS_STATUS.get(status_code, DEGRADED)
        statuses.append(s)
        if s != UP:
            log = ev.get("event_log") or []
            latest = " ".join(str(log[-1].get("message", "")).split())[:400] if log else ""
            line = f"{name} ({ev.get('region_name') or 'global'}): {summary}"
            details.append(f"{line} - latest update: {latest}" if latest else line)
    status = worst(statuses)
    summary = ("No active events" if status == UP
               else f"{len(details)} active event(s)")
    details.append("Status page: https://health.aws.amazon.com/health/status")
    return CheckResult(status, summary, details)


# --------------------------------------------------------------------------
# Status text on an ordinary web page (e.g. ERC Broadband's homepage)
# --------------------------------------------------------------------------

class _SectionText(HTMLParser):
    """Collect the visible text inside the first element with a given class."""

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
            "meta", "source", "track", "wbr"}

    def __init__(self, css_class: str | None):
        super().__init__(convert_charrefs=True)
        self.css_class = css_class
        self.depth = 0 if css_class else 1  # no class: whole page
        self.done = False
        self.skip = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        if tag in self.VOID or self.done:
            return
        if self.depth:
            self.depth += 1
        elif self.css_class in (dict(attrs).get("class") or "").split():
            self.depth = 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1
        if tag in self.VOID or self.done or not self.depth:
            return
        self.depth -= 1
        if self.depth == 0 and self.css_class:
            self.done = True

    def handle_data(self, data):
        if self.depth and not self.done and not self.skip:
            self.parts.append(data)

    def text(self) -> str:
        return " ".join(" ".join(self.parts).split())


def check_page_text(svc: dict) -> CheckResult:
    """Read a status message from a normal web page.

    UP when the section contains one of `ok_text`. Any other message means
    the provider posted something, so it's reported (as `problem_status`,
    default "degraded") with their message quoted in the alert.
    """
    url = svc["url"]
    try:
        code, body, _ = _request(url, timeout=svc.get("timeout", 15))
        if code >= 400:
            raise RuntimeError(f"HTTP {code}")
    except Exception as e:  # noqa: BLE001
        return CheckResult(UNKNOWN, f"Could not load status page: "
                                    f"{_describe_error(e)}", [url])
    parser = _SectionText(svc.get("section_class"))
    parser.feed(body.decode("utf-8", errors="replace"))
    text = parser.text()
    if not text:
        return CheckResult(UNKNOWN, "Status section not found on the page "
                                    "(the site layout may have changed)", [url])
    ok = svc["ok_text"]
    ok = [ok] if isinstance(ok, str) else ok
    if any(o.lower() in text.lower() for o in ok):
        return CheckResult(UP, "Provider reports normal operation", [text[:500], url])
    status = svc.get("problem_status", DEGRADED)
    return CheckResult(status, "Provider has posted a status message",
                       [f'"{text[:800]}"', url])


CHECKS: dict[str, Callable[[dict], CheckResult]] = {
    "http": check_http,
    "tcp": check_tcp,
    "statuspage": check_statuspage,
    "google_workspace": check_google_workspace,
    "google_cloud": check_google_cloud,
    "microsoft365": check_microsoft365,
    "aws": check_aws,
    "page_text": check_page_text,
}


def run_check(svc: dict) -> CheckResult:
    try:
        return CHECKS[svc["type"]](svc)
    except Exception as e:  # noqa: BLE001 - a bug in one check must not kill the run
        return CheckResult(UNKNOWN, f"Check crashed: {_describe_error(e)}")
