# Mileage Logger

Mileage Logger receives OwnTracks waypoint events from an Android phone over HTTP or MQTT,
stores them in PostgreSQL, lets you review and edit generated waypoint work trips, and produces
monthly mileage and expense PDF logs.

## Current Scope

- FastAPI web app with server-rendered review screens.
- Dashboard opens with a loading message before fetching calculated mileage and reimbursement
  summaries.
- PostgreSQL models and Alembic migration.
- OwnTracks HTTP endpoint at `/api/owntracks` and Recorder-compatible `/api/pub`.
- Optional MQTT subscriber for `owntracks/#` topics so location, waypoint, and transition events
  are available.
- Default-on persistent OwnTracks buffer that keeps accepting HTTP and MQTT payloads during
  PostgreSQL outages and replays them in receive order after the database returns.
- OwnTracks waypoint transition model used to turn leave/enter events into work trips, with
  location updates between those events used as the primary trip distance.
- Manual current-odometer entry from the Diagnostics page, with the Manual Odometer card showing
  the latest current reading before saving a new checkpoint.
- Optional Pushover app-health notifications for degraded or unavailable app state and restoration.
- Manual trip entry defaults to today's local date and uses saved waypoint dropdowns for From/To,
  with trip-row waypoint and mileage review on the Work Trips page. Work Trips uses one month/year
  picker that loads the selected month automatically and shows compact selected-month summary
  cards.
- Monthly gas price cache with a provider layer.
- Monthly PDF report generation with optional extra expense lines.
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

## Docker Deployment

Mileage Logger is intended to run as a Docker Compose stack. It runs the complete stack:

- Optional bundled PostgreSQL database, or a remote PostgreSQL database through `DATABASE_URL`.
- FastAPI mileage app.
- Web service reverse proxy on port `80`.
- Daily gas price snapshot scheduler inside the app container.
- Cloudflare Tunnel connector using the configured tunnel token.
- Persistent Docker volume for database data and host bind mounts for runtime logs.
- Persistent host bind mount for the local OwnTracks outage buffer so accepted phone data survives
  image rebuilds and container replacement.
- In-app diagnostics page for app logs, trip calculation logs, successful and failed web-login
  audit records, and OwnTracks state in the configured local timezone. The top Diagnostics cards
  are grouped into a three-column desktop grid, hard drive rows combine matching used and total
  space readings, a yellow or red degraded banner appears when monitored app-health checks need
  attention, detailed lists use compact 10-row pages, and Full Data Backup stays at the bottom
  under the App Log.
- Authenticated desktop navigation uses centered blue raised icon-and-label buttons matching the
  mobile web-app layout, where navigation becomes icon-only in one full-width top-bar row and
  leaves the bottom safe area clear for phone system navigation without opting into edge-to-edge
  phone drawing. App buttons use a raised treatment, brighten on hover, and press inward when
  clicked. The login page does not show the shared top navigation.
- Web-login audit records shown on Diagnostics and written into the host log directory, with an
  optional `/var/log/mileage-logger-login-failures.log` host symlink.
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

`scripts/init_docker_env.sh` tries to create the host log directory, host OwnTracks buffer
directory, web-login audit log file, and the short `/var/log/mileage-logger-login-failures.log`
symlink. If your user cannot write to `/var/log` or `/var/lib`, create them before starting
Docker:

