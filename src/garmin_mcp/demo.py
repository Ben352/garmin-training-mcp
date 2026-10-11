"""Build the synthetic demo database: the normal fetch commands, fed by the synthetic
client instead of a Garmin account.

Usage:
    uv run garmin-demo            # writes data/synthetic/ (re-runs reuse the cache)
    uv run garmin-demo --fresh    # delete data/synthetic/ first

Then point the MCP server at it with GARMIN_SYNTHETIC=1 (see README).
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from garmin_mcp import config

STEPS = ["fetch_activities", "fetch_runs", "fetch_daily"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--fresh", action="store_true", help="delete the synthetic data dir first"
    )
    args = parser.parse_args()

    env = {**os.environ, "GARMIN_SYNTHETIC": "1"}
    os.environ["GARMIN_SYNTHETIC"] = "1"
    out = config.data_dir()
    if args.fresh and out.exists():
        if out.resolve() == Path("data").resolve():
            sys.exit(
                "Refusing to delete data/: set GARMIN_DATA_DIR to a synthetic dir."
            )
        shutil.rmtree(out)
    for step in STEPS:
        print(f"\n== {step} ==", flush=True)
        subprocess.run(
            [sys.executable, "-m", f"garmin_mcp.{step}"], env=env, check=True
        )
    print(
        f"\nSynthetic data ready in {out}/. Start the server on it with:\n"
        "  GARMIN_SYNTHETIC=1 uv run garmin-mcp"
    )


if __name__ == "__main__":
    main()
