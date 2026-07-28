# DedDrop

**DedDrop**: A passive telemetry dead drop container for [WDGWars](https://wdgwars.pl). Ingests airborne ADS-B aircraft feeds and MeshMapper LoRa wardriving telemetry, presenting a real-time web dashboard and uploading HMAC-signed batches to WDGWars.

## Features

- **ADS-B Aircraft Accumulator**: Polls `aircraft.json` from readsb/tar1090 (default: 30s), accumulates seen aircraft in memory, and flushes HMAC-signed batches to WDGWars (default: 6h).
- **MeshMapper Wardrive Target**: Built-in HTTP ingest endpoint (`/api/wardrive`) for receiving LoRa wardriving nodes directly from MeshMapper.
- **Web Dashboard**: Interactive monitoring UI with real-time stats, live node/aircraft tables, manual Poll/Flush triggers, and a MeshMapper deep-link setup modal. Native ES modules with no build step, a strict CSP, and no external CDN or font requests — it works fully offline.
- **State Persistence & Recovery**: Accumulator state persists to `/data/state/accumulator.json` across container restarts. Saved snapshots provide an audit trail.
- **No Silent Data Loss**: A failed upload retains the accumulated window and retries it rather than discarding it.

## Quick Start

1. Copy `.env.example` to `.env`:
   ```bash
   cp .env.example .env
   ```
2. Set your `WDGWARS_API_KEY` and `TAR1090_URL` in `.env`.
3. Create the data directory with the right ownership (the container runs as uid 1000):
   ```bash
   mkdir -p ./data && sudo chown -R 1000:1000 ./data
   ```
4. Launch the service:
   ```bash
   docker compose up -d
   ```
5. Access the Web Dashboard at `http://localhost:8080` (or your configured port/host).

## Environment Variables

### Required

| Variable | Default | Description |
|---|---|---|
| `TAR1090_URL` | *(required)* | Full URL to tar1090/readsb `aircraft.json` |
| `WDGWARS_API_KEY` | *(required)* | WDGWars profile API key (64-hex string) |

### Endpoints

| Variable | Default | Description |
|---|---|---|
| `WDGWARS_API_URL` | `https://wdgwars.pl/endpoint/upload/` | Upload endpoint for signed batches |
| `WDGWARS_ME_URL` | `https://wdgwars.pl/api/me` | Profile/stats endpoint used by the dashboard |
| `MESHMAPPER_API_KEY` | `WDGWARS_API_KEY` | Optional separate API key for MeshMapper ingest |

### Timing

| Variable | Default | Description |
|---|---|---|
| `POLL_INTERVAL_SECONDS` | `30` | Interval between ADS-B feed polls |
| `UPLOAD_INTERVAL_HOURS` | `6` | Interval between WDGWars batch uploads |
| `RETRY_INTERVAL_MINUTES` | `15` | How soon a failed upload is retried |

### Storage

| Variable | Default | Description |
|---|---|---|
| `STATE_FILE` | `/data/state/accumulator.json` | Persisted accumulator state |
| `SNAPSHOT_DIR` | `/data/snapshots` | Directory for upload snapshots |
| `SNAPSHOT_RETENTION` | `200` | Number of historical upload snapshots to retain |
| `SAVE_LATEST_RAW` | `true` | Save raw feed dumps for debugging |
| `LATEST_RAW_PATH` | `/data/latest_raw.json` | Where the raw dump is written |

### Web dashboard & ingest

| Variable | Default | Description |
|---|---|---|
| `WEB_ENABLED` | `true` | Enable/disable the web dashboard |
| `WEB_BIND` | `0.0.0.0` | Bind address *inside* the container. Leave `0.0.0.0` under Docker |
| `WEB_PUBLISH_ADDR` | `0.0.0.0` | Host interface docker publishes the port on. `127.0.0.1` if MeshMapper doesn't need LAN access (compose only — not read by the app) |
| `WEB_PORT` | `8080` | Port for the web dashboard, on both the host and container side |
| `WEB_DIR` | `<repo root>/web` | Directory containing `index.html` |
| `PUBLIC_HOST` | *(empty)* | Public host/IP for the MeshMapper deep link (e.g. `192.168.1.100:8080`) |
| `CORS_ALLOW_ORIGIN` | *(empty)* | Origin allowed to read the API cross-origin. Empty = same-origin only |
| `MAX_BODY_BYTES` | `8388608` | Maximum accepted `/api/wardrive` request body |

### Upload tuning

| Variable | Default | Description |
|---|---|---|
| `BATCH_SIZE` | `500` | Records per upload batch chunk |
| `CHUNK_COOLDOWN_S` | `1` | Pause between chunks |
| `REQUEST_TIMEOUT_S` | `60` | HTTP timeout for outbound requests |
| `MAX_ATTEMPTS` | `3` | Attempts per chunk before giving up |
| `BACKOFF_BASE_S` | `2` | Base delay for exponential backoff |

### Behaviour

| Variable | Default | Description |
|---|---|---|
| `DRY_RUN` | `false` | Log upload batches without POSTing to WDGWars |
| `RUN_ONCE` | `false` | Poll once, force immediate flush, then exit |
| `LOG_LEVEL` | `INFO` | Python logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

## MeshMapper Integration

MeshMapper can push wardriving pings directly to DedDrop:

- **Ingest Endpoint**: `POST /api/wardrive`
- **Authentication**: `X-API-Key` header, or a `?key=` query parameter. Prefer the
  header — query strings tend to end up in proxy and access logs.
- **Quick Setup**: Click the **📡 MeshMapper Link** button on the dashboard to copy the
  deep link (`meshmapper://custom-api?url=...`).

## HTTP API

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/` | none | Dashboard (HTML shell, `no-store`) |
| `GET` | `/app.css`, `/js/*.js` | none | Dashboard assets (ETag revalidated) |
| `GET` | `/healthz` | none | Liveness probe |
| `GET` | `/api/status` | none | Counters, window progress, last upload result |
| `GET` | `/api/aircraft` | none | Accumulated aircraft in the current window |
| `GET` | `/api/mesh-nodes` | none | Accumulated mesh nodes in the current window |
| `GET` | `/api/snapshots` | none | Recent snapshot summaries |
| `GET` | `/api/user-stats` | none | Cached WDGWars profile stats |
| `GET` | `/api/meshmapper-link` | control | Deep link (contains the API key) |
| `POST` | `/api/wardrive` | API key | MeshMapper ping ingest |
| `POST` | `/api/trigger-poll` | control | Force an immediate feed poll |
| `POST` | `/api/trigger-flush` | control | Force an immediate upload flush |

**Control auth** means an `X-Control-Token` header or a valid API key. The control
token is minted per process and embedded in the dashboard when it is served, so the
UI buttons work with no login while cross-origin pages — which cannot read the
dashboard HTML — cannot drive these endpoints.

Cross-origin reads are disabled by default. The API exposes accumulated telemetry,
so only set `CORS_ALLOW_ORIGIN` if you specifically need another origin to read it.
When it is set, `OPTIONS` preflights are answered for `Content-Type`, `X-API-Key`, and
`X-Control-Token`; when it is empty, `OPTIONS` returns 405.

`CORS_ALLOW_ORIGIN` has no bearing on reaching the dashboard in a browser — that is a
same-origin load. If the dashboard itself won't load, see Troubleshooting.

## Protocol & Data Flow

Uploads are sent to WDGWars as HMAC-SHA256 signed JSON envelopes containing base64 payload data:

```http
POST /endpoint/upload/ HTTP/1.1
X-API-Key: <WDGWARS_API_KEY>
Content-Type: application/json

{
  "data":  "<base64_encoded_json>",
  "nonce": "<16_hex_chars>",
  "sig":   "<hmac_sha256_hex_signature>"
}
```

- **Fault Tolerance**: Chunks are retried with exponential backoff on HTTP 429 and
  5xx responses, honouring `Retry-After` when present. HTTP 413 is not retried —
  lower `BATCH_SIZE` instead. If any chunk ultimately fails, the whole window is
  retained and retried after `RETRY_INTERVAL_MINUTES`.
- **Audit Trail**: Every upload saves an exact snapshot to `/data/snapshots/upload_<timestamp>.json`.
- **Unknown telemetry is `null`**: an aircraft with no reported ground speed or track
  sends `null` rather than `0`, so "not received" stays distinct from "zero".

## Development

No runtime or test dependencies beyond the Python 3.10+ stdlib.

```bash
python3 -m deddrop                                  # run it directly
python3 -m unittest discover -s tests -t tests -v   # run the test suite
python3 -m pyflakes deddrop/ tests/                 # lint (pip install pyflakes)
```

Both checks run in CI on every push, along with a Docker build and container smoke test.

### Project layout

```
deddrop/
  config.py      environment-driven settings, read once at import
  runtime.py     state shared between the poll loop and the web server
  normalize.py   tar1090 / MeshMapper payloads -> WDGWars records (pure)
  storage.py     accumulator persistence and the snapshot archive
  uploader.py    HMAC envelope, chunking, retry policy
  webapp.py      dashboard, telemetry API, ingest, control endpoints
  service.py     poll/flush lifecycle and entry point
web/
  index.html     markup only — no inline script, no inline styles
  app.css        the stylesheet
  js/
    main.js      entry point; wires DOM events and refresh loops
    api.js       every call to the DedDrop HTTP API
    panels.js    status badge, stat cards, profile banner, archive list
    table.js     tab state, sorting, filtering, rendering
    ui.js        toasts and the MeshMapper modal
    format.js    escaping and value formatting
tests/           one module per source module
```

The dashboard uses native ES modules — no bundler, no `npm install`, no build
step. Because no inline script or style remains, the server can send a strict
`Content-Security-Policy` (`script-src 'self'`, no `unsafe-inline`), so injected
markup cannot execute even if an escaping bug slipped through.

`normalize.py` deliberately imports nothing from the rest of the package, so the
translation logic can be tested without touching state, disk, or the network.

## Troubleshooting

**`STATE_FILE directory ... is not writable` on startup.** The compose file bind-mounts
`./data` into the container, which masks the image's ownership. Docker creates a missing
`./data` owned by root, but DedDrop runs as uid 1000:

```bash
mkdir -p ./data && sudo chown -R 1000:1000 ./data
```

**Dashboard shows "Disconnected".** Check `docker compose logs deddrop`. If the web
server could not bind its port, DedDrop logs the error and continues headless — polling
and uploading still work.

**`ERR_CONNECTION_REFUSED` on the dashboard.** This is a reachability problem, not a
CORS one — a blocked cross-origin read returns a response the browser then refuses to
hand to the page, so it never looks like a refused connection. Two causes, both visible
in `docker compose logs deddrop`:

- `Cannot assign requested address` — `WEB_BIND` is set to a LAN address of the *host*.
  The container has no interface holding that address, so nothing ever listens. Leave
  `WEB_BIND=0.0.0.0` and set `WEB_PUBLISH_ADDR` to the LAN address instead.
- The log says `listening on http://0.0.0.0:<port>/` but the port still refuses. The
  published port and the container port disagree. Check `docker compose ps`: a mapping
  like `0.0.0.0:8989->8080/tcp` forwards to a container port nothing is bound to.
  Both sides must be `WEB_PORT`, and the port line only re-reads `.env` on
  `docker compose up -d`, not on `restart`.

**`poll rejected all N aircraft in the feed`.** `TAR1090_URL` is reachable but isn't
returning tar1090-shaped JSON, or the receiver has no position data. Confirm the URL
ends in `/data/aircraft.json`.
