# Web Changelog

## Unreleased

- Mobile navigation now stays in the top bar, with the bottom of the screen left clear for phone
  system navigation.
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
