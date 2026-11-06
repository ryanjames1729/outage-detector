import json
import os
import socket
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from outage_detector import checks, notifier
from outage_detector.checks import DEGRADED, DOWN, UNKNOWN, UP, CheckResult
from outage_detector.config import ConfigError, load_config
from outage_detector.monitor import evaluate, format_duration, run_once

# Responses served by the fake server: path -> (status, body)
ROUTES: dict[str, tuple[int, bytes]] = {}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        code, body = ROUTES.get(self.path, (404, b"not found"))
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def statuspage(indicator="none", components=(), incidents=()):
    return json.dumps({
        "status": {"indicator": indicator, "description": f"indicator {indicator}"},
        "components": [{"name": n, "status": s} for n, s in components],
        "incidents": list(incidents),
    }).encode()


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        ROUTES.clear()


class TestHttpCheck(ServerTest):
    def test_up(self):
        ROUTES["/"] = (200, b"Welcome to school")
        r = checks.check_http({"url": self.base + "/", "keyword": "Welcome"})
        self.assertEqual(r.status, UP)

    def test_missing_keyword(self):
        ROUTES["/"] = (200, b"Maintenance page")
        r = checks.check_http({"url": self.base + "/", "keyword": "Welcome"})
        self.assertEqual(r.status, DOWN)
        self.assertIn("Welcome", r.summary)

    def test_500(self):
        ROUTES["/"] = (503, b"oops")
        self.assertEqual(checks.check_http({"url": self.base + "/"}).status, DOWN)

    def test_expected_status(self):
        ROUTES["/x"] = (404, b"")
        self.assertEqual(checks.check_http(
            {"url": self.base + "/x", "expected_status": [404]}).status, UP)

    def test_unreachable(self):
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        r = checks.check_http({"url": f"http://127.0.0.1:{port}/", "timeout": 2})
        self.assertEqual(r.status, DOWN)
        self.assertIn("refused", r.summary)

    def test_slow_is_degraded(self):
        ROUTES["/"] = (200, b"ok")
        r = checks.check_http({"url": self.base + "/", "degraded_latency_ms": -1})
        self.assertEqual(r.status, DEGRADED)


class TestTcpCheck(ServerTest):
    def test_tcp(self):
        port = self.server.server_address[1]
        self.assertEqual(checks.check_tcp({"host": "127.0.0.1", "port": port}).status, UP)
        s = socket.socket(); s.bind(("127.0.0.1", 0)); free = s.getsockname()[1]; s.close()
        self.assertEqual(checks.check_tcp(
            {"host": "127.0.0.1", "port": free, "timeout": 2}).status, DOWN)


class TestStatuspage(ServerTest):
    def test_indicator_mapping(self):
        for ind, want in [("none", UP), ("minor", DEGRADED), ("major", DOWN),
                          ("critical", DOWN), ("maintenance", UP)]:
            ROUTES["/api/v2/summary.json"] = (200, statuspage(ind))
            self.assertEqual(checks.check_statuspage({"url": self.base}).status, want, ind)

    def test_components_filter(self):
        ROUTES["/api/v2/summary.json"] = (200, statuspage("major", components=[
            ("Canvas", "operational"), ("Studio", "major_outage")]))
        r = checks.check_statuspage({"url": self.base, "components": ["canvas"]})
        self.assertEqual(r.status, UP)
        r = checks.check_statuspage({"url": self.base, "components": ["Studio", "Canvas"]})
        self.assertEqual(r.status, DOWN)
        self.assertIn("Studio: major outage", r.details)

    def test_incidents_listed(self):
        ROUTES["/api/v2/summary.json"] = (200, statuspage("minor", incidents=[
            {"name": "Slow logins", "status": "investigating", "impact": "minor",
             "shortlink": "https://stspg.io/abc"},
            {"name": "Old", "status": "resolved", "impact": "minor"}]))
        r = checks.check_statuspage({"url": self.base})
        self.assertTrue(any("Slow logins" in d and "stspg.io" in d for d in r.details))
        self.assertFalse(any("Old" in d for d in r.details))

    def test_status_page_unreachable_is_unknown(self):
        r = checks.check_statuspage({"url": self.base + "/missing"})
        self.assertEqual(r.status, UNKNOWN)


