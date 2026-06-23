# Mileage Logger

Mileage Logger receives OwnTracks waypoint events from an Android phone over HTTP or MQTT,
stores them in PostgreSQL, lets you review and edit generated waypoint trips, and produces
monthly reimbursement PDF logs.

## Current Scope

- FastAPI web app with server-rendered review screens.
- PostgreSQL models and Alembic migration.
- OwnTracks HTTP endpoint at `/api/owntracks` and Recorder-compatible `/api/pub`.
- Optional MQTT subscriber for `owntracks/#` topics so location, waypoint, and transition events
  are available.
- OwnTracks waypoint transition model used to turn leave/enter events into trips, with
  location updates between those events used as the primary trip distance.
- Manual current-odometer entry from the Diagnostics page, with the Manual Odometer card showing
  the latest current reading before saving a new checkpoint.
- Manual trip entry defaults to today's local date and uses saved waypoint dropdowns for From/To,
  with trip-row waypoint and mileage review on the Trips page.
- Monthly gas price cache with a provider layer.
- Monthly PDF report generation.
- GitHub Actions CI for linting and tests.

## Fuel Price Policy

The reimbursement formula is:

```text
monthly trip miles / vehicle MPG = reimbursement gallons
reimbursement gallons * Michigan monthly average gas price = total reimbursement
```

The first provider can fetch the current AAA Michigan regular gasoline average and store daily
snapshots. Monthly reports use a saved manual monthly average when present, or the average of
stored daily snapshots for that month. Historical Michigan monthly pricing sources can be added
behind `mileage_logger.services.gas_prices.GasPriceProvider` without changing report generation.
Set `VEHICLE_MPG` to the fuel economy that should be used for reimbursement calculations.

EIA support is scaffolded because the official API requires an API key and the exact series should
be configured with `EIA_SERIES_ID` once the preferred Michigan data series is selected.

## Local Development

```bash
cp .env.example .env
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
docker compose up -d postgres
alembic upgrade head
uvicorn mileage_logger.app:app --reload
```

Open `http://localhost:8000`.

## Docker Deployment

Docker Compose is the preferred deployment path. It runs the complete stack:

- PostgreSQL database.
- FastAPI mileage app.
- Nginx reverse proxy on port `80`.
- Daily gas price snapshot worker.
- Cloudflare Tunnel connector using the configured tunnel token.
- Persistent Docker volume for database data and host bind mounts for runtime logs.
- In-app diagnostics page for app logs, trip calculation logs, failed-login audit records, and
  OwnTracks state in the configured local timezone. The Diagnostics Manual Odometer, EIA API, and
  OwnTracks State cards share one equal-width status row, while Full Data Backup stays at the
  bottom under the App Log.
- Failed web-login audit records shown on Diagnostics and written into the host log directory, with
  an optional `/var/log/mileage-logger-login-failures.log` host symlink.
- Optional web UI IP allowlist while keeping only the OwnTracks ingestion API public.

Create a production `.env` with generated passwords:

```bash
./scripts/init_docker_env.sh
```

Review `.env`, set `CLOUDFLARED_TUNNEL_TOKEN` to the token from the Cloudflare dashboard, then
start the stack:

```bash
docker compose up -d --build
```

`scripts/init_docker_env.sh` tries to create the host log directory, login-failure log file, and
the short `/var/log/mileage-logger-login-failures.log` symlink. If your user cannot write to
`/var/log`, create them before starting Docker:

```bash
sudo install -d -m 0750 /var/log/mileage-logger
sudo install -m 0640 /dev/null /var/log/mileage-logger/mileage-logger-login-failures.log
sudo ln -sfn /var/log/mileage-logger/mileage-logger-login-failures.log /var/log/mileage-logger-login-failures.log
```

If an earlier failed start created `/var/log/mileage-logger-login-failures.log` as a directory,
remove that empty directory first:

```bash
sudo rmdir /var/log/mileage-logger-login-failures.log
```

Useful commands:

```bash
docker compose ps
docker compose logs -f app
docker compose logs -f nginx
docker compose down
```

Database rows live in the Docker named volume `postgres_data`, mounted at
`/var/lib/postgresql/data` inside the PostgreSQL container. Normal rebuilds such as
`docker compose up -d --build` keep that volume. Do not use `docker compose down -v`, Docker volume
prune, or a different Compose/Portainer stack name unless you have a verified backup and intend to
move or recreate the database.

OwnTracks HTTP mode should point at:

```text
http://your-server/api/owntracks
```

