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
CLOUDFLARED_TUNNEL_TOKEN=dummy-token docker compose --env-file .env.docker.example config
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
- Purges only old raw OwnTracks location/event records based on `OWNTRACKS_LOCATION_RETENTION_DAYS`,
  with an enforced minimum retention of 90 days

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
- Docker runs recurring gas snapshots from the app container lifespan when
  `GAS_SNAPSHOT_ENABLED=true`; the `mileage-logger gas-snapshot` CLI remains available for manual
  or host systemd timer runs.

**[owntracks.py](mileage_logger/services/owntracks.py)** — Payload parsing
- Handles both HTTP and MQTT OwnTracks messages
- Parses `location` and `transition` event types
- Validates required fields: `lat`, `lon`, `tst`
- Supports OwnTracks encrypted HTTP payloads when `OWNTRACKS_ENCRYPTION_KEY` is set. The HTTP
  ingestion aliases `/api/owntracks`, `/api/owntracks/`, `/api/pub`, and `/api/pub/` then require
  both decryptable OwnTracks payloads and matching HTTP Basic Auth.
- Non-OwnTracks API routes require `Authorization: Bearer <WEB_API_KEY>` except `/api/health`,
  which stays unauthenticated for internal container health checks.
- Public web service exposes only `POST /api/owntracks`, `POST /api/owntracks/`, and `POST /api/pub`;
  other API routes stay internal to the app container and Docker network, and still require
  `WEB_API_KEY` when called internally.

**[login_failures.py](mileage_logger/services/login_failures.py)** — Web login audit logging
- Writes structured JSON-lines records for successful and failed web UI login attempts
- Saves client IP details, submitted username, authentication method for successful logins,
  failed-login password length, user agent, request path, lockout state, and UTC/local timestamps
  without storing the raw password
- Uses the same effective client key as login lockout and Cloudflare auto-blocking. The bundled
  loopback-only web service origin passes Cloudflare's `CF-Connecting-IP` through when present; otherwise
  the app falls back to the direct client.
- Feeds the Diagnostics successful-login and failed-login tables, per-row failed-login hide
  controls, per-row Cloudflare block buttons, and the raw download endpoint; the failed-login card
  intentionally has no separate footer refresh or download buttons
- Diagnostics shows the stored effective IP for successful-login and failed-login rows. Failed-login
  row block buttons must use that same visible, blockable client IP.

**[passkeys.py](mileage_logger/services/passkeys.py)** — WebAuthn passkey login
- Generates and verifies WebAuthn registration and authentication ceremonies with `py_webauthn`
- Stores passkeys in `passkey_credentials` for the single configured `WEB_LOGIN_USERNAME`
- Keeps registration behind an authenticated Diagnostics session; unauthenticated routes are
  limited to login challenge generation and assertion verification
- Failed passkey assertions use the same audit log, temporary lockout, and Cloudflare auto-block
  path as failed password logins

**[cloudflare_blocks.py](mileage_logger/services/cloudflare_blocks.py)** — Cloudflare IP blocking
- Creates and deletes app-managed Cloudflare zone IP Access Rules for failed-login and manually
  entered IP addresses
- Uses `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ZONE_ID`, and the app-managed block table to avoid
  touching unrelated Cloudflare rules. `CLOUDFLARE_API_TOKEN` must be a Cloudflare API token with
  `Account Firewall Access Rules Write` access for the configured zone, not
  `CLOUDFLARED_TUNNEL_TOKEN` or a Global API Key.
- Enforces `CLOUDFLARE_IP_BLOCK_ALLOWLIST` so trusted IPs/CIDRs are not blocked by the app, and
  records the block reason and manual/automatic source shown on Diagnostics

**[pdf.py](mileage_logger/services/pdf.py)** — Report generation
- Generates portrait PDF with trip table and condensed margins for report content
- Adds optional `REPORT_DISPLAY_NAME` identification under the title as `Submitted by:` when the
  deployment setting is configured
- Shows start/end odometers, miles, and location names
- Escapes trip and waypoint names before passing them to ReportLab `Paragraph` so user-managed
  names, including the optional report display name, render as text rather than PDF markup.
- Calculates total miles and total reimbursement amount

**[backups.py](mileage_logger/services/backups.py)** — Full app data backup and restore
- Creates a gzip-compressed JSON backup of every SQLAlchemy app table plus OwnTracks waypoint export
- Restores validated backup files transactionally by replacing current app table rows
- Creates a startup automatic backup followed by hourly automatic full-data backups when
  `AUTOMATIC_BACKUPS_ENABLED=true`, stores them in `AUTOMATIC_BACKUP_DIR`, and prunes to the
  newest 6 hourly backups plus one daily backup for today and each of the prior 2 days