class TestGoogle(ServerTest):
    def test_feed(self):
        feed = [
            {"end": None, "status_impact": "SERVICE_OUTAGE",
             "affected_products": [{"title": "Gmail"}], "external_desc": "Gmail is down",
             "most_recent_update": {"text": "We are investigating"}},
            {"end": None, "status_impact": "SERVICE_DISRUPTION",
             "affected_products": [{"title": "Google Chat"}], "external_desc": "x"},
            {"end": "2026-01-01T00:00:00Z", "status_impact": "SERVICE_OUTAGE",
             "affected_products": [{"title": "Google Drive"}], "external_desc": "old"},
        ]
        ROUTES["/g.json"] = (200, json.dumps(feed).encode())
        url = self.base + "/g.json"
        r = checks.check_google_workspace({"url": url, "products": ["Google Drive"]})
        self.assertEqual(r.status, UP)
        r = checks.check_google_workspace({"url": url, "products": ["Google Chat"]})
        self.assertEqual(r.status, DEGRADED)
        r = checks.check_google_workspace({"url": url, "products": ["gmail", "Google Chat"]})
        self.assertEqual(r.status, DOWN)
        self.assertIn("We are investigating", r.details[0])
        self.assertEqual(checks.check_google_workspace({"url": url}).status, DOWN)


def make_config(tmp: Path, services: str, failure_threshold: int = 2) -> Path:
    p = tmp / "config.toml"
    p.write_text(f"""
[settings]
state_file = "{tmp / 'state.json'}"
failure_threshold = {failure_threshold}
recovery_threshold = 2
reminder_minutes = 30

[email]
host = "smtp.test"
to = ["it@example.org"]
from = "alerts@example.org"
{services}
""")
    return p


SVC = '[[service]]\nname = "Canvas LMS"\ntype = "http"\nurl = "http://x"\n'


class TestConfig(unittest.TestCase):
    def test_load_and_env_override(self):
        with tempfile.TemporaryDirectory() as d:
            p = make_config(Path(d), SVC)
            with mock.patch.dict(os.environ, {"ALERT_TO": "a@x.org, b@x.org",
                                              "SMTP_PASSWORD": "secret"}):
                cfg = load_config(p)
            self.assertEqual(cfg.email.to, ["a@x.org", "b@x.org"])
            self.assertEqual(cfg.email.password, "secret")
            self.assertEqual(cfg.services[0]["id"], "canvas-lms")

    def test_errors(self):
        with tempfile.TemporaryDirectory() as d:
            for bad in ['[[service]]\nname="A"\ntype="nope"\n',
                        '[[service]]\nname="A"\ntype="http"\n',
                        SVC + SVC]:
                with self.assertRaises(ConfigError):
                    load_config(make_config(Path(d), bad))

    def test_repo_config_is_valid(self):
        cfg = load_config(Path(__file__).parent.parent / "config.toml")
        self.assertTrue(cfg.services)


class TestStateMachine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(make_config(Path(self.tmp.name), SVC))
        self.svc = self.cfg.services[0]
        self.st = {"confirmed": UP, "fail_streak": 0, "ok_streak": 0,
                   "last_effective": UP, "same_streak": 0, "since": None, "last_alert": None}
        self.t = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.tmp.cleanup()

    def step(self, status, minutes=5):
        self.t += timedelta(minutes=minutes)
        ev = evaluate(self.svc, CheckResult(status, "s"), self.st, self.cfg, self.t)
        return ev.kind if ev else None

    def test_full_cycle(self):
        self.assertIsNone(self.step(DOWN))               # 1st failure: wait
        self.assertEqual(self.step(DOWN), "down")        # 2nd: alert
        self.assertIsNone(self.step(DOWN))               # no spam
        self.assertIsNone(self.step(UNKNOWN))            # unknown ignored
        self.assertIsNone(self.step(UP))                 # 1 good check
        self.assertIsNone(self.step(DOWN))               # flap resets recovery
        self.assertIsNone(self.step(DOWN))
        self.assertEqual(self.step(DOWN), "reminder")    # 30 min after alert
        self.assertIsNone(self.step(UP))
        ev = evaluate(self.svc, CheckResult(UP, "ok"), self.st, self.cfg,
                      self.t + timedelta(minutes=5))
        self.assertEqual(ev.kind, "recovered")
        self.assertEqual(ev.duration, "45m")             # since first failure
        self.assertEqual(self.st["confirmed"], UP)

    def test_single_blip_never_alerts(self):
        for s in [DOWN, UP, DOWN, UP, DOWN, UP]:
            self.assertIsNone(self.step(s))

    def test_escalation(self):
        self.step(DEGRADED)
        self.assertEqual(self.step(DEGRADED), "degraded")
        self.assertIsNone(self.step(DOWN))
        self.assertEqual(self.step(DOWN), "changed")
        self.assertEqual(self.st["confirmed"], DOWN)

    def test_degraded_ignored_when_disabled(self):
        self.svc["alert_on_degraded"] = False
        for _ in range(5):
            self.assertIsNone(self.step(DEGRADED))

    def test_format_duration(self):
        self.assertEqual(format_duration(30), "0m")
        self.assertEqual(format_duration(3720), "1h 2m")
        self.assertEqual(format_duration(90000), "1d 1h")


