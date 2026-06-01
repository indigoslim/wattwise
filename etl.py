"""
etl.py — Wattwise ETL
==============================
Merges three CSV sources into a single unified daily record:
  • Daily production report   (Energy Produced in Wh, one row per day)
  • Daily consumption report  (Energy Consumed in kWh, one row per day)
  • EV charger session log    (session-based, prorated across midnight boundaries)

Output: List[dict] — one dict per date, ready for SQLite upsert.

Usage (standalone / debug):
    python etl.py \
        --prod  path/to/production.csv \
        --cons  path/to/consumption.csv \
        --ev    path/to/ev_sessions.csv \
        [--out  path/to/output.csv]

Designed to be imported by the FastAPI app:
    from etl import run_etl
    records = run_etl(prod_path, cons_path, ev_path)
"""

import argparse
import csv
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── CONSTANTS ────────────────────────────────────────────────────────────────

SECS_PER_DAY = 86_400

# Production CSV column names (case-insensitive match attempted)
PROD_DATE_COL   = "Date/Time"
PROD_ENERGY_COL  = "Energy Produced (Wh)"   # original column name
PROD_ENERGY_COL2 = "Energy Delivered (Wh)"   # alternate column name (newer Enphase exports)

# Consumption CSV column names
CONS_DATE_COL   = "Date/Time"
CONS_ENERGY_COL = "Energy Consumed (kWh)"

# Interval report CSV column names (combined prod + cons + grid)
INTERVAL_DATE_COL     = "Date/Time"
INTERVAL_PROD_COL     = "Energy Produced (Wh)"
INTERVAL_CONS_COL     = "Energy Consumed (Wh)"
INTERVAL_EXPORT_COL   = "Exported to Grid (Wh)"
INTERVAL_IMPORT_COL   = "Imported from Grid (Wh)"

# EV CSV column names
EV_START_COL    = "Start Date/Time"
EV_END_COL      = "End Date/Time"
EV_ENERGY_COL   = "Energy consumed (Wh)"


# ── HELPERS ───────────────────────────────────────────────────────────────────

def _strip(s: str) -> str:
    return s.strip()


def _parse_date(s: str) -> Optional[date]:
    """Try several common date formats, return None on failure."""
    s = _strip(s)
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def _parse_datetime(s: str) -> Optional[datetime]:
    """Parse datetime from EV session timestamps."""
    s = _strip(s)
    for fmt in (
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M",
        "%Y-%m-%dT%H:%M:%S",
    ):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


def _clean_number(s: str) -> float:
    """Remove commas/spaces and cast to float."""
    return float(str(s).replace(",", "").replace(" ", "").strip())


# ── PARSERS ───────────────────────────────────────────────────────────────────

def parse_production(path: Path) -> dict[date, float]:
    """
    Returns {date: kWh_produced}.
    Source unit: Wh  →  converted to kWh.
    Skips the 'Total' summary row if present.
    """
    result: dict[date, float] = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        # Detect which column name this export uses
        fieldnames = reader.fieldnames or []
        if PROD_ENERGY_COL in fieldnames:
            energy_col = PROD_ENERGY_COL
        elif PROD_ENERGY_COL2 in fieldnames:
            energy_col = PROD_ENERGY_COL2
            logger.info("Production: using alternate column name '%s'", energy_col)
        else:
            raise ValueError(
                f"Production CSV missing expected energy column. "
                f"Expected '{PROD_ENERGY_COL}' or '{PROD_ENERGY_COL2}'. "
                f"Found: {fieldnames}"
            )
        for row in reader:
            date_str = row.get(PROD_DATE_COL, "").strip()
            if not date_str or date_str.lower() == "total":
                continue
            d = _parse_date(date_str)
            if d is None:
                logger.warning("Production: unrecognised date '%s', skipping", date_str)
                continue
            try:
                wh = _clean_number(row[energy_col])
            except (KeyError, ValueError) as e:
                logger.warning("Production: bad energy value on %s — %s", date_str, e)
                continue
            result[d] = wh / 1_000  # Wh → kWh
    logger.info("Production: loaded %d daily records from %s", len(result), path.name)
    return result


