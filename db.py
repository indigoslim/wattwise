"""
db.py — Wattwise Database Layer
========================================
Handles:
  • Schema creation (safe, idempotent)
  • Upsert of ETL records
  • Query helpers used by FastAPI endpoints

The database lives at config.DB_PATH inside the named Docker volume.
"""

import logging
import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Generator, Optional

import config
from config import DB_PATH

logger = logging.getLogger(__name__)

# ── DDL ───────────────────────────────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS daily_energy (
    date            TEXT PRIMARY KEY,   -- ISO-8601 YYYY-MM-DD

    -- Raw measured values
    prod_kwh        REAL NOT NULL DEFAULT 0,
    cons_kwh        REAL NOT NULL DEFAULT 0,
    ev_kwh          REAL NOT NULL DEFAULT 0,
    ev_sessions     INTEGER NOT NULL DEFAULT 0,

    -- Derived values
    house_kwh       REAL NOT NULL DEFAULT 0,   -- cons - ev (non-EV load)
    net_kwh         REAL NOT NULL DEFAULT 0,   -- prod - cons
    self_suff_pct   REAL,                      -- prod/cons*100, NULL if cons=0

    -- Data quality flags (set at upsert time from config cutoffs)
    cons_valid      INTEGER NOT NULL DEFAULT 0, -- 1 when date >= CONS_START
    prod_valid      INTEGER NOT NULL DEFAULT 0, -- 1 when date >= PROD_START
    net_valid       INTEGER NOT NULL DEFAULT 0, -- 1 when date >= NET_START

    -- Audit
    imported_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_daily_date      ON daily_energy(date);
CREATE INDEX IF NOT EXISTS idx_daily_net_valid ON daily_energy(net_valid, date);

CREATE TABLE IF NOT EXISTS import_conflicts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    date         TEXT NOT NULL,
    field        TEXT NOT NULL,        -- 'prod_kwh' or 'cons_kwh'
    daily_val    REAL NOT NULL,
    interval_val REAL NOT NULL,
    abs_diff     REAL NOT NULL,
    pct_diff     REAL NOT NULL,
    direction    TEXT NOT NULL,        -- 'DAILY HIGH' or 'DAILY LOW'
    flagged_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    reviewed     INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_conflicts_date ON import_conflicts(date);
CREATE INDEX IF NOT EXISTS idx_conflicts_reviewed ON import_conflicts(reviewed);

CREATE TABLE IF NOT EXISTS interval_data (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    date         TEXT NOT NULL,          -- ISO-8601 YYYY-MM-DD
    hour         INTEGER NOT NULL,       -- 0-23
    slot         INTEGER NOT NULL,       -- 0-3 (15-min slot within hour)
    prod_wh      REAL NOT NULL DEFAULT 0,
    cons_wh      REAL NOT NULL DEFAULT 0,
    exported_wh  REAL NOT NULL DEFAULT 0,
    imported_wh  REAL NOT NULL DEFAULT 0,
    UNIQUE(date, hour, slot)
);

CREATE INDEX IF NOT EXISTS idx_interval_date ON interval_data(date);
CREATE INDEX IF NOT EXISTS idx_interval_hour ON interval_data(hour);

CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# ── CONNECTION ────────────────────────────────────────────────────────────────

def _get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_db() -> Generator[sqlite3.Connection, None, None]:
    """Context manager — yields a connection, commits on success, rolls back on error."""
    conn = _get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── SCHEMA INIT ───────────────────────────────────────────────────────────────

def init_db() -> None:
    """
    Create schema if it doesn't exist.
    Safe to call on every startup — uses CREATE IF NOT EXISTS throughout.
    Also runs idempotent ALTER TABLE migrations for new columns.
    """
    # Run schema creation (executescript auto-commits)
    with get_db() as conn:
        conn.executescript(SCHEMA_SQL)

    # Run column migrations in a separate connection after schema is committed
    migrations = [
        "ALTER TABLE daily_energy ADD COLUMN exported_kwh REAL NOT NULL DEFAULT 0",
        "ALTER TABLE daily_energy ADD COLUMN imported_kwh REAL NOT NULL DEFAULT 0",
        "ALTER TABLE daily_energy ADD COLUMN data_source  TEXT NOT NULL DEFAULT 'daily'",
        # interval_data is created via SCHEMA_SQL — no ALTER needed
    ]
    for sql in migrations:
        try:
            with get_db() as conn:
                conn.execute(sql)
        except Exception:
            pass  # Column already exists — safe to ignore

    logger.info("Database initialised at %s", DB_PATH)


