"""Where data lives, and whether to use the synthetic Garmin client.

GARMIN_SYNTHETIC=1   use generated data instead of a Garmin account (see synthetic.py)
GARMIN_DATA_DIR      root for CSVs, raw cache and databases
                     (default: data/, or data/synthetic/ in synthetic mode)
GARMIN_DB_PATH       main database (default: <data dir>/garmin.duckdb)
GARMIN_AUDIT_PATH    audit log database (default: <data dir>/audit.duckdb)
"""

import os
from pathlib import Path


def synthetic() -> bool:
    return os.getenv("GARMIN_SYNTHETIC", "").lower() in {"1", "true", "yes"}


def data_dir() -> Path:
    if custom := os.getenv("GARMIN_DATA_DIR"):
        return Path(custom)
    return Path("data/synthetic" if synthetic() else "data")
