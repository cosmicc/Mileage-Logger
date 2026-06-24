# AI Agent Instructions for Mileage Logger

This document helps AI coding agents understand the Mileage Logger codebase and be immediately productive.

## Project Overview

**Mileage Logger** is a FastAPI web application that:
- Receives location events from the [OwnTracks](https://owntracks.org/) mobile app
- Stores waypoint transitions in PostgreSQL
- Automatically generates **trips** from waypoint leave/enter events
- Calculates **trip mileage** using OwnTracks location path distance
- Generates monthly **PDF reimbursement reports** with gas price calculations
- Provides a web dashboard for trip review, editing, and manual entry

**Tech Stack**: Python 3.12, FastAPI, SQLAlchemy, PostgreSQL, Alembic, Jinja2, ReportLab, Docker Compose

---

## Quick Start

### Local Development
```bash
cp .env.example .env
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
docker compose up -d postgres
alembic upgrade head
uvicorn mileage_logger.app:app --reload
```

### Tests & Linting
```bash
pytest           # Run tests
ruff check .     # Lint
```

### Docker Deployment
```bash
./scripts/init_docker_env.sh  # Generate .env with secrets
# Set CLOUDFLARED_TUNNEL_TOKEN in .env
docker compose up -d --build
```

---

## Architecture

### Key Directories

| Directory | Purpose |
|-----------|---------|
| `mileage_logger/api/` | API routes for OwnTracks ingestion, trip updates, PDF export |
| `mileage_logger/web/` | Web UI routes, Jinja2 templates, HTML rendering |
| `mileage_logger/services/` | Business logic: trip generation, mileage calc, gas prices, MQTT |
| `mileage_logger/models.py` | SQLAlchemy ORM models (Trip, Site, OwnTracksLocation, etc.) |
| `alembic/versions/` | Database schema migrations |

### Core Services

**[trip_processor.py](mileage_logger/services/trip_processor.py)** — Automatic trip generation
- Watches for new OwnTracks location/transition events
- Runs `generate_trips()` when waypoint transitions occur
- Maintains rolling `TripProcessingCheckpoint` to track odometer distance
- Enforces minimum dwell time before confirming arrival at a waypoint
- Purges old OwnTracks location records based on `OWNTRACKS_LOCATION_RETENTION_DAYS`

**[mileage.py](mileage_logger/services/mileage.py)** — Trip mileage calculation
- `generate_trips()` - Core trip generation from waypoint transitions
- `haversine_miles()` - Calculates distance between GPS coordinates
- `site_for_location()` - Matches OwnTracks event to saved waypoint site
- Mileage priority: OwnTracks path distance → waypoint distance; odometer values are not a
  distance source
- Supports manual trip entry and deletion with suppression records

**[gas_prices.py](mileage_logger/services/gas_prices.py)** — Reimbursement calculation
- `GasPriceProvider` abstract class with two implementations:
  - `AaaMichiganGasPriceProvider` - Scrapes AAA website (default)
  - `EiaSeriesProvider` - Uses EIA API (requires configuration)
- Formula: `(trip_miles / VEHICLE_MPG) * gas_price = reimbursement`

**[owntracks.py](mileage_logger/services/owntracks.py)** — Payload parsing
- Handles both HTTP and MQTT OwnTracks messages
- Parses `location` and `transition` event types
- Validates required fields: `lat`, `lon`, `tst`
- Supports both HTTP Basic Auth and API token authentication
- Public nginx exposes only `POST /api/owntracks`, `POST /api/owntracks/`, and `POST /api/pub`;
  other API routes stay internal to the app container and Docker network.

**[login_failures.py](mileage_logger/services/login_failures.py)** — Web login audit logging
- Writes structured JSON-lines records for failed web UI login attempts
- Saves client IP details, submitted username, password length, user agent, request path,
  lockout state, and UTC/local timestamps without storing the raw password
- Feeds the Diagnostics failed-login table and download endpoint

**[pdf.py](mileage_logger/services/pdf.py)** — Report generation
- Generates landscape PDF with trip table
- Shows start/end odometers, miles, and location names
- Calculates total miles and total reimbursement amount

**[backups.py](mileage_logger/services/backups.py)** — Full app data backup and restore
- Creates a gzip-compressed JSON backup of every SQLAlchemy app table plus OwnTracks waypoint export
- Restores validated backup files transactionally by replacing current app table rows
- Creates hourly automatic full-data backups when `AUTOMATIC_BACKUPS_ENABLED=true`, stores them in
  `AUTOMATIC_BACKUP_DIR`, and prunes to the newest 6 hourly backups plus one daily backup for today
  and each of the prior 2 days
- Backs Diagnostics full backup/restore controls, retained automatic-backup downloads, and retained
  automatic-backup restore; backup download and restore require web login, and restore also requires
  typed confirmation

---

## Key Concepts

### Trip Generation Flow
1. OwnTracks sends waypoint transition events (enter/leave/arrival/departure)
2. Trip processor detects qualifying transitions:
   - `leave` from waypoint A + `enter` to waypoint B = one trip
   - Requires at least `OWNTRACKS_WAYPOINT_DWELL_MINUTES` (default 5) of data inside destination
   - Home → Home never generates a trip
   - Same-waypoint trips under 1.0 mile are invalid and are suppressed with an exact deleted-trip record
3. Mileage is calculated from OwnTracks location updates between the two events
4. If OwnTracks path data is unavailable, trip distance falls back to waypoint-to-waypoint distance
5. Odometer values are display/checkpoint values estimated from the selected distance
6. Trip is stored and shown on `/trips` page for review/editing

### Odometer Checkpoint System
- Rolling odometer anchor tracks cumulative distance from OwnTracks path
- Manual odometer readings reset the anchor to an exact value
- Manual trip starts use the current rolling OwnTracks odometer checkpoint before falling back to
  older trip odometers; later resequencing preserves existing positive non-trip odometer gaps
  between trips.
- Diagnostics shows the current odometer inside the Manual Odometer card before a new manual
  checkpoint value is saved
- Useful when actual odometer reading differs from GPS distance estimate
- Stored in `TripProcessingCheckpoint` table

### Timezone Handling
- All timestamps stored as UTC in database
- `LOCAL_TIMEZONE` (default `America/Detroit`) used for:
  - Trip date selection
  - Day/month boundaries
  - Dashboard display and PDF reports
- Services in `timezone.py` convert between UTC and local time

### Configuration
- **Source**: `.env` file loaded by `pydantic_settings.BaseSettings`
- **Key Variables**: `LOCAL_TIMEZONE`, `VEHICLE_MPG`, `OWNTRACKS_WAYPOINT_DWELL_MINUTES`, `LOG_LEVEL`,
  `LOGIN_FAILURE_LOG_PATH`, `AUTOMATIC_BACKUPS_ENABLED`, `AUTOMATIC_BACKUP_DIR`,
  `MAX_BACKUP_RESTORE_BYTES`
- See [README.md](README.md#Useful-Docker-environment-options) for all options

---

## Common Tasks

### Adding a New Database Field
1. Create migration in `alembic/versions/` with timestamp: `alembic revision -m "description"`
2. Update SQLAlchemy model in [models.py](mileage_logger/models.py)
3. Run `alembic upgrade head` to apply locally
4. Migration auto-runs on Docker container startup

### Adding an API Endpoint
1. Add route to [api/routes.py](mileage_logger/api/routes.py)
2. Use `Depends(get_db)` for database session
3. Add authentication check if needed (see `verify_owntracks_auth` in [api/deps.py](mileage_logger/api/deps.py))
4. Return JSON or raise `HTTPException`

### Adding a Web Page
1. Create Jinja2 template in `web/templates/`
2. Add route to [web/routes.py](mileage_logger/web/routes.py)
3. Use `authenticate_web_credentials` if page should require login
4. Pass context dict to `templates.TemplateResponse()`

### Trips Page Editing Boundaries
- Existing trip rows display trip dates and odometers as read-only values. Row update forms accept
  selected origin/destination waypoint IDs from dropdowns plus mileage edits; posted dates,
  free-text names, and odometer fields are not accepted for existing rows.
- Manual trip creation defaults the date field to the app's `LOCAL_TIMEZONE` current date and uses
  origin/destination waypoint dropdowns populated from saved waypoints. Manual inserts calculate
  and save start/end odometers immediately from the current rolling OwnTracks odometer checkpoint,
  then resequence that trip and all later trips when the inserted date is before existing trip rows.
  New manual trips are placed after existing trips on the selected local date, and resequencing keeps
  existing positive odometer gaps between trips so non-trip driving remains represented.
- Dashboard trip plus non-trip distance cards use OwnTracks path distance as the total-distance
  source but floor the combined total at the stored trip total after one-decimal rounding, so the
  displayed non-trip remainder is never negative.
- The shared top bar includes a mobile-only `X` close control that calls `window.close()` for
  installed full-screen mobile web-app sessions. Browsers may ignore the close request outside
  contexts they allow scripts to close.

### Debugging Trip Generation
1. Check `/diagnostics` page for OwnTracks state, recent events, and logs
2. View app logs: `docker compose logs -f app`
3. App logs are stored at `LOG_DIR`; Docker binds `/data/logs` to the host path in
   `HOST_LOG_DIR` so the Docker server can view `app.log`, `trip-calculation.log`, and worker logs.
4. Failed web login attempts are written to `LOGIN_FAILURE_LOG_PATH` and shown on `/diagnostics`.
   In Docker, this is `/data/logs/mileage-logger-login-failures.log`, backed by `HOST_LOG_DIR`.
   `HOST_LOGIN_FAILURE_LOG_PATH` may point to a host symlink such as
   `/var/log/mileage-logger-login-failures.log`.
5. Use Diagnostics `Download Full Backup` before destructive deployment or database work. The
   backup/restore card is at the bottom of the page under App Log; restore replaces all app table
   data from a validated `.json.gz` backup and is enabled only when web login is configured.
   Diagnostics also lists retained automatic backups from `AUTOMATIC_BACKUP_DIR`; each retained
   backup can be downloaded individually, and the selected file can be restored after typed
   `RESTORE` confirmation.
6. Diagnostics groups Manual Odometer, EIA API, and OwnTracks State cards in one equal-width row
   before the detailed OwnTracks state-change log.
7. Diagnostics shows hard drive space for key runtime paths, combines paths into one row when
   exact free bytes and total bytes match, and includes current database size plus total app record
   count at the bottom of the card.
8. Trip calculation details logged to `mileage_logger.trip_calculation` logger

---

## Testing Patterns

**Test Files**: Tests are in `tests/` with names like `test_mileage.py`, `test_owntracks.py`

Key test modules:
- `test_mileage.py` - Trip generation logic, odometer calculations
- `test_owntracks.py` - Payload parsing and event handling
- `test_pdf.py` - Report generation
- `test_timezone.py` - Timezone conversions
- `test_web.py` - Web UI routes

**Database Testing**: Tests use SQLite in-memory database by default. Check fixture setup in test files.

---

## Deployment

See [INSTALL.md](INSTALL.md) for complete Docker and Portainer setup guide.

**Key Points**:
- Requires Docker Engine and Docker Compose v2
- Uses `docker-compose.yml` with 5 services (postgres, app, nginx, cloudflared, gas-snapshot)
- PostgreSQL data is stored in the named `postgres_data` Docker volume and persists across normal
  `docker compose up -d --build` rebuilds. Do not use `docker compose down -v`, prune volumes, or
  change the Compose/Portainer stack name unless you have a verified backup and migration plan.
- Environment variables in `.env` control all configuration
- Migrations run automatically on app startup
- Diagnostics page available at `http://server/diagnostics`
- Public nginx exposes rendered web pages and OwnTracks ingestion only; `/api/health`, admin API
  routes, `/docs`, `/redoc`, and `/openapi.json` are intentionally not internet-facing.
- Diagnostics includes authenticated full data backup and restore controls for app database rows
  and saved OwnTracks waypoint export. Automatic hourly backups are stored under
  `AUTOMATIC_BACKUP_DIR`, defaulting to `LOG_DIR/backups`; treat backup files as sensitive location
  history. Retained automatic backups can be downloaded individually from Diagnostics after web
  login.
- Runtime app logs and failed-login audit records are host bind-mounted through `HOST_LOG_DIR`.
  Do not bind-mount the failed-login log as an individual file; use the host symlink documented in
  `INSTALL.md` if `/var/log/mileage-logger-login-failures.log` is needed.

---

## Common Pitfalls

1. **Timezone Confusion**: The server can run on UTC, but trip dates and day boundaries use `LOCAL_TIMEZONE`. Always convert with `datetime_to_local()` before displaying.

2. **Trip Dwell Time**: If waypoint transitions arrive too quickly, the trip won't be confirmed. The default is 5 minutes. Check `OWNTRACKS_WAYPOINT_DWELL_MINUTES` and OwnTracks event timestamps.

3. **Mileage Priority**: OwnTracks path distance is preferred, but if location updates are sparse, fallback to waypoint distance. Odometer values are never a distance source; manual distance edits override generated calculations.

4. **Odometer Precision**: Values stored and displayed as 0.1 mile precision. Manual entries are quantized during update.

5. **Data Retention**: OwnTracks location records are purged after `OWNTRACKS_LOCATION_RETENTION_DAYS` (default 14), but trips are kept. Set `OWNTRACKS_PURGE_ENABLED=false` to disable.

---

## AI Agent Skills

These specialized guides help AI agents with common development tasks:

- **[SKILL-trip-processor.md](.vscode/SKILL-trip-processor.md)** — Automatic trip generation, event sequences, odometer checkpoint, debugging trip detection
- **[SKILL-database-migrations.md](.vscode/SKILL-database-migrations.md)** — Adding database fields, creating Alembic migrations, schema changes, rollback patterns
- **[SKILL-mileage-calculation.md](.vscode/SKILL-mileage-calculation.md)** — Mileage priority system, Haversine distance, odometer estimation, trip editing, resequencing logic
- **[SKILL-api-and-web-routes.md](.vscode/SKILL-api-and-web-routes.md)** — Adding API endpoints, web pages, form handling, authentication, Jinja2 templates

---

## Documentation Links

- [README.md](README.md) — Project overview, setup, OwnTracks configuration, workflow
- [INSTALL.md](INSTALL.md) — Docker, Ubuntu, Portainer installation guide
- [CHANGELOG.md](CHANGELOG.md) — Release history and breaking changes
- [pyproject.toml](pyproject.toml) — Dependencies and build configuration