```bash
sudo install -d -m 0750 /var/log/mileage-logger
sudo install -d -m 0750 /var/lib/mileage-logger/owntracks-buffer
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

With the default `COMPOSE_PROFILES=local-postgres` setting, database rows live in the Docker named
volume `postgres_data`, mounted at `/var/lib/postgresql/data` inside the PostgreSQL container.
Normal rebuilds such as `docker compose up -d --build` keep that volume. Do not use
`docker compose down -v`, Docker volume prune, or a different Compose/Portainer stack name unless
you have a verified backup and intend to move or recreate the local database.
To use a central PostgreSQL server instead, set `COMPOSE_PROFILES=` and point `DATABASE_URL` at
that server. The bundled `postgres` service is then not deployed, and the `POSTGRES_DB`,
`POSTGRES_USER`, and `POSTGRES_PASSWORD` variables only matter if you enable the local PostgreSQL
profile again. The app startup and migrations always wait on the configured `DATABASE_URL`. If the
database password contains URL-reserved characters, encode it before adding it to `DATABASE_URL`.
For example, `@` becomes `%40`.

OwnTracks buffering is enabled by default. If PostgreSQL is unreachable when the container starts,
the app starts in limp mode instead of exiting: browser pages show a single responsive database
warning page, non-OwnTracks API routes return 503 JSON, and OwnTracks HTTP/MQTT payloads are
validated then written to the local FIFO buffer. Docker stores that buffer at
`/data/owntracks-buffer/owntracks-buffer.sqlite3`, backed by
`HOST_OWNTRACKS_BUFFER_DIR` on the host. If the primary buffer mount is unavailable, Docker keeps
accepting outage payloads in the local named-volume fallback buffer configured by
`OWNTRACKS_BUFFER_FALLBACK_PATH`. Fallback entries replay immediately while the primary buffer is
still down only when the app observed the primary buffer fail before the database outage; otherwise
the app waits for both queues so payloads replay in receive order. Automatic trip processing, gas
snapshots, and automatic backups pause their database-writing passes while PostgreSQL is
unreachable. The limp-mode warning page shows generic queued-payload status plus primary and
backup queue state without exposing database host, IP, or connection-string details. It hides
normal app navigation and retries the app home page until service returns. A malformed `DATABASE_URL`
is treated as database unavailable so
the app can still start in limp mode and accept buffered OwnTracks payloads while the environment
value is corrected. `APP_HEALTHCHECK_START_PERIOD` should stay longer than
`DB_WAIT_TIMEOUT_SECONDS`; this prevents Docker Swarm from replacing the app task while the
entrypoint is waiting before limp mode starts.

Docker Swarm deployments use [docker-stack.yml](docker-stack.yml) instead of `docker-compose.yml`.
Swarm cannot build images, use Compose profiles, or keep the normal Compose loopback-only port
binding. Build/tag `APP_IMAGE` and `NGINX_IMAGE` first, set Swarm environment variables through
Portainer or the shell, and deploy the base stack for remote PostgreSQL. Add
[docker-stack.local-postgres.yml](docker-stack.local-postgres.yml) only when the bundled
PostgreSQL service should be part of the Swarm stack. In Swarm, configure the Cloudflare Tunnel
origin service as `http://nginx` so cloudflared reaches nginx over the stack overlay network.

OwnTracks HTTP mode should point at:

```text
http://your-server/api/owntracks
```

Use the `OWNTRACKS_USERNAME` and `OWNTRACKS_PASSWORD` values from `.env` for
OwnTracks HTTP Basic Auth, and set the OwnTracks payload encryption key to the
`OWNTRACKS_ENCRYPTION_KEY` value from `.env`. If you put credentials directly in the URL, use:

```text
http://owntracks:password@your-server/api/owntracks
```

For internet-facing use, put TLS in front of this stack or extend the web service container
with certificates so OwnTracks sends location data over HTTPS.

Non-OwnTracks API routes require a separate key:

```bash
curl -H "Authorization: Bearer ${WEB_API_KEY}" \
  "http://127.0.0.1:${HTTP_PORT:-80}/api/locations"
```

Do not reuse `OWNTRACKS_ENCRYPTION_KEY` as `WEB_API_KEY`. `/api/health` stays unauthenticated for
internal container health checks.

To restrict the browser UI while leaving OwnTracks ingestion open, set `WEB_ALLOWED_CIDRS`
to comma-separated IP blocks:

```env
WEB_ALLOWED_CIDRS=192.168.1.0/24,10.8.0.0/24,203.0.113.44/32
```