def parse_consumption(path: Path) -> dict[date, float]:
    """
    Returns {date: kWh_consumed}.
    Source unit: kWh (already).
    Skips the 'Total' summary row if present.
    """
    result: dict[date, float] = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            date_str = row.get(CONS_DATE_COL, "").strip()
            if not date_str or date_str.lower() == "total":
                continue
            d = _parse_date(date_str)
            if d is None:
                logger.warning("Consumption: unrecognised date '%s', skipping", date_str)
                continue
            try:
                kwh = _clean_number(row[CONS_ENERGY_COL])
            except (KeyError, ValueError) as e:
                logger.warning("Consumption: bad energy value on %s — %s", date_str, e)
                continue
            result[d] = kwh
    logger.info("Consumption: loaded %d daily records from %s", len(result), path.name)
    return result


def _prorate_session(
    start: datetime,
    end: datetime,
    total_wh: float,
) -> dict[date, float]:
    """
    Split a single EV charging session across calendar days proportionally
    by the number of seconds that fell on each day.

    Example: session 22:00 → 06:00 (8 hrs), 40 kWh
      Day 1 gets 2/8 = 25% → 10 kWh
      Day 2 gets 6/8 = 75% → 30 kWh

    Returns {date: kWh_allocated}.
    """
    total_secs = (end - start).total_seconds()
    if total_secs <= 0:
        return {}

    allocation: dict[date, float] = {}
    cursor = start

    while cursor.date() <= end.date():
        day_end = datetime(cursor.year, cursor.month, cursor.day) + timedelta(days=1)
        segment_end = min(end, day_end)
        secs_on_day = (segment_end - cursor).total_seconds()

        if secs_on_day > 0:
            fraction = secs_on_day / total_secs
            allocation[cursor.date()] = (total_wh * fraction) / 1_000  # Wh → kWh

        cursor = day_end  # jump to midnight of next day

    return allocation


def parse_ev_sessions(path: Path) -> dict[date, dict]:
    """
    Returns {date: {ev_kwh: float, ev_sessions: int}}.
    Sessions spanning midnight are prorated by elapsed seconds per day.
    Multiple sessions on the same date are summed.
    """
    result: dict[date, dict] = {}

    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader, 1):
            start_str = row.get(EV_START_COL, "").strip()
            end_str   = row.get(EV_END_COL,   "").strip()
            wh_str    = row.get(EV_ENERGY_COL, "").strip()

            start = _parse_datetime(start_str)
            end   = _parse_datetime(end_str)

            if start is None or end is None:
                logger.warning("EV row %d: unparseable timestamps '%s' / '%s', skipping",
                               i, start_str, end_str)
                continue
            if end <= start:
                logger.warning("EV row %d: end <= start (%s / %s), skipping",
                               i, start_str, end_str)
                continue

            try:
                total_wh = _clean_number(wh_str)
            except ValueError:
                logger.warning("EV row %d: bad energy value '%s', skipping", i, wh_str)
                continue

            daily = _prorate_session(start, end, total_wh)

            for d, kwh in daily.items():
                if d not in result:
                    result[d] = {"ev_kwh": 0.0, "ev_sessions": 0}
                result[d]["ev_kwh"] += kwh
                # Count each physical session once on its start date only
                if d == start.date():
                    result[d]["ev_sessions"] += 1

    logger.info("EV: loaded %d session records → %d daily entries from %s",
                sum(v["ev_sessions"] for v in result.values()), len(result), path.name)
    return result


# ── INTERVAL REPORT PARSER ───────────────────────────────────────────────────

