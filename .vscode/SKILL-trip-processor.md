# Skill: Understanding Trip Processor and Automatic Trip Generation

**Purpose**: Guide AI agents through the automatic trip generation system, trip processor behavior, debugging, and extending trip detection logic.

## Overview

The trip processor (`mileage_logger/services/trip_processor.py`) is the core engine that:
1. Monitors incoming OwnTracks location and transition events
2. Detects qualifying waypoint transition pairs (leave + enter)
3. Generates `Trip` records with calculated mileage
4. Maintains a rolling odometer checkpoint
5. Purges only old raw OwnTracks location/event records after the 90-day minimum retention window

OwnTracks HTTP and MQTT ingestion first goes through
`mileage_logger.services.owntracks_buffer.ingest_or_buffer_owntracks_payload()`. When PostgreSQL is
unreachable, or when earlier payloads are already queued, payloads are stored in the persistent
local FIFO buffer and replayed later through the normal OwnTracks processing path. The primary
buffer may be an NFS-backed Docker bind mount; if it is unavailable, the app uses the configured
local fallback buffer. Fallback replay while the primary buffer is still unavailable is allowed
only when the app observed primary failure before the database outage. If the primary buffer had
older queued entries first, replay waits until both queues are readable so receive order is
preserved. Do not bypass that buffer-aware entry point when changing ingestion behavior.

---

## Trip Generation Logic

### How Trips Are Created

A trip is created when:
```
waypoint_leave_event (at time T1) 
  + waypoint_enter_event (at time T2) 
  + T2 > T1 
  + destination arrival starts inside its saved radius
  + destination remains valid for ≥ OWNTRACKS_WAYPOINT_DWELL_MINUTES
  + NOT (origin == "Home" AND destination == "Home")
```

OwnTracks region metadata is only a candidate signal. A destination visit starts from stored
latitude/longitude inside the saved waypoint radius, then dwell confirmation can come from later
coordinates inside the radius, a later same-waypoint `leave`, a later next-waypoint `enter`, or the
next processing pass after the dwell timer when no earlier event contradicts the visit. `desc`,
`rid`, or `inregions` labels alone must not override outside-radius coordinates.

### Key Entry Point

[`generate_trips(db, day, checkpoint)`](mileage_logger/services/mileage.py) in `mileage.py`:
- Called once per local calendar day by the trip processor
- Returns list of newly created `Trip` records
- Uses a `TripProcessingCheckpoint` to avoid re-processing old data

### Event Sequence Example

```
1. OwnTracks sends: transition event { "event": "leave", "desc": "Home", ... }
   → Stored in owntracks_locations as raw_payload

2. OwnTracks sends: 20 location updates inside "Work" waypoint
   → Each stored in owntracks_locations

3. OwnTracks sends: transition event { "event": "enter", "desc": "Work", ... }
   → Stored in owntracks_locations
   → Minimum dwell (5 min default) verified by later coordinates, later waypoint state, or the next
     processing pass after the dwell timer when no earlier event contradicts the visit
   → Trip auto-created from Home→Work

4. Trip processor updates checkpoint.last_owntracks_location_id
   → Next run skips already-processed locations
```

---

## Odometer Checkpoint System

### Rolling Odometer Calculation

The checkpoint maintains:
- `odometer_anchor_miles` — Last known absolute odometer value
- `odometer_anchor_recorded_at` — When that value was recorded
- `last_owntracks_location_id` — Position in location stream (prevents re-processing)

### Odometer Advancement

When trip processor runs:
1. Fetch new locations since last checkpoint
2. Sum point-to-point distances using Haversine formula
3. Advance rolling checkpoint: `new_checkpoint = anchor + distance_sum`
4. Stamp processed OwnTracks rows with the rolling odometer value for that point, whether or not
   the movement becomes a trip
5. Use stamped rolling odometer values for generated trip starts when available. If a generated
   trip has no stamped transition odometer yet, use the master rolling OwnTracks checkpoint before
   the trip start. If the available master checkpoint is later than the trip start, estimate the
   start only when retained OwnTracks path rows connect the trip start to that checkpoint. Prior
   trip end odometers are not a source for generated trip starts. End odometers are calculated from
   the chosen start plus the generated trip distance.