- Backs Diagnostics full backup/restore controls, retained automatic-backup downloads, and retained
  automatic-backup restore; backup download and restore require web login, restore also requires
  typed confirmation, and startup-created backup rows are labeled as Startup

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
5. Odometer values are display/checkpoint values: starts come from stamped rolling OwnTracks
   values when available, otherwise the master rolling OwnTracks odometer checkpoint before the
   trip start, and ends are start plus the selected trip distance. If only a later master
   checkpoint is available, missing generated-trip odometers may be estimated from retained
   OwnTracks path rows between the trip start and that checkpoint. Generated trips must not use
   prior trip end odometers as the source for a new trip start.
6. Trip is stored and shown on `/trips` page for review/editing

### Odometer Checkpoint System
- Rolling odometer anchor tracks cumulative distance from OwnTracks path
- Manual odometer readings reset the anchor to an exact value
- Trips do not update the master rolling odometer checkpoint. Only OwnTracks location processing
  and manual odometer entries move that checkpoint; trip odometer resequencing is display state for
  trip rows.
- Manual trip starts use the current rolling OwnTracks odometer checkpoint before falling back to
  zero when no master checkpoint exists; later resequencing preserves existing positive non-trip
  odometer gaps between trips.
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
- **Key Variables**: `LOCAL_TIMEZONE`, `VEHICLE_MPG`, `REPORT_DISPLAY_NAME`,
  `OWNTRACKS_WAYPOINT_DWELL_MINUTES`, `LOG_LEVEL`,
  `LOGIN_FAILURE_LOG_PATH`, `AUTOMATIC_BACKUPS_ENABLED`, `AUTOMATIC_BACKUP_DIR`,
  `MAX_BACKUP_RESTORE_BYTES`, `GAS_SNAPSHOT_ENABLED`, `GAS_SNAPSHOT_INTERVAL_SECONDS`,
  `GAS_SNAPSHOT_RUN_ON_STARTUP`, `CLOUDFLARE_IP_BLOCKING_ENABLED`, `CLOUDFLARE_API_TOKEN`,
  `CLOUDFLARE_ZONE_ID`, `CLOUDFLARE_IP_BLOCK_ALLOWLIST`,
  `CLOUDFLARE_AUTO_BLOCK_FAILED_LOGIN_ATTEMPTS`, `WEB_API_KEY`,
  `OWNTRACKS_ENCRYPTION_KEY`, `PASSKEY_RP_NAME`, `PASSKEY_RP_ID`, `PASSKEY_ORIGIN`
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
3. Leave the default API bearer-token middleware in place for non-OwnTracks API routes, and update
   the explicit exemption list in [api/deps.py](mileage_logger/api/deps.py) only for intentional
   health-check or OwnTracks-ingestion endpoints.
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
- The Work Trips page month selector is a single browser month/year picker. It defaults to the app's
  current `LOCAL_TIMEZONE` month, auto-loads the selected month, and displays the month as
  `Showing June 2026 (06/2026)` style text under the Work Trips title.
- The Work Trips page shows compact selected-month summary cards between the month selector line
  and Add Work Trip. Keep the cards scoped to the selected month: work trips plus non-work trips,
  work trips only, OwnTracks events, work trip count, reimbursement, and monthly average gas.
- The Trips root route renders a lightweight loading shell first. The selected-month cards,
  Add Work Trip form, work trip rows, and deleted-trip rows render through `/trips/content`, which
  is fetched by the shell so direct Work Trips loads show a loading message before month
  calculations finish.
- Manual trip creation defaults the date field to the app's `LOCAL_TIMEZONE` current date and uses
  origin/destination waypoint dropdowns populated from saved waypoints. Manual inserts calculate
  and save start/end odometers immediately from the current rolling OwnTracks odometer checkpoint,
  then resequence that trip and all later trips when the inserted date is before existing trip rows.
  New manual trips are placed after existing trips on the selected local date, and resequencing keeps
  existing positive odometer gaps between trips so non-trip driving remains represented.
- Dashboard work trip plus non-work trip distance cards use OwnTracks path distance as the
  total-distance source but floor the combined total at the stored work trip total after
  one-decimal rounding, so the displayed non-work trip remainder is never negative.
- Dashboard OwnTracks Events and Work Trips count cards are scoped to the current app-local month.
  The month starts at midnight on the first day in `LOCAL_TIMEZONE` (default America/Detroit), and
  month rollover must not delete prior-month trips, OwnTracks rows, gas price records, or derived
  app data. Monthly OwnTracks summary rollups preserve selected-month web totals and event counts
  after raw OwnTracks location/event rows are purged.
