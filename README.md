# Mileage Logger

Mileage Logger receives OwnTracks location events from an Android phone over HTTP or MQTT,
stores them in PostgreSQL, lets you review and edit generated waypoint trips, and produces
monthly reimbursement PDF logs.

## Current Scope

- FastAPI web app with server-rendered review screens.
- PostgreSQL models and Alembic migration.
- OwnTracks HTTP endpoint at `/api/owntracks` and Recorder-compatible `/api/pub`.
- Optional MQTT subscriber for `owntracks/#` topics so location, waypoint, and transition events
  are available.
- OwnTracks waypoint geofence model used to turn location points into daily trips.
- Personal trip marking that automatically excludes future matching routes.
- Stop-based trip detection: a client stop must last at least 10 minutes before it creates a trip.
- Monthly gas price cache with a provider layer.
- Monthly PDF report generation.
- GitHub Actions CI for linting and tests.

## Fuel Price Policy

The reimbursement formula is:

```text
monthly included miles / vehicle MPG = reimbursement gallons
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
- Persistent Docker volumes for database data and generated PDF reports.
- In-app diagnostics page for app and gas price query logs.
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
docker compose exec app mileage-logger report 2026 6
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

Then run the web app normally. The MQTT worker starts with the app. Use `owntracks/#` if you want
MQTT ingestion to receive waypoint and transition events, not just location updates.

## Trip Detection

The app generates trips between qualifying stops:

- A known OwnTracks waypoint stop qualifies after `OWNTRACKS_STOP_MINUTES`, default `10`.
- Unknown stationary places are ignored; valid automatic trip endpoints must be saved OwnTracks
  waypoints.
- Brief boundary misses are tolerated. A waypoint visit is only treated as ended after the dwell
  threshold and after the phone is moving away from the waypoint or is at least 500 meters away.
- A trip starts when you leave the previous qualifying stop and ends when you arrive at the next
  qualifying stop.

Trip data is calculated automatically. Every incoming OwnTracks location or transition payload is
stored in `owntracks_locations` and immediately triggers trip recalculation for that payload's
`LOCAL_TIMEZONE` day. When the app sees a qualifying trip, it writes the generated row to `trips`.
The server can run on UTC; app day/month selection, dashboard time, trip time display, and gas
snapshot dates use `LOCAL_TIMEZONE`, default `America/Detroit` for EST/EDT.

A background processor also runs while the web app is up. It recalculates the current local day on a
short interval and finalizes completed local days. Once a day is complete, the processor calculates
that day's trips one last time and purges the processed `owntracks_locations` rows for that completed
day. Current-day rows are kept so live tracking data is not deleted before the day is finished.

If a stop was not a real destination, use the trip's `False Stop` action on the Trips page. The app
deletes that trip, moves the next trip's start back to the deleted trip's start, and adds the miles
to the next trip so the intermediate stop is removed.

If a trip is personal, use its `Personal` action. The app excludes that trip from reports and saves
the route so future matching trips are automatically marked personal too.

In Docker, change the stop wait threshold with `OWNTRACKS_STOP_MINUTES`. If unset, it defaults to
`10`.

Useful Docker environment options:

```env
OWNTRACKS_SYNC_WAYPOINTS=true
OWNTRACKS_DEFAULT_SITE_RADIUS_M=150
OWNTRACKS_STOP_MINUTES=10
LOCAL_TIMEZONE=America/Detroit
AUTOMATIC_TRIP_PROCESSING_ENABLED=true
AUTOMATIC_TRIP_PROCESSING_INTERVAL_SECONDS=60
```

## Workflow

1. Create work waypoints in OwnTracks and publish/export them to the server.
2. Review or export saved waypoints from the `Waypoints` page.
3. Let the app automatically create trips from incoming OwnTracks data.
4. Review `Trips`, switch to the needed month, edit start/end location names if needed, and mark
   personal routes.
5. Add or fetch a monthly gas price for that report month.
6. Download the monthly PDF report from the `Trips` page.

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
