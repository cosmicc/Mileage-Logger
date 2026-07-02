# Installation

This app is intended to run as a Docker Compose stack on an Ubuntu server. The stack includes:

- `postgres`: PostgreSQL database.
- `app`: FastAPI mileage logger.
- `nginx`: web service reverse proxy that serves the web app on HTTP port `80`.
- `cloudflared`: Cloudflare Tunnel connector for public HTTPS access.
- Daily Michigan gas price snapshots run as a background scheduler in the app container.
- Host log bind mounts for diagnostics logs and failed web-login audit records.

## Requirements

- Ubuntu server with network access.
- Docker Engine with the Docker Compose plugin.
- A DNS name or static IP address for OwnTracks to reach the server.
- Port `80` open to the network where your phone will connect.

For internet-facing use, put HTTPS in front of this stack before using it with real location data.
OwnTracks HTTP mode uses Basic Auth plus payload encryption, but credentials and metadata should
still travel over TLS.

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
- `WEB_API_KEY`
- `POSTGRES_PASSWORD`
- `OWNTRACKS_PASSWORD`
- `OWNTRACKS_ENCRYPTION_KEY`

It also tries to prepare `HOST_LOG_DIR`, the default `backups/` directory inside it, the web-login
audit log file inside that directory, the persistent `HOST_OWNTRACKS_BUFFER_DIR`, and the optional
`HOST_LOGIN_FAILURE_LOG_PATH` symlink on the Docker host. If your user cannot write to `/var/log`
or `/var/lib`, create them before starting Docker:

```bash
sudo install -d -m 0750 /var/log/mileage-logger
sudo install -d -m 0750 /var/lib/mileage-logger/owntracks-buffer
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
SECRET_KEY=<generated-session-secret>
WEB_LOGIN_USERNAME=admin
WEB_LOGIN_PASSWORD=<generated-web-password>
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
OWNTRACKS_USERNAME=owntracks
OWNTRACKS_PASSWORD=<generated-password>
OWNTRACKS_SYNC_WAYPOINTS=true
AUTOMATIC_TRIP_PROCESSING_ENABLED=true
AUTOMATIC_TRIP_PROCESSING_INTERVAL_SECONDS=60
OWNTRACKS_PURGE_ENABLED=true
OWNTRACKS_LOCATION_RETENTION_DAYS=90
OWNTRACKS_BUFFER_ENABLED=true
OWNTRACKS_BUFFER_PATH=/data/owntracks-buffer/owntracks-buffer.sqlite3
OWNTRACKS_BUFFER_FALLBACK_PATH=/data/owntracks-buffer-fallback/owntracks-buffer.sqlite3
OWNTRACKS_BUFFER_REPLAY_INTERVAL_SECONDS=15
OWNTRACKS_BUFFER_REPLAY_BATCH_SIZE=100
LOG_DIR=/data/logs
HOST_LOG_DIR=/var/log/mileage-logger
HOST_LOGIN_FAILURE_LOG_PATH=/var/log/mileage-logger-login-failures.log
HOST_OWNTRACKS_BUFFER_DIR=/var/lib/mileage-logger/owntracks-buffer
AUTOMATIC_BACKUPS_ENABLED=true
AUTOMATIC_BACKUP_DIR=/data/logs/backups
MAX_BACKUP_RESTORE_BYTES=262144000
LOG_LEVEL=info
GAS_PRICE_SOURCE=aaa_current
VEHICLE_MPG=25.0
REPORT_DISPLAY_NAME=
GAS_SNAPSHOT_ENABLED=true
GAS_SNAPSHOT_INTERVAL_SECONDS=86400
GAS_SNAPSHOT_RUN_ON_STARTUP=true
CLOUDFLARED_TUNNEL_TOKEN=
CLOUDFLARED_LOG_LEVEL=info
CLOUDFLARED_METRICS=
CLOUDFLARED_TRANSPORT_PROTOCOL=auto
```

