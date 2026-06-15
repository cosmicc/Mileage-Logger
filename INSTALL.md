# Installation

This app is intended to run as a Docker Compose stack on an Ubuntu server. The stack includes:

- `postgres`: PostgreSQL database.
- `app`: FastAPI mileage logger.
- `nginx`: reverse proxy that serves the web app on HTTP port `80`.
- `cloudflared`: Cloudflare Tunnel connector for public HTTPS access.
- `gas-snapshot`: daily Michigan gas price snapshot worker.
- `logs_data`: shared Docker volume for diagnostics logs.

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
- `POSTGRES_PASSWORD`
- `OWNTRACKS_API_TOKEN`
- `OWNTRACKS_PASSWORD`
- `LOG_DIR`

Review the file before starting, and set `CLOUDFLARED_TUNNEL_TOKEN` to the token from the
Cloudflare dashboard:

```bash
nano .env
```

Important values:

```env
HTTP_PORT=80
WEB_ALLOWED_CIDRS=
OWNTRACKS_USERNAME=owntracks
OWNTRACKS_PASSWORD=<generated-password>
OWNTRACKS_SYNC_WAYPOINTS=true
AUTOMATIC_TRIP_PROCESSING_ENABLED=true
AUTOMATIC_TRIP_PROCESSING_INTERVAL_SECONDS=60
SMARTCAR_ENABLED=false
SMARTCAR_MANAGEMENT_TOKEN=
SMARTCAR_API_POLLING_ENABLED=false
SMARTCAR_WEBHOOK_MAX_BODY_BYTES=262144
SMARTCAR_ACCESS_TOKEN=
SMARTCAR_CLIENT_ID=
SMARTCAR_CLIENT_SECRET=
SMARTCAR_TOKEN_URL=https://iam.smartcar.com/oauth2/token
SMARTCAR_SCOPE=read_odometer
SMARTCAR_VEHICLE_ID=
SMARTCAR_API_BASE_URL=https://api.smartcar.com/v2.0
SMARTCAR_ODOMETER_UNIT=km
SMARTCAR_TIMEOUT_SECONDS=20
SMARTCAR_RETRY_ATTEMPTS=3
SMARTCAR_RETRY_DELAY_SECONDS=2
SMARTCAR_AUTH_FAILURE_COOLDOWN_SECONDS=3600
LOG_DIR=/data/logs
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

## Restrict Web UI By IP

The nginx container can leave OwnTracks/API endpoints open while restricting the browser UI to
specific IP blocks.

Set `WEB_ALLOWED_CIDRS` to comma-separated CIDR blocks:

```env
WEB_ALLOWED_CIDRS=192.168.1.0/24,10.8.0.0/24,203.0.113.44/32
```

With this set:

- `/api/` remains reachable from any IP so OwnTracks can keep sending data.
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
8. Optional: set `WEB_ALLOWED_CIDRS` to restrict web UI access while keeping `/api/` open.
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

It shows app status, recent database records, recent app logs, and recent gas price query logs.

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

For work waypoints, add OwnTracks regions/waypoints on the phone. If you use MQTT, publish waypoints
and keep `MQTT_TOPIC=owntracks/#` so the app receives waypoint and transition events. If you use
HTTP, OwnTracks sends its payloads to the configured endpoint and the app will process supported
waypoint and transition payloads.

You can also use the Recorder-compatible endpoint:

```text
http://your-server/api/pub
```

The app supports both `/api/owntracks` and `/api/pub`.

## Waypoint Trip Detection

Trips are generated from OwnTracks waypoint transition events.

Default behavior:

- A trip is created from a waypoint `leave` event followed by another waypoint `enter` event.
- `Home` is the exact waypoint name for home.
- `Home` to `Home` is never a trip.
- Trips between the same non-home waypoint are kept.
- If an `enter` event arrives without a matching `leave`, the app infers the origin from the
  previous waypoint. If there is no previous waypoint and the destination is not `Home`, the app
  assumes the missed origin was `Home`.

Trip generation is automatic. Every incoming OwnTracks transition payload is stored in
`owntracks_locations` and immediately triggers trip recalculation for that payload's
`LOCAL_TIMEZONE` day. Generated trip rows are stored in `trips`.
The server can run on UTC; app day/month selection, dashboard time, and gas
snapshot dates use `LOCAL_TIMEZONE`, default `America/Detroit` for EST/EDT.

Generated mileage uses this order:

1. Verified Smartcar webhook odometer delta when `SMARTCAR_ENABLED=true` and both endpoint
   readings are available.
2. Estimated start/end odometer values using this trip's waypoint distance and any available
   odometer anchor.
3. Waypoint-to-waypoint distance when no odometer anchor is available.

Because OwnTracks is only sending waypoint events, there is no full GPS path to measure. Edit a
trip's miles on the `Trips` page when the generated mileage needs correction. Manual corrections
apply only to that trip because the same waypoint pair can have different real-world mileage.
Deleting a trip from the `Trips` page also saves a suppression record so the same OwnTracks
transition pair is not generated again.

Smartcar setup uses a webhook callback for vehicle state data. In the Smartcar Dashboard, set the
callback URI to:

```text
https://your-host.example.com/api/smartcar/webhook
```