class TestEndToEnd(ServerTest):
    def test_run_once_emails_and_persists(self):
        with tempfile.TemporaryDirectory() as d:
            svc = (f'[[service]]\nname = "Site"\ntype = "http"\nurl = "{self.base}/"\n'
                   f'[[service]]\nname = "Vendor"\ntype = "statuspage"\nurl = "{self.base}"\n'
                   'notify = ["vendor-owner@example.org"]\n')
            cfg = load_config(make_config(Path(d), svc))
            ROUTES["/"] = (500, b"")
            ROUTES["/api/v2/summary.json"] = (200, statuspage("major"))

            with mock.patch("smtplib.SMTP") as smtp:
                self.assertEqual(run_once(cfg), [])
                events = run_once(cfg)
                self.assertEqual({e.kind for e in events}, {"down"})
                server = smtp.return_value
                sent = [c.args[0] for c in server.send_message.call_args_list]
                self.assertEqual(len(sent), 2)  # Vendor has an extra recipient
                subjects = sorted(m["Subject"] for m in sent)
                self.assertIn("DOWN: Site", subjects[0] + subjects[1])
                server.starttls.assert_called()

            state = json.loads((Path(d) / "state.json").read_text())
            self.assertEqual(state["site"]["confirmed"], DOWN)

            ROUTES["/"] = (200, b"ok")
            ROUTES["/api/v2/summary.json"] = (200, statuspage("none"))
            with mock.patch("smtplib.SMTP"):
                run_once(cfg)
                events = run_once(cfg)
            self.assertEqual([e.kind for e in events], ["recovered", "recovered"])

    def test_failed_email_is_retried(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = load_config(make_config(Path(d),
                f'[[service]]\nname = "Site"\ntype = "http"\nurl = "{self.base}/"\n',
                failure_threshold=1))
            ROUTES["/"] = (500, b"")
            with mock.patch("smtplib.SMTP", side_effect=OSError("smtp down")):
                with self.assertRaises(OSError):
                    run_once(cfg)
            with mock.patch("smtplib.SMTP"):
                self.assertEqual([e.kind for e in run_once(cfg)], ["down"])


class TestEmailFormatting(unittest.TestCase):
    def test_subject_and_bodies(self):
        now = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
        from outage_detector.monitor import Event
        evs = [
            Event("down", {"name": "Zoom", "description": "Video calls"},
                  CheckResult(DOWN, "Major outage", ["See https://status.zoom.us"]),
                  DOWN, now - timedelta(minutes=10), now),
            Event("recovered", {"name": "Gmail"}, CheckResult(UP, "fine"), UP,
                  now - timedelta(hours=2), now),
        ]
        cfg = load_config(Path(__file__).parent.parent / "config.toml").email
        subj = notifier.build_subject(cfg, evs)
        self.assertEqual(subj, "[Outage Detector] DOWN: Zoom | RECOVERED: Gmail")
        text = notifier.build_text(evs)
        self.assertIn("Gmail has RECOVERED (outage lasted 2h)", text)
        html = notifier.build_html(evs)
        self.assertIn('<a href="https://status.zoom.us">', html)
        self.assertIn("Video calls", html)


if __name__ == "__main__":
    unittest.main()