# ── UPSERT ───────────────────────────────────────────────────────────────────

UPSERT_SQL = """
INSERT INTO daily_energy (
    date, prod_kwh, cons_kwh, ev_kwh, ev_sessions,
    house_kwh, net_kwh, self_suff_pct,
    exported_kwh, imported_kwh, data_source,
    cons_valid, prod_valid, net_valid,
    imported_at
) VALUES (
    :date, :prod_kwh, :cons_kwh, :ev_kwh, :ev_sessions,
    :house_kwh, :net_kwh, :self_suff_pct,
    :exported_kwh, :imported_kwh, :data_source,
    :cons_valid, :prod_valid, :net_valid,
    strftime('%Y-%m-%dT%H:%M:%SZ','now')
)
ON CONFLICT(date) DO UPDATE SET
    prod_kwh      = excluded.prod_kwh,
    cons_kwh      = excluded.cons_kwh,
    ev_kwh        = excluded.ev_kwh,
    ev_sessions   = excluded.ev_sessions,
    house_kwh     = excluded.house_kwh,
    net_kwh       = excluded.net_kwh,
    self_suff_pct = excluded.self_suff_pct,
    exported_kwh  = excluded.exported_kwh,
    imported_kwh  = excluded.imported_kwh,
    data_source   = excluded.data_source,
    cons_valid    = excluded.cons_valid,
    prod_valid    = excluded.prod_valid,
    net_valid     = excluded.net_valid,
    imported_at   = excluded.imported_at;
"""


def get_cutoffs() -> dict:
    """
    Return the three cutoff dates as date objects (or None).
    Priority: env var override → DB app_settings → None (treat all data as valid).
    """
    from datetime import date as _date

    def _from_db(key: str) -> Optional[_date]:
        try:
            with get_db() as conn:
                row = conn.execute(
                    "SELECT value FROM app_settings WHERE key = ?", (key,)
                ).fetchone()
                if row:
                    return _date.fromisoformat(row["value"])
        except Exception:
            pass
        return None

    cons = config.CONS_START or _from_db("cons_start")
    prod = config.PROD_START or _from_db("prod_start")
    net  = config.NET_START  or _from_db("net_start")
    return {"cons_start": cons, "prod_start": prod, "net_start": net}


def save_setup(data: dict) -> None:
    """
    Persist onboarding / settings values to app_settings table.
    Keys: cons_start, prod_start, net_start, timezone, data_type,
          has_ev, has_battery (all stored as strings).
    """
    with get_db() as conn:
        for key, value in data.items():
            if value is None:
                conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))
            else:
                conn.execute(
                    "INSERT INTO app_settings (key, value) VALUES (?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, str(value)),
                )


def get_setup() -> dict:
    """Return all app_settings as a plain dict."""
    try:
        with get_db() as conn:
            rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
            return {r["key"]: r["value"] for r in rows}
    except Exception:
        return {}


def is_setup_complete() -> bool:
    """True if the user has completed the onboarding wizard."""
    s = get_setup()
    return s.get("setup_complete") == "1"


def _validity_flags(d: date) -> dict:
    cutoffs = get_cutoffs()
    cons = cutoffs["cons_start"]
    prod = cutoffs["prod_start"]
    net  = cutoffs["net_start"]
    return {
        "cons_valid": int(d >= cons) if cons else 1,
        "prod_valid": int(d >= prod) if prod else 1,
        "net_valid":  int(d >= net)  if net  else 1,
    }


