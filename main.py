"""
main.py — Wattwise FastAPI Application
===============================================
Endpoints:
  GET  /                    → serves index.html
  POST /import              → upload CSVs, run ETL, upsert to DB
  GET  /api/daily           → daily rows (default: from NET_START, net_valid only)
  GET  /api/summary         → aggregate stat card data
  GET  /api/monthly         → monthly aggregates
  GET  /api/health          → liveness check

Run locally:
    uvicorn main:app --host 0.0.0.0 --port 9521 --reload

See .env.example for all configuration options.
"""

import logging
import tempfile
import os
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

import config
import scheduler
from config import BE_VERSION, FE_VERSION, MAX_UPLOAD_BYTES
import db
from etl import run_etl

# ── LOGGING ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── APP ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title=config.APP_TITLE,
    version=config.APP_VERSION,
    docs_url="/docs",
)

# ── EXCEPTION HANDLERS — always return JSON, never HTML ──────────────────────
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={"detail": str(exc)},
    )

@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception on %s", request.url)
    return JSONResponse(
        status_code=500,
        content={"detail": f"Internal server error: {type(exc).__name__}"},
    )


# ── STARTUP ───────────────────────────────────────────────────────────────────
# ── LAST IMPORT RESULT (in-memory store) ─────────────────────────────────────
_last_import: dict = {}


@app.on_event("startup")
async def startup() -> None:
    """Initialise DB schema on every startup — safe and idempotent."""
    logger.info("Starting %s v%s", config.APP_TITLE, config.APP_VERSION)
    db.init_db()
    scheduler.start()
    logger.info("Listening on %s:%d", config.HOST, config.PORT)


# ── HEALTH ────────────────────────────────────────────────────────────────────
@app.get("/api/health")
async def health() -> JSONResponse:
    return JSONResponse(content={
        "status": "ok",
        "be_version": BE_VERSION,
        "fe_version": FE_VERSION,
        "db": str(config.DB_PATH),
    })


# ── VERSION ───────────────────────────────────────────────────────────────────
@app.get("/api/version")
async def version() -> JSONResponse:
    return JSONResponse(content={
        "be": BE_VERSION,
        "fe": FE_VERSION,
    })


