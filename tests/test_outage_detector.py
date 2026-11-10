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
            with mock.patch("smtplib.SMTP", side_effect=OSError("smtp down")), \
                 mock.patch("time.sleep"):
                with self.assertRaises(notifier.EmailError):
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


class TestStatuspageGroups(ServerTest):
    """Mirrors the real structure of status.blackbaud.com / status.finalsite.com."""

    def setUp(self):
        super().setUp()
        comps = [
            {"id": "g1", "name": "Blackbaud Education Management Solutions",
             "status": "partial_outage", "group": True, "components": ["c1", "c2"]},
            {"id": "c1", "name": "Blackbaud Student Information System\u2122",
             "status": "partial_outage", "group": False, "group_id": "g1"},
            {"id": "c2", "name": "Core Functionality", "status": "operational",
             "group": False, "group_id": "g1"},
            {"id": "c3", "name": "JustGiving\u00ae from Blackbaud\u00ae",
             "status": "major_outage", "group": False},
        ]
        incidents = [
            {"name": "SIS slow", "status": "investigating", "impact": "minor",
             "components": [{"id": "c1"}]},
            {"name": "JustGiving down", "status": "investigating", "impact": "major",
             "components": [{"id": "c3"}]},
        ]
        ROUTES["/api/v2/summary.json"] = (200, json.dumps(
            {"status": {"indicator": "major"}, "components": comps,
             "incidents": incidents}).encode())

    def test_group_expands_to_children(self):
        r = checks.check_statuspage({"url": self.base, "components": [
            "blackbaud education management solutions"]})
        self.assertEqual(r.status, DEGRADED)
        self.assertIn("Blackbaud Student Information System\u2122: partial outage", r.details)
        self.assertTrue(any("SIS slow" in d for d in r.details))
        # Incidents about products we don't watch are left out.
        self.assertFalse(any("JustGiving" in d for d in r.details))

    def test_trademark_symbols_optional(self):
        r = checks.check_statuspage({"url": self.base, "components": [
            "Blackbaud Student Information System", "Core Functionality"]})
        self.assertEqual(r.status, DEGRADED)
        self.assertFalse(any("not found" in d for d in r.details))
        r = checks.check_statuspage({"url": self.base, "components": ["JustGiving from Blackbaud"]})
        self.assertEqual(r.status, DOWN)


class TestGoogleCloud(ServerTest):
    def test_same_feed_format(self):
        ROUTES["/c.json"] = (200, json.dumps([{"end": None, "status_impact": "SERVICE_OUTAGE",
            "affected_products": [{"title": "Google Compute Engine"}],
            "external_desc": "GCE down"}]).encode())
        r = checks.check_google_cloud({"url": self.base + "/c.json"})
        self.assertEqual(r.status, DOWN)
        self.assertIn("status.cloud.google.com", r.details[-1])