def upsert_records(records: list[dict]) -> dict:
    """
    Upsert a list of ETL dicts into daily_energy.
    Deduplicates within the incoming batch by date (last value wins).
    Skips rows where incoming values are identical to existing DB values.
    Returns a summary dict: {processed, inserted, updated, skipped}.
    """
    # Dedup within the batch itself — keep last occurrence per date
    seen: dict[str, dict] = {}
    for r in records:
        seen[r["date"]] = r
    deduped = list(seen.values())

    # Fetch existing rows for the dates we're about to upsert
    dates = [r["date"] for r in deduped]
    placeholders = ",".join("?" * len(dates))
    with get_db() as conn:
        existing_rows = conn.execute(
            f"SELECT date, prod_kwh, cons_kwh, ev_kwh, ev_sessions, data_source FROM daily_energy WHERE date IN ({placeholders})",
            dates,
        ).fetchall()
    existing = {r["date"]: dict(r) for r in existing_rows}

    rows_to_upsert = []
    skipped = 0
    for r in deduped:
        d = date.fromisoformat(r["date"])
        row = {**r, **_validity_flags(d)}
        ex  = existing.get(r["date"])
        prod_provided = r.get("prod_provided", True)
        cons_provided = r.get("cons_provided", True)
        ev_provided   = r.get("ev_provided",   True)

        # Preserve existing DB values for any column not included in this import
        if ex:
            if not prod_provided:
                row["prod_kwh"] = ex["prod_kwh"]
            if not cons_provided:
                row["cons_kwh"] = ex["cons_kwh"]
            if not ev_provided and ex["ev_kwh"] > 0:
                row["ev_kwh"]      = ex["ev_kwh"]
                row["ev_sessions"] = ex["ev_sessions"]

        # Recalculate derived fields using final resolved values
        row["house_kwh"] = round(max(row["cons_kwh"] - row["ev_kwh"], 0.0), 4)
        row["net_kwh"]   = round(row["prod_kwh"] - row["cons_kwh"], 4)
        row["self_suff_pct"] = round(row["prod_kwh"] / row["cons_kwh"] * 100, 2)                                if row["cons_kwh"] > 0 else None

        # Skip if all tracked values are identical to what's already in the DB
        if ex and (
            abs(ex["prod_kwh"] - row["prod_kwh"]) < 0.001 and
            abs(ex["cons_kwh"] - row["cons_kwh"]) < 0.001 and
            abs(ex["ev_kwh"]   - row["ev_kwh"])   < 0.001
        ):
            skipped += 1
            continue
        rows_to_upsert.append(row)

    inserted = sum(1 for r in rows_to_upsert if r["date"] not in existing)
    updated  = len(rows_to_upsert) - inserted

    # Detect conflicts before writing — interval data overwriting daily data
    CONFLICT_THRESHOLD_ABS = 0.5   # kWh
    CONFLICT_THRESHOLD_PCT = 2.0   # percent
    conflict_rows = []
    for r in rows_to_upsert:
        ex = existing.get(r["date"])
        if not ex:
            continue
        src = r.get("data_source", "daily")
        ex_src = ex.get("data_source", "daily") if "data_source" in (ex or {}) else "daily"
        # Only flag when interval is overwriting a daily-sourced row
        if src != "interval" or ex_src == "interval":
            continue
        for field in ("prod_kwh", "cons_kwh"):
            old_val = ex.get(field, 0) or 0
            new_val = r.get(field, 0) or 0
            abs_diff = abs(new_val - old_val)
            pct_diff = (abs_diff / old_val * 100) if old_val > 0 else 0
            if abs_diff > CONFLICT_THRESHOLD_ABS and pct_diff > CONFLICT_THRESHOLD_PCT:
                conflict_rows.append({
                    "date":         r["date"],
                    "field":        field,
                    "daily_val":    round(old_val, 4),
                    "interval_val": round(new_val, 4),
                    "abs_diff":     round(abs_diff, 4),
                    "pct_diff":     round(pct_diff, 2),
                    "direction":    "DAILY HIGH" if old_val > new_val else "DAILY LOW",
                })

    # Strip processing flags — not DB columns
    DB_COLS = {"date", "prod_kwh", "cons_kwh", "ev_kwh", "ev_sessions",
               "house_kwh", "net_kwh", "self_suff_pct",
               "exported_kwh", "imported_kwh", "data_source",
               "cons_valid", "prod_valid", "net_valid"}
    if rows_to_upsert:
        with get_db() as conn:
            conn.executemany(UPSERT_SQL, [{k: v for k, v in r.items() if k in DB_COLS}
                                          for r in rows_to_upsert])

    # Write conflicts to audit table
    if conflict_rows:
        with get_db() as conn:
            conn.executemany("""
                INSERT INTO import_conflicts
                    (date, field, daily_val, interval_val, abs_diff, pct_diff, direction)
                VALUES
                    (:date, :field, :daily_val, :interval_val, :abs_diff, :pct_diff, :direction)
            """, conflict_rows)
        logger.info("Conflicts logged: %d rows flagged", len(conflict_rows))

    logger.info(
        "Upsert complete: %d processed, %d inserted, %d updated, %d skipped (identical), %d conflicts",
        len(deduped), inserted, updated, skipped, len(conflict_rows),
    )
    return {"processed": len(deduped), "inserted": inserted, "updated": updated,
            "skipped": skipped, "conflicts": len(conflict_rows)}