Use the `OWNTRACKS_USERNAME` and `OWNTRACKS_PASSWORD` values from `.env` for
OwnTracks HTTP Basic Auth. If you put credentials directly in the URL, use:

```text
http://owntracks:password@your-server/api/owntracks
```

For internet-facing use, put TLS in front of this stack or extend the Nginx container
with certificates so OwnTracks sends location data over HTTPS.

To restrict the browser UI while leaving OwnTracks ingestion open, set `WEB_ALLOWED_CIDRS`
to comma-separated IP blocks:

```env
WEB_ALLOWED_CIDRS=192.168.1.0/24,10.8.0.0/24,203.0.113.44/32
```

When this is blank, the web UI is open to all clients. When set, only
`POST /api/owntracks`, `POST /api/owntracks/`, and `POST /api/pub` stay reachable from any IP for
OwnTracks. Pages such as `/`, `/trips`, `/waypoints`, `/diagnostics`, and `/static/` require a
matching client IP. Other `/api/` routes, `/docs`, `/redoc`, and `/openapi.json` are blocked at
the public nginx reverse proxy.

To require a simple username/password login for browser pages, set both web login variables:

```env
WEB_LOGIN_USERNAME=admin
WEB_LOGIN_PASSWORD=change-web-login-password
WEB_SESSION_COOKIE_SECURE=true
WEB_LOGIN_MAX_ATTEMPTS=5
WEB_LOGIN_LOCKOUT_SECONDS=300
```

The login protects rendered web pages such as `/`, `/trips`, `/waypoints`, and `/diagnostics`.
The app still leaves `/api/` outside the web login internally, but public nginx only exposes the
OwnTracks ingestion endpoints. If you access the app over plain HTTP for local testing, set
`WEB_SESSION_COOKIE_SECURE=false` so the browser accepts the session cookie. The login page does
not show the app name before authentication and temporarily locks out repeated failed attempts.
Each failed login attempt and lockout rejection is appended to `LOGIN_FAILURE_LOG_PATH` as a
structured JSON-lines record with client IP details, submitted username, password length, user
agent, request path, reason, attempt count, lockout state, and timestamps. The raw submitted
password is never stored.

See [INSTALL.md](INSTALL.md) for the full Docker and Portainer installation guide.

## OwnTracks HTTP Setup

Set OwnTracks HTTP mode to:

```text
https://your-host.example.com/api/owntracks
```

If `OWNTRACKS_API_TOKEN` is set, send it as `X-Api-Key` or `Authorization: Bearer ...`.
If `OWNTRACKS_USERNAME` and `OWNTRACKS_PASSWORD` are set, use OwnTracks HTTP Basic Auth.

The `/api/pub` alias is also available for Recorder-style setups.

OwnTracks waypoints are saved as read-only work waypoints. When `OWNTRACKS_SYNC_WAYPOINTS=true`,
published OwnTracks waypoint payloads create or update matching app waypoints. The web app can
export the saved list as OwnTracks waypoint JSON for backup/import.

## Full Data Backup And Restore

Diagnostics includes a full app data backup and restore panel at the bottom of the page under the
App Log when `WEB_LOGIN_USERNAME` and `WEB_LOGIN_PASSWORD` are configured. `Download Full Backup`
creates a `.json.gz` file containing all Mileage Logger database tables plus an OwnTracks waypoint
export. Treat this file as sensitive location history.

The app also creates automatic full-data backups every hour when
`AUTOMATIC_BACKUPS_ENABLED=true`, which is the default. Automatic backups are stored in
`AUTOMATIC_BACKUP_DIR`, defaulting to `LOG_DIR/backups` such as `/data/logs/backups` in Docker.
Diagnostics lists retained automatic backups and can restore one after you type `RESTORE`. The
retention policy keeps the newest 6 hourly backups plus one daily backup for today and each of the
prior 2 days. Each listed automatic backup also has its own download button. Backup downloads use
`Cache-Control: no-store` and require the same web login as restore because the files contain
location history.

To restore, upload the same backup file on Diagnostics and type `RESTORE`. Restore validates the
file first, then replaces the current app table rows in one transaction. Restore is a replace, not
a merge: matching existing rows are overwritten from the backup and should not create duplicates.
Uploaded restore files and retained automatic backup files are limited by
`MAX_BACKUP_RESTORE_BYTES`, default `262144000` bytes. In-app restore does not restore Docker
volumes, PostgreSQL users, passwords, or host log files.