Production starts fail closed when `SECRET_KEY` is still `change-me`, when one web login field is
blank, when both web login fields are missing, when `WEB_API_KEY` is missing, or when
`OWNTRACKS_ENCRYPTION_KEY` plus OwnTracks Basic Auth credentials are missing. Docker publishes the web service
only on `127.0.0.1`, so public access should come through the bundled Cloudflare Tunnel service.
Passkeys are optional. Create them from Diagnostics after username/password login. In normal
Cloudflare Tunnel Docker use, the web service forwards the public HTTPS origin for WebAuthn. If your proxy
does not, set `PASSKEY_ORIGIN=https://your-host.example.com` and
`PASSKEY_RP_ID=your-host.example.com`.

The generated `OWNTRACKS_USERNAME`, `OWNTRACKS_PASSWORD`, and `OWNTRACKS_ENCRYPTION_KEY` are what
you enter in OwnTracks HTTP mode. Do not reuse `OWNTRACKS_ENCRYPTION_KEY` as `WEB_API_KEY`; the
latter is only for non-OwnTracks API routes through `Authorization: Bearer <WEB_API_KEY>`.

## Public Web And API Exposure

The web service container exposes rendered web pages and the OwnTracks ingestion API. The public web service only
forwards these API requests:

- `POST /api/owntracks`
- `POST /api/owntracks/`
- `POST /api/pub`
- `POST /api/pub/`

All other `/api/` routes, `/docs`, `/redoc`, and `/openapi.json` return `404` through the web service.
Internal app health checks still call `/api/health` directly inside the app container. Non-OwnTracks
API routes still require `Authorization: Bearer <WEB_API_KEY>` when called from inside the Docker
network or another trusted internal path.

The web service serves custom styled error pages for 400, 401, 403, 404, 405, 408, 413, 429,
500, 502, 503, and 504 responses. The pages explain the error and include a link back to `/login`.
App-generated JSON API errors are not globally intercepted, so API clients such as OwnTracks can
still receive machine-readable responses from the app.

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

If this stack is behind another reverse proxy, the web service will usually see that proxy's IP address
instead of the original client IP. In that setup, enforce IP restrictions at the outer proxy or
include the proxy's address in `WEB_ALLOWED_CIDRS`.

For web-login audit records, temporary lockouts, and automatic Cloudflare blocks, the web service passes
Cloudflare's `CF-Connecting-IP` header through to the app when present. The app uses that IP,
otherwise it falls back to the direct loopback/tunnel client.

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
   - `WEB_API_KEY`
   - `POSTGRES_PASSWORD`
   - `DATABASE_URL`
   - `OWNTRACKS_PASSWORD`
   - `OWNTRACKS_ENCRYPTION_KEY`
   - `CLOUDFLARED_TUNNEL_TOKEN`
8. Optional: set `WEB_ALLOWED_CIDRS` to restrict web UI access while keeping OwnTracks ingestion
   open.
9. Deploy the stack.

If you change `POSTGRES_PASSWORD`, make sure `DATABASE_URL` uses the same password:

```env
POSTGRES_PASSWORD=your-db-password
DATABASE_URL=postgresql+psycopg://mileage:your-db-password@postgres:5432/mileage_logger
```

To use a central PostgreSQL server on your network later, leave the bundled `postgres` service in
the stack until you are ready to remove it and change only `DATABASE_URL`, for example:

```env
DATABASE_URL=postgresql+psycopg://mileage:your-db-password@central-db-host:5432/mileage_logger
```

The app waits for and runs migrations against the configured `DATABASE_URL`. The bundled local
PostgreSQL container can keep running unused during the transition. For a network database, tune
`DATABASE_POOL_SIZE`, `DATABASE_MAX_OVERFLOW`, `DATABASE_POOL_TIMEOUT_SECONDS`,
`DATABASE_POOL_RECYCLE_SECONDS`, `DATABASE_CONNECT_TIMEOUT_SECONDS`, and `DB_WAIT_TIMEOUT_SECONDS`
only if the central server or network latency requires different limits.