# ── CONFLICTS ────────────────────────────────────────────────────────────────

def get_conflicts(unreviewed_only: bool = False) -> list[dict]:
    """Return import conflict records sorted by abs_diff descending."""
    where = "WHERE reviewed = 0" if unreviewed_only else ""
    sql = f"""
    SELECT id, date, field, daily_val, interval_val, abs_diff, pct_diff, direction, flagged_at, reviewed
    FROM import_conflicts
    {where}
    ORDER BY abs_diff DESC, date DESC
    """
    with get_db() as conn:
        rows = conn.execute(sql).fetchall()
    return [dict(r) for r in rows]


def mark_conflicts_reviewed(ids: list[int]) -> None:
    """Mark specific conflict rows as reviewed."""
    if not ids:
        return
    placeholders = ",".join("?" * len(ids))
    with get_db() as conn:
        conn.execute(f"UPDATE import_conflicts SET reviewed = 1 WHERE id IN ({placeholders})", ids)


def get_conflict_count() -> int:
    """Return count of unreviewed conflicts — used for gear badge."""
    with get_db() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM import_conflicts WHERE reviewed = 0").fetchone()
    return row["n"] if row else 0


# ── INTERVAL UPSERT ──────────────────────────────────────────────────────────

def upsert_intervals(rows: list[dict]) -> int:
    """
    Upsert raw 15-min interval rows into interval_data.
    Each row: {date, hour, slot, prod_wh, cons_wh, exported_wh, imported_wh}
    Returns count of rows written.
    """
    if not rows:
        return 0
    sql = """
    INSERT INTO interval_data (date, hour, slot, prod_wh, cons_wh, exported_wh, imported_wh)
    VALUES (:date, :hour, :slot, :prod_wh, :cons_wh, :exported_wh, :imported_wh)
    ON CONFLICT(date, hour, slot) DO UPDATE SET
        prod_wh     = excluded.prod_wh,
        cons_wh     = excluded.cons_wh,
        exported_wh = excluded.exported_wh,
        imported_wh = excluded.imported_wh
    """
    with get_db() as conn:
        conn.executemany(sql, rows)
    logger.info("Interval upsert: %d rows written", len(rows))
    return len(rows)


# ── INTERVAL QUERY HELPERS ────────────────────────────────────────────────────

def _season(month: int) -> str:
    return {12:"Winter",1:"Winter",2:"Winter",
            3:"Spring",4:"Spring",5:"Spring",
            6:"Summer",7:"Summer",8:"Summer",
            9:"Fall",10:"Fall",11:"Fall"}[month]


