"""Shared helpers for the fetch scripts: login, date-range args, CSV output."""

import argparse
import csv
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

DEFAULT_TOKENSTORE = "~/.garminconnect"


def login() -> Garmin:
    """Log in once using cached tokens or env credentials. Exits on failure, never retries."""
    load_dotenv(Path.cwd() / ".env")
    tokenstore = os.getenv("GARMINTOKENS", DEFAULT_TOKENSTORE)
    Path(tokenstore).expanduser().mkdir(mode=0o700, parents=True, exist_ok=True)

    client = Garmin(
        email=os.getenv("GARMIN_EMAIL"),
        password=os.getenv("GARMIN_PASSWORD"),
        prompt_mfa=lambda: input("Garmin MFA code: ").strip(),
    )
    try:
        client.login(tokenstore)
    except GarminConnectTooManyRequestsError:
        sys.exit("Login rate-limited by Garmin (429). Wait a while before retrying.")
    except GarminConnectAuthenticationError as e:
        sys.exit(
            f"Garmin login failed: {e}. Check GARMIN_EMAIL / GARMIN_PASSWORD in .env."
        )
    except GarminConnectConnectionError as e:
        sys.exit(f"Could not reach Garmin Connect: {e}")
    return client


def parse_args(description: str, default_out: str) -> argparse.Namespace:
    """Parse --start / --end (default: last 90 days) and --out."""
    today = date.today()
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--start", type=date.fromisoformat, default=today - timedelta(days=90)
    )
    parser.add_argument("--end", type=date.fromisoformat, default=today)
    parser.add_argument("--out", type=Path, default=Path(default_out))
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
