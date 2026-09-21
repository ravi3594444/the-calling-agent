"""Command line: migrate, onboard a business, mint a dashboard link.

Onboarding is meant to be a ten-minute form (PRD §15), and this is the form
before the form exists. Every command writes config that the dashboard can
then edit; none of them writes code.

    python -m calling_agent.cli migrate
    python -m calling_agent.cli onboard --slug copper-kettle --name "The Copper Kettle" \
        --timezone Asia/Kolkata --vertical restaurant --phone +911234567890
    python -m calling_agent.cli hours --slug copper-kettle --open 12:00 --close 22:00 --units 28
    python -m calling_agent.cli link --slug copper-kettle
    python -m calling_agent.cli demo
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import time
from getpass import getpass

from . import businesses, db, owners
from .api.deps import issue_dashboard_token


def _parse_time(value: str) -> time:
    hour, minute = (int(part) for part in value.split(":")[:2])
    return time(hour, minute)


def cmd_migrate(_args: argparse.Namespace) -> int:
    applied = db.migrate()
    print("applied:", ", ".join(applied) if applied else "nothing to do")
    return 0


def cmd_onboard(args: argparse.Namespace) -> int:
    config = json.loads(args.config) if args.config else {}
    config.setdefault("identity", {})
    config["identity"].setdefault("display_name", args.name)
    if args.agent_name:
        config["identity"]["agent_name"] = args.agent_name
    if args.description:
        config["identity"]["description"] = args.description
    if args.country:
        config.setdefault("locale", {})["country"] = args.country

    business = businesses.create(
        slug=args.slug,
        name=args.name,
        timezone=args.timezone,
        vertical=args.vertical,
        phone_number=args.phone,
        config=config,
    )
    print(f"created {business.slug} ({business.id})")
    return 0


def cmd_hours(args: argparse.Namespace) -> int:
    """Set one weekly shape. Days not named are closed."""
    business = businesses.by_slug(args.slug)
    weekdays = (
        [int(d) for d in args.weekdays.split(",")] if args.weekdays else list(range(7))
    )
    businesses.set_capacity_rules(
        business.id,
        [
            {
                "weekday": weekday,
                "start_time": _parse_time(args.open),
                "end_time": _parse_time(args.close),
                "total_units": args.units,
                "slot_minutes": args.slot_minutes,
                "turn_minutes": args.turn_minutes,
                "label": args.label,
            }
            for weekday in weekdays
        ],
    )
    print(f"{business.slug}: open {args.open}-{args.close} on {weekdays}, {args.units} units")
    return 0


def cmd_link(args: argparse.Namespace) -> int:
    """Mint a dashboard link. Printed once; only its hash is stored."""
    business = businesses.by_slug(args.slug)
    secret = issue_dashboard_token(business.id, label=args.label)
    base = args.base_url.rstrip("/")
    print(f"{base}/dashboard?token={secret}")
    return 0


def cmd_owner(args: argparse.Namespace) -> int:
    """Create or reset the account that can sign in to a venue's dashboard.

    The recovery path. Nothing can email a reset link yet, so a forgotten
    password is a person asking you, and this is what you run.
    """
    if args.password:
        password = args.password
    else:
        password = getpass("New password: ")
        if password != getpass("Again: "):
            print("Those do not match.")
            return 1

    try:
        if args.slug:
            business = businesses.by_slug(args.slug)
            owner = owners.create(business.id, email=args.email, password=password)
            print(f"created {owner.email} for {business.slug}")
        else:
            owner = owners.set_password(args.email, password)
            print(f"password reset for {owner.email}")
    except (owners.OwnerError, businesses.UnknownBusiness) as exc:
        print(str(exc))
        return 1
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    business = businesses.by_slug(args.slug)
    if not args.section:
        print(json.dumps(business.config, indent=2))
        return 0
    updated = businesses.save_config(
        business.id, args.section, json.loads(args.values), actor="cli"
    )
    print(json.dumps(updated.config[args.section], indent=2))
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """A venue to talk to in thirty seconds, so the voice layer has something to book."""
    slug = args.slug
    if businesses.exists(slug):
        print(f"{slug} already exists")
        business = businesses.by_slug(slug)
    else:
        business = businesses.create(
            slug=slug,
            name=args.name,
            timezone=args.timezone,
            vertical="restaurant",
            phone_number=args.phone,
            config={
                "identity": {
                    "display_name": args.name,
                    "agent_name": args.agent_name,
                    "description": args.description,
                },
                "locale": {"country": args.country},
                "voice": {"voice_id": args.voice, "languages": ["English", "Hindi"]},
                "capacity": {"slot_minutes": 30, "turn_minutes": 90, "sellable_pct": 0.7},
                "policy": {"min_lead_minutes": 30, "max_party_size": 12},
            },
        )
        businesses.set_capacity_rules(
            business.id,
            [
                {
                    "weekday": weekday,
                    "start_time": time(12, 0),
                    "end_time": time(22, 0),
                    "total_units": 28,
                    "slot_minutes": 30,
                    "turn_minutes": 90,
                    "label": "Service",
                }
                for weekday in range(7)
            ],
        )
        print(f"created {slug} ({business.id})")

    secret = issue_dashboard_token(business.id, label="demo")
    print(f"dashboard: {args.base_url.rstrip('/')}/dashboard?token={secret}")
    print(f"browser agent: {args.base_url.rstrip('/')}/?business={slug}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tableline")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="apply database migrations").set_defaults(fn=cmd_migrate)

    onboard = sub.add_parser("onboard", help="create a business")
    onboard.add_argument("--slug", required=True)
    onboard.add_argument("--name", required=True)
    onboard.add_argument("--timezone", default="UTC")
    onboard.add_argument("--vertical", default="restaurant")
    onboard.add_argument("--phone", default=None)
    onboard.add_argument("--country", default=None)
    onboard.add_argument("--agent-name", dest="agent_name", default="")
    onboard.add_argument("--description", default="")
    onboard.add_argument("--config", default="", help="JSON config document")
    onboard.set_defaults(fn=cmd_onboard)

    hours = sub.add_parser("hours", help="set the weekly capacity shape")
    hours.add_argument("--slug", required=True)
    hours.add_argument("--open", required=True)
    hours.add_argument("--close", required=True)
    hours.add_argument("--units", type=int, required=True)
    hours.add_argument("--slot-minutes", dest="slot_minutes", type=int, default=30)
    hours.add_argument("--turn-minutes", dest="turn_minutes", type=int, default=90)
    hours.add_argument("--weekdays", default="", help="e.g. 0,1,2,3,4 for Mon-Fri")
    hours.add_argument("--label", default="")
    hours.set_defaults(fn=cmd_hours)

    link = sub.add_parser("link", help="mint a dashboard link")
    link.add_argument("--slug", required=True)
    link.add_argument("--label", default="")
    link.add_argument("--base-url", dest="base_url", default="http://localhost:8080")
    link.set_defaults(fn=cmd_link)

    owner = sub.add_parser("owner", help="create or reset a dashboard account")
    owner.add_argument("--email", required=True)
    owner.add_argument(
        "--slug",
        default="",
        help="create an account for this venue; omit to reset an existing one",
    )
    owner.add_argument(
        "--password",
        default="",
        help="prompted for if omitted, which keeps it out of your shell history",
    )
    owner.set_defaults(fn=cmd_owner)

    config = sub.add_parser("config", help="read or write one config section")
    config.add_argument("--slug", required=True)
    config.add_argument("--section", default="")
    config.add_argument("--values", default="{}")
    config.set_defaults(fn=cmd_config)

    demo = sub.add_parser("demo", help="create a venue to talk to")
    demo.add_argument("--slug", default="demo")
    demo.add_argument("--name", default="The Copper Kettle")
    demo.add_argument("--agent-name", dest="agent_name", default="Meera")
    demo.add_argument("--description", default="modern North Indian food")
    demo.add_argument("--timezone", default="Asia/Kolkata")
    demo.add_argument("--country", default="IN")
    demo.add_argument("--voice", default="arjun")
    demo.add_argument("--phone", default=None)
    demo.add_argument("--base-url", dest="base_url", default="http://localhost:8080")
    demo.set_defaults(fn=cmd_demo)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
