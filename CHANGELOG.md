# Changelog

## 1.2.2 - Unreleased

- Changed the Dashboard home card order so Location State is the first card shown.
- Removed the Distance column from the Diagnostics OwnTracks State Changes table.
- Added Duration, Source, Received Delay, and Rolling Odometer columns to the Diagnostics
  OwnTracks State Changes table.
- Changed the top Diagnostics cards to render as one grouped three-column desktop grid ordered by
  overview, current state, actions, and storage: Application, Data, Latest Records, OwnTracks State,
  Manual Odometer, EIA API, Configure Passkey, and Hard Drive Space.

## 1.2.1 - 2026-06-27

- Bumped the Mileage Logger package version to 1.2.1.
- Added a lightweight Dashboard loading shell so direct homepage loads show a loading message while
  the calculated Dashboard content is fetched from an authenticated content route.
- Added a lightweight Trips loading shell so selected-month cards and trip rows are fetched from
  `/trips/content` after the initial Trips page opens.
- Changed Trips month navigation to a single month/year picker that defaults to the current local
  month, auto-loads selected months, and displays the selected month as `Showing June 2026
  (06/2026)` style text.
- Added compact selected-month summary cards to Trips above Add Trip for trip plus non-trip miles,
  trip-only miles, OwnTracks events, trip count, reimbursement, and monthly average gas.
- Added WebAuthn passkey login with a Device Sign-In button on the login page and a Configure
  Passkey card on Diagnostics for creating, listing, and removing the single configured user's
  passkeys.
- Changed the login page to place Device Sign-In below the normal password Continue button.
- Added the `passkey_credentials` database table plus optional `PASSKEY_RP_NAME`,
  `PASSKEY_RP_ID`, and `PASSKEY_ORIGIN` settings for public-origin WebAuthn validation.
- Added successful web-login audit records and a paginated Successful Login Attempts table above
  the Failed Login Attempts table on Diagnostics.
- Changed automatic backups created by the app startup pass to use a startup-marked filename and
  show a Startup label in the retained automatic-backup table.
- Changed Waypoints and Diagnostics mobile pagination so First, Previous, Next, and Last stay in
  one full-width row with the page count shown as plain text below.
- Changed the shared top-bar brand icon and Mileage Logger text to display-only content instead of
  a clickable home link.
- Added a manual valid-IP Cloudflare block form to Diagnostics, requiring a reason and showing
  Auto/Manual source pills with each reason in the app-managed blocked-IP list with per-row removal
  from Cloudflare and the local list.
- Changed Cloudflare authentication failures to explain that `CLOUDFLARE_API_TOKEN` must be a
  Cloudflare API token with `Account Firewall Access Rules Write` access, not the tunnel token or a
  Global API Key.
- Fixed web-login security startup checks so production fails closed without configured login
  credentials and a changed `SECRET_KEY`, and enabling web login in any environment rejects the
  default session secret.
- Fixed login lockout and Cloudflare auto-block identity handling so forwarded client IP headers
  are trusted only from configured `TRUSTED_PROXY_CIDRS`, with bundled nginx selecting one
  Cloudflare-derived client IP and overwriting spoofable forwarded client IP headers before
  proxying.
- Fixed Diagnostics login rows and Cloudflare block buttons to correct stale proxy/container
  `client_ip` values from trusted forwarded headers for both successful and failed web-login
  attempts.
- Fixed bundled nginx and Diagnostics login audit handling so successful and failed web-login rows
  use the Cloudflare-derived client IP when Cloudflare Tunnel supplies `CF-Connecting-IP`, instead
  of losing that value when the tunnel origin is not loopback.
- Changed bundled nginx to forward the public HTTPS scheme from loopback `cloudflared` traffic so
  passkey origin checks can match the browser's Cloudflare Tunnel origin.
- Fixed monthly PDF generation so trip and waypoint names are escaped before ReportLab parses
  table cell text.
- Changed CI Docker Compose validation to use `.env.docker.example` through `--env-file` with a
  dummy tunnel token instead of leaving a production `.env` file behind before tests.

## 1.2.0 - 2026-06-24

- Changed desktop navigation links to use the same boxed button treatment as Logout.
- Changed the mobile web-app shell so the top navigation buttons span the full width, the mobile
  close/title controls stay removed, fixed bottom navigation stays removed, the viewport no longer
  opts into phone edge-to-edge drawing, and the manifest includes a browser fallback for phone
  system navigation.
- Changed install metadata responses to use no-store caching so phones pick up updated manifest
  and service-worker shell settings promptly.
- Added the app version to the Diagnostics Application card.
- Bumped the Mileage Logger package version to 1.2.0.
- Fixed generated trip odometer starts to prefer the newest stored rolling checkpoint before the
  trip over older prior-trip odometers, then calculate the end odometer from that start plus the
  generated trip distance.

## 1.1.4 - 2026-06-23

- Changed Diagnostics hard drive space grouping to combine configured runtime paths only when exact
  used bytes and total bytes match.
- Bumped the Mileage Logger package version to 1.1.4 for dev-branch testing before release.
- Changed the Dashboard summary cards to remove the Waypoints card, move Trips into that slot, and
  show the current-month reimbursement total using the same mileage, gas price, and MPG formula as
  the downloadable PDF report summary, with one-decimal reimbursement gallons shown under the
  price.
