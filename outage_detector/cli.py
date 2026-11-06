"""Command line interface.

    python -m outage_detector check           # run checks, print results, no email
    python -m outage_detector run --once      # one monitoring pass (GitHub Actions / cron)
    python -m outage_detector run             # loop forever every interval_seconds
    python -m outage_detector test-email      # send a test email
    python -m outage_detector status          # show saved state
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from .checks import UP
from .config import ConfigError, load_config
from .monitor import ICON, StateStore, check_all, run_once
from .notifier import EmailError, send_email


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="outage_detector",
                                description="Email alerts when services go down.")
    p.add_argument("-c", "--config", default="config.toml", help="path to config.toml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="monitor and send alerts")
    r.add_argument("--once", action="store_true", help="single pass, then exit")
    r.add_argument("--dry-run", action="store_true", help="don't send email")
    sub.add_parser("check", help="run all checks and print results (no state, no email)")
    sub.add_parser("test-email", help="send a test email using [email] settings")
    sub.add_parser("status", help="print saved state")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2

    if args.cmd == "check":
        bad = 0
        for svc, res in check_all(cfg):
            print(f"[{ICON[res.status]}] {svc['name']}: {res.summary}")
            for d in res.details:
                print(f"         {d}")
            bad += res.status != UP
        return 1 if bad else 0

    if args.cmd == "test-email":
        try:
            send_email(cfg.email, cfg.email.to, f"{cfg.email.subject_prefix} Test email",
                       "If you can read this, outage-detector email alerts are working.")
        except (EmailError, RuntimeError) as e:
            print(f"\nTEST EMAIL FAILED\n{e}", file=sys.stderr)
            return 1
        print(f"Test email sent to {len(cfg.email.to)} recipient(s)")
        return 0

    if args.cmd == "status":
        store = StateStore(cfg.settings.state_file)
        for svc in cfg.services:
            st = store.data.get(svc["id"])
            if not st:
                print(f"{svc['name']}: never checked")
                continue
            print(f"{svc['name']}: {st['confirmed'].upper()} "
                  f"(last check {st.get('last_checked')}: {st.get('last_summary')})")
        return 0

    # run
    while True:
        try:
            run_once(cfg, send=not args.dry_run)
        except Exception:  # noqa: BLE001 - already logged; keep looping in daemon mode
            if args.once:
                return 1
        if args.once:
            return 0
        time.sleep(cfg.settings.interval_seconds)
