# Mileage Logger

Mileage Logger receives OwnTracks location events from an Android phone over HTTP or MQTT,
stores them in PostgreSQL, lets you review and edit generated work-site trips, and produces
monthly reimbursement PDF logs.

## Current Scope

- FastAPI web app with server-rendered review screens.
- PostgreSQL models and Alembic migration.
- OwnTracks HTTP endpoint at `/api/owntracks` and Recorder-compatible `/api/pub`.
- Optional MQTT subscriber for `owntracks/#` topics so location, waypoint, and transition events
  are available.
- Work-site geofence model used to turn location points into daily trips.
- Manual include/exclude controls for personal drives.
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
IP, but pages such as `/`, `/trips`, `/sites`, `/diagnostics`, and `/static/` require a matching
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

OwnTracks waypoints can be used as client sites. When `OWNTRACKS_AUTO_CREATE_SITES=true`, published
OwnTracks waypoint payloads create or update matching app sites. Location payloads with `inregions`
can also create an approximate site if the waypoint was not published first.

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

- A known site stop qualifies after `OWNTRACKS_STOP_MINUTES`, default `10`.
- An unknown stationary stop qualifies after the same duration when points stay within
  `OWNTRACKS_UNKNOWN_STOP_RADIUS_M`, default `150` meters.
- A trip starts when you leave the previous qualifying stop and ends when you arrive at the next
  qualifying stop.
- Unknown stops generate trips with a blank origin or destination site, so you can review them and
  either exclude them or add a site later.
- If `GOOGLE_PLACES_API_KEY` is set, unknown qualifying stops are checked against Google Places and
  a matching business can be created as an app site automatically.

Useful Docker environment options:

```env
OWNTRACKS_AUTO_CREATE_SITES=true
OWNTRACKS_DEFAULT_SITE_RADIUS_M=150
OWNTRACKS_STOP_MINUTES=10
OWNTRACKS_UNKNOWN_STOP_RADIUS_M=150
GOOGLE_PLACES_API_KEY=
GOOGLE_PLACES_RADIUS_M=100
GOOGLE_PLACES_AUTO_CREATE_SITES=true
```

Google Places enrichment is optional. Create a Google Maps Platform API key with Places API access
and set `GOOGLE_PLACES_API_KEY` if you want unknown client stops named automatically.

## Workflow

1. Create work sites in the `Sites` page with latitude, longitude, and geofence radius.
2. Send OwnTracks data through HTTP or MQTT.
3. Generate trips for a date range from the dashboard.
4. Review `Trips`, switch to the needed month, edit miles/notes, and exclude personal drives.
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