def get_hourly_profile(season: Optional[str] = None) -> list[dict]:
    """
    Average prod and cons kWh per hour of day, with SD.
    Groups by hour (0-23), optionally filtered by season.
    """
    season_clause = ""
    params: dict = {}
    if season and season != "All":
        months = {"Winter":(12,1,2),"Spring":(3,4,5),
                  "Summer":(6,7,8),"Fall":(9,10,11)}.get(season, ())
        if months:
            placeholders = ",".join("?" * len(months))
            season_clause = f"AND CAST(strftime('%m', date) AS INTEGER) IN ({placeholders})"
            params = {str(i): m for i, m in enumerate(months)}

    sql = f"""
    SELECT
        hour,
        AVG(prod_wh)  / 1000.0                AS avg_prod_kwh,
        AVG(cons_wh)  / 1000.0                AS avg_cons_kwh,
        AVG(exported_wh) / 1000.0             AS avg_exported_kwh,
        AVG(imported_wh) / 1000.0             AS avg_imported_kwh,
        -- SD across all slots for this hour
        (SELECT SQRT(AVG((sub.prod_wh/1000.0 - outer_avg.avg_prod)*
                         (sub.prod_wh/1000.0 - outer_avg.avg_prod)))
         FROM interval_data sub,
              (SELECT AVG(prod_wh)/1000.0 AS avg_prod FROM interval_data
               WHERE hour = interval_data.hour {season_clause}) outer_avg
         WHERE sub.hour = interval_data.hour {season_clause})  AS sd_prod,
        COUNT(DISTINCT date)                   AS day_count
    FROM interval_data
    WHERE 1=1 {season_clause}
    GROUP BY hour
    ORDER BY hour
    """
    # Simpler approach — compute in Python for correctness
    base_sql = f"""
    SELECT date, hour, SUM(prod_wh)/1000.0 AS prod_kwh, SUM(cons_wh)/1000.0 AS cons_kwh,
           SUM(exported_wh)/1000.0 AS exp_kwh, SUM(imported_wh)/1000.0 AS imp_kwh
    FROM interval_data
    WHERE 1=1 {season_clause}
    GROUP BY date, hour
    ORDER BY hour
    """
    with get_db() as conn:
        if season and season != "All":
            months_list = {"Winter":[12,1,2],"Spring":[3,4,5],
                           "Summer":[6,7,8],"Fall":[9,10,11]}.get(season, [])
            rows = conn.execute(base_sql, months_list).fetchall()
        else:
            rows = conn.execute(base_sql).fetchall()

    from collections import defaultdict
    import math
    by_hour: dict = defaultdict(list)
    for r in rows:
        by_hour[r["hour"]].append(dict(r))

    result = []
    for h in range(24):
        pts = by_hour.get(h, [])
        if not pts:
            result.append({"hour": h, "avg_prod": 0, "avg_cons": 0,
                           "avg_exp": 0, "avg_imp": 0, "sd_prod": 0,
                           "sd_cons": 0, "n": 0})
            continue
        n = len(pts)
        avg_p = sum(p["prod_kwh"] for p in pts) / n
        avg_c = sum(p["cons_kwh"] for p in pts) / n
        avg_e = sum(p["exp_kwh"]  for p in pts) / n
        avg_i = sum(p["imp_kwh"]  for p in pts) / n
        sd_p  = math.sqrt(sum((p["prod_kwh"]-avg_p)**2 for p in pts)/n) if n > 1 else 0
        sd_c  = math.sqrt(sum((p["cons_kwh"]-avg_c)**2 for p in pts)/n) if n > 1 else 0
        result.append({"hour": h, "avg_prod": round(avg_p,4), "avg_cons": round(avg_c,4),
                       "avg_exp": round(avg_e,4), "avg_imp": round(avg_i,4),
                       "sd_prod": round(sd_p,4), "sd_cons": round(sd_c,4),
                       "sem_prod": round(sd_p/math.sqrt(n),4) if n>1 else 0,
                       "sem_cons": round(sd_c/math.sqrt(n),4) if n>1 else 0,
                       "n": n})
    return result


def get_dow_profile(season: Optional[str] = None, exclude_ev: bool = False) -> list[dict]:
    """
    Average daily consumption kWh per day of week (0=Mon … 6=Sun), with SD.
    Uses daily_energy table (already aggregated).
    exclude_ev: if True, skip dates where ev_kwh > 0.
    """
    season_clause = ""
    months_list = []
    if season and season != "All":
        months_list = {"Winter":[12,1,2],"Spring":[3,4,5],
                       "Summer":[6,7,8],"Fall":[9,10,11]}.get(season, [])
        if months_list:
            placeholders = ",".join("?" * len(months_list))
            season_clause = f"AND CAST(strftime('%m', date) AS INTEGER) IN ({placeholders})"

    ev_clause = "AND ev_kwh = 0" if exclude_ev else ""

    sql = f"""
    SELECT
        CAST((strftime('%w', date) + 6) % 7 AS INTEGER) AS dow,
        date, cons_kwh, prod_kwh, house_kwh
    FROM daily_energy
    WHERE net_valid = 1 {season_clause} {ev_clause}
    ORDER BY dow
    """
    with get_db() as conn:
        rows = conn.execute(sql, months_list).fetchall()

    from collections import defaultdict
    import math
    by_dow: dict = defaultdict(list)
    for r in rows:
        by_dow[r["dow"]].append(dict(r))

    DOW = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
    result = []
    for d in range(7):
        pts = by_dow.get(d, [])
        if not pts:
            result.append({"dow": d, "label": DOW[d], "avg_cons": 0,
                           "avg_prod": 0, "sd_cons": 0, "sem_cons": 0, "n": 0})
            continue
        n = len(pts)
        avg_c = sum(p["cons_kwh"] for p in pts) / n
        avg_p = sum(p["prod_kwh"] for p in pts) / n
        sd_c  = math.sqrt(sum((p["cons_kwh"]-avg_c)**2 for p in pts)/n) if n>1 else 0
        result.append({"dow": d, "label": DOW[d],
                       "avg_cons": round(avg_c,4), "avg_prod": round(avg_p,4),
                       "sd_cons": round(sd_c,4),
                       "sem_cons": round(sd_c/math.sqrt(n),4) if n>1 else 0,
                       "n": n,
                       "days": [round(p["cons_kwh"],4) for p in pts]})
    return result


