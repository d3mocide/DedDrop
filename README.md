# DedDrop

**DedDrop**: A passive telemetry dead drop container for [WDGWars](https://wdgwars.pl). Ingests airborne ADS-B aircraft feeds and MeshMapper LoRa wardriving telemetry, presenting a real-time web dashboard and uploading HMAC-signed batches to WDGWars.

## Features

- **ADS-B Aircraft Accumulator**: Polls `aircraft.json` from readsb/tar1090 (default: 30s), accumulates seen aircraft in memory, and flushes HMAC-signed batches to WDGWars (default: 6h).
- **MeshMapper Wardrive Target**: Built-in HTTP ingest endpoint (`/api/wardrive`) for receiving LoRa wardriving nodes directly from MeshMapper. Node IDs are derived from each node's public key where the push carries one, so they clear the WDGWars gate instead of coming back as `bad_node_id`.
- **Web Dashboard**: Interactive monitoring UI with real-time stats, live node/aircraft tables, a **Dispatch Reports** tab holding what WDGWars made of each upload window, manual Poll/Flush triggers, and a MeshMapper deep-link setup modal. Native ES modules with no build step, a strict CSP, and no external CDN or font requests — it works fully offline.
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
| `WDGWARS_API_URL` | `https://wdgwars.pl/endpoint/upload/` | Upload endpoint for signed aircraft batches |
| `WDGWARS_MESH_API_URL` | *(same as `WDGWARS_API_URL`)* | Upload endpoint for signed mesh batches, if it differs |
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
| `PUBLIC_HOST` | *(empty)* | Public host/IP for the MeshMapper deep link (e.g. `192.168.1.100:8080`). A `http://` or `https://` prefix is stripped |
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
- **What a push contained**: `docker compose exec deddrop python3 -m deddrop.probe` prints
  the fields the last push carried and whether they included the node public keys mesh
  IDs are derived from. See
  [Checking whether your pushes carry the key](#checking-whether-your-pushes-carry-the-key).
- **No scheme in the link**: MeshMapper prepends `https://` to the endpoint it is given,
  so the link carries `host/api/wardrive` rather than `http://host/api/wardrive` — the
  latter imports as `https://http://host/api/wardrive`. DedDrop itself serves plain
  HTTP, so put it behind a TLS-terminating reverse proxy for MeshMapper to reach it.

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
| `GET` | `/api/dispatch-log` | none | Dispatch history, newest first |
| `GET` | `/api/user-stats` | none | Cached WDGWars profile stats |
| `GET` | `/api/meshmapper-link` | control | Deep link (contains the API key) |
| `GET` | `/api/mesh-ingest-report` | control | What the last MeshMapper push contained |
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

Each payload carries `networks`, `aircraft`, and `meshcore_nodes`, but only one of
them ever holds records:

- **Separate dispatches per feed**: aircraft go to `WDGWARS_API_URL` and mesh nodes
  to `WDGWARS_MESH_API_URL` as independent requests. Bundled, they shared one
  response — non-zero aircraft counters made the batch read as accepted even when
  every mesh node in it was discarded, so mesh data vanished with no error anywhere.
  Split, each response is judged on its own counters (`aircraft_imported` /
  `meshcore_imported` and friends), and a feed that reports nothing back is treated
  as a failure rather than a success.
- **Independent retention**: if one feed fails, only that feed's records are
  retained and retried; the one that landed is cleared instead of being re-sent.
  The dashboard names which feed is pending.
- **A refusal is a verdict, not a failure**: records WDGWars itemises in
  `meshcore_reject_reasons` are counted as delivered and cleared. Re-sending what
  the server has explicitly refused would retry forever.
- **Rejections are predicted before they happen**: WDGWars' per-record gates are
  mirrored client-side, so the log says which nodes will be refused and why
  rather than leaving a rejected count as the only clue. See
  [Mesh nodes and `bad_node_id`](#mesh-nodes-and-bad_node_id).
- **Fault Tolerance**: Chunks are retried with exponential backoff on HTTP 429 and
  5xx responses, honouring `Retry-After` when present. HTTP 413 is not retried —
  lower `BATCH_SIZE` instead. If any chunk of a feed ultimately fails, that feed's
  window is retained and retried after `RETRY_INTERVAL_MINUTES`.
- **Audit Trail**: Every upload saves an exact snapshot to
  `/data/snapshots/upload_<timestamp>.json`, holding the records in the form they
  were sent.
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
  probe.py       CLI: what a real MeshMapper push carried
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

### Mesh nodes and `bad_node_id`

**Mesh nodes upload cleanly but your WDGWars profile still shows 0.** WDGWars gates
every node on `node_id` being **8-16 lowercase hex** before storing it. A MeshCore
node's on-air name is the **leading bytes of its Ed25519 public key**, so
`repeater_id` is a 1-3 byte prefix — 2-6 hex — which is under that floor and comes
back as `meshcore_reject_reasons: {"bad_node_id": n}`.

Where a ping carries the node's full `public_key`, DedDrop takes `node_id` from its
**first 16 hex (8 bytes)** instead. That is not a workaround: the short ID *is* that
key's leading hex, so it is the same identity with more digits. 8 bytes is the
canonical length — `node_id` is `varchar(16)` server-side, and shorter prefixes
collide across live nodes, which would make two repeaters overwrite each other's
coordinates. The full key is sent along as the optional `public_key` field; the
server verifies `node_id` is its prefix, and its absence never rejects.

Two guard rails apply:

- A key is used only when the short ID it arrived with is actually its leading hex,
  so a mispaired key cannot rename a node into someone else's identity.
- `heard_repeats` tokens carry a short ID and no key. They are resolved against keys
  seen elsewhere in the **same push**, and only when exactly one key matches that
  prefix. Ambiguous ones keep the short ID and are reported as a predicted rejection.

A node heard without a key anywhere in its push still misses the floor. DedDrop names
that rather than hiding it: the flush log warns before uploading, the dispatch panel
shows `n refused (bad_node_id)`, and those records are cleared rather than retried
forever.

The approach, the 8-byte length, and the guard rails come from the reference feeder,
[Heimdall](https://github.com/Yggdrasil-AI-labs/meshcore-to-wdgwars) — see
[issue #6](https://github.com/d3mocide/DedDrop/issues/6). Two other gates are worth
knowing: a node at `lat/lon 0,0` is refused as `no_gps` (DedDrop already drops those
at ingest), and `node_type` no longer rejects — it coerces to `Unknown` server-side.

### Dispatch Reports

The dashboard's third tab is the history of what WDGWars made of each upload
window — one row per dispatch, newest first:

| Dispatched | Window | Polls | Aircraft | Mesh |
|---|---|---|---|---|
| 14:02 | 6.00h | 720 | 412 sent / 388 new | 16 sent / 15 new / 1 refused (bad_node_id) |
| 08:01 | 6.00h | 719 | 390 sent / 361 new | 14 sent / 14 new |
| 02:00 | 6.00h | 718 | 377 sent / 350 new | 11 sent / 0 new / 11 refused (bad_node_id) |

A refusal and a failed delivery read differently on purpose: **refused** means
WDGWars saw the records and itemised why it said no, while **not delivered**
means it never returned a verdict and the window was retained for retry. A dry
run is tagged as one.

Entries are written on every flush — successes and failures alike, since the
failures are the ones worth looking back at — and kept in
`/data/state/dispatch_log.json`, bounded to `DISPATCH_LOG_LIMIT` (default 50).
The same history is served at `GET /api/dispatch-log`, newest first. The tab also
carries the MeshMapper ingest summary described below, so the two questions —
"did my last upload land" and "is MeshMapper sending the keys" — are answered on
one screen.

### Checking whether your pushes carry the key

MeshMapper's push payload is undocumented, so whether it includes `public_key` is
answered by looking at a real push rather than by reasoning about it. Every push is
surveyed at ingest and the result is kept:

```bash
docker compose exec deddrop python3 -m deddrop.probe
```

It prints the fields the last push carried, whether any held a 64-hex key, and how
many node IDs that let DedDrop widen past the gate. It exits `0` when keys are
arriving, `3` when they are not (or no push has landed yet), and `2` on a connection
or auth failure. The same report is served at `GET /api/mesh-ingest-report`, and the
first push of each shape logs its verdict at `INFO` — so `docker compose logs deddrop`
answers the question too, with no debug level needed.

`python3 -m deddrop.probe --self-test` runs a synthetic key-carrying push through the
normaliser in-process instead, checking the derivation without a live feed. It sends
nothing: POSTing a synthetic ping to `/api/wardrive` would merge invented nodes into
the upload window and ship them to WDGWars.
