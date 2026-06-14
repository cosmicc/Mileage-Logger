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
- OwnTracks waypoint transition model used to turn leave/enter events into trips.
- Optional Smartcar webhook ingestion for real-time vehicle state and odometer mileage.
- Manual review for generated waypoint names and trip mileage.
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
- Persistent Docker volumes for database data and runtime logs.
- In-app diagnostics page for app, trip calculation, and gas price query logs in the configured
  local timezone.
- Optional web UI IP allowlist while keeping `/api/` reachable for OwnTracks.

Create a production `.env` with generated passwords:

```bash
./scripts/init_docker_env.sh
```

Review `.env`, then start the stack:

```bash
docker compose up -d --build
```

Useful commands:

```bash
docker compose ps
docker compose logs -f app
docker compose logs -f nginx
docker compose down
```

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

To restrict the browser UI while leaving OwnTracks/API access open, set `WEB_ALLOWED_CIDRS`
to comma-separated IP blocks:

```env
WEB_ALLOWED_CIDRS=192.168.1.0/24,10.8.0.0/24,203.0.113.44/32
```

When this is blank, the web UI is open to all clients. When set, `/api/` stays reachable from any
IP, but pages such as `/`, `/trips`, `/waypoints`, `/diagnostics`, and `/static/` require a matching
client IP.

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
ingestion can receive waypoint definitions and transition events.

## Trip Detection

The app generates trips directly from OwnTracks waypoint transitions:

- A trip is created from a waypoint `leave` event followed by another waypoint `enter` event.
- `Home` is the exact waypoint name for home.
- `Home` to `Home` is never a trip.
- Trips between the same non-home waypoint are kept, because they can represent work travel that
  returns to the same waypoint.
- If an `enter` event arrives without a matching `leave`, the app infers the most likely origin
  from the previous waypoint. If there is no previous waypoint and the destination is not `Home`,
  the app assumes the missed origin was `Home`.

Trip data is calculated automatically. Every incoming OwnTracks transition payload is stored in
`owntracks_locations` and immediately triggers trip recalculation for that payload's
`LOCAL_TIMEZONE` day. When the app sees a qualifying trip, it writes the generated row to `trips`.
The server can run on UTC; app day/month selection, dashboard time, and gas snapshot dates use
`LOCAL_TIMEZONE`, default `America/Detroit` for EST/EDT.

Generated mileage uses this order:

1. Verified Smartcar webhook odometer delta when `SMARTCAR_ENABLED=true` and both endpoint
   readings are available.
2. Estimated start/end odometer values using this trip's waypoint distance and any available
   odometer anchor.
3. Waypoint-to-waypoint distance when no odometer anchor is available.

Because OwnTracks is only sending waypoint events, there is no full GPS path to measure. Edit a
trip's miles on the `Trips` page when the generated mileage needs correction. Manual corrections
apply only to that trip because the same waypoint pair can have different real-world mileage.

Smartcar setup uses a webhook callback for vehicle state data. Set the Smartcar callback URI to:

```text
https://your-host.example.com/api/smartcar/webhook
```

The `/api/webhooks/smartcar` alias is also available if you prefer that path. Smartcar sends a
`VERIFY` event when the callback URI is created or changed. The app answers that challenge with an
HMAC-SHA256 hash generated from `SMARTCAR_MANAGEMENT_TOKEN`, then requires the `SC-Signature`
header on normal vehicle events before storing any data.

Set these in `.env` or Docker when you want webhook-based odometer mileage:

```env
SMARTCAR_ENABLED=true
SMARTCAR_MANAGEMENT_TOKEN=your-smartcar-application-management-token
SMARTCAR_API_POLLING_ENABLED=false
SMARTCAR_WEBHOOK_MAX_BODY_BYTES=262144
SMARTCAR_ODOMETER_UNIT=km
```

Webhook deliveries are stored in `smartcar_webhook_events`, and each included signal is stored in
`smartcar_webhook_signals`. The event row also summarizes common values such as odometer, fuel
level, lock state, online state, nickname, VIN, firmware version, and vehicle metadata.

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
connected vehicle. The connected vehicle must have the `read_odometer` permission. If Smartcar
rejects authentication or permissions, automatic API reads pause for
`SMARTCAR_AUTH_FAILURE_COOLDOWN_SECONDS` so the background processor does not keep retrying a bad or
expired token every cycle.

## Cloudflare Tunnel

The Compose file includes an optional `cloudflared` service for a remotely managed Cloudflare
Tunnel. In the Cloudflare dashboard, publish the application route to the internal service:

```text
http://nginx:80
```

Then set the tunnel token and start the profile:

```env
CLOUDFLARED_TUNNEL_TOKEN=your-cloudflare-tunnel-token
CLOUDFLARED_LOG_LEVEL=info
CLOUDFLARED_METRICS=
CLOUDFLARED_TRANSPORT_PROTOCOL=auto
```

```bash
docker compose --profile cloudflare up -d --build
```

A background processor also runs while the web app is up. It recalculates the current local day on a
short interval and finalizes completed local days. At the start of each new month, old location
points and raw gas snapshots are removed so only the current month remains. Trips and waypoints are
not reset.

Useful Docker environment options:

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

Docker Compose passes `LOCAL_TIMEZONE` through as the container `TZ` value for the app stack.
Set `LOG_LEVEL` to `debug`, `info`, or `warning`; error lines are always included at every level.

## Workflow

1. Create work waypoints in OwnTracks and publish/export them to the server.
2. Review or export saved waypoints from the `Waypoints` page.
3. Configure OwnTracks to send waypoint transition events.
4. Let the app automatically create trips from incoming OwnTracks transitions.
5. Review `Trips`, switch to the needed month, and edit waypoint names or miles if needed.
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