def get_grid_dependency(season: Optional[str] = None) -> list[dict]:
    """
    Average imported and exported kWh per hour of day.
    Shows when you draw from grid vs. push to grid.
    """
    months_list = []
    season_clause = ""
    if season and season != "All":
        months_list = {"Winter":[12,1,2],"Spring":[3,4,5],
                       "Summer":[6,7,8],"Fall":[9,10,11]}.get(season, [])
        if months_list:
            placeholders = ",".join("?" * len(months_list))
            season_clause = f"AND CAST(strftime('%m', date) AS INTEGER) IN ({placeholders})"

    sql = f"""
    SELECT date, hour,
           SUM(imported_wh)/1000.0 AS imp_kwh,
           SUM(exported_wh)/1000.0 AS exp_kwh
    FROM interval_data
    WHERE 1=1 {season_clause}
    GROUP BY date, hour
    """
    with get_db() as conn:
        rows = conn.execute(sql, months_list).fetchall()

    from collections import defaultdict
    import math
    by_hour: dict = defaultdict(list)
    for r in rows:
        by_hour[r["hour"]].append(dict(r))

    result = []
    for h in range(24):
        pts = by_hour.get(h, [])
        if not pts:
            result.append({"hour": h, "avg_imp": 0, "avg_exp": 0,
                           "sd_imp": 0, "sd_exp": 0, "n": 0})
            continue
        n = len(pts)
        avg_i = sum(p["imp_kwh"] for p in pts) / n
        avg_e = sum(p["exp_kwh"] for p in pts) / n
        sd_i  = math.sqrt(sum((p["imp_kwh"]-avg_i)**2 for p in pts)/n) if n>1 else 0
        sd_e  = math.sqrt(sum((p["exp_kwh"]-avg_e)**2 for p in pts)/n) if n>1 else 0
        result.append({"hour": h, "avg_imp": round(avg_i,4), "avg_exp": round(avg_e,4),
                       "sd_imp": round(sd_i,4), "sd_exp": round(sd_e,4),
                       "sem_imp": round(sd_i/math.sqrt(n),4) if n>1 else 0,
                       "sem_exp": round(sd_e/math.sqrt(n),4) if n>1 else 0,
                       "n": n})
    return result


def get_peak_demand(limit: int = 20, exclude_ev: bool = False) -> list[dict]:
    """
    Top N highest 15-min consumption slots ever recorded.
    Returns date, hour, slot, cons_wh, and a datetime label.
    exclude_ev: if True, skip slots on dates where ev_kwh > 0 in daily_energy.
    """
    ev_clause = """
        AND i.date NOT IN (
            SELECT date FROM daily_energy WHERE ev_kwh > 0
        )""" if exclude_ev else ""
    sql = f"""
    SELECT i.date, i.hour, i.slot,
           i.cons_wh, i.prod_wh, i.imported_wh,
           printf('%02d:%02d', i.hour, i.slot*15) AS time_label
    FROM interval_data i
    WHERE 1=1 {ev_clause}
    ORDER BY i.cons_wh DESC
    LIMIT ?
    """
    with get_db() as conn:
        rows = conn.execute(sql, (limit,)).fetchall()
    return [dict(r) for r in rows]