The `/api/webhooks/smartcar` alias is also available. Smartcar sends a `VERIFY` event when the
callback URI is created or changed. The app answers that challenge with an HMAC-SHA256 hash
generated from `SMARTCAR_MANAGEMENT_TOKEN`, then requires the `SC-Signature` header on normal
vehicle events before storing any data.

Set these in Docker or `.env` when you want webhook-based mileage:

```env
SMARTCAR_ENABLED=true
SMARTCAR_MANAGEMENT_TOKEN=your-smartcar-application-management-token
SMARTCAR_API_POLLING_ENABLED=false
SMARTCAR_WEBHOOK_MAX_BODY_BYTES=262144
SMARTCAR_ODOMETER_UNIT=km
```

Webhook deliveries are stored in `smartcar_webhook_events`, and every included signal is stored in
`smartcar_webhook_signals`. The app also summarizes common vehicle state fields on the event row:
odometer, fuel level, lock state, online state, nickname, VIN, firmware version, vehicle metadata,
webhook metadata, and the full raw payload.

Direct Smartcar API odometer polling is now only an optional automatic fallback. Leave it disabled
for webhook-only operation. The Diagnostics page test button can still force-test configured API
credentials. If you explicitly want automatic fallback reads, set:

```env
SMARTCAR_API_POLLING_ENABLED=true
SMARTCAR_ACCESS_TOKEN=your-smartcar-api-access-token
SMARTCAR_CLIENT_ID=optional-smartcar-client-id
SMARTCAR_CLIENT_SECRET=optional-smartcar-client-secret
SMARTCAR_TOKEN_URL=https://iam.smartcar.com/oauth2/token
SMARTCAR_SCOPE=read_odometer
SMARTCAR_VEHICLE_ID=optional-smartcar-vehicle-id
SMARTCAR_API_BASE_URL=https://api.smartcar.com/v2.0
SMARTCAR_TIMEOUT_SECONDS=20
SMARTCAR_RETRY_ATTEMPTS=3
SMARTCAR_RETRY_DELAY_SECONDS=2
SMARTCAR_AUTH_FAILURE_COOLDOWN_SECONDS=3600
```

`SMARTCAR_ODOMETER_UNIT` is the raw unit returned by Smartcar before conversion to report miles;
Smartcar odometer values commonly use kilometers. If direct API polling is enabled and
`SMARTCAR_VEHICLE_ID` is blank, the app calls Smartcar's vehicles endpoint and uses the first
connected vehicle. The connected vehicle must have the `read_odometer` permission.

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

For Smartcar, use the public Cloudflare hostname as the callback base, for example:

```text
https://mileage.example.com/api/smartcar/webhook
```

The web app also starts a background processor. It recalculates the current local day on a short
interval and finalizes completed local days. At the start of each new month, old location points
and raw gas snapshots are removed so only the current month remains. Trips and waypoints are not
reset.

Configuration:

```env
OWNTRACKS_SYNC_WAYPOINTS=true
OWNTRACKS_DEFAULT_SITE_RADIUS_M=150
LOCAL_TIMEZONE=America/Detroit
AUTOMATIC_TRIP_PROCESSING_ENABLED=true
AUTOMATIC_TRIP_PROCESSING_INTERVAL_SECONDS=60
SMARTCAR_ENABLED=false
SMARTCAR_MANAGEMENT_TOKEN=
SMARTCAR_API_POLLING_ENABLED=false
```

When `OWNTRACKS_SYNC_WAYPOINTS=true`, published OwnTracks waypoint payloads create or update app
waypoints. Location `inregions` values are only used to match already-saved waypoints; they do not
create new waypoints.

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

View the newest stored point:

```bash
curl "http://127.0.0.1:${HTTP_PORT:-80}/api/locations?limit=1"
```

## Configure Waypoints And Reports

1. Open `http://your-server/`.
2. Add work waypoints in OwnTracks and publish them to the server.
3. Go to `Waypoints` to review saved waypoints or export an OwnTracks waypoint backup.
4. Configure OwnTracks to send waypoint transition events.
5. Review automatically generated trips from the `Trips` page.
6. Open `Trips`, choose the report month, and correct waypoint names or miles if needed.
7. Confirm `VEHICLE_MPG` is set correctly and add or fetch the monthly gas price for that month.
8. Click `Download PDF Report` to generate and download the PDF.

The PDF can be generated for any retained month that has trips and a saved monthly gas price or
daily gas snapshots for that month. The app resets location points, trips, and raw gas snapshots at
the start of each new month.

Reimbursement is calculated as:

```text
total trip miles / VEHICLE_MPG = reimbursement gallons
reimbursement gallons * Michigan monthly average gas price = total reimbursement
```

PDF reports are generated only when you click `Download PDF Report`; they are streamed to the
browser and are not saved on the server.

Runtime logs are stored in the Docker volume `logs_data` at `/data/logs` inside the app and
gas-snapshot containers. Log timestamps are formatted in `LOCAL_TIMEZONE`, and Docker Compose also
sets the container `TZ` value from `LOCAL_TIMEZONE`.
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

You can also view recent gas price query logs from the in-app `Diagnostics` page.

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

Update from GitHub:

```bash
git pull
docker compose up -d --build
```

## Backups

Back up PostgreSQL:

```bash
docker compose exec -T postgres pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" > mileage_logger.sql
```

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
curl -i "http://127.0.0.1:${HTTP_PORT:-80}/api/health"
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
The `/api/health` endpoint should still be reachable because API paths are intentionally open.
