#!/usr/bin/env python3
"""Email the schedule changes that build_calendar.py wrote with --changes-file.

SMTP settings come from the environment (GitHub Actions secrets and variables in CI):
  SMTP_USERNAME, SMTP_PASSWORD  the sending account; for Gmail, an app password
  ALERT_EMAIL_TO                comma-separated recipients, sent as Bcc so they do not see each other
  SMTP_HOST, SMTP_PORT          optional; default smtp.gmail.com and 465 (TLS from the start)

Stdlib only, like build_calendar.py.
"""

from __future__ import annotations

import argparse
import json
import os
import smtplib
import ssl
import sys
from email.message import EmailMessage
from pathlib import Path

import build_calendar

DEFAULT_HOST = "smtp.gmail.com"
DEFAULT_PORT = 465
REQUIRED = ("SMTP_USERNAME", "SMTP_PASSWORD", "ALERT_EMAIL_TO")

EXAMPLE_CHANGE = "Changed: Sun Oct 18 vs Old Flames (away)\n  Time: 9:00 AM -> 11:00 AM\n  Field: Beach #4 (Turf) -> Beach #2 (Turf)"


def recipients(value: str) -> list[str]:
    return [address.strip() for address in value.split(",") if address.strip()]


def test_report(config: dict) -> dict[str, str]:
    report = build_calendar.change_report(config, [
        "This is a test of the schedule change alerts. Nothing has changed. A real alert looks like this:",
        EXAMPLE_CHANGE,
    ])
    return {"subject": f"{config['team']} schedule: test alert", "body": report["body"]}


def build_message(report: dict[str, str], sender: str, to: list[str]) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = report["subject"]
    message["From"] = sender
    message["To"] = sender
    message["Bcc"] = ", ".join(to)
    message.set_content(report["body"])
    return message


def send(message: EmailMessage, env: dict[str, str]) -> None:
    host = env.get("SMTP_HOST") or DEFAULT_HOST
    port = int(env.get("SMTP_PORT") or DEFAULT_PORT)
    with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30) as smtp:
        smtp.login(env["SMTP_USERNAME"], env["SMTP_PASSWORD"])
        smtp.send_message(message)  # delivers to the Bcc recipients and strips the Bcc header


def main(argv: list[str] | None = None, env: dict[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("report", nargs="?", type=Path, help="the JSON file written by build_calendar.py --changes-file")
    source.add_argument("--test", action="store_true", help="send a test alert instead")
    parser.add_argument("--config", default="config.json", type=Path)
    args = parser.parse_args(argv)
    env = dict(os.environ) if env is None else env

    missing = [name for name in REQUIRED if not env.get(name, "").strip()]
    if missing:
        print(f"error: set {', '.join(missing)} to send alerts", file=sys.stderr)
        return 2
    to = recipients(env["ALERT_EMAIL_TO"])
    if not to:
        print("error: ALERT_EMAIL_TO has no addresses", file=sys.stderr)
        return 2

    config = json.loads(args.config.read_text(encoding="utf-8"))
    report = test_report(config) if args.test else json.loads(args.report.read_text(encoding="utf-8"))
    send(build_message(report, env["SMTP_USERNAME"], to), env)
    print(f"sent {report['subject']!r} to {len(to)} recipient(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