# ── SOLAR FINGERPRINT ────────────────────────────────────────────────────────

def get_solar_fingerprint(year: Optional[int] = None) -> list[dict]:
    """
    Return all daily production curves for a given year (or all years if year is None).
    Each row: {date, points: [{x: float (0-23.75), w: float (watts)}]}
    Power = Wh / 0.25h for each 15-min slot.
    Only returns slots where prod_wh > 0 to keep payload small.
    """
    if year:
        sql = """
        SELECT date, hour, slot, prod_wh
        FROM interval_data
        WHERE date >= :start AND date <= :end
          AND prod_wh > 0
        ORDER BY date, hour, slot
        """
        params = {"start": f"{year}-01-01", "end": f"{year}-12-31"}
    else:
        sql = """
        SELECT date, hour, slot, prod_wh
        FROM interval_data
        WHERE prod_wh > 0
        ORDER BY date, hour, slot
        """
        params = {}
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()

    from collections import defaultdict
    by_date: dict = defaultdict(list)
    for r in rows:
        x = r["hour"] + r["slot"] * 0.25
        w = round(r["prod_wh"] / 0.25, 1)  # Wh → W
        by_date[r["date"]].append({"x": x, "w": w})

    return [{"date": d, "points": pts} for d, pts in sorted(by_date.items())]


# ── HEATMAP ───────────────────────────────────────────────────────────────────

def get_heatmap(from_date: Optional[str] = None) -> list[dict]:
    """
    Return all daily net_kwh values for the heatmap.
    Returns {date, net_kwh, self_suff_pct} for all net_valid rows.
    """
    net_start = get_cutoffs()["net_start"]
    from_date = from_date or (net_start.isoformat() if net_start else "2000-01-01")
    sql = """
    SELECT date, net_kwh, self_suff_pct, prod_kwh, cons_kwh
    FROM daily_energy
    WHERE net_valid = 1 AND date >= :from_date
    ORDER BY date ASC
    """
    with get_db() as conn:
        rows = conn.execute(sql, {"from_date": from_date}).fetchall()
    return [dict(r) for r in rows]


# ── QUERY HELPERS ─────────────────────────────────────────────────────────────

def get_daily(
    from_date: Optional[str] = None,
    to_date:   Optional[str] = None,
    valid_only: bool = True,
) -> list[dict]:
    """
    Return daily rows as list of dicts.
    Defaults to NET_START onward with net_valid=1.
    """
    net_start = get_cutoffs()["net_start"]
    from_date = from_date or (net_start.isoformat() if net_start else "2000-01-01")
    clauses = ["date >= :from_date"]
    params: dict = {"from_date": from_date}

    if to_date:
        clauses.append("date <= :to_date")
        params["to_date"] = to_date

    if valid_only:
        clauses.append("net_valid = 1")

    where = " AND ".join(clauses)
    sql = f"SELECT * FROM daily_energy WHERE {where} ORDER BY date ASC"

    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()

    return [dict(r) for r in rows]


