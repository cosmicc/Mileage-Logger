# Installation

This app is intended to run as a Docker Compose stack on an Ubuntu server. The stack includes:

- `postgres`: PostgreSQL database.
- `app`: FastAPI mileage logger.
- `nginx`: reverse proxy that serves the web app on HTTP port `80`.
- `cloudflared`: Cloudflare Tunnel connector for public HTTPS access.
- `gas-snapshot`: daily Michigan gas price snapshot worker.
- Host log bind mounts for diagnostics logs and failed web-login audit records.

## Requirements

- Ubuntu server with network access.
- Docker Engine with the Docker Compose plugin.
- A DNS name or static IP address for OwnTracks to reach the server.
- Port `80` open to the network where your phone will connect.

For internet-facing use, put HTTPS in front of this stack before using it with real location data.
OwnTracks HTTP mode supports Basic Auth, but credentials and location data should still travel over
TLS.

## Install Docker On Ubuntu

If Docker is not installed yet, install it from Docker's Ubuntu repository or from Ubuntu packages.
The simplest package-based install is:

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-v2
sudo systemctl enable --now docker
```

Confirm Docker Compose works:

```bash
docker compose version
```

If you want to run Docker without `sudo`, add your user to the `docker` group and log out/in:

```bash
sudo usermod -aG docker "$USER"
```

## Get The App

Clone the repository on the server:

```bash
git clone https://github.com/cosmicc/Mileage-Logger.git
cd Mileage-Logger
```

## Create Configuration

Generate a production `.env` file:

```bash
./scripts/init_docker_env.sh
```

This creates `.env` from `.env.docker.example` and generates values for:

- `SECRET_KEY`
- `WEB_LOGIN_PASSWORD`
- `POSTGRES_PASSWORD`
- `OWNTRACKS_API_TOKEN`
- `OWNTRACKS_PASSWORD`

It also tries to prepare `HOST_LOG_DIR`, the default `backups/` directory inside it, the failed-login
log file inside that directory, and the optional `HOST_LOGIN_FAILURE_LOG_PATH` symlink on the Docker host. If your user cannot write to
`/var/log`, create them before starting Docker:

```bash
sudo install -d -m 0750 /var/log/mileage-logger
sudo install -m 0640 /dev/null /var/log/mileage-logger/mileage-logger-login-failures.log
sudo ln -sfn /var/log/mileage-logger/mileage-logger-login-failures.log /var/log/mileage-logger-login-failures.log
```

If an earlier failed container start created `/var/log/mileage-logger-login-failures.log` as a
directory, remove that empty directory before creating the symlink:

```bash
sudo rmdir /var/log/mileage-logger-login-failures.log
```

Review the file before starting, and set `CLOUDFLARED_TUNNEL_TOKEN` to the token from the
Cloudflare dashboard:

```bash
nano .env
```

Important values:

```env
HTTP_PORT=80
WEB_ALLOWED_CIDRS=
WEB_LOGIN_USERNAME=admin
WEB_LOGIN_PASSWORD=<generated-web-password>
WEB_SESSION_COOKIE_SECURE=true
WEB_LOGIN_MAX_ATTEMPTS=5
WEB_LOGIN_LOCKOUT_SECONDS=300
OWNTRACKS_USERNAME=owntracks
OWNTRACKS_PASSWORD=<generated-password>
OWNTRACKS_SYNC_WAYPOINTS=true
AUTOMATIC_TRIP_PROCESSING_ENABLED=true
AUTOMATIC_TRIP_PROCESSING_INTERVAL_SECONDS=60
OWNTRACKS_PURGE_ENABLED=true
OWNTRACKS_LOCATION_RETENTION_DAYS=14
LOG_DIR=/data/logs
HOST_LOG_DIR=/var/log/mileage-logger
HOST_LOGIN_FAILURE_LOG_PATH=/var/log/mileage-logger-login-failures.log
AUTOMATIC_BACKUPS_ENABLED=true
AUTOMATIC_BACKUP_DIR=/data/logs/backups
MAX_BACKUP_RESTORE_BYTES=262144000
LOG_LEVEL=info
GAS_PRICE_SOURCE=aaa_current
VEHICLE_MPG=25.0
CLOUDFLARED_TUNNEL_TOKEN=
CLOUDFLARED_LOG_LEVEL=info
CLOUDFLARED_METRICS=
CLOUDFLARED_TRANSPORT_PROTOCOL=auto
```

The generated `OWNTRACKS_USERNAME` and `OWNTRACKS_PASSWORD` are what you enter in OwnTracks HTTP
mode.

## Public Web And API Exposure

The nginx container exposes rendered web pages and the OwnTracks ingestion API. Public nginx only
forwards these API requests:

- `POST /api/owntracks`
- `POST /api/owntracks/`
- `POST /api/pub`

All other `/api/` routes, `/docs`, `/redoc`, and `/openapi.json` return `404` through nginx.
Internal app health checks still call `/api/health` directly inside the app container.

You can restrict browser UI pages to specific IP blocks while keeping OwnTracks ingestion open.

Set `WEB_ALLOWED_CIDRS` to comma-separated CIDR blocks:

```env
WEB_ALLOWED_CIDRS=192.168.1.0/24,10.8.0.0/24,203.0.113.44/32
```

With this set:

- OwnTracks ingestion endpoints remain reachable from any IP so OwnTracks can keep sending data.
- `/`, `/trips`, `/waypoints`, `/diagnostics`, `/static/`, and other web UI paths require a matching
  client IP.

Leave `WEB_ALLOWED_CIDRS` blank to keep the current behavior and allow all clients to access the
web UI.

If this stack is behind another reverse proxy, nginx will usually see that proxy's IP address
instead of the original client IP. In that setup, enforce IP restrictions at the outer proxy or
include the proxy's address in `WEB_ALLOWED_CIDRS`.

## Start The Stack

Build and start everything:

```bash
docker compose up -d --build
```

Check status:

```bash
docker compose ps
```

Expected result:

- `postgres` healthy.
- `app` healthy.
- `nginx` running.
- `gas-snapshot` running.
- `cloudflared` running.

Open the app:

```text
http://your-server/
```

The app container runs database migrations automatically on startup.

## Portainer Stack Install

Portainer can deploy this repository directly from GitHub using `docker-compose.yml`.
The Compose file does not use `env_file`, so Portainer does not need a `.env` file mounted beside
the stack.

In Portainer:

1. Go to `Stacks`.
2. Add a new stack.
3. Choose the Git repository option.
4. Repository URL:

```text
https://github.com/cosmicc/Mileage-Logger.git
```

5. Compose path:

```text
docker-compose.yml
```

6. Import or enter the environment variables from `.env.docker.example`.
7. Change these required secret values before deploying:
   - `SECRET_KEY`
   - `POSTGRES_PASSWORD`
   - `DATABASE_URL`
   - `OWNTRACKS_API_TOKEN`
   - `OWNTRACKS_PASSWORD`
   - `CLOUDFLARED_TUNNEL_TOKEN`
8. Optional: set `WEB_ALLOWED_CIDRS` to restrict web UI access while keeping OwnTracks ingestion
   open.
9. Deploy the stack.

If you change `POSTGRES_PASSWORD`, make sure `DATABASE_URL` uses the same password:

```env
POSTGRES_PASSWORD=your-db-password
DATABASE_URL=postgresql+psycopg://mileage:your-db-password@postgres:5432/mileage_logger
```

The app will receive configuration from the environment variables imported into the Portainer
stack.

The diagnostics page is available at:

```text
http://your-server/diagnostics
```

It shows app status, recent database records, failed web-login attempts, and recent app logs.

## Configure OwnTracks

In OwnTracks on Android:

1. Set connection mode to `HTTP`.
2. Set the URL to:

```text
http://your-server/api/owntracks
```

3. Set HTTP Basic Auth credentials:
   - Username: value of `OWNTRACKS_USERNAME` in `.env`
   - Password: value of `OWNTRACKS_PASSWORD` in `.env`
4. Set Identification:
   - Username: your name or short ID, for example `ian`
   - Device name: your phone name, for example `pixel`
   - Tracker ID: two letters, for example `IP`
5. Set monitoring mode to `Move`.
6. Grant location permission `Allow all the time`.
7. Disable Android battery optimization for OwnTracks.
8. Publish a test payload or trigger a waypoint transition to confirm the server receives OwnTracks.

For work waypoints, add OwnTracks regions/waypoints on the phone. Keep OwnTracks location
reporting enabled so the app receives location updates between waypoint transitions. If you use
MQTT, publish waypoints and keep `MQTT_TOPIC=owntracks/#` so the app receives waypoint,
transition, and location update events. If you use HTTP, OwnTracks sends its payloads to the
configured endpoint and the app will process supported waypoint, transition, and location
payloads.