When this is blank, the web UI is open to all clients. When set, only
`POST /api/owntracks`, `POST /api/owntracks/`, `POST /api/pub`, and `POST /api/pub/` stay
reachable from any IP for OwnTracks. Pages such as `/`, `/trips`, `/waypoints`, `/diagnostics`,
and `/static/` require a matching client IP. Other `/api/` routes, `/docs`, `/redoc`, and
`/openapi.json` are blocked at the public web service reverse proxy.

The web service serves matching, end-user-focused error pages for common browser and gateway errors: 400,
401, 403, 404, 405, 408, 413, 429, 500, 502, 503, and 504. Each page explains the error and links
to `/login`, or back home when the browser already has an authenticated session.

To require a simple username/password login for browser pages, set both web login variables:

```env
SECRET_KEY=generate-a-long-random-value
WEB_LOGIN_USERNAME=admin
WEB_LOGIN_PASSWORD=change-web-login-password
WEB_SESSION_COOKIE_SECURE=true
WEB_LOGIN_MAX_ATTEMPTS=5
WEB_LOGIN_LOCKOUT_SECONDS=300
PASSKEY_RP_NAME=Mileage Logger
PASSKEY_RP_ID=
PASSKEY_ORIGIN=
```

The login protects rendered web pages such as `/`, `/trips`, `/waypoints`, and `/diagnostics`.
Unauthenticated browser paths are limited to `/login`, passkey login challenge/verify endpoints,
root icon and manifest files, the service worker, and `/static/` assets needed to render those
pages. Non-OwnTracks `/api/` routes use `WEB_API_KEY` instead of the web login, and the public web service
only exposes the OwnTracks ingestion endpoints. If you access the app over plain HTTP for local
testing, set
`WEB_SESSION_COOKIE_SECURE=false` so the browser accepts the session cookie. The login page does
not show the app name before authentication and temporarily locks out repeated failed attempts.
`WEB_LOGIN_USERNAME` and `WEB_LOGIN_PASSWORD` must be set together. When web login is enabled,
`SECRET_KEY` must be changed from `change-me`; production Docker starts fail closed if the login
credentials or session secret are missing. Docker publishes the web service only on `127.0.0.1`, so public
access should come through the bundled Cloudflare Tunnel service.
Each successful login, failed login attempt, and lockout rejection is appended to
`LOGIN_FAILURE_LOG_PATH` as a structured JSON-lines audit record. Failed entries include client IP
details, submitted username, password length, user agent, request path, reason, attempt count,
lockout state, and timestamps. Successful entries include client IP details, submitted username,
authentication method, web client, request path, and timestamps. The raw submitted password is
never stored. Diagnostics uses the stored effective client IP for successful-login and failed-login
rows, and the failed-login block button targets that same IP.

Diagnostics includes a Configure Passkey card for the single configured web-login user. Sign in
with the normal username/password once, create a passkey from Diagnostics, then use Device Sign-In
on the login page. Passkeys require a secure browser origin; when the app is published through
Cloudflare Tunnel, the default Docker web service config forwards the public HTTPS origin. If your proxy
setup is unusual, set `PASSKEY_ORIGIN=https://your-host.example.com` and
`PASSKEY_RP_ID=your-host.example.com` explicitly.

See [INSTALL.md](INSTALL.md) for the full Docker and Portainer installation guide.

## OwnTracks HTTP Setup

Set OwnTracks HTTP mode to:

```text
https://your-host.example.com/api/owntracks
```

Set HTTP Basic Auth in OwnTracks from `OWNTRACKS_USERNAME` and `OWNTRACKS_PASSWORD`, then set the
OwnTracks payload encryption key to `OWNTRACKS_ENCRYPTION_KEY`. When the encryption key is
configured, plaintext OwnTracks HTTP payloads are rejected.

`OWNTRACKS_ENCRYPTION_KEY` must be 32 UTF-8 bytes or fewer. The app pads shorter keys to
libsodium's 32-byte SecretBox key size, matching OwnTracks Recorder behavior.

The `/api/pub` alias, including `/api/pub/`, is also available for Recorder-style setups.

