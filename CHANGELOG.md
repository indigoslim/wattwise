# Changelog

All notable changes to Wattwise are documented here.  
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [0.2.0-beta] — 2026-06-02

### Added
- CLEAR button on date range picker to reset to full dataset

### Changed
- Dark theme is now the default (was system preference)
- Settings page top "← BACK TO DASHBOARD" button removed — back navigation available in status bar and sticky bottom button only
- Date range picker now applies to all charts (scatter, trend, monthly, YoY) — previously only affected stat cards
- Preset period buttons (ALL / 1Y / YTD / 90D / 30D) removed — replaced by FROM / TO date inputs with APPLY
- Stat cards capped at 960px max-width to prevent over-stretching on wide displays
- All sections centred on page — main container switched to column layout
- Scatter and Solar Fingerprint charts standardised to 360px height
- Trend, DOW, and YoY charts standardised to 300px height
- `two-col` panels now stretch to equal height within each row
- Monthly + YoY panels now match height of adjacent rows

### Fixed
- Stat cards no longer different sizes — fixed 4-column grid regardless of content
- Solar Fingerprint chart was squished relative to Scatter — heights now equal
- Layout left-alignment — all sections now properly centred within max-width container

---

## [0.1.0-beta] — 2026-06-01

Initial public release.

### Added
- **Onboarding wizard** — 5-step first-run setup (data type, EV, battery, timezone, system dates)
- **Setup API** — `GET /POST /api/setup` stores and retrieves onboarding preferences
- **Re-run wizard** — available from Settings → System Setup at any time
- Done screen shows dashboard URL derived from `window.location`
- Full dashboard: stat cards, scatter plot, 30-day net trend, day-of-week consumption, monthly production bars, year-over-year chart, solar fingerprint, interval analytics
- 15-minute interval CSV import with hourly profiles, grid dependency, peak demand, solar fingerprint views
- EV charging session import with midnight proration
- Import conflict detection — flags mismatches between daily and interval CSV data
- Automated backups — daily × 7 + weekly × 4, skips silently if backup dir unavailable
- Manual backup download and restore via Settings
- Drag-to-reorder dashboard sections with show/hide toggles
- Interval Analytics section hidden by default — enable in Settings → Dashboard Sections
- Mobile carousel layout with dot indicators
- Light / dark / system theme toggle
- `app_settings` database table for persistent user configuration
- Named Docker volumes (`wattwise_data`, `wattwise_backups`)
- `compose.yml` using Docker Compose V2 conventions

### Changed
- All hardcoded install-specific dates removed — cutoffs now optional env vars or set via wizard
- Timezone defaulted to `UTC` in `.env.example` (was `America/New_York`)
- Compose file switched from bind mounts to named volumes
- All `energy_*` naming convention renamed to `wattwise_*` throughout (volumes, container, image, DB filename, localStorage keys, backup filenames)
- DST toggle in Solar Fingerprint renamed to **TIME ADJ.** for timezone neutrality
- Curve fit controls hidden in scatter plot
- Season selector removed from DOW chart
- Error bar toggle simplified to SD ON / OFF (SEM option removed)
- Version history reset — public release starts at `0.1.0-beta`
- Old project overview doc removed from repository

### Security
- No authentication (by design — homelab LAN use only)
- Personal IP addresses, hostnames, and paths removed from all files
- `.gitignore` prevents `.env` and `*.db` files from being committed

---

## Versioning

Wattwise uses semantic versioning (`MAJOR.MINOR.PATCH-label`):

| Increment | When |
|---|---|
| `MAJOR` | Breaking change to data format, DB schema, or API |
| `MINOR` | New feature, new endpoint, new chart |
| `PATCH` | Bug fix, copy change, style tweak |
| `-beta` | Pre-release; public API and schema may still change |

Version strings are defined in `config.py` (`APP_VERSION`, `BE_VERSION`, `FE_VERSION`) and `Dockerfile` (`LABEL version`). Both BE and FE are tracked separately but released together.