- The Dashboard current-month reimbursement card must use the same monthly trip miles,
  reimbursement gallons, monthly gas price, and `VEHICLE_MPG` calculation as the PDF report.
  Display the card's reimbursement gallons at one decimal place.
- The Dashboard home content shows the Location State card as the first visible card before other
  stat cards and distance summary cards. On full-width layouts, keep Dashboard top statistic cards
  and distance summary cards compact like the Work Trips selected-month cards, with each row still
  spanning the app width. Mobile should continue stacking those cards one per row.
- The shared top bar uses centered blue raised navigation buttons with icons and labels on
  authenticated desktop pages. On mobile, hide the brand/icon and keep the blue navigation buttons
  as icon-only controls in one full-width top-bar row. App buttons and button-style links are
  raised, brighten on hover, and press inward when clicked while preserving non-navigation button
  colors. Avoid fixed bottom navigation, and use a normal non-edge-to-edge viewport plus
  standalone/browser manifest fallback so phone system navigation remains visible. The login page
  has no shared top navigation. The brand icon/text is display-only and must not be a clickable
  home link.
- The Dashboard root route renders a lightweight loading shell first. The expensive Dashboard
  summary queries render through `/dashboard/content`, which is fetched by the shell so direct
  homepage loads show a loading message before calculations finish.

### Debugging Trip Generation
1. Check `/diagnostics` page for OwnTracks state, recent events, and logs
2. View app logs: `docker compose logs -f app`
3. App logs are stored at `LOG_DIR`; Docker binds `/data/logs` to the host path in
   `HOST_LOG_DIR` so the Docker server can view `app.log`, `trip-calculation.log`, and worker logs.
4. Successful and failed web login attempts are written to `LOGIN_FAILURE_LOG_PATH` and shown on
   `/diagnostics` in separate compact tables.
   In Docker, this is `/data/logs/mileage-logger-login-failures.log`, backed by `HOST_LOG_DIR`.
   `HOST_LOGIN_FAILURE_LOG_PATH` may point to a host symlink such as
   `/var/log/mileage-logger-login-failures.log`.
5. Diagnostics can hide individual failed-login rows from the UI while preserving the raw audit log
   download. When Cloudflare IP blocking is enabled and configured, Diagnostics can block/unblock
   failed-login IPs and manually entered valid IPs through app-managed Cloudflare zone IP Access
   Rules. Manual blocks require a reason, automatic blocks record the failed-login threshold
   reason, and the app-managed blocked-IP list shows each reason with an Auto or Manual pill plus a
   remove button that deletes the Cloudflare rule and local row. Automatic blocking occurs after
   the configured consecutive failed-login threshold and successful login resets that IP's
   consecutive count. Diagnostics paginates successful-login rows, failed-login rows, and
   app-managed Cloudflare blocks in compact 10-row pages; successful-login rows show a Password or
   Passkey method pill instead of an account column. On mobile, pagination keeps First, Previous,
   Next, and Last in one full-width row with the page count as text below. Failed-login row block
   buttons must use the failed-login table's corrected effective client IP.
6. Diagnostics includes a Configure Passkey card for the single configured web user. Passkey
   creation requires an authenticated web session, lists configured passkeys, and removes only the
   selected local credential row. Passkey login failures must stay on the same failed-login audit,
   lockout, and Cloudflare auto-block path as password login failures.
7. Use Diagnostics `Download Full Backup` before destructive deployment or database work. The
   backup/restore card is at the bottom of the page under App Log, and the manual full-backup
   download control sits with the lower upload-restore controls. Restore replaces all app table
   data from a validated `.json.gz` backup and is enabled only when web login is configured.
   Diagnostics also lists retained automatic backups from `AUTOMATIC_BACKUP_DIR`; each retained
   backup can be downloaded individually, startup-created files are labeled, and the selected file
   can be restored after typed `RESTORE` confirmation.
8. Diagnostics groups the top cards together in this order: Application, Data, Latest Records,
   OwnTracks State, Manual Odometer, EIA API, Configure Passkey, and Hard Drive Space. Keep the
   group at three cards per row on desktop and one card per row on mobile. The detailed OwnTracks
   state-change log and recent OwnTracks database entries are paginated in compact 10-row pages
   with the same mobile full-width pagination row used by the login and Cloudflare block lists.
   The recent OwnTracks entries table shows original event time, capture-to-receive delay, and
   readable event labels instead of the database row ID, raw receive timestamps, battery level, or
   MQTT topic details.
   The OwnTracks state-change log intentionally omits per-section distance and shows original event
   time, received delay, state, waypoint, source, elapsed duration since the prior state change,
   and the event row's rolling odometer when available.