OwnTracks waypoints are saved as read-only work waypoints. When `OWNTRACKS_SYNC_WAYPOINTS=true`,
published OwnTracks waypoint payloads create or update matching app waypoints. The web app can
export the saved list as OwnTracks waypoint JSON for backup/import.

## Full Data Backup And Restore

Diagnostics includes a full app data backup and restore panel at the bottom of the page under the
App Log when `WEB_LOGIN_USERNAME` and `WEB_LOGIN_PASSWORD` are configured. The manual
`Download Full Backup` action sits with the lower upload-restore controls and creates a `.json.gz`
file containing all Mileage Logger database tables plus an OwnTracks waypoint export. Treat this
file as sensitive location history.

The app also creates automatic full-data backups every 6 hours when
`AUTOMATIC_BACKUPS_ENABLED=true`, which is the default. Automatic backups are stored in
`AUTOMATIC_BACKUP_DIR`, defaulting to `LOG_DIR/backups` such as `/data/logs/backups` in Docker.
Diagnostics lists retained automatic backups and can restore one after you type `RESTORE`. The
retention policy keeps the newest 4 recent automatic backups plus one daily backup for each of the
prior 2 days. Startup-created automatic backups are labeled in the table. Each listed automatic
backup also has its own download button. Backup downloads use `Cache-Control: no-store` and
require the same web login as restore because the files contain location history.

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

The app generates work trips directly from OwnTracks waypoint transitions:

- A trip is created from a waypoint `leave` event followed by another waypoint `enter` event.
- The destination `enter` event must be confirmed by at least
  `OWNTRACKS_WAYPOINT_DWELL_MINUTES` minutes of later OwnTracks data inside that saved waypoint.
  The default is 5 minutes so driving through a waypoint does not create a trip.
- `Home` is the exact waypoint name for home.
- `Home` to `Home` is never a trip.
- Work trips between the same non-home waypoint are kept only when the calculated distance is at
  least 1.0 mile, because shorter same-waypoint loops are treated as invalid GPS drift or
  non-work trips.
- If an `enter` event arrives without a matching `leave`, the app infers the most likely origin
  from the previous waypoint. If there is no previous waypoint and the destination is not `Home`,
  the app assumes the missed origin was `Home`.