6. Use the current rolling checkpoint for new manual trip starts instead of the previous trip end
   odometer
7. Run the missing-trip-odometer backfill pass so existing rows with blank odometers can be filled
   from the master checkpoint when retained OwnTracks path data is available.
8. Generated, edited, deleted, and resequenced trip rows do not update the master rolling
   checkpoint. When the user manually enters odometer, reset anchor to exact value.
9. Before old raw OwnTracks rows are purged, refresh monthly OwnTracks summary rollups so older
   month web totals and event counts remain stable after raw location/event cleanup.

### Example

```
Initial state:
  odometer_anchor_miles = 50000.0
  
Location 1→2 distance: 5.2 mi
  → checkpoint becomes 50005.2
  
Location 2→3 distance: 3.1 mi
  → checkpoint becomes 50008.3

If user then enters "manual odometer: 50010.0":
  → anchor resets to 50010.0
  → next distances calculate from 50010.0
```

---

## Debugging Trip Generation

### Common Issues

**1. No trips being generated**
- Check `AUTOMATIC_TRIP_PROCESSING_ENABLED=true` in `.env`
- Verify OwnTracks is sending transition events (not just locations)
- Check minimum dwell time: the destination arrival must start inside the waypoint radius and stay
  uncontradicted for at least 5 minutes
- Confirm waypoint names match exactly (case-sensitive)

**2. Trips generated but with wrong mileage**
- Check mileage priority:
  1. OwnTracks path distance (preferred)
  2. Waypoint-to-waypoint distance (fallback)
- Odometer values are display/checkpoint values and must not be used as generated trip distance
- Manual edit on `/trips` page overrides calculation

**3. Trip dwell time not met**
- Default: `OWNTRACKS_WAYPOINT_DWELL_MINUTES=5`
- If user drives through a waypoint quickly, trip won't generate
- Check OwnTracks event timestamps: `tst` field must show an inside-radius arrival and no early
  same-waypoint leave, next-waypoint arrival, or clearly-away movement before the dwell deadline.
  A later same-waypoint leave after the dwell window confirms that earlier arrival.

### Diagnostics Page

Visit `/diagnostics` to see:
- Current OwnTracks state (at waypoint, traveling, etc.)
- Recent events (transitions and location updates)
- Recent app logs
- Recent trip calculation logs

### Trip Calculation Logger

Enable debug logging to see trip calculation details:
```env
LOG_LEVEL=debug
```

Logs go to `mileage_logger.trip_calculation` logger. Check file at `LOG_DIR/logs/`.

---

## Code Structure

### Main Classes

**`AutomaticTripProcessor`** — Background thread that runs trip generation on interval
- `start()` — Begin background thread
- `stop()` — Stop gracefully
- Runs every `AUTOMATIC_TRIP_PROCESSING_INTERVAL_SECONDS` (default 60)

**`TripProcessingCheckpoint`** — Database model tracking processing state
- `name` — Always `"automatic_trip_processing"`
- `last_owntracks_location_id` — Prevents re-processing
- `odometer_anchor_miles` — Rolling odometer value
- `odometer_anchor_recorded_at` — When anchor was recorded

**`TripGenerationKey`** — Tuple identifying a unique trip: `(origin_id, dest_id, started_at, ended_at)`

### Key Functions

**`_new_locations_after_checkpoint(db, checkpoint)`**
- Returns list of unprocessed OwnTracks location rows since last checkpoint
- Used to detect new events

**`update_odometer_anchor_from_reading(db, odometer_miles, recorded_at, source)`**
- Called when user manually enters odometer on `/diagnostics` page
- Resets rolling checkpoint to exact value

---

## Extending Trip Generation

### Adding Custom Trip Detection Logic

If you need to generate trips from sources other than OwnTracks:

1. **For manual trips**: Use [`create_manual_trip()`](mileage_logger/services/mileage.py#L400) in mileage.py
   ```python
   trip = create_manual_trip(
       db,
       trip_date,
       origin_name,
       destination_name,
       start_lat, start_lon,
       end_lat, end_lon,
       miles
   )
   ```

2. **To skip a trip**: Use [`delete_trip()`](mileage_logger/services/mileage.py#L450) to create a deletion tombstone
   - Prevents auto-regeneration from same OwnTracks events

3. **To edit a trip**: Use [`update_trip_details()`](mileage_logger/services/mileage.py#L480)
   - Updates trip date, names, or miles
   - Re-sequences month's odometer chain when miles change

### Modifying Waypoint Matching

Edit `site_for_location()` in [mileage.py](mileage_logger/services/mileage.py#L250) to customize how OwnTracks events match to saved waypoints:
- Currently matches by: region ID → name → distance (within radius)
- Can add custom rules (e.g., time-of-day, frequency bias)

### Adding Custom Odometer Source

The mileage calculation supports custom odometer sources. Add a new source type:
1. Edit mileage calculation in `_calculate_mileage_for_trip()`
2. Set `start_odometer_source` and `end_odometer_source` accordingly
3. Odometer display will use the source name in web UI

---

## Configuration

Key settings for trip processing:

| Variable | Default | Purpose |
|----------|---------|---------|
| `AUTOMATIC_TRIP_PROCESSING_ENABLED` | `true` | Enable/disable background processor |
| `AUTOMATIC_TRIP_PROCESSING_INTERVAL_SECONDS` | `60` | How often to run trip generation |
| `OWNTRACKS_WAYPOINT_DWELL_MINUTES` | `5` | Minimum time inside destination before trip confirmed |
| `OWNTRACKS_LOCATION_RETENTION_DAYS` | `90` | Days to keep raw OwnTracks location/event records before purging; values below 90 are treated as 90 |
| `OWNTRACKS_PURGE_ENABLED` | `true` | Enable/disable automatic purge |
| `OWNTRACKS_BUFFER_ENABLED` | `true` | Keep OwnTracks HTTP/MQTT ingest available during database outages by buffering payloads locally |
| `OWNTRACKS_BUFFER_PATH` | `data/owntracks-buffer.sqlite3` locally, `/data/owntracks-buffer/owntracks-buffer.sqlite3` in Docker | Persistent SQLite FIFO queue for outage-time OwnTracks payloads |
| `OWNTRACKS_BUFFER_FALLBACK_PATH` | `data/owntracks-buffer-fallback.sqlite3` locally, `/data/owntracks-buffer-fallback/owntracks-buffer.sqlite3` in Docker | Local fallback SQLite queue used when the primary buffer path is unavailable |
| `OWNTRACKS_BUFFER_REPLAY_INTERVAL_SECONDS` | `15` | How often the replay worker checks for queued payloads |
| `OWNTRACKS_BUFFER_REPLAY_BATCH_SIZE` | `100` | Maximum queued payloads replayed per worker pass |
| `LOCAL_TIMEZONE` | `America/Detroit` | Timezone for trip date selection |

---

## Testing

See [test_mileage.py](tests/test_mileage.py) for comprehensive trip generation tests:
- Trip detection from waypoint transitions
- Odometer calculation and advancement
- Manual trip entry
- Trip deletion and suppression records
- Mileage fallback priority system

Key test patterns:
```python
# Create mock locations and transitions
# Call generate_trips(db, day, checkpoint)
# Assert Trip records created with correct mileage
# Verify checkpoint advanced correctly
```

---

## Performance Considerations

- **Database queries**: Trip processor runs once per minute (configurable), one query per unprocessed location
- **OwnTracks retention**: Purge keeps at least the last 90 days of raw OwnTracks rows and stores
  monthly summaries before cleanup so older month web totals remain stable
- **Checkpoint**: One row in database, updated once per processor run
- **Lock**: Single thread processing to prevent concurrent modification conflicts

See [trip_processor.py](mileage_logger/services/trip_processor.py#L1) `_PROCESSING_LOCK` for concurrency guard.
