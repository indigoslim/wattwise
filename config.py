"""
config.py — Wattwise Configuration
===========================================
All environment-specific constants live here.
Cutoff dates are read from the database (set via onboarding wizard) or from
optional environment variables for advanced/scripted deployments.
"""

from datetime import date
import os
from pathlib import Path
from typing import Optional


def _parse_date_env(key: str) -> Optional[date]:
    """Read an ISO date (YYYY-MM-DD) from an env var. Returns None if unset or invalid."""
    val = os.getenv(key, "").strip()
    if not val:
        return None
    try:
        return date.fromisoformat(val)
    except ValueError:
        return None


# ── DATA QUALITY CUTOFFS ─────────────────────────────────────────────────────
# These are normally set via the first-run onboarding wizard and stored in the
# database. Environment variables are provided as an override for advanced users.
#
# CONS_START — date your consumption monitoring became reliable (e.g. system install date)
# PROD_START — date solar production became fully unclipped (e.g. grid export enabled)
# NET_START  — earliest date used as the default display floor (both sources valid)
#
# If unset here and not configured via onboarding, all data is treated as valid.

CONS_START: Optional[date] = _parse_date_env("CONS_START")
PROD_START: Optional[date] = _parse_date_env("PROD_START")
NET_START:  Optional[date] = _parse_date_env("NET_START")

# ── PATHS ────────────────────────────────────────────────────────────────────
BASE_DIR:   Path = Path(__file__).parent
DATA_DIR:   Path = Path(os.getenv("DB_PATH", "/app/data/wattwise.db")).parent
DB_PATH:    Path = Path(os.getenv("DB_PATH", "/app/data/wattwise.db"))
BACKUP_DIR: Path = Path(os.getenv("BACKUP_DIR", "/app/backups"))
STATIC_DIR: Path = BASE_DIR / "static"

# ── SERVER ───────────────────────────────────────────────────────────────────
HOST: str = "0.0.0.0"
PORT: int = int(os.getenv("PORT", "9521"))

# ── APP ──────────────────────────────────────────────────────────────────────
APP_TITLE:   str = "Wattwise"
APP_VERSION: str = "0.2.0-beta"

# ── VERSIONS ─────────────────────────────────────────────────────────────────
# BE increments on any Python file change; FE increments on index.html change.
BE_VERSION: str = "0.2.0-beta"
FE_VERSION: str = "0.2.0-beta"

# ── IMPORT LIMITS ─────────────────────────────────────────────────────────────
MAX_UPLOAD_BYTES: int = 50 * 1024 * 1024   # 50 MB per file