Trip data is calculated automatically. Every incoming OwnTracks location or transition payload is
stored in `owntracks_locations` and immediately triggers trip recalculation for that payload's
`LOCAL_TIMEZONE` day when PostgreSQL is reachable. During a database outage, the payload is stored
in the persistent OwnTracks buffer first, then replayed later through the same processing path.
When the app sees a qualifying trip, it writes the generated row to `trips`.
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
Dashboard work trip plus non-work trip totals, or monthly work trip plus non-work trip totals.
Generated work trips use the master rolling OwnTracks odometer checkpoint or stamped rolling
OwnTracks event odometers for display; they do not use the previous work trip end odometer as the
source for the next start.
Dashboard work trip plus non-work trip cards are floored at the stored work trip total after
one-decimal rounding, so the displayed combined total is never lower than the work-trips-only total
and the implied non-work trip remainder is never negative. Edit a work trip's saved waypoint route
or miles on the `Work Trips` page when generated values need correction. A distance correction
resequences that month's displayed start and end odometers in chronological work trip order.
Manual work trips entered from the `Work Trips` page default to today's local
date, use saved waypoint dropdowns for From/To, and save start/end odometers immediately from the
current rolling OwnTracks odometer checkpoint plus the entered work trip miles. A manual work trip
is placed after the existing work trips on the selected local date, so backdated manual entries
land at the end of that day and today's manual entries become the latest work trip for today. If
the manual work trip is inserted before existing work trips, the app resequences that work trip and
every later work trip so odometers remain cumulative across month boundaries while preserving
existing positive odometer gaps between work trips for non-work trip driving. Deleting a
work trip from the `Work Trips` page also saves an exact deleted-trip record so only that same
OwnTracks transition pair is not generated again; future work trips with the same route are still
generated normally.
Automatic same-waypoint work trips under 1.0 mile are also removed with an exact suppression record
so older invalid rows do not return from the same OwnTracks transition pair.
The checkpoint odometer is advanced from OwnTracks path distance between received points even when
those points do not become a trip. Each processed OwnTracks location row stores the rolling
odometer value for that point, and generated trips use those rolling values for start odometers
when available. If transition rows are not stamped yet, generated trips use the master rolling
checkpoint before the trip start. Prior trip end odometers are not used as the source for generated
trip starts. The trip end odometer is always advanced from the start odometer by the stored trip
distance so the odometer display follows the trip miles. If a recently recorded work trip is
missing displayed odometers, automatic trip processing can backfill those blanks from the master
checkpoint when retained OwnTracks path rows support the estimate. Segments fully inside the same
saved waypoint are ignored to reduce stationary GPS drift. Manual odometer entries on Diagnostics
reset the checkpoint to the entered value and OwnTracks distance continues from that new rolling
value.
Trip creation, editing, deletion, and resequencing do not move the master rolling odometer
checkpoint.
Dashboard total-driven cards and the Work Trips selected-month cards sum OwnTracks coordinate
segments directly for the selected local day or month, so manual odometer resets do not affect work
trip plus non-work trip totals.
Dashboard OwnTracks Events and Work Trips count cards are scoped to the current app-local month,
which starts at midnight on the first day of the month in `LOCAL_TIMEZONE`. Prior months remain
available from the Work Trips month picker; month rollover does not delete prior-month trip,
OwnTracks, or gas price records. Before raw OwnTracks location/event rows age out, the app stores
monthly OwnTracks summary rollups so selected-month web totals and event counts remain stable after
the raw location rows are purged.
The Dashboard current-month reimbursement card uses the same trip-mile total, reimbursement
gallons, monthly gas price, `VEHICLE_MPG`, and extra expense total as the downloadable PDF report,
with displayed gallons limited to one decimal place. Dashboard top statistic and distance cards use
the same compact sizing as the Work Trips selected-month cards on full-width layouts, while mobile
keeps each card on its own row. Dashboard and Work Trips summary cards use comma thousands
separators for large displayed totals.
The Diagnostics Manual Odometer card shows the current reading and its source next to the form so
the existing checkpoint can be checked before entering a correction. The top Diagnostics cards are
grouped together in this order: Application, System Status, Data, Latest Records, OwnTracks State,
Manual Odometer, EIA API, Configure Passkey, and Hard Drive Space. On desktop they render three
cards per row. The System Status card shows PostgreSQL reachability, whether the configured
PostgreSQL host is remote, database latency, database size, total app records, pool/timeout
details, and compact primary/backup OwnTracks buffer status indicators. The Data card includes
lowest, current, current-month average, and highest gas price readings and comma-formatted large
record counts. Diagnostics also shows hard drive space for key runtime paths with used-space bars,
combining paths into one row when their exact used space and total capacity match, and includes
current database size plus total app record count at the bottom of the card. When app-health checks
detect degraded or unavailable service, Diagnostics shows a yellow or red banner above the top
cards for database, buffer, disk-space, login-lockout, or app-managed Cloudflare block issues.
Recent OwnTracks entries,
OwnTracks state changes, successful-login attempts, failed-login attempts, and app-managed
Cloudflare blocked IPs are displayed 10 rows at a time with mobile pagination buttons in one
full-width row and the page count shown as text below. Recent OwnTracks entries show original event
time, received delay, and a readable event label instead of the database row ID, raw receive
timestamps, battery level, or MQTT topic details.
Successful-login attempts show Password or Passkey method pills instead of an account column. The
OwnTracks state-change list omits the per-segment distance column and shows original event time,
received delay, state, waypoint, source, duration, and rolling odometer when available.

## Cloudflare Tunnel

The Compose file includes `cloudflared` as a normal required service for a remotely managed
Cloudflare Tunnel. The `cloudflared` container uses host networking so it can reach the host-bound
web service listener. In the Cloudflare dashboard, publish the application route to the host listener:

```text
http://127.0.0.1:80
```

