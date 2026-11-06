"""Build and send alert emails over SMTP (Gmail, Microsoft 365, any relay)."""

from __future__ import annotations

import html
import smtplib
import ssl
from collections import defaultdict
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from .checks import DEGRADED, DOWN, UP
from .config import Config, EmailConfig
from .monitor import Event

LABEL = {
    DOWN: ("DOWN", "#c62828"),
    DEGRADED: ("DEGRADED", "#ef6c00"),
    UP: ("RECOVERED", "#2e7d32"),
}


def _headline(ev: Event) -> str:
    name = ev.service["name"]
    if ev.kind == "recovered":
        return f"{name} has RECOVERED (outage lasted {ev.duration})"
    word = LABEL[ev.status][0]
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
    order = ["DOWN", "STILL DOWN", "DEGRADED", "STILL DEGRADED", "RECOVERED"]
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

    ctx = ssl.create_default_context()
    if cfg.security == "ssl":
        server = smtplib.SMTP_SSL(cfg.host, cfg.port, context=ctx, timeout=30)
    else:
        server = smtplib.SMTP(cfg.host, cfg.port, timeout=30)
    with server:
        if cfg.security == "starttls":
            server.starttls(context=ctx)
        if cfg.username:
            server.login(cfg.username, cfg.password)
        server.send_message(msg)


def send_events(cfg: Config, events: list[Event]) -> None:
    """Send one email per distinct recipient list (services can add `notify`)."""
    batches: dict[tuple[str, ...], list[Event]] = defaultdict(list)
    for ev in events:
        rcpts = list(dict.fromkeys(cfg.email.to + ev.service.get("notify", [])))
        batches[tuple(rcpts)].append(ev)
    for rcpts, evs in batches.items():
        send_email(cfg.email, list(rcpts), build_subject(cfg.email, evs),
                   build_text(evs), build_html(evs))
