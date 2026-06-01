# ☀ Wattwise

A self-hosted solar energy dashboard for your homelab. Visualises solar production, household consumption, EV charging, and grid import/export — all from CSV exports your monitoring system already provides.

**Version:** 0.1.0-beta | **Stack:** Python · FastAPI · SQLite · Vanilla JS · Chart.js | **Container:** Single Docker container

---

## Contents

- [What it does](#what-it-does)
- [Requirements](#requirements)
- [Installation](#installation)
- [First run — onboarding wizard](#first-run--onboarding-wizard)
- [Importing your data](#importing-your-data)
- [CSV formats](#csv-formats)
- [Configuration](#configuration)
- [Daily operation](#daily-operation)
- [Backups](#backups)
- [Upgrading](#upgrading)
- [Troubleshooting](#troubleshooting)
- [FAQ](#faq)
- [Notes & limitations](#notes--limitations)

---

## What it does

| Feature | Detail |
|---|---|
| Production vs consumption | Daily scatter, monthly bar, year-over-year comparison |
| Net energy & self-sufficiency | 30-day trend, stat cards with configurable date range |
| EV charging | Separated from household load, prorated across midnight |
| 15-min interval analytics | Hourly profiles, grid dependency, peak demand, solar fingerprint |
| Import conflict detection | Flags mismatches between daily and interval CSV data |
| Automated backups | Daily × 7 + weekly × 4 SQLite snapshots |
| Mobile friendly | Swipeable carousel layout on phones and tablets |
| Light / dark theme | System preference + manual toggle |

---

## Requirements

- **Docker** with **Compose V2** — that's `docker compose` (with a space), not the legacy `docker-compose`
- Docker Desktop (Mac/Windows) includes Compose V2 by default
- Linux: install the `docker-compose-plugin` package if `docker compose version` returns an error

No other dependencies. Everything else runs inside the container.

---

## Installation

```bash
# 1. Get the files
git clone https://github.com/youruser/wattwise.git
cd wattwise

# 2. Create your environment file
cp .env.example .env

# 3. Edit .env — minimum required: set TZ to your timezone
#    Example: TZ=America/New_York  or  TZ=Europe/London  or  TZ=Australia/Sydney
nano .env

# 4. Build and start
docker compose up -d --build

# 5. Check it started cleanly
docker compose logs -f
# Press Ctrl+C to stop watching logs

# 6. Open the dashboard
# http://localhost:9521
```

On first load the onboarding wizard will appear. It takes about two minutes.

---

## First run — onboarding wizard

The wizard runs automatically when no setup has been completed. It walks you through:

1. **Data type** — whether you'll import 15-minute interval data or daily summary CSVs
2. **EV tracking** — whether you have an EV charger to track separately
3. **Battery storage** — preference saved (feature coming in a future release)
4. **Port & timezone** — the port you're running on (default 9521) and your IANA timezone
5. **System dates** — optional cutoff dates for when your data became reliable

At the end, the wizard shows your dashboard URL. Bookmark it — it works from any device on your local network.

You can re-run the wizard at any time from **Settings → System Setup → Re-run Setup Wizard**.

---

## Importing your data

1. Click **IMPORT DATA** in the header
2. Select your file type (interval report or daily CSVs)
3. Upload the file(s) and click Import
4. The dashboard reloads automatically when the import completes

You can import multiple times — the importer is idempotent. Existing records are updated, not duplicated. New data is merged with existing data.

### Import results

After each import the header shows a brief summary (rows inserted / updated / skipped). Full details are in **Settings → Last Import**.

### Conflict detection

If you import interval data over existing daily CSV data, the dashboard compares the values. Conflicts above the threshold (>0.5 kWh and >2% difference) are logged in **Settings → Import Conflict Report**. Review and mark them as resolved — they do not affect the charts.

---

## CSV formats

### 15-minute interval report ✓ recommended

A single combined file with one row per 15-minute slot. This format unlocks the full set of charts including hourly profiles, grid dependency, and solar fingerprint.

| Column | Unit | Notes |
|---|---|---|
| `Date/Time` | `MM/DD/YYYY HH:MM` | One row per 15-min slot |
| `Energy Produced (Wh)` | Wh | Required |
| `Energy Consumed (Wh)` | Wh | Required |
| `Exported to Grid (Wh)` | Wh | Optional |
| `Imported from Grid (Wh)` | Wh | Optional |

### Daily summary CSVs

Two separate files — one for production, one for consumption.

**Production file:**

| Column | Unit |
|---|---|
| `Date/Time` | `MM/DD/YYYY` |
| `Energy Produced (Wh)` or `Energy Delivered (Wh)` | Wh |

**Consumption file:**

| Column | Unit |
|---|---|
| `Date/Time` | `MM/DD/YYYY` |
| `Energy Consumed (kWh)` | kWh |

### EV charging sessions (optional)

| Column | Unit | Notes |
|---|---|---|
| `Start Date/Time` | `YYYY/MM/DD HH:MM:SS` | |
| `End Date/Time` | `YYYY/MM/DD HH:MM:SS` | |
| `Energy consumed (Wh)` | Wh | |

Sessions that span midnight are automatically split and prorated across both days. If your charger's export uses different column names, rename the columns before importing.

---

## Configuration

All configuration lives in `.env`. Copy `.env.example` to get started. Most settings can also be adjusted via the in-app wizard.

| Variable | Default | Description |
|---|---|---|
| `PORT` | `9521` | Host port the dashboard listens on |
| `TZ` | `UTC` | Container timezone — match your monitoring system's export timezone |
| `DB_PATH` | `/app/data/wattwise.db` | Database path inside the container |
| `BACKUP_DIR` | `/app/backups` | Backup directory inside the container |
| `DEBUG` | `false` | Verbose logging |

### Optional date cutoff overrides

These are normally set via the onboarding wizard and stored in the database. Set them here only if you want to override the wizard values at the environment level.

| Variable | Example | Description |
|---|---|---|
| `CONS_START` | `2024-10-17` | Date consumption monitoring became reliable |
| `PROD_START` | `2024-12-17` | Date solar production became fully valid |
| `NET_START` | `2025-01-01` | Default display floor for charts |

If unset, all data is treated as valid and shown in full.

---

## Daily operation

### Checking the container is running

```bash
docker compose ps
docker compose logs --tail=50
```

### Stopping and starting

```bash
docker compose stop      # stop without removing
docker compose start     # start again
docker compose down      # stop and remove container (data is safe in volumes)
docker compose up -d     # start again after down
```

### Accessing the dashboard from other devices

Use your host machine's local IP address instead of `localhost`:

```
http://192.168.1.x:9521
```

Find your IP with `ip addr` (Linux) or `ipconfig` (Windows) or `ifconfig` (Mac).

---

## Backups

The scheduler runs automatically inside the container — no configuration needed.

| Schedule | Retention | Filename pattern |
|---|---|---|
| Daily at 02:00 | Last 7 | `wattwise_db_YYYYMMDD-HHMMSS_daily.db` |
| Weekly Sunday at 02:00 | Last 4 | `wattwise_db_YYYYMMDD-HHMMSS_weekly.db` |

Backups are written to `BACKUP_DIR` inside the container, which maps to the `wattwise_backups` Docker volume by default.

### Manual backup

Download a backup at any time from **Settings → Backup & Restore → Download Backup**, or directly:

```bash
curl http://localhost:9521/api/backup -o my_backup.db
```

### Restore from backup

From **Settings → Backup & Restore → Restore from Backup** — upload any `.db` file. The app performs an integrity check before swapping. The container does not need to be restarted.

### Using a NAS or network share for backups

See [DOCKER.md](DOCKER.md) for bind mount configuration. If the backup directory is on a network mount that becomes unavailable, the scheduler skips silently — it will not crash the app.

---

## Upgrading

Your data lives in named Docker volumes and is unaffected by container rebuilds.

```bash
docker compose down
# Replace the project files with the new version
# Keep your existing .env — do not overwrite it
docker compose up -d --build --no-cache
docker compose logs -f
```

---

## Troubleshooting

### Dashboard won't load

```bash
# Check the container is running
docker compose ps

# Check for startup errors
docker compose logs --tail=100

# Check the port isn't already in use
lsof -i :9521        # Mac/Linux
netstat -ano | findstr 9521   # Windows
```

### Import fails silently

- Check **Settings → Last Import** for the error detail
- Confirm your CSV column names exactly match the expected format (see [CSV formats](#csv-formats))
- Check file encoding — files must be UTF-8. Re-export from your monitoring software if unsure

### Charts show no data after import

- Open **Settings → System** and confirm the record count is non-zero
- Check the date range — if you set `NET_START` or `CONS_START` in the wizard, data before those dates is hidden. Adjust in **Settings → System Setup**
- Try the date range picker on the stat cards to widen the window

### "No data yet" screen appears after data has been imported

The wizard's `setup_complete` flag may not have been set. Go to **Settings → System Setup → Re-run Setup Wizard** and complete it.

### Container restarts repeatedly

```bash
docker compose logs --tail=50
```
Look for Python tracebacks. Most common cause is a misconfigured `DB_PATH` pointing to a directory that doesn't exist inside the container. Check your volume mounts in `compose.yml`.

### Backup directory errors in logs

If you see backup errors, check that `BACKUP_DIR` is writable inside the container. The scheduler will skip and log a warning — it won't crash. See [DOCKER.md](DOCKER.md) for NAS mount guidance.

---

## FAQ

**Q: Does this work with systems other than Enphase?**
Yes — any monitoring system that can export CSVs matching the column names above will work. If your export uses different column names, rename the columns in a spreadsheet before importing.

**Q: Can I run this on a Raspberry Pi?**
Yes. The container is lightweight — a Pi 4 or Pi 5 handles it comfortably. Use the standard Docker install for your Pi's OS.

**Q: Is my data sent anywhere?**
No. Everything runs locally. The only outbound connection is Chart.js loaded from `cdnjs.cloudflare.com` — if you need fully air-gapped operation, download Chart.js and serve it from the static folder.

**Q: Can I access this from outside my home network?**
Not safely without additional security. See [DOCKER.md](DOCKER.md) for reverse proxy configuration with basic auth. Do not expose port 9521 directly to the internet.

**Q: What happens to my data if I delete the container?**
Your data lives in named Docker volumes (`wattwise_data`, `wattwise_backups`) which persist independently of the container. `docker compose down` is safe. Only `docker volume rm wattwise_data` would delete the database.

**Q: Can I import historical data?**
Yes — import as many CSV files as you have. Re-importing the same data is safe (idempotent). If you have both daily and interval exports for the same period, import the interval data last — it takes precedence.

**Q: The EV column shows zero even though I imported EV sessions.**
Check that your EV CSV column names match exactly: `Start Date/Time`, `End Date/Time`, `Energy consumed (Wh)`. Column names are case-sensitive.

---

## Notes & limitations

- **Authentication:** none built-in. Designed for trusted LAN use only. Add a reverse proxy with authentication before exposing externally — see [DOCKER.md](DOCKER.md).
- **DST toggle (Solar Fingerprint):** uses US Eastern DST rules only. If you're in a different timezone the hour-alignment toggle will be incorrect — leave it off.
- **Battery storage:** not yet implemented. The wizard accepts the preference and it will activate in a future release.
- **Single system:** one solar installation per instance. Multi-system support is on the roadmap.
