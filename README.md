# Mileage Logger

Mileage Logger receives OwnTracks location events from an Android phone over HTTP or MQTT,
stores them in PostgreSQL, lets you review and edit generated work-site trips, and produces
monthly reimbursement PDF logs.

## Current Scope

- FastAPI web app with server-rendered review screens.
- PostgreSQL models and Alembic migration.
- OwnTracks HTTP endpoint at `/api/owntracks` and Recorder-compatible `/pub`.
- Optional MQTT subscriber for `owntracks/+/+` topics.
- Work-site geofence model used to turn location points into daily trips.
- Manual include/exclude controls for personal drives.
- Monthly gas price cache with a provider layer.
- Monthly PDF report generation.
- GitHub Actions CI for linting and tests.

## Fuel Price Policy

The reimbursement formula is implemented exactly as requested:

```text
monthly included miles * (Michigan monthly average gas price + $0.50 buffer)
```

The first provider can fetch the current AAA Michigan regular gasoline average and store daily
snapshots. Monthly reports use a saved manual monthly average when present, or the average of
stored daily snapshots for that month. Historical Michigan monthly pricing sources can be added
behind `mileage_logger.services.gas_prices.GasPriceProvider` without changing report generation.

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

## OwnTracks HTTP Setup

Set OwnTracks HTTP mode to:

```text
https://your-host.example.com/api/owntracks
```

If `OWNTRACKS_API_TOKEN` is set, send it as `X-Api-Key` or `Authorization: Bearer ...`.
If `OWNTRACKS_USERNAME` and `OWNTRACKS_PASSWORD` are set, use OwnTracks HTTP Basic Auth.

The `/pub` alias is also available for Recorder-style setups.

## MQTT Setup

Set these in `.env`:

```text
MQTT_ENABLED=true
MQTT_HOST=your-broker
MQTT_PORT=1883
MQTT_USERNAME=optional
MQTT_PASSWORD=optional
MQTT_TOPIC=owntracks/+/+
```

Then run the web app normally. The MQTT worker starts with the app.

## Workflow

1. Create work sites in the `Sites` page with latitude, longitude, and geofence radius.
2. Send OwnTracks data through HTTP or MQTT.
3. Generate trips for a date range from the dashboard.
4. Review `Trips`, edit miles/notes, and exclude personal drives.
5. Add or fetch a monthly gas price.
6. Generate the monthly PDF.

## Project Commands

```bash
ruff check .
pytest
alembic revision --autogenerate -m "message"
alembic upgrade head
```
