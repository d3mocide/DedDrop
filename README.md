# DedDrop

**DedDrop**: A passive telemetry dead drop container for [WDGWars](https://wdgwars.pl). Ingests airborne ADS-B aircraft feeds and MeshMapper LoRa wardriving telemetry, presenting a real-time web dashboard and uploading HMAC-signed batches to WDGWars.

## Features

- **ADS-B Aircraft Accumulator**: Polls `aircraft.json` from readsb/tar1090 (default: 30s), accumulates seen aircraft in memory, and flushes HMAC-signed batches to WDGWars (default: 6h).
- **MeshMapper Wardrive Target**: Built-in HTTP ingest endpoint (`/api/wardrive`) for receiving LoRa wardriving nodes directly from MeshMapper.
- **Web Dashboard**: Interactive monitoring UI with real-time stats, live node/aircraft tables, manual Poll/Flush triggers, and a MeshMapper deep-link setup modal.
- **State Persistence & Recovery**: Accumulator state persists to `/data/state/accumulator.json` across container restarts. Saved snapshots provide an audit trail.

## Quick Start

1. Copy `.env.example` to `.env`:
   ```bash
   cp .env.example .env
   ```
2. Set your `WDGWARS_API_KEY` and `TAR1090_URL` in `.env`.
3. Launch the service:
   ```bash
   docker compose up -d
   ```
4. Access the Web Dashboard at `http://localhost:8080` (or your configured port/host).

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `TAR1090_URL` | *(required)* | Full URL to tar1090/readsb `aircraft.json` |
| `WDGWARS_API_KEY` | *(required)* | WDGWars profile API key (64-hex string) |
| `MESHMAPPER_API_KEY` | `WDGWARS_API_KEY` | Optional separate API key for MeshMapper ingest |
| `PUBLIC_HOST` | *(empty)* | Optional public host/IP for MeshMapper deep-link target (e.g. `192.168.1.100:8080`) |
| `WEB_ENABLED` | `true` | Enable/disable the web dashboard |
| `WEB_PORT` | `8080` | Port for the web dashboard |
| `POLL_INTERVAL_SECONDS` | `30` | Interval between ADS-B feed polls |
| `UPLOAD_INTERVAL_HOURS` | `6` | Interval between WDGWars batch uploads |
| `SNAPSHOT_RETENTION` | `200` | Number of historical upload snapshots to retain |
| `SAVE_LATEST_RAW` | `true` | Save raw feed dumps to `/data/latest_raw.json` |
| `BATCH_SIZE` | `500` | Records per upload batch chunk |
| `DRY_RUN` | `false` | Log upload batches without POSTing to WDGWars |
| `RUN_ONCE` | `false` | Poll once, force immediate flush, then exit |
| `LOG_LEVEL` | `INFO` | Python logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

## MeshMapper Integration

MeshMapper can push wardriving pings directly to DedDrop:
- **Ingest Endpoint**: `POST /api/wardrive`
- **Authentication**: `X-API-Key` header or `?key=` query parameter
- **Quick Setup**: Click the **📡 MeshMapper Link** button on the web dashboard to copy the deep link (`meshmapper://custom-api?url=...`).

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

- **Fault Tolerance**: Automatic retry with exponential backoff on HTTP 429 / 5xx errors.
- **Audit Trail**: Every upload saves an exact snapshot to `/data/snapshots/upload_<timestamp>.json`.
