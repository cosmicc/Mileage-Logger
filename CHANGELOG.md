# Changelog

## Unreleased

- Fixed Docker startup by removing the individual failed-login log file bind mount; failed-login
  audit records now use the shared host log directory with an optional `/var/log/...` symlink.
- Added structured failed-login audit logging, including client IP details, submitted username,
  password length, user agent, lockout state, and timestamps without storing raw passwords;
  Diagnostics now shows and downloads those entries.
- Changed Docker logging so app and worker logs bind to a host log directory and the container
  prepares mounted log paths before dropping to the non-root app user.
- Changed Docker environment generation so `WEB_LOGIN_PASSWORD` is generated instead of leaving
  the template placeholder in new `.env` files.
- Changed Trips page row editing so existing trip dates render as read-only text and cannot be
  changed by the row update form.
- Added a mobile-only top-bar close button that calls the browser close action for installed full-screen
  mobile web-app sessions.
- Added installable mobile web-app metadata, home-screen icons, and full-screen mobile shell styling
  so Mileage Logger opens like a phone app when saved to the home screen.
- Changed automatic trip generation so same-waypoint trips under 1.0 mile are
  suppressed as invalid non-trips. Existing automatic rows matching that rule
  are removed with an exact deleted-trip suppression record so the same
  OwnTracks transition pair does not recreate them.
- Fixed Dashboard Today distance cards so the trip and non-trip totals stay on the
  `LOCAL_TIMEZONE` day until local midnight instead of rolling over with UTC.
- Fixed automatic trip processing so a waypoint arrival can create the trip after the dwell timer
  expires even when OwnTracks sends no follow-up location rows while the phone remains there.
- Replaced the Dashboard Vehicle MPG card with a current OwnTracks location state card showing
  inside-waypoint, driving, stationary, or no-data status.
- Changed PDF trip table headers to spell out Start Odometer and End Odometer instead of
  abbreviating odometer.
- Added Dashboard distance cards for today's total driven miles, today's trip miles, this month's
  total driven miles, and this month's trip miles.
- Changed trip deletion records to be documented and displayed as exact deleted-trip records, not
  route-pattern rules, so future trips with the same route are still generated normally.
- Changed trip deletion to preserve the rolling odometer checkpoint from the deleted trip when that
  trip has the most recent odometer reading.
- Added a stored OwnTracks odometer timeline so every processed location row records the rolling
  odometer value used by later trip generation.
- Changed automatic trip processing to advance the rolling OwnTracks odometer before generating
  trips, ensuring generated trip start/end odometers come from the rolling odometer and the
  odometer difference always matches trip distance.
- Changed automatic trip generation so waypoint arrivals require a five-minute OwnTracks dwell
  confirmation before a trip is created, preventing drive-through waypoint trips.
- Added rolling checkpoint odometer updates from OwnTracks path distance outside generated trips,
  while Diagnostics manual odometer readings reset the checkpoint to a new rolling value.
- Removed external vehicle odometer integration and now derives odometer movement from OwnTracks
  path distance plus optional manual checkpoint corrections.
- Changed trip distances and odometer values to store and display one decimal place.
- Removed speed-based Diagnostics movement handling in favor of distance-based travel detection.
- Added Waypoints page delete buttons that remove stale app waypoints while preserving historical
  trip details.
- Changed Trips page row editing so trip names and odometers are read-only, while distance edits
  automatically resequence that month's trip odometers.
- Added a simple session-based web login for rendered pages, configured by Docker environment
  variables while leaving `/api/` routes outside the web login.
- Removed visible app branding from the login page and added temporary failed-login lockouts.
- Added an editable deleted-trip records list on the Trips page so mistaken automatic-trip
  deletion records can be removed.
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
- Added a Trips page delete button and exact deleted-trip records so user-deleted automatic trips
  are not recreated from the same OwnTracks transition events.
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
