# Changelog

## Unreleased

- Changed automatic trip generation so waypoint arrivals require a five-minute OwnTracks dwell
  confirmation before a trip is created, preventing drive-through waypoint trips.
- Added rolling checkpoint odometer updates from OwnTracks path distance outside generated trips,
  while Diagnostics manual odometer readings reset the checkpoint to a new rolling value.
- Removed external vehicle odometer integration and now derives odometer movement from OwnTracks
  path distance plus optional manual checkpoint corrections.
- Removed speed-based Diagnostics movement handling in favor of distance-based travel detection.
- Added Waypoints page delete buttons that remove stale app waypoints while preserving historical
  trip details.
- Changed Trips page row editing so trip names and odometers are read-only, while distance edits
  automatically resequence that month's trip odometers.
- Added a simple session-based web login for rendered pages, configured by Docker environment
  variables while leaving `/api/` routes outside the web login.
- Removed visible app branding from the login page and added temporary failed-login lockouts.
- Added an editable trip suppression rules list on the Trips page so mistaken automatic-trip
  suppression records can be removed.
- Added Diagnostics current OwnTracks state detection for inside-waypoint and travel statuses,
  plus a state-change log limited to waypoint arrivals, waypoint departures, and travel detected.
- Added a Trips page manual-entry form and trip-date editing so date, origin, destination, and
  distance can be entered or corrected manually.
- Added a Diagnostics page manual odometer form that updates the rolling checkpoint used for
  future OwnTracks-derived odometer estimates.
- Added automatic checkpoint-aware OwnTracks location retention so old processed raw location data
  is purged after the configured retention window without deleting trips or other app data.
- Changed generated trip mileage to prefer summed OwnTracks location path distance between
  waypoint leave/enter events before falling back to estimated odometer or waypoint distance.
- Added a Trips page delete button and deleted-trip suppression records so user-deleted automatic
  trips are not recreated from the same OwnTracks transition events.
- Fixed automatic trip generation so unchanged existing trips are not rewritten and counted as
  generated on every processing pass.
- Added `cloudflared` to the normal Docker Compose stack as a required Cloudflare Tunnel service
  configured by environment variables.
- Changed automatic trip processing to use a persistent OwnTracks checkpoint and append/update trips in place without deleting existing trip rows.
- Fixed checkpoint table startup recovery so automatic trip processing creates the missing table safely before querying it.
- Stopped ignoring Alembic migrations so database schema updates are included in normal commits and Docker builds.
- Added diagnostics page API test cards for EIA, plus last OwnTracks received age.
- Fixed app log download handling and added an app log refresh action.
- Redacted sensitive query values such as API keys from app log formatting, display, and download.
- Updated diagnostics log colors so DEBUG is green and INFO is white.
- Removed the OwnTracks Region ID column from the Waypoints page.