If `HTTP_PORT=2082`, use `http://127.0.0.1:2082` as the Cloudflare Tunnel service URL. The Compose
stack always publishes the web service on `127.0.0.1:${HTTP_PORT:-80}` so it is not exposed on the host's
public interfaces.

The web service passes Cloudflare's `CF-Connecting-IP` to the app when present. The app uses that IP for login
audit records, lockouts, and automatic Cloudflare blocks; otherwise it falls back to the direct
loopback/tunnel client.

Set the tunnel token in `.env`:

```env
CLOUDFLARED_TUNNEL_TOKEN=your-cloudflare-tunnel-token
CLOUDFLARED_LOG_LEVEL=info
CLOUDFLARED_METRICS=
CLOUDFLARED_TRANSPORT_PROTOCOL=auto
```

Then start the normal stack with `docker compose up -d --build`.

Background processors also run while the web app is up. Trip processing recalculates the current
local day on a short interval and finalizes completed local days. After trip processing updates its
checkpoint, processed OwnTracks location/event rows older than
`OWNTRACKS_LOCATION_RETENTION_DAYS` are purged automatically, with an enforced minimum retention of
90 days. Work trips, odometer fields, waypoints, reports, gas price records, monthly OwnTracks
summary rollups, backups, and other derived app data are not removed by this purge. The app
container also runs the daily gas snapshot scheduler when `GAS_SNAPSHOT_ENABLED=true`.

Useful Docker environment options:

```env
COMPOSE_PROFILES=local-postgres
OWNTRACKS_SYNC_WAYPOINTS=true
OWNTRACKS_DEFAULT_SITE_RADIUS_M=150
LOCAL_TIMEZONE=America/Detroit
DATABASE_URL=postgresql+psycopg://mileage:change-postgres-password@postgres:5432/mileage_logger
DATABASE_POOL_SIZE=5
DATABASE_MAX_OVERFLOW=10
DATABASE_POOL_TIMEOUT_SECONDS=30
DATABASE_POOL_RECYCLE_SECONDS=1800
DATABASE_CONNECT_TIMEOUT_SECONDS=10
DATABASE_RUN_MIGRATIONS_ON_RECONNECT=true
DB_WAIT_TIMEOUT_SECONDS=60
AUTOMATIC_TRIP_PROCESSING_ENABLED=true
AUTOMATIC_TRIP_PROCESSING_INTERVAL_SECONDS=60
OWNTRACKS_PURGE_ENABLED=true
OWNTRACKS_LOCATION_RETENTION_DAYS=90
OWNTRACKS_WAYPOINT_DWELL_MINUTES=5
OWNTRACKS_TRAVEL_DISTANCE_M=50.0
OWNTRACKS_BUFFER_ENABLED=true
OWNTRACKS_BUFFER_PATH=/data/owntracks-buffer/owntracks-buffer.sqlite3
OWNTRACKS_BUFFER_FALLBACK_PATH=/data/owntracks-buffer-fallback/owntracks-buffer.sqlite3
OWNTRACKS_BUFFER_REPLAY_INTERVAL_SECONDS=15
OWNTRACKS_BUFFER_REPLAY_BATCH_SIZE=100
OWNTRACKS_ENCRYPTION_KEY=change-owntracks-encryption-key
WEB_API_KEY=change-web-api-key
WEB_LOGIN_USERNAME=admin
WEB_LOGIN_PASSWORD=change-web-login-password
WEB_SESSION_COOKIE_SECURE=true
WEB_LOGIN_MAX_ATTEMPTS=5
WEB_LOGIN_LOCKOUT_SECONDS=300
PASSKEY_RP_NAME=Mileage Logger
PASSKEY_RP_ID=
PASSKEY_ORIGIN=
CLOUDFLARE_IP_BLOCKING_ENABLED=false
CLOUDFLARE_API_TOKEN=
CLOUDFLARE_ZONE_ID=
CLOUDFLARE_IP_BLOCK_ALLOWLIST=
CLOUDFLARE_AUTO_BLOCK_FAILED_LOGIN_ATTEMPTS=5
PUSHOVER_ENABLED=false
PUSHOVER_TOKEN=
PUSHOVER_USER=
PUSHOVER_APP_KEY=
PUSHOVER_USER_KEY=
PUSHOVER_DEVICE=
PUSHOVER_PRIORITY=0
APP_HEALTH_MONITOR_INTERVAL_SECONDS=60
APP_HEALTH_DB_LATENCY_WARNING_MS=500
APP_HEALTH_DB_LATENCY_CRITICAL_MS=2000
APP_HEALTH_DISK_WARNING_PERCENT=85.0
APP_HEALTH_DISK_CRITICAL_PERCENT=95.0
APP_HEALTH_STATE_PATH=/data/logs/app-health-state.json
HTTP_PORT=80
HOST_LOG_DIR=/var/log/mileage-logger
HOST_LOGIN_FAILURE_LOG_PATH=/var/log/mileage-logger-login-failures.log
HOST_OWNTRACKS_BUFFER_DIR=/var/lib/mileage-logger/owntracks-buffer
AUTOMATIC_BACKUPS_ENABLED=true
AUTOMATIC_BACKUP_DIR=/data/logs/backups
MAX_BACKUP_RESTORE_BYTES=262144000
REPORT_DISPLAY_NAME=
GAS_SNAPSHOT_ENABLED=true
GAS_SNAPSHOT_INTERVAL_SECONDS=86400
GAS_SNAPSHOT_RUN_ON_STARTUP=true
```

