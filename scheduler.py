"""
backup.py — Scheduled database backup with rotation
=====================================================
Runs as a background thread started at app startup.

Schedule:
  • Daily   — every day at 02:00 local time  → kept for 7 days
  • Weekly  — every Sunday at 02:00          → kept for 4 weeks

Filenames:
  wattwise_db_YYYYMMDD-HHMMSS_daily.db
  wattwise_db_YYYYMMDD-HHMMSS_weekly.db

Gracefully skips if BACKUP_DIR is not mounted or not writable.
"""

import logging
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import config

logger = logging.getLogger(__name__)

KEEP_DAILY  = 7
KEEP_WEEKLY = 4

# ── STATE (read by /api/backup/status) ───────────────────────────────────────
_state: dict = {
    "last_daily":       None,   # ISO datetime string
    "last_weekly":      None,
    "last_error":       None,
    "daily_count":      0,
    "weekly_count":     0,
    "backup_dir_ok":    False,
}


def get_status() -> dict:
    return dict(_state)


# ── CORE ─────────────────────────────────────────────────────────────────────

def _dir_ok() -> bool:
    """Return True if backup dir exists and is writable."""
    try:
        d = config.BACKUP_DIR
        if not d.exists():
            return False
        test = d / ".write_test"
        test.touch()
        test.unlink()
        return True
    except Exception:
        return False


def _do_backup(kind: str) -> bool:
    """Write one backup file. Returns True on success."""
    if not config.DB_PATH.exists():
        logger.warning("Backup (%s): DB not found at %s — skipping", kind, config.DB_PATH)
        return False

    if not _dir_ok():
        logger.warning("Backup (%s): backup dir %s not mounted or not writable — skipping",
                       kind, config.BACKUP_DIR)
        _state["backup_dir_ok"] = False
        return False

    _state["backup_dir_ok"] = True
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename  = f"wattwise_db_{timestamp}_{kind}.db"
    dest      = config.BACKUP_DIR / filename

    try:
        shutil.copy2(str(config.DB_PATH), str(dest))
        logger.info("Backup (%s): written → %s", kind, dest)
        _state[f"last_{kind}"] = datetime.now().isoformat()
        _state["last_error"]   = None
        return True
    except Exception as exc:
        logger.error("Backup (%s): write failed — %s", kind, exc)
        _state["last_error"] = str(exc)
        return False


def _prune(kind: str, keep: int) -> None:
    """Remove oldest backup files beyond the keep limit."""
    if not _dir_ok():
        return
    pattern = f"wattwise_db_*_{kind}.db"
    files   = sorted(config.BACKUP_DIR.glob(pattern))
    to_delete = files[:-keep] if len(files) > keep else []
    for f in to_delete:
        try:
            f.unlink()
            logger.info("Backup prune (%s): removed %s", kind, f.name)
        except Exception as exc:
            logger.warning("Backup prune (%s): could not remove %s — %s", kind, f.name, exc)
    _refresh_counts()


def _refresh_counts() -> None:
    if not _dir_ok():
        _state["daily_count"]  = 0
        _state["weekly_count"] = 0
        return
    _state["daily_count"]  = len(list(config.BACKUP_DIR.glob("wattwise_db_*_daily.db")))
    _state["weekly_count"] = len(list(config.BACKUP_DIR.glob("wattwise_db_*_weekly.db")))


# ── SCHEDULER LOOP ───────────────────────────────────────────────────────────

def _scheduler() -> None:
    """
    Wakes every 60 s. Fires daily backup at 02:00, weekly on Sunday at 02:00.
    Tracks last-run dates in memory so restarts don't double-fire.
    """
    last_daily_date:  Optional[str] = None
    last_weekly_date: Optional[str] = None

    logger.info("Backup scheduler started")
    _refresh_counts()

    while True:
        try:
            now  = datetime.now()
            date = now.strftime("%Y-%m-%d")

            if now.hour == 2 and now.minute < 1:
                if last_daily_date != date:
                    if _do_backup("daily"):
                        _prune("daily", KEEP_DAILY)
                    last_daily_date = date

                if now.weekday() == 6 and last_weekly_date != date:  # Sunday
                    if _do_backup("weekly"):
                        _prune("weekly", KEEP_WEEKLY)
                    last_weekly_date = date

        except Exception as exc:
            logger.error("Backup scheduler loop error: %s", exc)
            _state["last_error"] = str(exc)

        time.sleep(60)


def start() -> None:
    """Start the scheduler in a daemon thread."""
    t = threading.Thread(target=_scheduler, name="backup-scheduler", daemon=True)
    t.start()
    logger.info("Backup scheduler thread started (daily×%d, weekly×%d)", KEEP_DAILY, KEEP_WEEKLY)