OwnTracks outage buffering is enabled by default. If the configured database is unreachable at
startup, Docker starts the app in limp mode instead of stopping the container. Browser pages show a
single responsive database warning page, non-OwnTracks API routes return 503 JSON, and OwnTracks
HTTP/MQTT payloads are validated then written to the local FIFO buffer. The buffer is stored at
`OWNTRACKS_BUFFER_PATH` inside the app container and should be backed by
`HOST_OWNTRACKS_BUFFER_DIR` so it survives rebuilds. If that primary buffer mount is unavailable,
the app writes outage payloads to the local Docker named-volume fallback path configured by
`OWNTRACKS_BUFFER_FALLBACK_PATH`. Fallback replay runs immediately while the primary buffer remains
unavailable only when the app observed the primary buffer fail before the database outage;
otherwise replay waits for both queues so payload order is preserved. When PostgreSQL is reachable
again, the app can run migrations on reconnect when `DATABASE_RUN_MIGRATIONS_ON_RECONNECT=true`,
then replay buffered payloads in receive order. Automatic trip processing, gas snapshots, and
automatic backups pause their database-writing passes while PostgreSQL is unreachable. The
limp-mode warning page shows PostgreSQL status plus primary and backup buffer state with
queued-payload totals.

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
4. Set payload encryption:
   - Encryption key: value of `OWNTRACKS_ENCRYPTION_KEY` in `.env`
5. Set Identification:
   - Username: your name or short ID, for example `ian`
   - Device name: your phone name, for example `pixel`
   - Tracker ID: two letters, for example `IP`
6. Set monitoring mode to `Move`.
7. Grant location permission `Allow all the time`.
8. Disable Android battery optimization for OwnTracks.
9. Publish a test payload or trigger a waypoint transition to confirm the server receives OwnTracks.

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

The app supports both `/api/owntracks` and `/api/pub`, including their trailing-slash aliases.

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
Cloudflare Tunnel. The `cloudflared` container uses host networking so it can reach the host-bound
web service listener. In the Cloudflare dashboard, publish the application route to the host listener:

```text
http://127.0.0.1:80
```

The Compose stack always publishes the web service on `127.0.0.1:${HTTP_PORT:-80}`. To use a different
local tunnel port, set:

```env
HTTP_PORT=2082
```

Then set the Cloudflare Tunnel service URL to:

```text
http://127.0.0.1:2082
```

The web service passes Cloudflare's `CF-Connecting-IP` to the app for login audit records, lockouts, and
automatic Cloudflare blocks. If that header is not present, the app uses the direct tunnel client.

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
processed OwnTracks location/event rows older than `OWNTRACKS_LOCATION_RETENTION_DAYS` are purged
automatically, with an enforced minimum retention of 90 days. Trips, odometer fields, waypoints,
reports, gas price records, monthly OwnTracks summary rollups, backups, and other derived app data
are not removed by this purge.

Configuration:

```env
OWNTRACKS_SYNC_WAYPOINTS=true
OWNTRACKS_DEFAULT_SITE_RADIUS_M=150
LOCAL_TIMEZONE=America/Detroit
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
HTTP_PORT=80
HOST_LOG_DIR=/var/log/mileage-logger
HOST_LOGIN_FAILURE_LOG_PATH=/var/log/mileage-logger-login-failures.log
HOST_OWNTRACKS_BUFFER_DIR=/var/lib/mileage-logger/owntracks-buffer
AUTOMATIC_BACKUPS_ENABLED=true
AUTOMATIC_BACKUP_DIR=/data/logs/backups
MAX_BACKUP_RESTORE_BYTES=262144000
```