You can also use the Recorder-compatible endpoint:

```text
http://your-server/api/pub
```

The app supports both `/api/owntracks` and `/api/pub`.

## Waypoint Trip Detection

Trips are generated from OwnTracks waypoint transition events.

Default behavior:

- A trip is created from a waypoint `leave` event followed by another waypoint `enter` event.
- The destination `enter` event must be confirmed by at least
  `OWNTRACKS_WAYPOINT_DWELL_MINUTES` minutes of later OwnTracks data inside that saved waypoint.
  The default is 5 minutes so driving through a waypoint does not create a trip.
- `Home` is the exact waypoint name for home.
- `Home` to `Home` is never a trip.
- Trips between the same non-home waypoint are kept.
- If an `enter` event arrives without a matching `leave`, the app infers the origin from the
  previous waypoint. If there is no previous waypoint and the destination is not `Home`, the app
  assumes the missed origin was `Home`.

Trip generation is automatic. Every incoming OwnTracks location or transition payload is stored in
`owntracks_locations` and immediately triggers trip recalculation for that payload's
`LOCAL_TIMEZONE` day. Generated trip rows are stored in `trips`.
OwnTracks `tst` event time is the authoritative timestamp for trip dates and ordering; the server
receive time is kept separately for diagnostics because phone data can be buffered.
The server can run on UTC; app day/month selection, dashboard time, and gas
snapshot dates use `LOCAL_TIMEZONE`, default `America/Detroit` for EST/EDT.