Docker Compose passes `LOCAL_TIMEZONE` through as the container `TZ` value for the app stack.
Set both `WEB_LOGIN_USERNAME` and `WEB_LOGIN_PASSWORD` to enable login on rendered web pages while
the public web service exposes only the OwnTracks ingestion endpoints under `/api/`; production Docker
requires both values, `WEB_API_KEY`, `OWNTRACKS_ENCRYPTION_KEY`, OwnTracks Basic Auth credentials,
and a non-default `SECRET_KEY`. `WEB_LOGIN_MAX_ATTEMPTS` and
`WEB_LOGIN_LOCKOUT_SECONDS` control the temporary lockout for repeated failed attempts.
Passkeys are optional and are configured from Diagnostics after username/password login.
`PASSKEY_RP_NAME` controls the device prompt name. `PASSKEY_RP_ID` and `PASSKEY_ORIGIN` can be left
blank for normal same-host use, or set to the public HTTPS host and origin when a custom reverse
proxy does not forward the browser origin correctly.
When `CLOUDFLARE_IP_BLOCKING_ENABLED=true`, Diagnostics can create and remove app-managed
Cloudflare zone IP Access Rule blocks using `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ZONE_ID`.
`CLOUDFLARE_API_TOKEN` must be a Cloudflare API token with `Account Firewall Access Rules Write`
access for the configured zone; do not use `CLOUDFLARED_TUNNEL_TOKEN` or a Global API Key in that
field.
The app auto-blocks an IP after `CLOUDFLARE_AUTO_BLOCK_FAILED_LOGIN_ATTEMPTS` consecutive failed
web-login attempts, and a successful login from that IP resets the local consecutive-failure count.
The Cloudflare blocked-IP card also accepts a manual valid IP address plus a required reason, shows
manual or automatic source pills with the block reason in the app-managed block list, and removes
both the Cloudflare rule and local list row when you remove the block.
Set `CLOUDFLARE_IP_BLOCK_ALLOWLIST` to comma-separated trusted IPs or CIDRs that should never be
blocked by the app.
Set `PUSHOVER_ENABLED=true`, `PUSHOVER_TOKEN` to the Pushover app API token, and `PUSHOVER_USER`
to the Pushover user/group key to receive app-health notifications. `PUSHOVER_APP_KEY` and
`PUSHOVER_USER_KEY` are accepted aliases. The monitor watches PostgreSQL availability and
latency, OwnTracks buffer state and queued payloads, runtime disk usage, active web-login
lockouts, and app-managed Cloudflare blocks. It sends a degraded or unavailable notification when
the monitored issue set changes, and one restored notification when all monitored checks are
healthy again.
Docker binds `/data/logs` to `HOST_LOG_DIR` so the Docker host can read `app.log`,
`trip-calculation.log`, worker logs, gas snapshot logs, and
`mileage-logger-login-failures.log` directly. The app writes web-login audit records inside the
mounted log directory; `HOST_LOGIN_FAILURE_LOG_PATH` is only a host-side symlink alias for the
shorter `/var/log/mileage-logger-login-failures.log` path. Successful and failed login entries are
shown separately in Diagnostics.
Docker also binds `/data/owntracks-buffer` to `HOST_OWNTRACKS_BUFFER_DIR` and keeps
`/data/owntracks-buffer-fallback` in the local `owntracks_buffer_fallback` named volume. Keep both
stores mounted and access-restricted because they can contain buffered location history while
PostgreSQL is offline. Do not delete either store during rebuilds unless you have confirmed the
buffer is empty or you are intentionally discarding unreplayed OwnTracks payloads.
Automatic backups default to `/data/logs/backups`, which is inside the same `HOST_LOG_DIR` bind
mount, and are listed/restorable from Diagnostics after web login. Long automatic-backup filenames
are truncated in the Diagnostics table but remain visible on hover and available to download.
Backups created by the app startup pass are labeled as startup backups.
Diagnostics marks travel when recent OwnTracks movement outside saved waypoints covers at least
`OWNTRACKS_TRAVEL_DISTANCE_M` meters.
Set `LOG_LEVEL` to `debug`, `info`, or `warning`; error lines are always included at every level.