## MQTT Setup

Set these in `.env`:

```text
MQTT_ENABLED=true
MQTT_HOST=your-broker
MQTT_PORT=1883
MQTT_USERNAME=optional
MQTT_PASSWORD=optional
MQTT_TOPIC=owntracks/#
```

Then run the web app normally. The MQTT worker starts with the app. Use `owntracks/#` so MQTT
ingestion can receive waypoint definitions, transition events, and location updates.

## Trip Detection

The app generates trips directly from OwnTracks waypoint transitions:

- A trip is created from a waypoint `leave` event followed by another waypoint `enter` event.
- The destination `enter` event must be confirmed by at least
  `OWNTRACKS_WAYPOINT_DWELL_MINUTES` minutes of later OwnTracks data inside that saved waypoint.
  The default is 5 minutes so driving through a waypoint does not create a trip.
- `Home` is the exact waypoint name for home.
- `Home` to `Home` is never a trip.
- Trips between the same non-home waypoint are kept only when the calculated distance is at least
  1.0 mile, because shorter same-waypoint loops are treated as invalid GPS drift or non-trips.
- If an `enter` event arrives without a matching `leave`, the app infers the most likely origin
  from the previous waypoint. If there is no previous waypoint and the destination is not `Home`,
  the app assumes the missed origin was `Home`.

Trip data is calculated automatically. Every incoming OwnTracks location or transition payload is
stored in `owntracks_locations` and immediately triggers trip recalculation for that payload's
`LOCAL_TIMEZONE` day. When the app sees a qualifying trip, it writes the generated row to `trips`.
OwnTracks `tst` event time is the authoritative timestamp for trip dates and ordering; the server
receive time is kept separately for diagnostics because phone data can be buffered.
The server can run on UTC; app day/month selection, dashboard time, and gas snapshot dates use
`LOCAL_TIMEZONE`, default `America/Detroit` for EST/EDT.

Generated mileage uses this order:

1. OwnTracks location path distance from the location updates received between the waypoint
   `leave` and `enter` events.
2. Waypoint-to-waypoint distance when OwnTracks path data is not available.

Keep OwnTracks location reporting enabled so the app can sum the actual path between waypoint
events. If a trip window has only transition events and no location updates between them, the app
falls back to waypoint distance. Odometer values are never used to calculate trip distance,
Dashboard trip plus non-trip totals, or monthly trip plus non-trip totals. Dashboard trip plus
non-trip cards are floored at the stored trip total after one-decimal rounding, so the displayed
combined total is never lower than the trips-only total and the implied non-trip remainder is never
negative. Edit a trip's saved waypoint route or miles on the `Trips` page when generated values
need correction. A distance correction resequences that month's displayed start and end odometers
in chronological trip order. Manual trips entered from the `Trips` page default to today's local
date, use saved waypoint dropdowns for From/To, and save start/end odometers immediately from the
current rolling OwnTracks odometer checkpoint plus the entered trip miles. A manual trip is placed
after the existing trips on the selected local date, so backdated manual entries land at the end of
that day and today's manual entries become the latest trip for today. If the manual trip is inserted
before existing trips, the app resequences that trip and every later trip so odometers remain
cumulative across month boundaries while preserving existing positive odometer gaps between trips
for non-trip driving. Deleting a
trip from the `Trips` page also saves an exact deleted-trip record so only that same OwnTracks
transition pair is not generated again; future trips with the same route are still generated
normally.
Automatic same-waypoint trips under 1.0 mile are also removed with an exact suppression record so
older invalid rows do not return from the same OwnTracks transition pair.
The checkpoint odometer is advanced from OwnTracks path distance between received points even when
those points do not become a trip. Each processed OwnTracks location row stores the rolling
odometer value for that point, and generated trips use those rolling values for start and end
odometers. The trip end odometer is always advanced from the start odometer by the stored trip
distance so the odometer display follows the trip miles. Segments fully inside the same saved
waypoint are ignored to reduce stationary GPS drift. Manual odometer entries on Diagnostics reset
the checkpoint to the entered value and OwnTracks distance continues from that new rolling value.
Dashboard total-driven cards sum OwnTracks coordinate segments directly for the selected local day
or month, so manual odometer resets do not affect trip plus non-trip totals.
The Diagnostics Manual Odometer card shows the current reading and its source next to the form so
the existing checkpoint can be checked before entering a correction. That card sits in the same
Diagnostics row as the EIA API test card and the current OwnTracks State card. Diagnostics also
shows hard drive space for key runtime paths, combining paths into one row when their exact free
space and total capacity match.