Generated mileage uses this order:

1. OwnTracks location path distance from the location updates received between the waypoint
   `leave` and `enter` events.
2. Waypoint-to-waypoint distance when OwnTracks path data is not available.

If a trip window has only transition events and no location updates between them, the app falls
back to waypoint distance. Odometer values are never used to calculate trip distance, Dashboard
trip plus non-trip totals, or monthly trip plus non-trip totals. Edit a trip's miles on the
`Trips` page when the generated mileage needs correction. A distance correction resequences that month's displayed
start and end odometers in chronological trip order. Deleting a trip from the
`Trips` page also saves an exact deleted-trip record so only that same OwnTracks transition pair
is not generated again; future trips with the same route are still generated normally.
The checkpoint odometer is advanced from OwnTracks path distance between received points even when
those points do not become a trip. Each processed OwnTracks location row stores the rolling
odometer value for that point, and generated trips use those rolling values for start and end
odometers. The trip end odometer is always advanced from the start odometer by the stored trip
distance so the odometer display follows the trip miles. Segments fully inside the same saved
waypoint are ignored to reduce stationary GPS drift. Manual odometer entries on Diagnostics reset
the checkpoint to the entered value and OwnTracks distance continues from that new rolling value.

## Cloudflare Tunnel

The Compose file includes `cloudflared` as a normal required service for a remotely managed
Cloudflare Tunnel. In the Cloudflare dashboard, publish the application route to the
Compose-internal service:

```text
http://nginx:80
```

Then set:

```env
CLOUDFLARED_TUNNEL_TOKEN=your-cloudflare-tunnel-token
CLOUDFLARED_LOG_LEVEL=info
CLOUDFLARED_METRICS=
CLOUDFLARED_TRANSPORT_PROTOCOL=auto
```

Start the normal stack:

```bash
docker compose up -d --build
```

