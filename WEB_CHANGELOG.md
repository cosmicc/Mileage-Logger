# Web Changelog

## 1.2.2 - Unreleased

- The home page now shows Location State as the first card.
- Diagnostics no longer shows a Distance column in the OwnTracks State Changes table.
- Diagnostics now shows Duration, Source, Received Delay, and Rolling Odometer in the OwnTracks
  State Changes table.
- Diagnostics now groups the top eight cards into one three-column desktop grid in a clearer order.

## 1.2.1 - 2026-06-27

- Dashboard now shows a loading message while the calculated home-page cards and recent trips load.
- Trips now shows a loading message while the selected-month cards and trip records load.
- Trips now uses a single month/year picker and shows the selected month as `Showing June 2026
  (06/2026)`.
- Trips now shows selected-month summary cards above Add Trip for mileage, OwnTracks events, trip
  count, reimbursement, and monthly gas price.
- The login page now supports Device Sign-In after a passkey is created, with the Device Sign-In
  button below the normal Continue button.
- Diagnostics now has a Configure Passkey card to create passkeys, see configured passkeys, and
  remove passkeys for the single web-login user.
- Diagnostics now lists successful logins above failed logins, with both lists paginated.
- Automatic backups created at app startup are labeled in Diagnostics.
- Waypoints and Diagnostics pagination are more compact on mobile, with the nav buttons filling one
  row and the page count shown underneath.
- The top-bar brand is no longer clickable.
- Diagnostics now lets you manually send a valid IP address and reason to Cloudflare, then shows
  the reason with an Auto or Manual pill in the removable blocked-IP list.
- Successful and failed login rows now use the Cloudflare-derived client IP when available, so the
  failed-login block button targets the real browser IP.

## 1.2.0 - 2026-06-24

- Desktop navigation links now use boxed button styling like Logout.
- Mobile navigation now stays in one full-width top-bar row, with the bottom of the screen left
  clear for phone system navigation and without opting the page into edge-to-edge phone drawing.
- Diagnostics now shows the app version in the Application card.
- Generated trips now start from the latest stored rolling odometer checkpoint when it is newer
  than the previous trip odometer, and the end odometer follows the trip distance.

## 1.1.4 - 2026-06-23

- Dashboard summary cards now show Trips in place of Waypoints and add the current-month
  reimbursement total with one-decimal reimbursement gallons.
- Diagnostics now uses compact 10-row pages for recent OwnTracks entries, OwnTracks state changes,
  failed login attempts, and app-managed Cloudflare blocked IPs.
- Diagnostics hard drive space now groups matching runtime paths by exact used space and total
  space, shows used-space bars, and includes database size plus total app-record count.
- Trips now show newest dates first, while the Dashboard recent trips order stays unchanged.
- Retained automatic backups on Diagnostics can be downloaded individually.
- Manual trip entry now uses the rolling OwnTracks odometer checkpoint, inserts new manual trips at
  the end of the selected date, and preserves positive non-trip odometer gaps during resequencing.
- Trips manual-entry and row-edit forms now use saved waypoint dropdowns for From and To.
- Dashboard trip plus non-trip distance totals no longer show a negative non-trip remainder after
  one-decimal rounding.
