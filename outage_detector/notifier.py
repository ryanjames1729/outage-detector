"""Build and send alert emails over SMTP (Gmail, Microsoft 365, any relay)."""

from __future__ import annotations

import html
import logging
import smtplib
import ssl
import time
from collections import defaultdict
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from .checks import DEGRADED, DOWN, UNKNOWN, UP
from .config import Config, EmailConfig
from .monitor import Event

log = logging.getLogger("outage_detector")

LABEL = {
    DOWN: ("DOWN", "#c62828"),
    DEGRADED: ("DEGRADED", "#ef6c00"),
    UP: ("RECOVERED", "#2e7d32"),
    UNKNOWN: ("CAN'T CHECK", "#616161"),
}


def _headline(ev: Event) -> str:
    name = ev.service["name"]
    if ev.kind == "recovered":
        return f"{name} has RECOVERED (outage lasted {ev.duration})"
    word = LABEL[ev.status][0]
    if ev.kind == "unknown":
        return (f"{name} could not be checked for {ev.duration} - "
                "monitoring for it is not working")
    if ev.kind == "reminder":
        return f"{name} is still {word} (for {ev.duration})"
    if ev.kind == "changed":
        return f"{name} is now {word} (problem started {ev.duration} ago)"
    return f"{name} is {word}"


def build_subject(cfg: EmailConfig, events: list[Event]) -> str:
    groups: dict[str, list[str]] = defaultdict(list)
    for ev in events:
        label = "RECOVERED" if ev.kind == "recovered" else LABEL[ev.status][0]
        if ev.kind == "reminder":
            label = f"STILL {label}"
        groups[label].append(ev.service["name"])
    order = ["DOWN", "STILL DOWN", "DEGRADED", "STILL DEGRADED", "RECOVERED",
             "CAN'T CHECK"]
    parts = [f"{k}: {', '.join(groups[k])}" for k in order if k in groups]
    return f"{cfg.subject_prefix} {' | '.join(parts)}".strip()


def build_text(events: list[Event]) -> str:
    lines = []
    for ev in events:
        lines.append(_headline(ev))
        lines.append(f"  {ev.result.summary}")
        lines += [f"  - {d}" for d in ev.result.details]
        if ev.service.get("description"):
            lines.append(f"  Note: {ev.service['description']}")
        lines.append("")
    ts = events[0].now.strftime("%Y-%m-%d %H:%M UTC") if events[0].now else ""
    lines.append(f"Checked at {ts} by outage-detector.")
    return "\n".join(lines)


def build_html(events: list[Event]) -> str:
    rows = []
    for ev in events:
        word, color = LABEL[ev.status]
        if ev.kind == "recovered":
            word, color = LABEL[UP]
        details = "".join(f"<li>{_linkify(d)}</li>" for d in ev.result.details)
        note = (f"<p style='margin:4px 0;color:#555'><em>{html.escape(ev.service['description'])}</em></p>"
                if ev.service.get("description") else "")
        rows.append(f"""
<div style="border-left:6px solid {color};padding:8px 12px;margin:0 0 14px;background:#fafafa">
  <div style="font-size:16px;font-weight:bold">
    <span style="background:{color};color:#fff;padding:2px 6px;border-radius:3px;font-size:12px">{word}</span>
    {html.escape(_headline(ev))}
  </div>
  <p style="margin:6px 0">{html.escape(ev.result.summary)}</p>
  {note}
  <ul style="margin:4px 0 0 18px;padding:0;color:#333">{details}</ul>
</div>""")
    return (
        "<html><body style='font-family:Segoe UI,Arial,sans-serif;max-width:720px'>"
        + "".join(rows)
        + "<p style='color:#888;font-size:12px'>Sent by outage-detector.</p></body></html>"
    )


def _linkify(text: str) -> str:
    out = []
    for word in text.split(" "):
        if word.startswith(("http://", "https://")):
            out.append(f'<a href="{html.escape(word)}">{html.escape(word)}</a>')
        else:
            out.append(html.escape(word))
    return " ".join(out)