9. Diagnostics shows the app version in the Application card, shows hard drive space for key
   runtime paths, combines paths into one row when exact used bytes and total bytes match, and
   includes current database size plus total app record count at the bottom of the card.
10. Trip calculation details logged to `mileage_logger.trip_calculation` logger

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
- Uses `docker-compose.yml` with 4 services (postgres, app, nginx, cloudflared)
- Docker publishes the web service on `127.0.0.1:${HTTP_PORT:-80}`. The bundled `cloudflared` service uses
  host networking so Cloudflare Tunnel can target the loopback listener, such as
  `http://127.0.0.1:2082` when `HTTP_PORT=2082`.
- PostgreSQL data is stored in the named `postgres_data` Docker volume and persists across normal
  `docker compose up -d --build` rebuilds. Do not use `docker compose down -v`, prune volumes, or
  change the Compose/Portainer stack name unless you have a verified backup and migration plan.
- Environment variables in `.env` control all configuration. Production Docker must have
  `SECRET_KEY`, `WEB_LOGIN_USERNAME`, and `WEB_LOGIN_PASSWORD` set; the app fails closed when
  production login credentials are missing or the session secret is still `change-me`. When web
  login is enabled in any environment, change `SECRET_KEY` from the default.
- Migrations run automatically on app startup
- Daily gas snapshots run as an app-container background scheduler; there is no separate
  `gas-snapshot` Compose service.
- Diagnostics page available at `http://server/diagnostics`
- Public web service exposes rendered web pages and OwnTracks ingestion only; `/api/health`, admin API
  routes, `/docs`, `/redoc`, and `/openapi.json` are intentionally not internet-facing.
- Public web service serves custom, unbranded end-user error pages from `deploy/nginx/error-pages/` for
  common 4xx and 5xx responses. Keep the pages visually matched, include a `/login` link that can
  switch to home for authenticated browsers. Browser/static proxy locations intercept upstream app
  errors so missing page URLs show those custom pages; OwnTracks API proxy locations do not
  intercept errors, so API clients keep JSON responses.
- Public web service passes Cloudflare's `CF-Connecting-IP` through to the app when present. The app uses
  that effective client IP for login lockouts, login audit rows, and Cloudflare auto-blocks.
- Passkey login derives its WebAuthn origin from `PASSKEY_ORIGIN`, the browser `Origin` header, or
  trusted reverse-proxy scheme/host headers. For public Cloudflare Tunnel deployments, verify the
  browser origin is the public HTTPS URL or set `PASSKEY_ORIGIN` and `PASSKEY_RP_ID` explicitly.
- Diagnostics includes authenticated full data backup and restore controls for app database rows
  and saved OwnTracks waypoint export. Automatic hourly backups are stored under
  `AUTOMATIC_BACKUP_DIR`, defaulting to `LOG_DIR/backups`; treat backup files as sensitive location
  history. Retained automatic backups can be downloaded individually from Diagnostics after web
  login.
- Runtime app logs and web-login audit records are host bind-mounted through `HOST_LOG_DIR`.
  Do not bind-mount the login audit log as an individual file; use the host symlink documented in
  `INSTALL.md` if `/var/log/mileage-logger-login-failures.log` is needed.

---

## Common Pitfalls

1. **Timezone Confusion**: The server can run on UTC, but trip dates and day boundaries use `LOCAL_TIMEZONE`. Always convert with `datetime_to_local()` before displaying.

2. **Trip Dwell Time**: If waypoint transitions arrive too quickly, the trip won't be confirmed. The default is 5 minutes. Check `OWNTRACKS_WAYPOINT_DWELL_MINUTES` and OwnTracks event timestamps.

3. **Mileage Priority**: OwnTracks path distance is preferred, but if location updates are sparse, fallback to waypoint distance. Odometer values are never a distance source; manual distance edits override generated calculations. Prior trip end odometers are not the source for new generated trip starts.

4. **Odometer Precision**: Values stored and displayed as 0.1 mile precision. Manual entries are quantized during update.

5. **Data Retention**: Only raw OwnTracks location/event records are purged automatically, and only
   after at least 90 days even when `OWNTRACKS_LOCATION_RETENTION_DAYS` is set lower. Trips,
   odometers, reports, gas prices, monthly OwnTracks summary rollups, backups, and other derived
   app data are kept. Set `OWNTRACKS_PURGE_ENABLED=false` to disable raw OwnTracks cleanup.

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
