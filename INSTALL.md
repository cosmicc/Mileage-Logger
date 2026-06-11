# Installation

This app is intended to run as a Docker Compose stack on an Ubuntu server. The stack includes:

- `postgres`: PostgreSQL database.
- `app`: FastAPI mileage logger.
- `nginx`: reverse proxy that serves the web app on HTTP port `80`.
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

Review the file before starting:

```bash
nano .env
```

Important values:

```env
HTTP_PORT=80
OWNTRACKS_USERNAME=owntracks
OWNTRACKS_PASSWORD=<generated-password>
REPORT_OUTPUT_DIR=/data/reports
LOG_DIR=/data/logs
GAS_PRICE_SOURCE=aaa_current
```

The generated `OWNTRACKS_USERNAME` and `OWNTRACKS_PASSWORD` are what you enter in OwnTracks HTTP
mode.

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
8. Deploy the stack.

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
8. Tap the manual publish button once to send a test location.

You can also use the Recorder-compatible endpoint:

```text
http://your-server/pub
```

The app supports both `/api/owntracks` and `/pub`.

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

## Configure Sites And Reports

1. Open `http://your-server/`.
2. Go to `Sites`.
3. Add each work site with latitude, longitude, and geofence radius.
4. Let OwnTracks collect location points.
5. Generate trips from the dashboard.
6. Review trips and uncheck personal drives.
7. Add or fetch the monthly gas price.
8. Generate the monthly PDF.

PDF reports are stored in the Docker volume `reports_data` at `/data/reports` inside the app
container.

Runtime logs are stored in the Docker volume `logs_data` at `/data/logs` inside the app and
gas-snapshot containers.

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
MQTT_TOPIC=owntracks/+/+
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

Run a command inside the app container:

```bash
docker compose exec app mileage-logger report 2026 6
```

## Backups

Back up PostgreSQL:

```bash
docker compose exec -T postgres pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" > mileage_logger.sql
```

Back up generated PDFs:

```bash
docker run --rm -v mileage-logger_reports_data:/reports -v "$PWD":/backup alpine \
  tar czf /backup/mileage-reports.tar.gz -C /reports .
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