def get_summary(from_date: Optional[str] = None) -> dict:
    """
    Aggregate stats for the summary cards.
    Operates on net_valid rows only (from NET_START by default).
    """
    net_start = get_cutoffs()["net_start"]
    from_date = from_date or (net_start.isoformat() if net_start else "2000-01-01")

    sql = """
    SELECT
        COUNT(*)                        AS total_days,
        MIN(date)                       AS first_date,
        MAX(date)                       AS last_date,
        SUM(prod_kwh)                   AS total_prod,
        SUM(cons_kwh)                   AS total_cons,
        SUM(ev_kwh)                     AS total_ev,
        SUM(house_kwh)                  AS total_house,
        SUM(net_kwh)                    AS total_net,
        AVG(prod_kwh)                   AS avg_daily_prod,
        AVG(cons_kwh)                   AS avg_daily_cons,
        MAX(prod_kwh)                   AS max_prod,
        MAX(cons_kwh)                   AS max_cons,
        SUM(CASE WHEN net_kwh > 0 THEN 1 ELSE 0 END) AS surplus_days,
        SUM(ev_sessions)                AS total_ev_sessions
    FROM daily_energy
    WHERE net_valid = 1 AND date >= :from_date
    """

    best_prod_sql = """
    SELECT date, prod_kwh FROM daily_energy
    WHERE net_valid = 1 AND date >= :from_date
    ORDER BY prod_kwh DESC LIMIT 1
    """

    peak_cons_sql = """
    SELECT date, cons_kwh FROM daily_energy
    WHERE net_valid = 1 AND date >= :from_date
    ORDER BY cons_kwh DESC LIMIT 1
    """

    with get_db() as conn:
        stats_row     = conn.execute(sql,           {"from_date": from_date}).fetchone()
        best_prod_row = conn.execute(best_prod_sql, {"from_date": from_date}).fetchone()
        peak_cons_row = conn.execute(peak_cons_sql, {"from_date": from_date}).fetchone()

    # Empty database — return a safe zero-state dict
    if stats_row is None or dict(stats_row).get("total_days", 0) == 0:
        return {
            "total_days": 0, "first_date": None, "last_date": None,
            "total_prod": 0, "total_cons": 0, "total_ev": 0,
            "total_house": 0, "total_net": 0, "avg_daily_prod": 0,
            "avg_daily_cons": 0, "max_prod": 0, "max_cons": 0,
            "surplus_days": 0, "total_ev_sessions": 0,
            "self_suff_pct": None, "best_prod_date": None,
            "best_prod_kwh": 0, "peak_cons_date": None, "peak_cons_kwh": 0,
        }

    stats     = dict(stats_row)
    best_prod = dict(best_prod_row) if best_prod_row else {"date": None, "prod_kwh": 0}
    peak_cons = dict(peak_cons_row) if peak_cons_row else {"date": None, "cons_kwh": 0}

    total_prod = stats["total_prod"] or 0
    total_cons = stats["total_cons"] or 0

    return {
        **stats,
        "self_suff_pct":  round(total_prod / total_cons * 100, 1) if total_cons else None,
        "best_prod_date": best_prod["date"],
        "best_prod_kwh":  best_prod["prod_kwh"],
        "peak_cons_date": peak_cons["date"],
        "peak_cons_kwh":  peak_cons["cons_kwh"],
    }


def get_monthly_agg(from_date: Optional[str] = None) -> list[dict]:
    """Monthly aggregates for the coverage bar chart."""
    net_start = get_cutoffs()["net_start"]
    from_date = from_date or (net_start.isoformat() if net_start else "2000-01-01")
    sql = """
    SELECT
        strftime('%Y-%m', date)         AS month,
        SUM(prod_kwh)                   AS prod_kwh,
        SUM(cons_kwh)                   AS cons_kwh,
        SUM(ev_kwh)                     AS ev_kwh,
        SUM(house_kwh)                  AS house_kwh,
        SUM(net_kwh)                    AS net_kwh,
        COUNT(*)                        AS days
    FROM daily_energy
    WHERE net_valid = 1 AND date >= :from_date
    GROUP BY month
    ORDER BY month ASC
    """
    with get_db() as conn:
        rows = conn.execute(sql, {"from_date": from_date}).fetchall()
    return [dict(r) for r in rows]


# ── RECOMPUTE VALIDITY FLAGS ──────────────────────────────────────────────────

def recompute_validity_flags() -> int:
    """
    After cutoff dates change (via setup/settings), recompute cons_valid,
    prod_valid, and net_valid for every row in daily_energy.
    Returns the number of rows updated.
    """
    cutoffs  = get_cutoffs()
    cons = cutoffs["cons_start"]
    prod = cutoffs["prod_start"]
    net  = cutoffs["net_start"]

    with get_db() as conn:
        rows = conn.execute("SELECT date FROM daily_energy").fetchall()
        updated = 0
        for row in rows:
            d = date.fromisoformat(row["date"])
            flags = {
                "cons_valid": int(d >= cons) if cons else 1,
                "prod_valid": int(d >= prod) if prod else 1,
                "net_valid":  int(d >= net)  if net  else 1,
                "date":       row["date"],
            }
            conn.execute(
                "UPDATE daily_energy SET cons_valid=:cons_valid, prod_valid=:prod_valid,"
                " net_valid=:net_valid WHERE date=:date",
                flags,
            )
            updated += 1
    logger.info("Recomputed validity flags for %d rows", updated)
    return updated
