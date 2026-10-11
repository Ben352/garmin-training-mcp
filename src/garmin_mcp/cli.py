"""Shared helpers for the fetch scripts: login, date-range args, CSV output."""

import argparse
import csv
import getpass
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from garmin_mcp import config

DEFAULT_TOKENSTORE = "~/.garminconnect"


def _login_with_token(tokenstore: str) -> Garmin | None:
    """Resume the cached session. None if there's no token or Garmin rejects it."""
    client = Garmin()  # no credentials: can only succeed from the token
    try:
        client.login(tokenstore)
    except GarminConnectAuthenticationError:
        return None
    return client


def login() -> Garmin:
    """Log in with the cached session token, asking for the password only if needed.

    The password is never read from a file or env var: it's typed at the prompt
    (not echoed), used once, and only the resulting session token is saved. One
    password attempt, never retried. Exits with a message on failure.

    With GARMIN_SYNTHETIC=1 returns the synthetic client instead: no account needed.
    """
    if config.synthetic():
        from garmin_mcp.synthetic import SyntheticGarmin

        print("Synthetic mode: generated data, no Garmin account used.")
        return SyntheticGarmin()
    load_dotenv(Path.cwd() / ".env")
    if os.getenv("GARMIN_PASSWORD"):
        print(
            "Note: GARMIN_PASSWORD is ignored; you'll be asked for the password when "
            "needed. Remove it from .env.",
            file=sys.stderr,
        )
    tokenstore = os.getenv("GARMINTOKENS", DEFAULT_TOKENSTORE)
    Path(tokenstore).expanduser().mkdir(mode=0o700, parents=True, exist_ok=True)

    try:
        if client := _login_with_token(tokenstore):
            return client
        if not sys.stdin.isatty():
            sys.exit(
                "No valid Garmin session token, and no terminal to ask for the "
                "password. Run `uv run garmin-fetch` in a terminal once to log in."
            )
        print("No valid Garmin session token: log in once to create one.")
        email = os.getenv("GARMIN_EMAIL") or input("Garmin email: ").strip()
        client = Garmin(
            email=email,
            password=getpass.getpass("Garmin password: "),
            prompt_mfa=lambda: input("Garmin MFA code: ").strip(),
        )
        client.login(tokenstore)  # saves a fresh token on success
    except GarminConnectTooManyRequestsError:
        sys.exit("Login rate-limited by Garmin (429). Wait a while before retrying.")
    except GarminConnectAuthenticationError as e:
        sys.exit(f"Garmin login failed: {e}. Not retried; run the command again.")
    except GarminConnectConnectionError as e:
        sys.exit(f"Could not reach Garmin Connect: {e}")
    print(f"Logged in. Session token saved in {tokenstore}.")
    return client


def parse_args(description: str, default_name: str) -> argparse.Namespace:
    """Parse --start / --end and --out (default: <data dir>/<default_name>).

    Dates default to the last 90 days, or the fixed synthetic window in synthetic mode.
    """
    if config.synthetic():
        from garmin_mcp.synthetic import WINDOW_END, WINDOW_START

        default_start, default_end = WINDOW_START, WINDOW_END
    else:
        default_end = date.today()
        default_start = default_end - timedelta(days=90)
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--start", type=date.fromisoformat, default=default_start)
    parser.add_argument("--end", type=date.fromisoformat, default=default_end)
    parser.add_argument("--out", type=Path, default=config.data_dir() / default_name)
    args = parser.parse_args()
    if args.start > args.end:
        parser.error("--start must be on or before --end")
    return args


def write_csv(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def report_empty_columns(columns: list[str], rows: list[dict[str, Any]]) -> None:
    """Print columns that are empty in every row. Names only, never values."""
    empty = [c for c in columns if rows and all(r[c] is None for r in rows)]
    if empty:
        print(f"Columns empty for every row (may be expected): {', '.join(empty)}")