When `OWNTRACKS_SYNC_WAYPOINTS=true`, published OwnTracks waypoint payloads create or update app
waypoints. Location `inregions` values are only used to match already-saved waypoints; they do not
create new waypoints.
The web login protects rendered browser pages only. Public unauthenticated browser paths are
limited to `/login`, passkey login challenge/verify endpoints, root icon and manifest files, the
service worker, and `/static/` assets needed to render those pages. Non-OwnTracks `/api/` routes
use `WEB_API_KEY` instead of the web login, while the public web service exposes only the OwnTracks ingestion
endpoints so OwnTracks can continue to use its existing API authentication. Set
`WEB_SESSION_COOKIE_SECURE=false` only when testing over plain HTTP. The login page does not reveal
the app name before authentication and temporarily locks out repeated failed attempts. Successful
logins, failed login attempts, and lockout rejections are written as structured JSON-lines records
to `/data/logs/mileage-logger-login-failures.log` inside the app container, which is backed by
`HOST_LOG_DIR` on the Docker host. `HOST_LOGIN_FAILURE_LOG_PATH` is an optional host symlink alias.
The submitted password value is never stored; failed-login entries record only its length.
Diagnostics resolves successful-login and failed-login rows from trusted forwarded metadata, so the
failed-login block button targets the real browser IP.
Diagnostics has a Configure Passkey card for the single configured web-login user. After creating a
passkey, the login page shows Device Sign-In. Failed passkey assertions are logged and counted
through the same lockout and Cloudflare auto-block path as failed password logins.
When `CLOUDFLARE_IP_BLOCKING_ENABLED=true`, Diagnostics can create and remove app-managed
Cloudflare zone IP Access Rule blocks using `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ZONE_ID`.
`CLOUDFLARE_API_TOKEN` must be a Cloudflare API token with `Account Firewall Access Rules Write`
access for the configured zone; do not use `CLOUDFLARED_TUNNEL_TOKEN` or a Global API Key in that
field.
The app automatically blocks a client IP after `CLOUDFLARE_AUTO_BLOCK_FAILED_LOGIN_ATTEMPTS`
consecutive failed web-login attempts. A successful login from that IP resets the consecutive
failure count. The Cloudflare blocked-IP card can also send a manually entered valid IP address
with a required reason, then shows the reason with an Auto or Manual source pill in the app-managed
list. Removing a block from the list removes both the Cloudflare rule and the local app-managed
row. Set `CLOUDFLARE_IP_BLOCK_ALLOWLIST` to comma-separated trusted IPs or CIDRs that
should never be blocked by this app.
The Diagnostics page marks travel when recent OwnTracks movement outside saved waypoints covers at
least `OWNTRACKS_TRAVEL_DISTANCE_M` meters.

## Test Ingestion

From the server, send a test point:

