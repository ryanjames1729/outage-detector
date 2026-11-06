"""Run checks, track state between runs, and decide when to alert.

Alerting rules (per service):

* A service must fail ``failure_threshold`` checks in a row before a DOWN /
  DEGRADED alert is sent. This filters out one-off network blips.
* While it stays broken, a reminder is sent every ``reminder_minutes``
  (0 disables reminders).
* If it gets worse (degraded -> down) or better (down -> degraded) for
  ``failure_threshold`` checks in a row, an update is sent.
* After ``recovery_threshold`` successful checks in a row, a RECOVERED alert is
  sent with the total outage duration.
* ``unknown`` results (e.g. a vendor status page we couldn't reach) never
  count as an outage. If a check stays unknown for ``unknown_alert_minutes``
  a single "can't check" email is sent so monitoring doesn't silently stop.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .checks import DEGRADED, DOWN, UNKNOWN, UP, CheckResult, run_check
from .config import Config

log = logging.getLogger("outage_detector")


@dataclass
class Event:
    kind: str  # down | degraded | changed | reminder | recovered | unknown
    service: dict
    result: CheckResult
    status: str
    since: datetime | None = None
    now: datetime | None = None

    @property
    def duration(self) -> str:
        if not (self.since and self.now):
            return ""
        return format_duration((self.now - self.since).total_seconds())


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    parts = [f"{d}d" if d else "", f"{h}h" if h else "", f"{m}m" if m or not (d or h) else ""]
    return " ".join(p for p in parts if p)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


class StateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data: dict[str, dict] = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError) as e:
                log.warning("Could not read state file %s (%s); starting fresh",
                            self.path, e)

    def get(self, sid: str) -> dict:
        return self.data.setdefault(sid, {
            "confirmed": UP, "fail_streak": 0, "ok_streak": 0,
            "last_effective": UP, "same_streak": 0,
            "since": None, "last_alert": None,
        })

    def save(self, keep_ids: set[str]) -> None:
        # Drop services that were removed from the config.
        self.data = {k: v for k, v in self.data.items() if k in keep_ids}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-")
        with os.fdopen(fd, "w") as f:
            json.dump(self.data, f, indent=2, sort_keys=True)
        os.replace(tmp, self.path)


def evaluate(svc: dict, result: CheckResult, st: dict, cfg: Config,
             now: datetime) -> Event | None:
    """Update one service's state with a new result; return an Event to alert on."""
    s = cfg.settings
    fail_threshold = svc.get("failure_threshold", s.failure_threshold)
    ok_threshold = svc.get("recovery_threshold", s.recovery_threshold)
    reminder = svc.get("reminder_minutes", s.reminder_minutes)
    alert_degraded = svc.get("alert_on_degraded", s.alert_on_degraded)

    st["last_checked"] = _iso(now)
    st["last_status"] = result.status
    st["last_summary"] = result.summary
    if result.status == UNKNOWN:
        # Can't tell whether it's up. Never treat that as an outage, but if the
        # check stays broken (expired credentials, moved status page...) say so
        # once, so monitoring doesn't silently stop.
        if not st.get("unknown_since"):
            st["unknown_since"] = _iso(now)
        limit = svc.get("unknown_alert_minutes", s.unknown_alert_minutes)
        since = _dt(st["unknown_since"])
        if limit and not st.get("unknown_alerted") and \
                (now - since).total_seconds() >= limit * 60:
            st["unknown_alerted"] = True
            return Event("unknown", svc, result, UNKNOWN, since, now)
        return None
    st["unknown_since"], st["unknown_alerted"] = None, False

    effective = result.status
    if effective == DEGRADED and not alert_degraded:
        effective = UP

    st["same_streak"] = st["same_streak"] + 1 if effective == st["last_effective"] else 1
    st["last_effective"] = effective

    if effective == UP:
        st["fail_streak"] = 0
        st["ok_streak"] += 1
        if st["confirmed"] != UP and st["ok_streak"] >= ok_threshold:
            ev = Event("recovered", svc, result, UP, _dt(st["since"]), now)
            st.update(confirmed=UP, since=None, last_alert=None)
            return ev
        return None

    st["ok_streak"] = 0
    st["fail_streak"] += 1
    if st["fail_streak"] == 1 and st["confirmed"] == UP:
        st["since"] = _iso(now)  # outage clock starts at the first failure

    if st["fail_streak"] < fail_threshold:
        return None
    if st["confirmed"] == UP:
        st.update(confirmed=effective, last_alert=_iso(now))
        return Event(effective, svc, result, effective, _dt(st["since"]), now)
    if st["confirmed"] != effective and st["same_streak"] >= fail_threshold:
        st.update(confirmed=effective, last_alert=_iso(now))
        return Event("changed", svc, result, effective, _dt(st["since"]), now)
    last = _dt(st["last_alert"])
    if reminder and last and (now - last).total_seconds() >= reminder * 60:
        st["last_alert"] = _iso(now)
        return Event("reminder", svc, result, st["confirmed"], _dt(st["since"]), now)
    return None


def check_all(cfg: Config) -> list[tuple[dict, CheckResult]]:
    with ThreadPoolExecutor(max_workers=cfg.settings.max_workers) as pool:
        results = list(pool.map(run_check, cfg.services))
    return list(zip(cfg.services, results))


ICON = {UP: "OK  ", DEGRADED: "WARN", DOWN: "DOWN", UNKNOWN: "????"}


def run_once(cfg: Config, *, send: bool = True, now: datetime | None = None,
             notifier=None) -> list[Event]:
    """One monitoring pass: check everything, update state, send alerts."""
    from .notifier import send_events

    now = now or datetime.now(timezone.utc)
    store = StateStore(cfg.settings.state_file)
    events: list[Event] = []
    for svc, result in check_all(cfg):
        log.info("[%s] %-28s %s", ICON[result.status], svc["name"], result.summary)
        ev = evaluate(svc, result, store.get(svc["id"]), cfg, now)
        if ev:
            log.warning("ALERT %s: %s", ev.kind.upper(), svc["name"])
            events.append(ev)

    if events and send:
        try:
            (notifier or send_events)(cfg, events)
        except Exception:
            # Don't save state, so the alert is retried on the next run.
            log.exception("Failed to send alert email; will retry next run")
            raise
    store.save({s["id"] for s in cfg.services})
    return events