def send_email(cfg: EmailConfig, to: list[str], subject: str, text: str,
               html_body: str | None = None) -> None:
    if not cfg.host:
        raise RuntimeError("SMTP host is not configured ([email] host or SMTP_HOST)")
    if not to:
        raise RuntimeError("No recipients configured ([email] to or ALERT_TO)")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.sender
    msg["To"] = ", ".join(to)
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain="outage-detector")
    msg.set_content(text)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    # Try the configured settings, retry once, and for Gmail fall back to
    # implicit TLS on 465 (some cloud networks have trouble on 587).
    attempts = [(cfg.security, cfg.port), (cfg.security, cfg.port)]
    if cfg.host.lower() in GMAIL_HOSTS and (cfg.security, cfg.port) != ("ssl", 465):
        attempts.append(("ssl", 465))
    errors: list[str] = []
    for i, (security, port) in enumerate(attempts):
        if i:
            time.sleep(3)
        stage = "connect"
        try:
            ctx = ssl.create_default_context()
            log.info("SMTP: connecting to %s:%s (%s)", cfg.host, port, security)
            if security == "ssl":
                server = smtplib.SMTP_SSL(cfg.host, port, context=ctx, timeout=30)
            else:
                server = smtplib.SMTP(cfg.host, port, timeout=30)
            with server:
                if security == "starttls":
                    stage = "STARTTLS"
                    server.starttls(context=ctx)
                if cfg.username:
                    stage = "login"
                    server.ehlo_or_helo_if_needed()
                    log.info("SMTP: server offers AUTH %s",
                             server.esmtp_features.get("auth", "(none)").strip())
                    server.login(cfg.username, cfg.password)
                stage = "send"
                server.send_message(msg)
            if errors:
                log.warning("SMTP: sent after earlier failure(s): %s", "; ".join(errors))
            return
        except smtplib.SMTPAuthenticationError as e:
            # Wrong credentials won't fix themselves on retry.
            raise EmailError(_explain(cfg, "login", e)) from None
        except (smtplib.SMTPException, OSError) as e:
            errors.append(f"{cfg.host}:{port} {stage}: {type(e).__name__}: {e}")
            log.warning("SMTP: %s", errors[-1])
            last_stage, last_exc = stage, e
    raise EmailError(_explain(cfg, last_stage, last_exc, errors))


GMAIL_HOSTS = {"smtp.gmail.com", "smtp.googlemail.com"}


class EmailError(Exception):
    """Sending failed; the message explains likely causes (never secrets)."""


def _explain(cfg: EmailConfig, stage: str, exc: BaseException,
             errors: list[str] | None = None) -> str:
    lines = [f"Could not send email via {cfg.host} (failed at: {stage})."]
    if errors:
        lines.append("Attempts:")
        lines += [f"  - {e}" for e in errors]
    else:
        lines.append(f"Server said: {exc}")
    gmail = cfg.host.lower() in GMAIL_HOSTS
    pw = cfg.password or ""
    lines.append("Things to check:")
    if not cfg.username or not pw:
        lines.append("  - SMTP_USERNAME and/or SMTP_PASSWORD are empty.")
    if stage == "login" and gmail:
        lines.append("  - SMTP_USERNAME must be the full Gmail/Workspace address "
                     f"({'looks OK' if '@' in cfg.username else 'it has no @'}).")
        looks_app_pw = len(pw) == 16 and pw.isalpha()
        lines.append("  - SMTP_PASSWORD must be a 16-letter Google App Password, not "
                     "the account's normal password "
                     f"({'looks like one' if looks_app_pw else 'it does NOT look like one'}). "
                     "Create one at https://myaccount.google.com/apppasswords "
                     "(requires 2-Step Verification).")
        lines.append("  - Google Workspace: an admin may have blocked App Passwords or "
                     "SMTP for this account/OU. If so, use smtp-relay.gmail.com, or "
                     "send through another provider.")
        lines.append("  - Check the sending account's inbox for a Google "
                     "'sign-in attempt blocked' security alert.")
    elif stage == "login":
        lines.append("  - Check SMTP_USERNAME / SMTP_PASSWORD and that the provider "
                     "allows SMTP AUTH for this mailbox.")
    elif stage in ("connect", "STARTTLS"):
        lines.append(f"  - Check SMTP_HOST/SMTP_PORT ({cfg.host}:{cfg.port}, "
                     f"{cfg.security}). Use 587 with starttls, or 465 with ssl.")
    return "\n".join(lines)


def send_events(cfg: Config, events: list[Event]) -> None:
    """Send one email per distinct recipient list (services can add `notify`)."""
    batches: dict[tuple[str, ...], list[Event]] = defaultdict(list)
    for ev in events:
        rcpts = list(dict.fromkeys(cfg.email.to + ev.service.get("notify", [])))
        batches[tuple(rcpts)].append(ev)
    for rcpts, evs in batches.items():
        send_email(cfg.email, list(rcpts), build_subject(cfg.email, evs),
                   build_text(evs), build_html(evs))