## Cloudflare Tunnel

The Compose file includes `cloudflared` as a normal required service for a remotely managed
Cloudflare Tunnel. In the Cloudflare dashboard, publish the application route to the internal
service:

```text
http://nginx:80
```

Set the tunnel token in `.env`:

```env
CLOUDFLARED_TUNNEL_TOKEN=your-cloudflare-tunnel-token
CLOUDFLARED_LOG_LEVEL=info
CLOUDFLARED_METRICS=
CLOUDFLARED_TRANSPORT_PROTOCOL=auto
```

Then start the normal stack with `docker compose up -d --build`.

A background processor also runs while the web app is up. It recalculates the current local day on a
short interval and finalizes completed local days. After trip processing updates its checkpoint,
processed OwnTracks rows older than `OWNTRACKS_LOCATION_RETENTION_DAYS` are purged automatically.
Trips, waypoints, reports, and gas price records are not removed by this purge.

Useful Docker environment options:

```env
OWNTRACKS_SYNC_WAYPOINTS=true
OWNTRACKS_DEFAULT_SITE_RADIUS_M=150
LOCAL_TIMEZONE=America/Detroit
AUTOMATIC_TRIP_PROCESSING_ENABLED=true
AUTOMATIC_TRIP_PROCESSING_INTERVAL_SECONDS=60
OWNTRACKS_PURGE_ENABLED=true
OWNTRACKS_LOCATION_RETENTION_DAYS=14
OWNTRACKS_WAYPOINT_DWELL_MINUTES=5
OWNTRACKS_TRAVEL_DISTANCE_M=50.0
WEB_LOGIN_USERNAME=admin
WEB_LOGIN_PASSWORD=change-web-login-password
WEB_SESSION_COOKIE_SECURE=true
WEB_LOGIN_MAX_ATTEMPTS=5
WEB_LOGIN_LOCKOUT_SECONDS=300
HOST_LOG_DIR=/var/log/mileage-logger
HOST_LOGIN_FAILURE_LOG_PATH=/var/log/mileage-logger-login-failures.log
AUTOMATIC_BACKUPS_ENABLED=true
AUTOMATIC_BACKUP_DIR=/data/logs/backups
MAX_BACKUP_RESTORE_BYTES=262144000
```

Docker Compose passes `LOCAL_TIMEZONE` through as the container `TZ` value for the app stack.
Set both `WEB_LOGIN_USERNAME` and `WEB_LOGIN_PASSWORD` to enable login on rendered web pages while
public nginx exposes only the OwnTracks ingestion endpoints under `/api/`. `WEB_LOGIN_MAX_ATTEMPTS` and
`WEB_LOGIN_LOCKOUT_SECONDS` control the temporary lockout for repeated failed attempts.
Docker binds `/data/logs` to `HOST_LOG_DIR` so the Docker host can read `app.log`,
`trip-calculation.log`, worker logs, and `mileage-logger-login-failures.log` directly. The app
writes failed-login audit records inside the mounted log directory; `HOST_LOGIN_FAILURE_LOG_PATH`
is only a host-side symlink alias for the shorter `/var/log/mileage-logger-login-failures.log`
path. The same failed-login entries are shown and downloadable from Diagnostics.
Automatic backups default to `/data/logs/backups`, which is inside the same `HOST_LOG_DIR` bind
mount, and are listed/restorable from Diagnostics after web login.
Diagnostics marks travel when recent OwnTracks movement outside saved waypoints covers at least
`OWNTRACKS_TRAVEL_DISTANCE_M` meters.
Set `LOG_LEVEL` to `debug`, `info`, or `warning`; error lines are always included at every level.

## Workflow

1. Create work waypoints in OwnTracks and publish/export them to the server.
2. Review or export saved waypoints from the `Waypoints` page.
3. Configure OwnTracks to send waypoint transition events and normal location updates.
4. Let the app automatically create trips from incoming OwnTracks transitions.
5. Review `Trips`, switch to the needed month, add manual trips with the local-today date default,
   and edit row waypoint dropdowns or miles if needed. Existing row dates and odometers are
   read-only.
6. Add or fetch a monthly gas price for that report month.
7. Download the monthly PDF report from the `Trips` page.

## Project Commands

```bash
ruff check .
pytest
alembic revision --autogenerate -m "message"
alembic upgrade head
bash -n scripts/*.sh
cp .env.docker.example .env
docker compose config
```