```bash
source .env
python - <<'PY'
import base64
import json
import os
import urllib.request
from datetime import datetime, UTC

from nacl.secret import SecretBox

key = os.environ["OWNTRACKS_ENCRYPTION_KEY"].encode("utf-8").ljust(SecretBox.KEY_SIZE, b"\0")
payload = {
    "_type": "location",
    "lat": 42.3314,
    "lon": -83.0458,
    "tst": int(datetime.now(UTC).timestamp()),
    "tid": "IP",
    "topic": "owntracks/test/phone",
}
encrypted = SecretBox(key).encrypt(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
body = json.dumps({
    "_type": "encrypted",
    "data": base64.b64encode(bytes(encrypted)).decode("ascii"),
}).encode("utf-8")
request = urllib.request.Request(
    f"http://127.0.0.1:{os.environ.get('HTTP_PORT', '80')}/api/owntracks",
    data=body,
    method="POST",
    headers={
        "Content-Type": "application/json",
        "Authorization": "Basic " + base64.b64encode(
            f"{os.environ['OWNTRACKS_USERNAME']}:{os.environ['OWNTRACKS_PASSWORD']}".encode()
        ).decode("ascii"),
    },
)
print(urllib.request.urlopen(request, timeout=10).status)
PY
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
6. Open `Trips`, choose the report month/year, add manual trips, and correct waypoints or miles if
   needed.
7. Confirm `VEHICLE_MPG` is set correctly and add or fetch the monthly gas price for that month.
8. Click `Download PDF Report` to generate and download the PDF.

The PDF can be generated for any retained month that has trips and a saved monthly gas price or
daily gas snapshots for that month. The automatic OwnTracks purge removes only processed raw
OwnTracks rows after the retention window and keeps generated trips locked in.
Set `REPORT_DISPLAY_NAME` in `.env` when the downloaded PDF should identify the report submitter;
when set, the name appears under the PDF title as `Submitted by:`.
The PDF report title shows the selected report month as a month name and year, such as
`Mileage Log - June 2026`.
The PDF summary highlights the final total reimbursement dollar amount with a yellow background.

The OwnTracks outage buffer can contain unreplayed location history while the database is down.
Keep `HOST_OWNTRACKS_BUFFER_DIR` and the `owntracks_buffer_fallback` Docker named volume mounted
and access-restricted, and do not delete either store during normal container rebuilds unless
Diagnostics or logs confirm the queues are empty.

Reimbursement is calculated as:

```text
total trip miles / VEHICLE_MPG = reimbursement gallons
reimbursement gallons * Michigan monthly average gas price = total reimbursement
```

PDF reports use a portrait page layout and are generated only when you click `Download PDF Report`;
they are streamed to the browser and are not saved on the server.

Runtime logs are written to `/data/logs` inside the app container, and Docker binds that directory
to `HOST_LOG_DIR` on the server. The web-login audit file is stored in the same mounted directory
as `mileage-logger-login-failures.log`; do not bind-mount that file individually because Docker
can create a directory at the source path and prevent the container from starting.
Log timestamps are formatted in `LOCAL_TIMEZONE`, and Docker Compose also sets the container `TZ`
value from `LOCAL_TIMEZONE`.
Set `LOG_LEVEL` to `debug`, `info`, or `warning`. Error log lines are always included.

## Gas Price Snapshot Scheduler

The app container runs the gas price snapshot scheduler when `GAS_SNAPSHOT_ENABLED=true`. It uses
the same command that remains available for manual or host-timer runs:

```bash
mileage-logger gas-snapshot
```

By default Docker runs one snapshot on app startup and then every 24 hours.

Relevant `.env` settings:

```env
GAS_PRICE_SOURCE=aaa_current
GAS_SNAPSHOT_ENABLED=true
GAS_SNAPSHOT_INTERVAL_SECONDS=86400
GAS_SNAPSHOT_RUN_ON_STARTUP=true
```

View gas snapshot logs with the normal app logs:

```bash
docker compose logs -f app
```

You can disable the in-app scheduler with `GAS_SNAPSHOT_ENABLED=false` and use a host systemd
timer instead of cron. For example, a timer can run
`docker compose exec -T app mileage-logger gas-snapshot` every 24 hours while the app container
keeps serving requests. Do not try to run systemd inside the app container; the Docker image runs a
single application process.

Optional host service:

```ini
# /etc/systemd/system/mileage-logger-gas-snapshot.service
[Unit]
Description=Mileage Logger gas price snapshot
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
WorkingDirectory=/opt/Mileage-Logger
ExecStart=/usr/bin/docker compose exec -T app mileage-logger gas-snapshot
```

Optional host timer:

```ini
# /etc/systemd/system/mileage-logger-gas-snapshot.timer
[Unit]
Description=Run Mileage Logger gas price snapshot every 24 hours

[Timer]
OnBootSec=15min
OnUnitActiveSec=24h
Persistent=true
Unit=mileage-logger-gas-snapshot.service

[Install]
WantedBy=timers.target
```

Use the actual repository path for `WorkingDirectory`, then enable the timer:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mileage-logger-gas-snapshot.timer
```

You can also view recent app logs and web-login audit records from the in-app `Diagnostics` page.

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

The app also creates one automatic startup full-data backup and then 6-hour full-data backups by
default. In Docker they are stored under `/data/logs/backups`, backed by `HOST_LOG_DIR` on the
host, unless `AUTOMATIC_BACKUP_DIR` is set to another private path. Diagnostics labels startup
backups, lists retained automatic backups, and can restore a selected file after you type
`RESTORE`. Retention keeps the newest 4 recent automatic backups plus one daily backup for each of
the prior 2 days.

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

Validate the web service proxy:

```bash
curl -i "http://127.0.0.1:${HTTP_PORT:-80}/"
curl -i "http://127.0.0.1:${HTTP_PORT:-80}/api/health" # Expected public result: 404
```

If OwnTracks returns unauthorized, confirm `.env` values and restart:

```bash
grep OWNTRACKS .env
grep WEB_API_KEY .env
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
intentionally blocked by the web service.