The web app also starts a background processor. It recalculates the current local day on a short
interval and finalizes completed local days. After trip processing updates its checkpoint,
processed OwnTracks rows older than `OWNTRACKS_LOCATION_RETENTION_DAYS` are purged automatically.
Trips, waypoints, reports, and gas price records are not removed by this purge.

Configuration:

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

When `OWNTRACKS_SYNC_WAYPOINTS=true`, published OwnTracks waypoint payloads create or update app
waypoints. Location `inregions` values are only used to match already-saved waypoints; they do not
create new waypoints.
The web login protects rendered browser pages only. The app leaves `/api/` outside web login
internally, while public nginx exposes only the OwnTracks ingestion endpoints so OwnTracks can
continue to use its existing API authentication. Set
`WEB_SESSION_COOKIE_SECURE=false` only when testing over plain HTTP. The login page does not reveal
the app name before authentication and temporarily locks out repeated failed attempts. Failed login
attempts and lockout rejections are written as structured JSON-lines records to
`/data/logs/mileage-logger-login-failures.log` inside the app container, which is backed by
`HOST_LOG_DIR` on the Docker host. `HOST_LOGIN_FAILURE_LOG_PATH` is an optional host symlink alias.
The submitted password value is never stored; only its length is recorded.
The Diagnostics page marks travel when recent OwnTracks movement outside saved waypoints covers at
least `OWNTRACKS_TRAVEL_DISTANCE_M` meters.

## Test Ingestion

From the server, send a test point:

```bash
source .env
curl -u "${OWNTRACKS_USERNAME}:${OWNTRACKS_PASSWORD}" \
  -H "Content-Type: application/json" \
  -d "{\"_type\":\"location\",\"lat\":42.3314,\"lon\":-83.0458,\"tst\":$(date +%s),\"tid\":\"IP\",\"topic\":\"owntracks/test/phone\"}" \
  "http://127.0.0.1:${HTTP_PORT:-80}/api/owntracks"
```

Expected response:

```json
[]
```

View the newest stored point on Diagnostics:

```text
http://127.0.0.1:${HTTP_PORT:-80}/diagnostics
```

## Configure Waypoints And Reports

1. Open `http://your-server/`.
2. Add work waypoints in OwnTracks and publish them to the server.
3. Go to `Waypoints` to review saved waypoints or export an OwnTracks waypoint backup.
4. Configure OwnTracks to send waypoint transition events and normal location updates.
5. Review automatically generated trips from the `Trips` page.
6. Open `Trips`, choose the report month, add manual trips, and correct dates, waypoint names, or
   miles if needed.
7. Confirm `VEHICLE_MPG` is set correctly and add or fetch the monthly gas price for that month.
8. Click `Download PDF Report` to generate and download the PDF.

The PDF can be generated for any retained month that has trips and a saved monthly gas price or
daily gas snapshots for that month. The automatic OwnTracks purge removes only processed raw
OwnTracks rows after the retention window and keeps generated trips locked in.

Reimbursement is calculated as:

```text
total trip miles / VEHICLE_MPG = reimbursement gallons
reimbursement gallons * Michigan monthly average gas price = total reimbursement
```

PDF reports are generated only when you click `Download PDF Report`; they are streamed to the
browser and are not saved on the server.

Runtime logs are written to `/data/logs` inside the app and gas-snapshot containers, and Docker
binds that directory to `HOST_LOG_DIR` on the server. The failed-login audit file is stored in the
same mounted directory as `mileage-logger-login-failures.log`; do not bind-mount that file
individually because Docker can create a directory at the source path and prevent the container
from starting.
Log timestamps are formatted in `LOCAL_TIMEZONE`, and Docker Compose also sets the container `TZ`
value from `LOCAL_TIMEZONE`.
Set `LOG_LEVEL` to `debug`, `info`, or `warning`. Error log lines are always included.

## Gas Price Worker

The `gas-snapshot` service runs:

```bash
mileage-logger gas-snapshot
```

By default it runs once on startup and then every 24 hours.

Relevant `.env` settings:

