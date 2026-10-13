#!/usr/bin/env python3
"""Place an outbound phone call through Bland AI.

Usage:
    python scripts/bland_call.py +918084794983
    python scripts/bland_call.py +918084794983 --task "Remind them about dinner"
    python scripts/bland_call.py --status <call_id>

Reads BLAND_API_KEY (and optional BLAND_* settings) from the environment or .env.
Stdlib only, so it needs no install step.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.bland.ai/v1"
DEFAULT_TASK = (
    "You are Meera, the host at The Copper Kettle, a modern North Indian "
    "restaurant. This is a short test call. Greet the person warmly, tell them "
    "the voice setup works, offer to help with a table reservation, and keep "
    "it brief and natural. Speak in Hinglish if they do."
)


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def request(method: str, url: str, key: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        url,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"authorization": key, "content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        sys.exit(f"Bland API error {exc.code}: {exc.read().decode(errors='replace')}")


def main() -> None:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("phone", nargs="?", help="E.164 number, e.g. +918084794983")
    parser.add_argument("--task", default=os.getenv("BLAND_TASK", DEFAULT_TASK))
    parser.add_argument("--voice", default=os.getenv("BLAND_VOICE"))
    parser.add_argument("--first-sentence", default=os.getenv("BLAND_FIRST_SENTENCE"))
    parser.add_argument("--status", metavar="CALL_ID", help="fetch a call's details")
    args = parser.parse_args()

    key = os.getenv("BLAND_API_KEY")
    if not key:
        sys.exit("BLAND_API_KEY is not set (put it in .env).")

    if args.status:
        print(json.dumps(request("GET", f"{API}/calls/{args.status}", key), indent=2))
        return
    if not args.phone:
        parser.error("phone number required")

    body = {"phone_number": args.phone, "task": args.task, "wait_for_greeting": True}
    if args.voice:
        body["voice"] = args.voice
    if args.first_sentence:
        body["first_sentence"] = args.first_sentence
    from_number = os.getenv("BLAND_FROM_NUMBER")
    if from_number:
        body["from"] = from_number

    print(json.dumps(request("POST", f"{API}/calls", key, body), indent=2))


if __name__ == "__main__":
    main()