- Tightened Diagnostics list cards so recent OwnTracks entries, OwnTracks state changes, failed
  login attempts, and app-managed Cloudflare blocked IPs show 10 rows per page, shortened the app
  log window, and made automatic-backup rows slimmer with truncated filenames and accessible
  restore confirmation inputs.
- Removed the separate Docker `gas-snapshot` service and moved recurring gas price snapshots into
  the app container background scheduler while keeping the manual `mileage-logger gas-snapshot`
  command available.
- Moved the Diagnostics manual full-backup description and `Download Full Backup` button down to
  the lower upload-restore area of the Full Data Backup card.
- Removed the Failed Login Attempts card refresh and download action row from Diagnostics so the
  card fits its table content more tightly.
- Added a Docker `BIND_ADDRESS` setting for the nginx host-port binding and changed bundled
  `cloudflared` to host networking so Cloudflare Tunnel can route to that bound host listener.
- Added Diagnostics controls to hide failed-login rows, manually block/unblock failed-login IPs at
  Cloudflare, list app-managed Cloudflare IP blocks, and automatically block an IP after the
  configured consecutive failed-login threshold.
- Added database size and total app-record count totals to the bottom of the Diagnostics hard drive
  space card.
- Changed the Trips page to show newest trip dates first while leaving the Dashboard recent trips
  unchanged.
- Added used-space display bars to the Diagnostics hard drive space card.
- Added a Diagnostics hard drive space card that combines configured runtime paths when exact used
  bytes and total bytes match, reducing duplicate same-drive rows.
- Added per-file download buttons for retained automatic backups on Diagnostics, using the same web
  login guard, filename validation, restore size limit, and no-store caching as other backup
  downloads.
- Changed manual trip creation to start from the current rolling OwnTracks odometer checkpoint when
  available, place new manual trips after existing trips on the selected local date, and preserve
  existing positive non-trip odometer gaps when resequencing later trips.
- Changed Trips page manual-entry and row-edit forms to use saved waypoint dropdowns for From/To,
  with manual trip dates defaulting to today's `LOCAL_TIMEZONE` date.
- Fixed Dashboard trip plus non-trip distance totals so the combined total is never lower than the
  trips-only total and the implied non-trip remainder is never negative after one-decimal rounding.
- Added hourly automatic full-data backups under `AUTOMATIC_BACKUP_DIR`, with Diagnostics listing
  retained files and supporting typed-confirmation restore from a selected automatic backup.
- Fixed Dashboard today and month trip plus non-trip totals so they are summed from OwnTracks
  coordinate path data instead of rolling odometer differences, preventing manual odometer resets
  from inflating driven-mile totals.
- Changed automatic trip generation so odometer deltas are never used as the trip distance source;
  transition-only trips fall back to waypoint distance while odometer fields remain display values.
- Moved the Diagnostics Full Data Backup card to the bottom of the page under the App Log.
- Changed the Diagnostics layout so Manual Odometer, EIA API, and OwnTracks State share one
  equal-width card row.
- Changed the Diagnostics manual odometer card to show the current odometer reading before saving
  a new manual checkpoint.
- Fixed manual trip creation so new manual trips save start/end odometers from the latest known
  odometer reading, and prior-date manual inserts resequence every later trip odometer
  cumulatively across month boundaries.
- Added a restore regression check proving full backup restore replaces changed same-row data
  instead of creating duplicate rows.
- Changed public nginx routing so only rendered web pages and OwnTracks ingestion endpoints are
  internet-facing; all other `/api/` routes and generated FastAPI docs are blocked at nginx while
  internal container health checks still use `/api/health`.
- Documented that PostgreSQL data persists in the named Docker `postgres_data` volume across normal
  rebuilds, and warned against `down -v`, volume pruning, or stack-name changes without backup.
- Added Diagnostics full app data backup and restore controls. Backups download as sensitive
  `.json.gz` files containing all app database tables plus OwnTracks waypoint export, while restore
  requires web login, a validated file, and typed confirmation before replacing current app rows.
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
  trips, allowing generated trip start/end odometer displays to follow OwnTracks-derived movement
  without making odometer deltas a distance source.
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
- Changed Trips page row editing so odometers are read-only, while waypoint and distance edits
  automatically preserve the trip route metadata and resequence that month's trip odometers when
  mileage changes.
- Added a simple session-based web login for rendered pages, configured by Docker environment
  variables while leaving `/api/` routes outside the app-level web login.
- Removed visible app branding from the login page and added temporary failed-login lockouts.
- Added an editable deleted-trip records list on the Trips page so mistaken automatic-trip
  deletion records can be removed.
- Added Diagnostics current OwnTracks state detection for inside-waypoint and travel statuses,
  plus a state-change log limited to waypoint arrivals, waypoint departures, and travel detected.
- Added a Trips page manual-entry form so date, origin, destination, and distance can be entered
  manually.
- Added a Diagnostics page manual odometer form that updates the rolling checkpoint used for
  future OwnTracks-derived odometer estimates.
- Added automatic checkpoint-aware OwnTracks location retention so old processed raw location data
  is purged after the configured retention window without deleting trips or other app data.
- Changed generated trip mileage to prefer summed OwnTracks location path distance between
  waypoint leave/enter events before falling back to waypoint distance.
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