def parse_interval_report(path: Path) -> tuple[dict[date, float], dict[date, float], dict[date, float], dict[date, float]]:
    """
    Parse a combined 15-min interval report CSV.
    Returns four dicts keyed by date:
      prod   {date: kWh}
      cons   {date: kWh}
      export {date: kWh}
      import {date: kWh}
    Aggregates all intervals per calendar day by summing Wh then converting to kWh.
    """
    prod:      dict[date, float] = {}
    cons:      dict[date, float] = {}
    export:    dict[date, float] = {}
    imp:       dict[date, float] = {}
    slot_rows: list[dict]        = []

    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []

        # Validate required columns
        if INTERVAL_PROD_COL not in fieldnames or INTERVAL_CONS_COL not in fieldnames:
            raise ValueError(
                f"Interval report missing required columns. "
                f"Expected '{INTERVAL_PROD_COL}' and '{INTERVAL_CONS_COL}'. "
                f"Found: {fieldnames}"
            )

        has_grid = INTERVAL_EXPORT_COL in fieldnames and INTERVAL_IMPORT_COL in fieldnames

        for i, row in enumerate(reader, 1):
            dt_str = row.get(INTERVAL_DATE_COL, "").strip()
            if not dt_str or dt_str.lower() == "total":
                continue

            # Parse datetime — extract date only
            d = None
            for fmt in ("%m/%d/%Y %H:%M", "%Y-%m-%d %H:%M", "%m/%d/%Y %H:%M:%S"):
                try:
                    from datetime import datetime as _dt
                    d = _dt.strptime(dt_str, fmt).date()
                    break
                except ValueError:
                    pass
            if d is None:
                # Try as plain date (daily summary row mixed in)
                d = _parse_date(dt_str)
            if d is None:
                logger.warning("Interval: unrecognised datetime '%s' on row %d, skipping", dt_str, i)
                continue

            try:
                prod_wh   = _clean_number(row.get(INTERVAL_PROD_COL, "0") or "0")
                cons_wh   = _clean_number(row.get(INTERVAL_CONS_COL, "0") or "0")
                exp_wh    = _clean_number(row.get(INTERVAL_EXPORT_COL, "0") or "0") if has_grid else 0.0
                imp_wh    = _clean_number(row.get(INTERVAL_IMPORT_COL, "0") or "0") if has_grid else 0.0
            except ValueError as e:
                logger.warning("Interval: bad value on row %d — %s, skipping", i, e)
                continue

            prod[d]   = prod.get(d, 0.0)   + prod_wh
            cons[d]   = cons.get(d, 0.0)   + cons_wh
            export[d] = export.get(d, 0.0) + exp_wh
            imp[d]    = imp.get(d, 0.0)    + imp_wh

            # Store raw slot row — derive hour and slot from datetime string
            try:
                from datetime import datetime as _dt2
                dt_obj = _dt2.strptime(dt_str.strip(), "%m/%d/%Y %H:%M")
                hour = dt_obj.hour
                slot = dt_obj.minute // 15
                slot_rows.append({
                    "date":        d.isoformat(),
                    "hour":        hour,
                    "slot":        slot,
                    "prod_wh":     prod_wh,
                    "cons_wh":     cons_wh,
                    "exported_wh": exp_wh,
                    "imported_wh": imp_wh,
                })
            except Exception:
                pass  # skip slot row if datetime parse fails

    # Convert Wh totals → kWh
    prod   = {d: round(v / 1000, 4) for d, v in prod.items()}
    cons   = {d: round(v / 1000, 4) for d, v in cons.items()}
    export = {d: round(v / 1000, 4) for d, v in export.items()}
    imp    = {d: round(v / 1000, 4) for d, v in imp.items()}

    logger.info("Interval report: %d daily records, %d slot rows parsed from %s",
                len(prod), len(slot_rows), path.name)
    return prod, cons, export, imp, slot_rows


# ── MERGE ─────────────────────────────────────────────────────────────────────

def merge(
    prod: dict[date, float],
    cons: dict[date, float],
    ev:   dict[date, dict],
    prod_provided: bool = True,
    cons_provided: bool = True,
    ev_provided:   bool = False,
    exported: dict | None = None,
    imported: dict | None = None,
    data_source: str = "daily",
) -> list[dict]:
    """
    Outer-join all sources on date.
    Computes derived fields:
      • net_kwh       = prod_kwh - cons_kwh
      • house_kwh     = cons_kwh - ev_kwh  (non-EV consumption)
      • self_suff_pct = prod / cons * 100
    data_source: 'daily' or 'interval' — stored per row in DB.
    """
    exported = exported or {}
    imported = imported or {}
    all_dates = sorted(set(prod) | set(cons) | set(ev))
    records = []

    for d in all_dates:
        prod_kwh     = round(prod.get(d, 0.0), 4)
        cons_kwh     = round(cons.get(d, 0.0), 4)
        ev_info      = ev.get(d, {"ev_kwh": 0.0, "ev_sessions": 0})
        ev_kwh       = round(ev_info["ev_kwh"], 4)
        ev_sess      = ev_info["ev_sessions"]
        exported_kwh = round(exported.get(d, 0.0), 4)
        imported_kwh = round(imported.get(d, 0.0), 4)

        house_kwh = round(max(cons_kwh - ev_kwh, 0.0), 4)
        net_kwh   = round(prod_kwh - cons_kwh, 4)
        self_suff = round(prod_kwh / cons_kwh * 100, 2) if cons_kwh > 0 else None

        records.append({
            "date":          d.isoformat(),
            "prod_kwh":      prod_kwh,
            "cons_kwh":      cons_kwh,
            "ev_kwh":        ev_kwh,
            "ev_sessions":   ev_sess,
            "house_kwh":     house_kwh,
            "net_kwh":       net_kwh,
            "self_suff_pct": self_suff,
            "exported_kwh":  exported_kwh,
            "imported_kwh":  imported_kwh,
            "data_source":   data_source,
            "prod_provided": prod_provided,
            "cons_provided": cons_provided,
            "ev_provided":   ev_provided,
        })

    logger.info("Merge complete: %d daily records (%d prod / %d cons / %d ev dates)",
                len(records), len(prod), len(cons), len(ev))
    return records