```env
GAS_PRICE_SOURCE=aaa_current
GAS_SNAPSHOT_INTERVAL_SECONDS=86400
GAS_SNAPSHOT_RUN_ON_STARTUP=true
```

View gas worker logs:

```bash
docker compose logs -f gas-snapshot
```

You can also view recent app logs and failed-login audit records from the in-app `Diagnostics`
page.

## MQTT Mode

HTTP mode is recommended for OwnTracks Android. If you want MQTT instead, set these in `.env`:

```env
MQTT_ENABLED=true
MQTT_HOST=your-broker
MQTT_PORT=1883
MQTT_USERNAME=your-user
MQTT_PASSWORD=your-password
MQTT_TOPIC=owntracks/#
```

Restart the app after changing MQTT settings:

```bash
docker compose up -d --build
```

## HTTPS

Do not expose real location data over plain HTTP on the internet.

Recommended options:

- Put this stack behind an existing reverse proxy that handles Let's Encrypt.
- Use Cloudflare Tunnel, Tailscale Funnel, Caddy, Traefik, or another TLS terminator.
- Extend `deploy/nginx` later to include certificates directly.

If TLS terminates outside this Compose stack, proxy traffic to this stack's `HTTP_PORT`.

## Maintenance

View logs:

```bash
docker compose logs -f app
docker compose logs -f nginx
docker compose logs -f postgres
```

Restart:

```bash
docker compose restart
```

Stop:

```bash
docker compose down
```

`docker compose down` stops and removes containers but keeps the named PostgreSQL volume. Do not
run `docker compose down -v` or Docker volume prune unless you have a verified full backup and
intend to delete the database.

Update from GitHub:

```bash
git pull
docker compose up -d --build
```

Normal rebuilds keep database rows because PostgreSQL stores data in the named Docker volume
`postgres_data` mounted at `/var/lib/postgresql/data`. In Portainer, keep the same stack name when
redeploying; changing the Compose project or stack name can make Docker create a different
`postgres_data` volume and look like a fresh install.

## Backups

The Diagnostics page includes authenticated full app data backup and restore controls. Use
`Download Full Backup` before updates or database work. The downloaded `.json.gz` file contains all
Mileage Logger app tables plus an OwnTracks waypoint export. To restore it, open Diagnostics,
upload the file, and type `RESTORE`; the app validates the backup before replacing current app
table rows in one transaction. Backup files contain sensitive location history and should be stored
securely.

The app also creates automatic hourly full-data backups by default. In Docker they are stored under
`/data/logs/backups`, backed by `HOST_LOG_DIR` on the host, unless `AUTOMATIC_BACKUP_DIR` is set to
another private path. Diagnostics lists retained automatic backups and can restore a selected file
after you type `RESTORE`. Retention keeps the newest 6 hourly backups plus one daily backup for
today and each of the prior 2 days.

Back up PostgreSQL:

```bash
docker compose exec -T postgres pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" > mileage_logger.sql
```

The in-app backup is the preferred quick recovery file for this application. `pg_dump` remains
useful for low-level PostgreSQL administration or migration outside the app.

Volume names may differ if your Compose project name is not `mileage-logger`. Check with:

```bash
docker volume ls | grep mileage
```

## Troubleshooting

Check container health:

```bash
docker compose ps
```

Check app startup and migration logs:

```bash
docker compose logs app
```

Validate Nginx proxy:

```bash
curl -i "http://127.0.0.1:${HTTP_PORT:-80}/"
curl -i "http://127.0.0.1:${HTTP_PORT:-80}/api/health" # Expected public result: 404
```

If OwnTracks returns unauthorized, confirm `.env` values and restart:

```bash
grep OWNTRACKS .env
docker compose restart app
```

If ports conflict, change `HTTP_PORT` in `.env`:

```env
HTTP_PORT=8080
```

Then restart:

```bash
docker compose up -d
```

If the web UI returns `403 Forbidden`, your client IP does not match `WEB_ALLOWED_CIDRS`.
OwnTracks ingestion endpoints should still be reachable; other public `/api/` routes are
intentionally blocked by nginx.