class TestAws(ServerTest):
    EVENTS = [
        {"service": "ec2-us-east-1", "service_name": "Amazon Elastic Compute Cloud",
         "region_name": "N. Virginia", "status": "3", "summary": "Instances unreachable"},
        {"service": "s3-us-west-2", "service_name": "Amazon Simple Storage Service",
         "region_name": "Oregon", "status": "2", "summary": "Elevated errors"},
        {"service": "cloudfront", "service_name": "Amazon CloudFront",
         "region_name": "", "status": "1", "summary": "[RESOLVED] Latency"},
    ]

    def test_filters_and_utf16(self):
        ROUTES["/aws"] = (200, json.dumps(self.EVENTS).encode("utf-16"))
        url = self.base + "/aws"
        r = checks.check_aws({"url": url, "regions": ["us-east-1"]})
        self.assertEqual(r.status, DOWN)
        self.assertIn("Instances unreachable", r.details[0])
        r = checks.check_aws({"url": url, "regions": ["us-west-2"]})
        self.assertEqual(r.status, DEGRADED)
        r = checks.check_aws({"url": url, "services": ["Amazon CloudFront"]})
        self.assertEqual(r.status, UP)  # only a resolved event
        r = checks.check_aws({"url": url, "services": ["ec2"]})
        self.assertEqual(r.status, DOWN)

    def test_multiple_services_event(self):
        # Shape taken from the live feed.
        ev = [{"service": "multipleservices-us-east-1", "service_name": "Multiple services",
               "region_name": "N. Virginia", "status": "3", "summary": "Region Availability",
               "event_log": [{"message": "old"}, {"message": "We are investigating  errors."}],
               "impacted_services": {
                   "ec2-us-east-1": {"service_name": "Amazon Elastic Compute Cloud", "current": "3"},
                   "cloudfront-us-east-1": {"service_name": "Amazon CloudFront", "current": "1"}}}]
        ROUTES["/aws"] = (200, b"\xfe\xff" + json.dumps(ev).encode("utf-16-be"))
        url = self.base + "/aws"
        r = checks.check_aws({"url": url, "services": ["ec2"]})
        self.assertEqual(r.status, DOWN)
        self.assertIn("Amazon Elastic Compute Cloud (N. Virginia)", r.details[0])
        self.assertIn("latest update: We are investigating errors.", r.details[0])
        r = checks.check_aws({"url": url, "services": ["Amazon CloudFront"]})
        self.assertEqual(r.status, DEGRADED)
        self.assertEqual(checks.check_aws({"url": url, "services": ["Amazon S3"]}).status, UP)
        self.assertEqual(checks.check_aws({"url": url, "regions": ["us-west-2"]}).status, UP)

    def test_empty_feed(self):
        ROUTES["/aws"] = (200, b"")
        self.assertEqual(checks.check_aws({"url": self.base + "/aws"}).status, UP)
        ROUTES["/aws"] = (200, b"[]")
        self.assertEqual(checks.check_aws({"url": self.base + "/aws"}).status, UP)


