# Wattwise — Development Guide

**Current version:** 0.1.0-beta  
**Stack:** FastAPI + SQLite + Vanilla JS + Chart.js  
**Default port:** 9521  
**Container:** Docker Compose (named volumes)

---

## Table of Contents

1. [File Structure](#1-file-structure)
2. [Architecture](#2-architecture)
3. [Database Schema](#3-database-schema)
4. [ETL Pipeline](#4-etl-pipeline)
5. [API Endpoints](#5-api-endpoints)
6. [Frontend Architecture](#6-frontend-architecture)
7. [Dashboard Sections & Layout](#7-dashboard-sections--layout)
8. [Settings Page](#8-settings-page)
9. [Onboarding Wizard](#9-onboarding-wizard)
10. [Known Gotchas & Critical Rules](#10-known-gotchas--critical-rules)
11. [Version History](#11-version-history)
12. [Pending Backlog](#12-pending-backlog)

---

## 1. File Structure

```
energy/
├── config.py          — Constants: paths, port, version strings, optional date overrides
├── etl.py             — CSV parsing, interval aggregation, EV midnight proration
├── db.py              — Schema, migrations, upsert, all query helpers, setup/settings store
├── main.py            — FastAPI app: all routes
├── scheduler.py       — Backup daemon thread (daily×7, weekly×4 retention)
├── static/
│   └── index.html     — Entire frontend: JS + CSS inline, no build step
├── Dockerfile
├── compose.yml
├── requirements.txt
├── .env.example
├── README.md          — User-facing install and operation guide
├── DOCKER.md          — Docker architecture, volumes, networking, reverse proxy
└── DEVELOPMENT.md     — This file: architecture, contributing, coding standards
```

---

## 2. Architecture

### Data flow

1. User uploads CSV(s) via import modal → XHR POST to `/api/import`
2. FastAPI writes to temp dir, calls `run_etl()`
3. ETL parses and aggregates → returns `(daily_records, slot_rows)` tuple
4. `db.upsert_records(daily_records)` — dedup-aware, partial-import safe
5. `db.upsert_intervals(slot_rows)` — 15-min raw slots (interval CSV only)
6. Conflict detection runs automatically when interval data overwrites daily data
7. Frontend fetches `/api/summary`, `/api/daily`, `/api/monthly` on load

### Scheduler

`scheduler.py` runs as a daemon thread started at app startup. Fires at 02:00 daily and 02:00 Sunday. Creates `wattwise_db_YYYYMMDD-HHMMSS_daily.db` / `_weekly.db`. Prunes to 7 daily + 4 weekly. Skips silently if backup directory is unmounted or unreachable.

### Config constants

```python
# Read from env vars — override via .env or environment
CONS_START  # date consumption monitoring became reliable (optional)
PROD_START  # date solar production became fully valid (optional)
NET_START   # default display floor for charts (optional)
```

These are normally set via the onboarding wizard and stored in `app_settings`. Env var overrides take priority over DB values.

---

## 3. Database Schema

### `daily_energy` — one row per calendar date

| Column | Type | Notes |
|---|---|---|
| `date` | TEXT PK | ISO-8601 YYYY-MM-DD |
| `prod_kwh` | REAL | Solar production |
| `cons_kwh` | REAL | Total consumption |
| `ev_kwh` | REAL | EV portion (prorated across midnight) |
| `ev_sessions` | INT | Sessions starting this date |
| `house_kwh` | REAL | `cons_kwh − ev_kwh` |
| `net_kwh` | REAL | `prod_kwh − cons_kwh` |
| `self_suff_pct` | REAL | `prod/cons × 100`, NULL if cons=0 |
| `exported_kwh` | REAL | Grid export (interval data only) |
| `imported_kwh` | REAL | Grid import (interval data only) |
| `data_source` | TEXT | `'daily'` or `'interval'` |
| `cons_valid` | INT | 1 when date ≥ CONS_START (or always 1 if unset) |
| `prod_valid` | INT | 1 when date ≥ PROD_START (or always 1 if unset) |
| `net_valid` | INT | 1 when date ≥ NET_START (or always 1 if unset) |
| `imported_at` | TEXT | UTC timestamp of last upsert |

### `interval_data` — raw 15-min slots

| Column | Type | Notes |
|---|---|---|
| `id` | INT PK | Auto-increment |
| `date` | TEXT | ISO-8601 date |
| `hour` | INT | 0–23 |
| `slot` | INT | 0–3 (15-min slot within hour) |
| `prod_wh` | REAL | Production in Wh |
| `cons_wh` | REAL | Consumption in Wh |
| `exported_wh` | REAL | Grid export in Wh |
| `imported_wh` | REAL | Grid import in Wh |

Unique constraint on `(date, hour, slot)` — idempotent upserts.

### `import_conflicts` — audit table

| Column | Notes |
|---|---|
| `date` | Date of conflicting record |
| `field` | `'prod_kwh'` or `'cons_kwh'` |
| `daily_val` | Value from daily CSV |
| `interval_val` | Value from interval CSV |
| `abs_diff` / `pct_diff` | Magnitude of conflict |
| `direction` | `'DAILY HIGH'` or `'DAILY LOW'` |
| `reviewed` | 0/1 — user has reviewed |

Conflict threshold: abs_diff > 0.5 kWh AND pct_diff > 2% (both must be true).

### `app_settings` — key/value store

Persists onboarding wizard values and user preferences:

| Key | Example value |
|---|---|
| `cons_start` | `2024-10-17` |
| `prod_start` | `2024-12-17` |
| `net_start` | `2025-01-01` |
| `timezone` | `America/New_York` |
| `data_type` | `interval` |
| `has_ev` | `true` |
| `has_battery` | `false` |
| `port` | `9521` |
| `setup_complete` | `1` |

### DB migrations

`init_db()` runs idempotent `ALTER TABLE` migrations on every startup. To add a new column: add to `SCHEMA_SQL` AND add an `ALTER TABLE` migration in the migrations list. Each migration runs in its own `with get_db()` connection — critical, `executescript()` breaks subsequent commits in the same connection.

---

## 4. ETL Pipeline

### Input formats

**Daily CSVs (separate files):**
- Production: `Date/Time, Energy Produced (Wh)` or `Energy Delivered (Wh)`
- Consumption: `Date/Time, Energy Consumed (kWh)`
- EV sessions: `Start Date/Time`, `End Date/Time`, `Energy consumed (Wh)`

**Combined interval report (15-min):**
- Single file: `Date/Time, Energy Produced (Wh), Energy Consumed (Wh), Exported to Grid (Wh), Imported from Grid (Wh)`
- Timestamps in local wall clock time — DST-aware display handled in frontend

### `run_etl()` return value

Always returns a `(daily_records, slot_rows)` tuple. `slot_rows` is `[]` for daily CSV imports.

### Partial import logic

`prod_provided`, `cons_provided`, `ev_provided` flags preserve existing DB values for columns not in the current import. EV data is never overwritten by a prod/cons-only import.

### Conflict detection

When interval data overwrites a `data_source='daily'` row, values are compared. If both threshold conditions are met, a row is written to `import_conflicts`. Daily CSVs systematically under-report vs interval sums due to rounding — expect DAILY LOW conflicts on low-production days.

---

## 5. API Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/api/health` | Health check |
| GET | `/api/version` | BE/FE version strings |
| POST | `/api/import` | Upload CSVs — `prod_file`, `cons_file`, `ev_file`, `interval_file` |
| GET | `/api/daily` | Daily rows (`from_date`, `to_date`, `valid_only` params) |
| GET | `/api/summary` | Aggregated totals (`from_date` param) |
| GET | `/api/monthly` | Monthly aggregates (`from_date` param) |
| GET | `/api/backup` | Download DB as `.db` file |
| POST | `/api/restore` | Upload DB file — integrity check + atomic swap |
| GET | `/api/conflicts` | Import conflict records (`unreviewed_only` param) |
| POST | `/api/conflicts/review` | Mark conflict IDs as reviewed |
| GET | `/api/settings` | System info + conflict count + setup values |
| GET | `/api/setup` | Current onboarding/setup state |
| POST | `/api/setup` | Save onboarding/setup values |
| GET | `/api/import/last` | Last import result (in-memory, resets on restart) |
| GET | `/api/diagnostics` | Static diagnostics |
| POST | `/api/diagnostics/run` | Active diagnostics check |
| GET | `/api/backup/status` | Scheduler status + backup file counts |
| GET | `/api/solar-fingerprint` | All daily curves (`year` param optional) |
| GET | `/api/heatmap` | Daily net_kwh for heatmap (`from_date` param) |
| GET | `/api/hourly-profile` | Hour-of-day prod/cons profile (`season` param) |
| GET | `/api/dow-profile` | Day-of-week consumption (`season`, `exclude_ev` params) |
| GET | `/api/grid-dependency` | Hour-of-day grid import/export (`season` param) |
| GET | `/api/peak-demand` | Top-N highest 15-min consumption slots |

**Important:** `app.mount("/static", ...)` must come **after** all route definitions or StaticFiles intercepts API paths.

---

## 6. Frontend Architecture

### Structure

Single `static/index.html` — all JS and CSS inline. No build step. No npm. Chart.js loaded from `cdnjs.cloudflare.com`. No other CDN scripts.

### Script initialisation order (critical)

These must be defined **before** `applyTheme()` runs at boot:

```
cssVar() → chartDefaults() → tooltipStyle() → applyTheme()
```

`applyTheme()` is called synchronously at top-level during script init. Any function it calls must be a `function` declaration (hoisted), not a `const`/`let` expression.

### Theme system

`html[data-theme]` attribute set to `'dark'` or `'light'`. CSS vars defined per theme. `chartDefaults()` returns `gridSoft: 'rgba(128,128,128,0.15)'` — use this for chart grid lines, NOT `cssVar('--border')` which is too dark in light mode.

### LocalStorage keys

| Key | Contents |
|---|---|
| `wattwise_theme` | `'dark'` / `'light'` / `'system'` |
| `wattwise_prefs` | `{key: bool, ..., order: [key1, key2, ...]}` |

---

## 7. Dashboard Sections & Layout

### Section registry

```javascript
{ key: 'cards',    label: 'Stat Cards',                 half: false }
{ key: 'scatter',  label: 'Prod vs Cons Scatter',        half: false }
{ key: 'trend',    label: 'Net Energy Trend (30d)',       half: true  }
{ key: 'dow',      label: 'Day-of-Week Consumption',     half: false }  // enforced half internally
{ key: 'myoy',     label: 'Monthly + Year-over-Year',    half: false }
{ key: 'solar',    label: 'Solar Fingerprint',           half: false, requiresInterval: true }
{ key: 'interval', label: 'Interval Analytics (15-min)', half: false, requiresInterval: true }
```

`requiresInterval: true` sections are hidden when no interval data exists.

### Render pipeline

1. `loadDashboard()` fetches data, builds `visibleSecs` (ordered, filtered)
2. Calls each render function in order with `await`
3. Post-process: desktop (>768px) pairs adjacent `half:true` sections into `two-col` rows; mobile/tablet builds swipeable carousel

### Carousel (mobile/tablet)

- **≤540px:** Every section = one full-width swipe page
- **541–768px:** Adjacent `half:true` pairs share one page
- **>768px:** No carousel — standard vertical scroll

### Critical render rules

- `renderCards()` calls `main.innerHTML = ''` — must always be the first section rendered
- The render loop uses `await (_renderFns[key]?.())` — all render functions are async; missing `await` means DOM nodes aren't present when post-process pairing runs

---

## 8. Settings Page

Rendered by `renderSettings()`. Two-column grid layout. Sections:

- **System** — versions, DB path/size, record count, date range, conflict count
- **Last Import** — timestamp, inserted/updated/skipped, source, conflicts, filenames
- **Backup & Restore** — backup dir status, last daily/weekly, download + restore
- **System Setup** — onboarding values (port, timezone, dates, data type, EV, battery) with RE-RUN SETUP WIZARD button
- **Theme** — light/system/dark toggle
- **Diagnostics** — DB reachable, backup dir status, run active check
- **Dashboard Sections** — draggable reorder, checkbox show/hide, SAVE & RELOAD
- **Import Conflict Report** — grouped by year, mark reviewed

---

## 9. Onboarding Wizard

Triggered on first load when `setup_complete` is not set in `app_settings`. Steps:

1. **Welcome** — intro
2. **Data type** — interval vs daily CSV
3. **EV** — enable/disable EV tracking, CSV format shown
4. **Battery** — preference saved, marked PENDING FEATURE
5. **Dates** — port (default 9521), timezone, cons_start, prod_start, net_start (all optional except port)
6. **Done** — shows dashboard URL (`http://<hostname>:<port>`), bookmark prompt

State stored in module-level `_wizData` object. Saved via `POST /api/setup` on finish. Can be re-run from Settings → System Setup → RE-RUN SETUP WIZARD.

---

## 10. Known Gotchas & Critical Rules

### DB layer

- `executescript()` auto-commits — migrations must run in **separate** `with get_db()` connections after the `executescript` block
- `get_db()` is a `@contextmanager` — use as `with get_db() as conn:`
- `run_etl()` always returns a 2-tuple `(records, slot_rows)` — both paths return this
- `get_cutoffs()` reads live from DB each call — no module-level caching of date values

### Frontend

- `renderCards()` clears `main.innerHTML = ''` — must always be first render call
- Solar fingerprint and interval DOM rebuild on every call — toggle listeners must use `querySelector` at click time, never closed-over DOM vars
- `'Share Tech Mono'` inside single-quoted JS strings breaks parsing — use double quotes or a font variable
- Template literals with nested single quotes break Safari's stricter parser — use `createElement` + `style.cssText` or string concatenation

### Chart.js

- `parsing: false` requires numeric x/y — string dates silently produce empty charts
- Use `gridSoft` from `chartDefaults()` for grid lines — `cssVar('--border')` is too dark in light mode

### FastAPI

- `app.mount("/static", ...)` must come **after** all `@app.get`/`@app.post` routes
- `UploadFile.filename` can be `""` for unprovided optional files — use `_file_provided()` helper

### DST

- Interval CSVs use local wall clock time — no correction needed for raw data
- Solar fingerprint DST toggle uses US Eastern rules only — incorrect for other timezones

---

## 11. Version History

| Version | Changes |
|---|---|
| **0.1.0-beta** | Initial public release. Full dashboard feature set: daily/interval CSV import, scatter, trend, monthly YoY, DOW, solar fingerprint, interval analytics, stat cards, conflict detection, automated backups, drag-to-reorder sections, mobile carousel, light/dark theme, onboarding wizard, setup API, named Docker volumes. |

---

## 12. Pending Backlog

### Dashboard

| ID | Feature |
|---|---|
| D-04 | CSV export — download filtered daily data |
| D-05 | User annotations — notes on specific dates shown as chart markers |
| D-06 | Weather overlay — temperature/cloud cover correlated with production |
| D-07 | Tariff/cost conversion — kWh → $ with configurable rate |

### New data sources

| ID | Feature |
|---|---|
| DS-01 | Battery storage — charge/discharge, state of charge |
| DS-02 | Grid import/export display — columns exist, not yet surfaced on dashboard |

### Backend

| ID | Feature |
|---|---|
| B-01 | Scheduled auto-import — folder watch for new CSVs |
| B-03 | REST pagination on /api/daily |
| B-04 | Multi-system support (system_id column) |
| B-05 | PostgreSQL migration path |
| B-08 | Enphase API OAuth2 auto-pull |

### Architecture

| ID | Feature |
|---|---|
| A-01 | Test suite — pytest for ETL/API, Playwright for FE smoke |
| A-02 | JS module split (needs build step) |

### Future platforms

| ID | Feature |
|---|---|
| F-01 | iOS SwiftUI app |

---

## 13. Local Development (without Docker)

Running outside Docker is faster for active development — no rebuild cycle.

### Prerequisites

- Python 3.12+
- pip

### Setup

```bash
# Clone the repo
git clone https://github.com/youruser/wattwise.git
cd wattwise

# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate       # Mac/Linux
.venv\Scripts\activate          # Windows

# Install dependencies
pip install -r requirements.txt

# Create a local .env
cp .env.example .env
# Edit .env — set DB_PATH to a local path, e.g. DB_PATH=./data/wattwise.db
mkdir -p data

# Start the dev server with auto-reload
uvicorn main:app --host 0.0.0.0 --port 9521 --reload
```

The `--reload` flag restarts the server automatically on any Python file change. The frontend (`static/index.html`) is served directly — just hard-refresh the browser after edits.

### Environment for local dev

Minimum `.env` for local development:

```
PORT=9521
DB_PATH=./data/wattwise.db
BACKUP_DIR=./data/backups
TZ=UTC
DEBUG=true
PYTHONUNBUFFERED=1
```

---

## 14. Contributing

### Before you start

- Open an issue or comment on an existing one before starting significant work — avoids duplicate effort
- Check the [Pending Backlog](#12-pending-backlog) for known planned items
- For bug fixes, a PR with a clear description of the problem and fix is welcome without prior discussion

### Branching

```
main          — stable, tagged releases only
dev           — integration branch for new features
feature/xxx   — individual feature branches, branched from dev
fix/xxx       — bug fix branches
```

### Commit messages

Use plain, descriptive present-tense messages:

```
Add battery storage preference to onboarding wizard
Fix EV midnight proration for sessions > 24h
Update Chart.js to 4.5.0
```

Prefix with a scope if helpful: `FE:`, `BE:`, `DB:`, `Docker:`, `Docs:`

### Pull request checklist

- [ ] Tested locally with both daily and interval CSV imports
- [ ] No personal data, hardcoded paths, or IP addresses introduced
- [ ] Version strings updated in `config.py` if any Python or HTML file changed
- [ ] `DEVELOPMENT.md` updated if architecture changed
- [ ] No new CDN dependencies added (homelab deployments may have restricted egress)

---

## 15. Coding Standards

### Python

- **Style:** PEP 8. Line length 100 characters.
- **Type hints:** use them on all function signatures
- **Docstrings:** one-line summary for every public function; multi-line for anything non-obvious
- **Error handling:** catch specific exceptions, not bare `except:`. Log with `logger.exception()` to preserve tracebacks
- **DB access:** always use the `with get_db() as conn:` context manager — never manage connections manually
- **No ORM:** direct SQL only. Keep queries in `db.py` — no raw SQL in `main.py` or `etl.py`

```python
# Good
def get_daily_rows(from_date: str, to_date: Optional[str] = None) -> list[dict]:
    """Return daily_energy rows between from_date and to_date inclusive."""
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]

# Bad — bare except, no type hints, SQL outside db.py
def get_rows(d):
    try:
        conn = sqlite3.connect(DB_PATH)
        return conn.execute("SELECT * FROM daily_energy").fetchall()
    except:
        return []
```

### DB migrations

When adding a new column:

1. Add the column definition to `SCHEMA_SQL` (for fresh installs)
2. Add an idempotent `ALTER TABLE` migration to the migrations list in `init_db()`
3. Each migration must run in its **own** `with get_db()` block — never inside the `executescript()` call

```python
# Good — separate connection for each migration
migrations = [
    "ALTER TABLE daily_energy ADD COLUMN new_col REAL NOT NULL DEFAULT 0",
]
for migration in migrations:
    try:
        with get_db() as conn:
            conn.execute(migration)
    except sqlite3.OperationalError:
        pass  # Column already exists
```

### FastAPI routes

- All routes return `JSONResponse` with explicit `content=` dict
- Use `Optional` params with `Query(None, description="...")` for all optional query params
- `app.mount("/static", ...)` must remain **last** in `main.py` — StaticFiles will intercept API routes if mounted first
- Use `_file_provided(f)` helper to check uploaded files — `UploadFile.filename` can be an empty string for unprovided optional files

### JavaScript (frontend)

The entire frontend is one file — `static/index.html`. There is no build step.

**Initialisation order is critical:**

```javascript
// These must be function declarations (hoisted), defined before applyTheme() runs:
function cssVar(name) { ... }
function chartDefaults() { ... }
function tooltipStyle() { ... }
function applyTheme() { ... }

// applyTheme() is called synchronously at top level — any const/let defined
// after it will not yet exist when it runs.
applyTheme();
```

**Chart grid lines:** always use `gridSoft` from `chartDefaults()`, never `cssVar('--border')`:

```javascript
const { color, grid, gridSoft } = chartDefaults();
// grid lines:
color: gridSoft   // correct — works in both light and dark mode
color: grid       // also ok for axes
color: cssVar('--border')  // wrong — too dark in light mode
```

**Font family in JS strings:**

```javascript
// Wrong — breaks string parsing
const font = 'Share Tech Mono, monospace';

// Correct — use double quotes for the family name
const font = "'Share Tech Mono', monospace";
// Or define a variable:
const mono = "'Share Tech Mono', monospace";
```

**DOM listeners in rebuilt sections:** solar fingerprint and interval dashboards rebuild their DOM on every render call. Always wire event listeners with `querySelector` at click time:

```javascript
// Wrong — el is stale after next render
const btn = sec.querySelector('#toggle-btn');
btn.addEventListener('click', () => { ... });

// Correct — querySelector at the time of the click
someParent.addEventListener('click', e => {
    if (e.target.id === 'toggle-btn') { ... }
});
// Or re-query inside the handler via the section element
```

**Async render functions:** all render functions in `_renderFns` must be `async` and must be `await`-ed in the render loop. Without `await`, the DOM pairing post-process runs before nodes are present.

### Adding a new API endpoint

1. Add the route handler in `main.py` **before** `app.mount("/static", ...)`
2. Add any required query helpers to `db.py`
3. Document the endpoint in the API Endpoints table in this file
4. Test with `curl` before wiring the frontend

### Adding a new dashboard section

1. Add an entry to the `SECTIONS` array in `index.html`:
   ```javascript
   { key: 'mysection', label: 'My Section', half: false }
   // half: true  → takes 50% width on desktop, pairs with adjacent half sections
   // half: false → full width
   // requiresInterval: true → hidden when no interval data exists
   ```
2. Add a render function `async function renderMySection(...)` 
3. Add it to `_renderFns` map
4. Follow the `renderCards()` pattern — return early gracefully if no data
5. Add it to the Settings → Dashboard Sections draggable list (handled automatically by the SECTIONS registry)

---

## 16. Versioning

Version strings live in `config.py`:

```python
APP_VERSION: str = "0.1.0-beta"   # overall app version
BE_VERSION:  str = "0.1.0-beta"   # increment on any Python file change
FE_VERSION:  str = "0.1.0-beta"   # increment on any index.html change
```

Version scheme: `MAJOR.MINOR.PATCH[-label]`

- `MAJOR` — breaking change to data format or API
- `MINOR` — new feature, new endpoint, new chart
- `PATCH` — bug fix, copy change, style tweak
- Label: `beta` until the first stable public release

Also update `Dockerfile`:
```
LABEL version="x.x.x"
```