# ── PUBLIC API ────────────────────────────────────────────────────────────────

def run_etl(
    prod_path:     Optional[Path | str] = None,
    cons_path:     Optional[Path | str] = None,
    ev_path:       Optional[Path | str] = None,
    interval_path: Optional[Path | str] = None,
) -> list[dict]:
    """
    Main entry point — call from FastAPI or CLI.
    Supply either:
      - interval_path: combined 15-min report (replaces prod + cons)
      - prod_path / cons_path: separate daily CSVs
    ev_path is always optional and independent.
    Returns a list of dicts, one per date, ready for SQLite upsert.
    """
    if not any([prod_path, cons_path, ev_path, interval_path]):
        raise ValueError("At least one file path must be provided")

    if interval_path:
        i_prod, i_cons, i_export, i_import, slot_rows = parse_interval_report(Path(interval_path))
        ev   = parse_ev_sessions(Path(ev_path)) if ev_path else {}
        daily = merge(
            i_prod, i_cons, ev,
            prod_provided=True, cons_provided=True, ev_provided=bool(ev_path),
            exported=i_export, imported=i_import,
            data_source="interval",
        )
        return daily, slot_rows

    prod = parse_production(Path(prod_path)) if prod_path else {}
    cons = parse_consumption(Path(cons_path)) if cons_path else {}
    ev   = parse_ev_sessions(Path(ev_path))   if ev_path   else {}

    return merge(prod, cons, ev, prod_provided=bool(prod_path),
                 cons_provided=bool(cons_path), ev_provided=bool(ev_path),
                 data_source="daily"), []


# ── CLI ───────────────────────────────────────────────────────────────────────

def _cli() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s  %(message)s",
        stream=sys.stderr,
    )

    parser = argparse.ArgumentParser(description="Energy ETL — merge CSV sources to daily records")
    parser.add_argument("--prod", required=True, help="Production CSV path")
    parser.add_argument("--cons", required=True, help="Consumption CSV path")
    parser.add_argument("--ev",   default=None,  help="EV session CSV path (optional)")
    parser.add_argument("--out",  default=None,  help="Write merged CSV here (optional)")
    args = parser.parse_args()

    records = run_etl(args.prod, args.cons, args.ev)

    if args.out:
        out_path = Path(args.out)
        with open(out_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=records[0].keys())
            writer.writeheader()
            writer.writerows(records)
        print(f"Wrote {len(records)} records to {out_path}", file=sys.stderr)

    # Always print summary to stdout
    print(f"\n{'DATE':<12} {'PROD':>8} {'CONS':>8} {'EV':>8} {'HOUSE':>8} {'NET':>9} {'SELF%':>7}  {'SESS':>4}")
    print("-" * 72)
    for r in records:
        print(
            f"{r['date']:<12}"
            f" {r['prod_kwh']:>8.2f}"
            f" {r['cons_kwh']:>8.2f}"
            f" {r['ev_kwh']:>8.2f}"
            f" {r['house_kwh']:>8.2f}"
            f" {r['net_kwh']:>9.2f}"
            f" {str(r['self_suff_pct'] or '—'):>7}"
            f"  {r['ev_sessions']:>4}"
        )
    print(f"\nTotal records: {len(records)}")


if __name__ == "__main__":
    _cli()