# ── IMPORT ────────────────────────────────────────────────────────────────────
@app.post("/api/import")
async def import_data(
    prod_file:     Optional[UploadFile] = File(None, description="Daily production CSV (Wh)"),
    cons_file:     Optional[UploadFile] = File(None, description="Daily consumption CSV (kWh)"),
    ev_file:       Optional[UploadFile] = File(None, description="EV session CSV (optional)"),
    interval_file: Optional[UploadFile] = File(None, description="Combined 15-min interval report CSV (optional — replaces prod+cons)"),
) -> JSONResponse:
    """
    Accept CSV uploads, run ETL pipeline, upsert results into SQLite.
    All three files are written to a temp directory, processed, then cleaned up.
    EV file is optional — omit if no EV data is available.
    """
    def _file_provided(f) -> bool:
        return bool(f and getattr(f, "filename", None) and str(f.filename).strip())

    prod_provided     = _file_provided(prod_file)
    cons_provided     = _file_provided(cons_file)
    ev_provided       = _file_provided(ev_file)
    interval_provided = _file_provided(interval_file)

    if not any([prod_provided, cons_provided, ev_provided, interval_provided]):
        raise HTTPException(status_code=422, detail="At least one file must be provided")

    # Interval file overrides separate prod/cons
    if interval_provided and (prod_provided or cons_provided):
        raise HTTPException(status_code=422,
            detail="Provide either a combined interval report OR separate prod/cons CSVs, not both")

    logger.info("Import request received: prod=%s cons=%s ev=%s interval=%s",
                prod_file.filename     if prod_provided     else "none",
                cons_file.filename     if cons_provided     else "none",
                ev_file.filename       if ev_provided       else "none",
                interval_file.filename if interval_provided else "none")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        prod_path     = None
        cons_path     = None
        ev_path       = None
        interval_path = None

        if interval_provided:
            iv_bytes = await interval_file.read()
            if len(iv_bytes) > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413,
                    detail=f"Interval file exceeds the {MAX_UPLOAD_BYTES // (1024*1024)} MB limit")
            interval_path = tmp_path / "interval.csv"
            interval_path.write_bytes(iv_bytes)

        if prod_provided:
            prod_bytes = await prod_file.read()
            if len(prod_bytes) > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413,
                    detail=f"Production file exceeds the {MAX_UPLOAD_BYTES // (1024*1024)} MB limit")
            prod_path = tmp_path / "production.csv"
            prod_path.write_bytes(prod_bytes)

        if cons_provided:
            cons_bytes = await cons_file.read()
            if len(cons_bytes) > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413,
                    detail=f"Consumption file exceeds the {MAX_UPLOAD_BYTES // (1024*1024)} MB limit")
            cons_path = tmp_path / "consumption.csv"
            cons_path.write_bytes(cons_bytes)

        if ev_provided:
            ev_bytes = await ev_file.read()
            if ev_bytes and len(ev_bytes) > 0:
                if len(ev_bytes) > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413,
                        detail=f"EV file exceeds the {MAX_UPLOAD_BYTES // (1024*1024)} MB limit")
                ev_path = tmp_path / "ev_sessions.csv"
                ev_path.write_bytes(ev_bytes)

        # Run ETL
        try:
            records, slot_rows = run_etl(prod_path, cons_path, ev_path, interval_path)
        except Exception as exc:
            logger.exception("ETL failed")
            raise HTTPException(status_code=422, detail=f"ETL error: {exc}") from exc

        if not records:
            raise HTTPException(status_code=422, detail="ETL produced no records — check CSV format")

        # Upsert into DB
        try:
            count = db.upsert_records(records)
        except Exception as exc:
            logger.exception("DB upsert failed")
            raise HTTPException(status_code=500, detail=f"Database error: {exc}") from exc

        # Upsert raw interval rows if present
        if slot_rows:
            try:
                db.upsert_intervals(slot_rows)
            except Exception as exc:
                logger.warning("Interval upsert failed (non-fatal): %s", exc)

    logger.info("Import complete: %d inserted, %d updated, %d skipped, %d conflicts, source=%s",
                count["inserted"], count["updated"], count["skipped"],
                count.get("conflicts", 0),
                "interval" if interval_provided else "daily")
    _last_import.update({
        "timestamp":        datetime.now().isoformat(),
        "inserted":         count["inserted"],
        "updated":          count["updated"],
        "skipped":          count["skipped"],
        "processed":        count["processed"],
        "ev_included":      ev_path is not None,
        "interval_used":    interval_path is not None,
        "conflicts_logged": count.get("conflicts", 0),
        "data_source": "interval" if interval_provided else "daily",
        "files": {
            "prod":     prod_file.filename     if prod_provided     else None,
            "cons":     cons_file.filename     if cons_provided     else None,
            "ev":       ev_file.filename       if ev_provided       else None,
            "interval": interval_file.filename if interval_provided else None,
        },
    })
    return JSONResponse({
        "status":        "ok",
        "processed":     count["processed"],
        "inserted":      count["inserted"],
        "updated":       count["updated"],
        "skipped":       count["skipped"],
        "ev_included":   ev_path is not None,
        "interval_used": interval_path is not None,
        "conflicts":     count.get("conflicts", 0),
    })


# ── DATA ENDPOINTS ────────────────────────────────────────────────────────────
@app.get("/api/daily")
async def daily(
    from_date:  Optional[str] = Query(None, description="ISO date YYYY-MM-DD (default: NET_START)"),
    to_date:    Optional[str] = Query(None, description="ISO date YYYY-MM-DD (default: today)"),
    valid_only: bool          = Query(True,  description="Filter to net_valid rows only"),
) -> JSONResponse:
    """Daily energy records, used by the scatter plot."""
    try:
        return JSONResponse(content=db.get_daily(from_date, to_date, valid_only))
    except Exception as exc:
        logger.exception("daily query failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/summary")
async def summary(
    from_date: Optional[str] = Query(None, description="ISO date floor (default: NET_START)"),
) -> JSONResponse:
    """Aggregate stats for the dashboard stat cards."""
    try:
        return JSONResponse(content=db.get_summary(from_date))
    except Exception as exc:
        logger.exception("summary query failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/monthly")
