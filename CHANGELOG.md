# Changelog

## Unreleased

- Added a Trips page delete button and deleted-trip suppression records so user-deleted automatic
  trips are not recreated from the same OwnTracks transition events.
- Fixed automatic trip generation so unchanged existing trips are not rewritten and counted as
  generated on every processing pass.
- Added signed Smartcar webhook ingestion with VERIFY challenge handling, raw-body
  `SC-Signature` validation, event deduplication, vehicle state storage, signal row storage, and
  webhook-first odometer readings for trip mileage.
- Added `cloudflared` to the normal Docker Compose stack as a required Cloudflare Tunnel service
  configured by environment variables.
- Changed automatic trip processing to use a persistent OwnTracks checkpoint and append/update trips in place without deleting existing trip rows.
- Replaced the FordPass odometer package with direct Smartcar API odometer reads.
- Added an initial Smartcar odometer anchor when no odometer data exists yet.
- Added Smartcar authentication backoff so 401/403 failures stop retrying every background cycle.
- Fixed checkpoint table startup recovery so automatic trip processing creates the missing table safely before querying it.
- Stopped ignoring Alembic migrations so database schema updates are included in normal commits and Docker builds.
- Added diagnostics page API test cards for Smartcar and EIA, plus last OwnTracks received age.
- Fixed app log download handling and added an app log refresh action.
- Redacted sensitive query values such as API keys from app log formatting, display, and download.
- Updated diagnostics log colors so DEBUG is green and INFO is white.
- Removed the OwnTracks Region ID column from the Waypoints page.
