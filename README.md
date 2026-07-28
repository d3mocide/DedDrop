# DedDrop

**DedDrop**: A passive telemetry dead drop container for [WDGWars](https://wdgwars.pl). Ingests airborne ADS-B aircraft feeds and MeshMapper LoRa wardriving telemetry, presenting a real-time web dashboard and uploading HMAC-signed batches to WDGWars.

- **Poll** (default every 30s): fetch `aircraft.json` from your
  tar1090/readsb feed, normalize it, and merge it into an in-memory
  rolling set of every aircraft seen — keyed by ICAO, latest position
  wins, `first_seen` preserved from when that aircraft first appeared
  in the current window.
- **Flush** (default every 6h): save the accumulated set to
  `/data/snapshots` and upload it to [wdgwars.pl](https://wdgwars.pl)
  (Watch Dogs Go WARS) in one batch, then clear the set for the next
  window.

Polling frequently and uploading rarely matters because most aircraft are
only in range for a few minutes — a single point-in-time snapshot every 6
hours would miss almost all of them. The accumulator is what fixes that.

The accumulator is persisted to `/data/state/accumulator.json` after every
poll, so a container restart mid-window resumes instead of losing
progress. If the container was down long enough that the window already
elapsed, it flushes whatever it has on startup before resuming polling.

## How the upload actually works

wdgwars.pl's `/help#adsb` docs are behind login, so instead of guessing I
pulled the reference client's source
([HiroAlleyCat/adsb-to-wdgwars](https://github.com/HiroAlleyCat/adsb-to-wdgwars)
"Muninn", and its [gungnir](https://github.com/HiroAlleyCat/gungnir)
transport library) to confirm the actual wire protocol. It's **not** a raw
file upload — it's a JSON body wrapped in an HMAC-SHA256 envelope:

```
POST https://wdgwars.pl/endpoint/upload/     (alias of /api/upload/)
X-API-Key: <your key>
Content-Type: application/json

{
  "data":  base64(json({"networks": [], "aircraft": [...], "meshcore_nodes": []})),
  "nonce": <16 hex chars>,
  "sig":   hex(HMAC_SHA256(key, nonce + data))
}
```

Each aircraft record:
```json
{"icao": "ABD971", "callsign": "SWA1378", "lat": 41.452068, "lon": -82.023862,
 "alt_ft": 11475, "speed_kt": 326, "heading": 250,
 "first_seen": "2026-07-10 12:00:00", "type": "ADSB"}
```

which is a direct field-for-field mapping from tar1090's own
`hex/flight/lat/lon/alt_baro/gs/track` — no external converter needed.

**This was verified against a third-party client's source, not wdgwars.pl's
own docs**, since those require a login I don't have. The normalize/merge
logic and the envelope-building have been tested locally end-to-end
(multi-poll merging, restart/recovery, a mock tar1090 server) and all
check out, but I haven't been able to POST to the live wdgwars.pl endpoint
from here. Run your first cycle with `DRY_RUN=true`, then a real one, and
check `/api/upload-history` in your WDGWars profile (or `curl -H
"X-API-Key: $KEY" "https://wdgwars.pl/api/upload-history?limit=5"`) to
confirm it landed. If wdgwars.pl ever changes the shape, the fix is
localized to `normalize_one()` and the `UPLOAD_URL`/`ME_URL` constants.

## Setup

1. Get an API key: log into wdgwars.pl → profile → generate API key.
2. `cp .env.example .env` and fill in `TAR1090_URL` and `WDGWARS_API_KEY`.
3. First run as a dry run — polls once, forces an immediate flush, exits:
   ```bash
   docker compose run --rm -e DRY_RUN=true -e RUN_ONCE=true adsb-wdgwars-uploader
   ```
   Confirms it can reach your tar1090 feed, parses aircraft correctly, and
   logs what it *would* send — without uploading anything.
4. One real cycle:
   ```bash
   docker compose run --rm -e RUN_ONCE=true adsb-wdgwars-uploader
   ```
   Check the log line `upload accepted: N sent, N new, N already on file`,
   then verify via `/api/upload-history` or your profile page.
5. Go live:
   ```bash
   docker compose up -d
   ```

## Config (env vars)

| Var | Default | Notes |
|---|---|---|
| `TAR1090_URL` | *(required)* | Full URL to your `aircraft.json` |
| `WDGWARS_API_KEY` | *(required)* | From your WDGWars profile |
| `WDGWARS_API_URL` | `https://wdgwars.pl/endpoint/upload/` | Override if needed |
| `POLL_INTERVAL_SECONDS` | `30` | How often to fetch + merge (no upload) |
| `UPLOAD_INTERVAL_HOURS` | `6` | How often to flush + upload |
| `SNAPSHOT_RETENTION` | `200` | Upload snapshots kept before pruning |
| `SAVE_LATEST_RAW` | `true` | Overwrite `/data/latest_raw.json` each poll |
| `BATCH_SIZE` | `500` | Aircraft per upload chunk |
| `DRY_RUN` | `false` | Log without POSTing |
| `RUN_ONCE` | `false` | Poll once, force a flush, exit |
| `LOG_LEVEL` | `INFO` | Python logging level |

## Notes

- Aircraft with no lat/lon (Mode-S contacts without ADS-B position) are
  skipped — they've got nothing for the map to plot.
- An aircraft that flies out of range mid-window stays in the accumulator
  (at its last known position) until the next flush — it was still seen
  during the window, so it's still worth reporting.
- On a 200-OK response where every known counter (`aircraft_imported`,
  `duplicates`, etc.) comes back zero for a non-empty upload, the script
  logs a warning instead of treating it as a quiet success — that pattern
  has apparently bitten other wdgwars.pl integrations before (a
  server-side validation change silently dropping records).
- 429 (rate limited) and 5xx are retried with backoff; at a 6-hour cadence
  neither should realistically trigger.
- `/data/snapshots/upload_<timestamp>.json` is the exact aircraft list
  that got uploaded for that window — a proper audit trail, not just the
  raw tar1090 dump.