async def monthly(
    from_date: Optional[str] = Query(None, description="ISO date floor (default: NET_START)"),
) -> JSONResponse:
    """Monthly aggregates for the coverage bar chart."""
    try:
        return JSONResponse(content=db.get_monthly_agg(from_date))
    except Exception as exc:
        logger.exception("monthly query failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ── BACKUP ───────────────────────────────────────────────────────────────────
@app.get("/api/backup")
async def backup() -> FileResponse:
    """Stream the SQLite database file as a download."""
    if not config.DB_PATH.exists():
        raise HTTPException(status_code=404, detail="Database file not found")
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"wattwise_db_{timestamp}.db"
    return FileResponse(
        path=str(config.DB_PATH),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── RESTORE ──────────────────────────────────────────────────────────────────
@app.post("/api/restore")
async def restore(db_file: UploadFile = File(..., description="SQLite .db backup file")):
    """Accept a .db upload, validate integrity, atomically swap in place."""
    data = await db_file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"File exceeds the {MAX_UPLOAD_BYTES // (1024*1024)} MB limit")

    # Write to a temp file first for integrity check
    tmp = config.DATA_DIR / "_restore_tmp.db"
    try:
        tmp.write_bytes(data)
        # Validate it's a real SQLite file with a passing integrity check
        import sqlite3 as _sqlite3
        with _sqlite3.connect(str(tmp)) as _conn:
            result = _conn.execute("PRAGMA integrity_check").fetchone()
            if result[0] != "ok":
                raise HTTPException(status_code=400, detail=f"Integrity check failed: {result[0]}")
        # Atomic swap — rename over the live DB
        tmp.replace(config.DB_PATH)
        logger.info("Database restored from uploaded file: %s (%d bytes)", db_file.filename, len(data))
        return JSONResponse(content={"success": True, "message": "Database restored successfully"})
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Restore failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Restore failed: {exc}")
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


# ── CONFLICTS ────────────────────────────────────────────────────────────────
@app.get("/api/conflicts")
async def conflicts(unreviewed_only: bool = Query(False)) -> JSONResponse:
    """Return import conflict records."""
    return JSONResponse(content=db.get_conflicts(unreviewed_only))


@app.post("/api/conflicts/review")
async def review_conflicts(request: Request) -> JSONResponse:
    """Mark conflict IDs as reviewed."""
    body = await request.json()
    ids  = body.get("ids", [])
    db.mark_conflicts_reviewed(ids)
    return JSONResponse(content={"reviewed": len(ids)})


# ── SETTINGS ─────────────────────────────────────────────────────────────────
@app.get("/api/settings")
async def settings() -> JSONResponse:
    """System info for the Settings page."""
    db_size = config.DB_PATH.stat().st_size if config.DB_PATH.exists() else 0
    summary = db.get_summary()
    return JSONResponse(content={
        "be_version":    BE_VERSION,
        "fe_version":    FE_VERSION,
        "db_path":       str(config.DB_PATH),
        "db_size_bytes": db_size,
        "db_size_mb":    round(db_size / (1024 * 1024), 2),
        "record_count":  summary.get("total_days", 0),
        "first_date":    summary.get("first_date"),
        "last_date":     summary.get("last_date"),
        "backup_dir":    str(config.BACKUP_DIR),
        "conflict_count": db.get_conflict_count(),
        "setup":         db.get_setup(),
    })


# ── SETUP / ONBOARDING ────────────────────────────────────────────────────────
@app.get("/api/setup")
async def get_setup() -> JSONResponse:
    """Return current setup/onboarding state."""
    return JSONResponse(content={
        "setup_complete": db.is_setup_complete(),
        "settings": db.get_setup(),
    })


@app.post("/api/setup")
async def save_setup(request: Request) -> JSONResponse:
    """
    Save onboarding / settings values.
    Accepts JSON body with any combination of:
      cons_start    — ISO date YYYY-MM-DD (system install / consumption valid from)
      prod_start    — ISO date YYYY-MM-DD (solar production fully valid from)
      net_start     — ISO date YYYY-MM-DD (default display floor)
      port          — port number (default 9521, for display purposes)
      timezone      — IANA timezone string e.g. 'America/New_York'
      data_type     — 'interval' or 'daily'
      has_ev        — 'true' / 'false'
      has_battery   — 'true' / 'false'
      setup_complete — '1' to mark onboarding done
    """
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    allowed = {
        "cons_start", "prod_start", "net_start",
        "timezone", "data_type", "has_ev", "has_battery", "port", "setup_complete",
    }
    filtered = {k: v for k, v in data.items() if k in allowed}
    if not filtered:
        raise HTTPException(status_code=400, detail="No valid fields provided")

    db.save_setup(filtered)

    # If dates were updated, recompute validity flags on all existing rows
    date_keys = {"cons_start", "prod_start", "net_start"}
    if date_keys & set(filtered.keys()):
        db.recompute_validity_flags()

    return JSONResponse(content={"saved": list(filtered.keys()), "setup": db.get_setup()})


# ── LAST IMPORT ───────────────────────────────────────────────────────────────
@app.get("/api/import/last")
async def import_last() -> JSONResponse:
    """Return the last import result (in-memory, resets on container restart)."""
    return JSONResponse(content=_last_import if _last_import else {})


# ── DIAGNOSTICS ───────────────────────────────────────────────────────────────
@app.get("/api/diagnostics")
async def diagnostics() -> JSONResponse:
    """Passive diagnostics — no writes."""
    db_ok      = config.DB_PATH.exists()
    db_size    = config.DB_PATH.stat().st_size if db_ok else 0
    backup_ok  = config.BACKUP_DIR.exists() and os.access(config.BACKUP_DIR, os.W_OK)
    return JSONResponse(content={
        "db_reachable":     db_ok,
        "db_size_bytes":    db_size,
        "backup_dir_ok":    backup_ok,
        "backup_dir":       str(config.BACKUP_DIR),
        "backup_status":    scheduler.get_status(),
    })


@app.get("/api/diagnostics/run")
async def diagnostics_run() -> JSONResponse:
    """Active diagnostics — performs a write test on the DB."""
    results: dict = {}
    # DB write test
    try:
        with db.get_db() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS _diag_test (id INTEGER PRIMARY KEY)")
            conn.execute("INSERT INTO _diag_test DEFAULT VALUES")
            conn.execute("DELETE FROM _diag_test")
            conn.execute("DROP TABLE _diag_test")
        results["db_write_ok"] = True
    except Exception as exc:
        results["db_write_ok"] = False
        results["db_write_error"] = str(exc)
    # Backup dir write test
    try:
        test_file = config.BACKUP_DIR / ".diag_test"
        test_file.touch()
        test_file.unlink()
        results["backup_write_ok"] = True
    except Exception as exc:
        results["backup_write_ok"] = False
        results["backup_write_error"] = str(exc)
    results["timestamp"] = datetime.now().isoformat()
    return JSONResponse(content=results)


# ── BACKUP STATUS ─────────────────────────────────────────────────────────────
@app.get("/api/backup/status")
async def backup_status() -> JSONResponse:
    """Return current backup scheduler state."""
    return JSONResponse(content=backup.get_status())


# ── SOLAR FINGERPRINT ────────────────────────────────────────────────────────
@app.get("/api/solar-fingerprint")
async def solar_fingerprint(year: Optional[int] = Query(None, description="Year e.g. 2025 — omit for all years")) -> JSONResponse:
    """All daily production curves for one year or all years (15-min resolution, watts)."""
    return JSONResponse(content=db.get_solar_fingerprint(year))


# ── HEATMAP ──────────────────────────────────────────────────────────────────
@app.get("/api/heatmap")
async def heatmap(from_date: Optional[str] = Query(None)) -> JSONResponse:
    """Daily net_kwh values for the self-sufficiency heatmap."""
    return JSONResponse(content=db.get_heatmap(from_date))


# ── INTERVAL ANALYTICS ENDPOINTS ─────────────────────────────────────────────
@app.get("/api/hourly-profile")
async def hourly_profile(season: Optional[str] = Query(None)) -> JSONResponse:
    """Hour-of-day production and consumption profile with SD/SEM."""
    return JSONResponse(content=db.get_hourly_profile(season))


@app.get("/api/dow-profile")
async def dow_profile(
    season:     Optional[str] = Query(None),
    exclude_ev: bool          = Query(False),
) -> JSONResponse:
    """Day-of-week consumption profile with SD/SEM."""
    return JSONResponse(content=db.get_dow_profile(season, exclude_ev))


@app.get("/api/grid-dependency")
async def grid_dependency(season: Optional[str] = Query(None)) -> JSONResponse:
    """Hour-of-day grid import/export profile with SD."""
    return JSONResponse(content=db.get_grid_dependency(season))


@app.get("/api/peak-demand")
async def peak_demand(
    limit:      int  = Query(20, ge=5, le=100),
    exclude_ev: bool = Query(False),
) -> JSONResponse:
    """Top N highest 15-min consumption slots."""
    return JSONResponse(content=db.get_peak_demand(limit, exclude_ev))


# ── STATIC FILES — mounted last so /api/* routes take priority ───────────────
@app.get("/", include_in_schema=False)
async def root() -> FileResponse:
    return FileResponse(config.STATIC_DIR / "index.html")

app.mount("/static", StaticFiles(directory=str(config.STATIC_DIR)), name="static")


# ── ENTRYPOINT ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=config.HOST, port=config.PORT, reload=False)