## Gas Price Snapshot Scheduler

In Docker, the app container runs the gas price snapshot scheduler instead of a separate
`gas-snapshot` sidecar container. It uses the same code as the manual command:

```bash
mileage-logger gas-snapshot
```

By default Docker runs one snapshot on app startup and then every 24 hours. Configure it with:

```env
GAS_SNAPSHOT_ENABLED=true
GAS_SNAPSHOT_INTERVAL_SECONDS=86400
GAS_SNAPSHOT_RUN_ON_STARTUP=true
```

Set `GAS_SNAPSHOT_ENABLED=false` to disable the in-app scheduler. The manual command remains
available, so a host systemd timer can run `docker compose exec -T app mileage-logger gas-snapshot`
on a schedule without cron if you prefer host-managed timing. The Docker image itself does not run
systemd inside the container.
Set `REPORT_DISPLAY_NAME` when downloaded PDF reports should identify who submitted the report; the
name appears under the report title as `Submitted by:`.
The PDF title uses `Mileage & Expense Report` plus the selected month and year. The Work Trips
page can add up to five extra expense lines per report month, and those lines appear after trip
rows in the PDF with date, reason, and price. The PDF summary highlights the final total
reimbursement dollar amount with a yellow background.

## Workflow

1. Create work waypoints in OwnTracks and publish/export them to the server.
2. Review or export saved waypoints from the `Waypoints` page.
3. Configure OwnTracks to send waypoint transition events and normal location updates.
4. Let the app automatically create work trips from incoming OwnTracks transitions.
5. Review `Work Trips`, choose the needed month/year with the month picker, edit row waypoint
   dropdowns or miles if needed, and add manual work trips with the local-today date default.
   Existing row dates and odometers are read-only. The summary cards show the selected month's work
   trip plus non-work trip miles, work-trip-only miles, OwnTracks events, work trip count,
   reimbursement, and monthly average gas price.
6. Add optional extra report expenses for the selected month, up to five lines.
7. Add or fetch a monthly gas price for that report month.
8. Download the portrait monthly PDF report from the `Work Trips` page.

## Project Commands

```bash
ruff check .
pytest
bash -n scripts/*.sh
CLOUDFLARED_TUNNEL_TOKEN=dummy-token docker compose --env-file .env.docker.example config
docker compose run --rm app alembic revision --autogenerate -m "message"
docker compose run --rm app alembic upgrade head
```