class TestUnknownAlert(unittest.TestCase):
    def test_alerts_once_when_check_keeps_failing(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = load_config(make_config(Path(d), SVC))
            svc, st = cfg.services[0], {"confirmed": UP, "fail_streak": 0, "ok_streak": 0,
                                        "last_effective": UP, "same_streak": 0,
                                        "since": None, "last_alert": None}
            t = datetime(2026, 11, 6, tzinfo=timezone.utc)
            kinds = []
            for i in range(30):  # 30 x 5 min = 150 min
                ev = evaluate(svc, CheckResult(UNKNOWN, "?"), st, cfg, t + timedelta(minutes=5 * i))
                kinds.append(ev.kind if ev else None)
            self.assertEqual([k for k in kinds if k], ["unknown"])
            self.assertEqual(kinds.index("unknown"), 24)  # after 120 minutes
            evaluate(svc, CheckResult(UP, "ok"), st, cfg, t + timedelta(hours=3))
            self.assertFalse(st["unknown_alerted"])
            self.assertEqual(st["confirmed"], UP)


class TestPageText(ServerTest):
    PAGE = """<html><head><style>.network-status{color:red}</style></head><body>
    <div class="footer"><p>fully operational elsewhere</p></div>
    <div class="network-status"><div class="vertical-line"></div>
      <div class="inner-content"><h2>NETWORK STATUS</h2><br>
      <p>As of November 06, 2026</p><p>%s</p></div></div>
    <div class="copyright">&reg; ERC</div></body></html>"""

    def svc(self):
        return {"url": self.base + "/", "section_class": "network-status",
                "ok_text": "fully operational"}

    def test_operational(self):
        ROUTES["/"] = (200, (self.PAGE % "All ERC network systems are fully operational.").encode())
        r = checks.check_page_text(self.svc())
        self.assertEqual(r.status, UP)
        self.assertNotIn("ERC</div>", r.details[0])
        self.assertNotIn("\u00ae", r.details[0])  # stopped at the end of the section

    def test_changed_message_is_reported(self):
        ROUTES["/"] = (200, (self.PAGE % "Fiber cut near Asheville, crews dispatched.").encode())
        r = checks.check_page_text(self.svc())
        self.assertEqual(r.status, DEGRADED)
        self.assertIn("Fiber cut near Asheville", r.details[0])
        r = checks.check_page_text({**self.svc(), "problem_status": "down"})
        self.assertEqual(r.status, DOWN)

    def test_layout_change_is_unknown(self):
        ROUTES["/"] = (200, b"<html><body>redesigned</body></html>")
        self.assertEqual(checks.check_page_text(self.svc()).status, UNKNOWN)


class TestSmtpDiagnostics(unittest.TestCase):
    def cfg(self, **kw):
        from outage_detector.config import EmailConfig
        base = dict(host="smtp.gmail.com", port=587, security="starttls",
                    username="alerts@example.org", password="abcdefghijklmnop",
                    sender="alerts@example.org", to=["it@example.org"])
        return EmailConfig(**{**base, **kw})

    def test_gmail_disconnect_falls_back_to_465(self):
        import smtplib
        with mock.patch("smtplib.SMTP") as plain, mock.patch("smtplib.SMTP_SSL") as tls, \
             mock.patch("time.sleep"):
            plain.return_value.login.side_effect = smtplib.SMTPServerDisconnected(
                "Connection unexpectedly closed")
            plain.return_value.esmtp_features = {"auth": " LOGIN PLAIN"}
            notifier.send_email(self.cfg(), ["it@example.org"], "s", "t")
        self.assertEqual(plain.call_count, 2)          # tried 587 twice
        self.assertEqual(tls.call_args.args[:2], ("smtp.gmail.com", 465))
        tls.return_value.send_message.assert_called_once()

    def test_all_attempts_fail_gives_readable_explanation(self):
        import smtplib
        with mock.patch("smtplib.SMTP") as plain, mock.patch("smtplib.SMTP_SSL") as tls, \
             mock.patch("time.sleep"):
            for m in (plain, tls):
                m.return_value.login.side_effect = smtplib.SMTPServerDisconnected("closed")
                m.return_value.esmtp_features = {}
            with self.assertRaises(notifier.EmailError) as ctx:
                notifier.send_email(self.cfg(password="hunter2"), ["x@y.z"], "s", "t")
        msg = str(ctx.exception)
        self.assertIn("failed at: login", msg)
        self.assertIn("smtp.gmail.com:465", msg)
        self.assertIn("does NOT look like one", msg)
        self.assertNotIn("hunter2", msg)

    def test_bad_password_is_not_retried(self):
        import smtplib
        with mock.patch("smtplib.SMTP") as plain, mock.patch("time.sleep"):
            plain.return_value.login.side_effect = smtplib.SMTPAuthenticationError(
                535, b"5.7.8 Username and Password not accepted")
            plain.return_value.esmtp_features = {}
            with self.assertRaises(notifier.EmailError) as ctx:
                notifier.send_email(self.cfg(), ["x@y.z"], "s", "t")
        self.assertEqual(plain.call_count, 1)
        self.assertIn("Username and Password not accepted", str(ctx.exception))
        self.assertIn("looks like one", str(ctx.exception))

    def test_secret_whitespace_cleanup(self):
        with tempfile.TemporaryDirectory() as d:
            p = make_config(Path(d), SVC)
            env = {"SMTP_PASSWORD": "abcd efgh ijkl mnop\n", "SMTP_USERNAME": " a@b.org\n",
                   "SMTP_PORT": "465\n"}
            with mock.patch.dict(os.environ, env):
                cfg = load_config(p)
        self.assertEqual(cfg.email.password, "abcdefghijklmnop")
        self.assertEqual(cfg.email.username, "a@b.org")
        self.assertEqual((cfg.email.port, cfg.email.security), (465, "ssl"))


class TestDurationLoop(ServerTest):
    def test_runs_until_deadline_then_exits(self):
        from outage_detector.cli import main
        with tempfile.TemporaryDirectory() as d:
            p = make_config(Path(d), f'[[service]]\nname = "Site"\ntype = "http"\nurl = "{self.base}/"\n')
            s = p.read_text().replace("[settings]", "[settings]\ninterval_seconds = 60")
            p.write_text(s)
            ROUTES["/"] = (200, b"ok")
            clock = [0.0]
            with mock.patch("time.monotonic", side_effect=lambda: clock[0]), \
                 mock.patch("time.sleep", side_effect=lambda n: clock.__setitem__(0, clock[0] + n)), \
                 mock.patch("outage_detector.cli.run_once") as ro:
                self.assertEqual(main(["-c", str(p), "run", "--duration", "5"]), 0)
            self.assertEqual(ro.call_count, 6)  # t=0..5 min; t=6 would pass the deadline


if __name__ == "__main__":
    unittest.main()
