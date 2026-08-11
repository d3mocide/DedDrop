"""Answer, from a running instance, whether MeshMapper's push carries the key.

Mesh ``node_id`` is derived from a node's public key (see ``normalize.py``), and
whether the pushed payload actually carries one is not documented anywhere — the
only authority is a real push. Run this against an instance MeshMapper has been
pushing to:

    docker compose exec deddrop python3 -m deddrop.probe

``--self-test`` instead runs a synthetic key-carrying push through the
normaliser in-process, so the derivation can be checked without a live feed.
That path is deliberately local: POSTing a synthetic ping to /api/wardrive would
merge invented nodes into the upload window and ship them to WDGWars.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

from . import config
from .normalize import describe_mesh_ingest, normalize_mesh_capture

# Not a real node: an obvious 64-hex key whose leading bytes are the short id,
# which is the whole shape the derivation depends on.
_SAMPLE_KEY = "0ce8f1e2d3c4b5a6" + "7" * 48

_SELF_TEST_PUSH = [
    {"type": "DISC", "lat": 52.1, "lon": 21.0, "repeater_id": "0CE8",
     "node_type": "R", "local_rssi": "-67", "public_key": _SAMPLE_KEY},
    {"type": "TRACE", "lat": 52.2, "lon": 21.1, "repeater_id": "none",
     "heard_repeats": "0ce8(R)(-95.5), ffff(-80)"},
]


def _fetch_report(base_url: str, api_key: str) -> dict:
    url = f"{base_url.rstrip('/')}/api/mesh-ingest-report"
    req = urllib.request.Request(url, headers={
        "X-API-Key": api_key,
        "User-Agent": config.USER_AGENT,
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=config.REQUEST_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _print_report(report: dict) -> None:
    print(f"verdict: {report.get('verdict', 'unknown')}\n")
    print(f"  pings in the last push     {report.get('pings', 0)}")
    print(f"  fields carried             {', '.join(report.get('fields') or []) or '(none)'}")
    print(f"  public key field           {report.get('public_key_field') or '(absent)'}")
    print(f"  pings with a usable key    {report.get('pings_with_public_key', 0)}")
    print(f"  nodes normalized           {report.get('nodes', 0)}")
    print(f"  nodes clearing the gate    {report.get('nodes_passing_node_id_gate', 0)}")
    print(f"  nodes still short          {report.get('nodes_short_id', 0)}")
    sample = report.get("sample_ping")
    if sample:
        print(f"\nsample ping:\n{json.dumps(sample, indent=2, sort_keys=True)}")


def _self_test() -> int:
    records = normalize_mesh_capture(_SELF_TEST_PUSH)
    print("Synthetic push (not sent anywhere), one DISC with a key and one "
          "heard_repeats token naming the same node:\n")
    for record in records:
        key = record.get("public_key")
        print(f"  {record['node_id']:<18} "
              f"{f'derived from key {key[:16]}…' if key else 'short id alone — no key for it'}")
    print()
    _print_report(describe_mesh_ingest(_SELF_TEST_PUSH, records))
    derived = [r for r in records if r.get("public_key")]
    # Two records — the DISC ping's own key, and the heard token resolved
    # against it — both under the 8-byte id.
    return 0 if len(derived) == 2 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m deddrop.probe", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=f"http://127.0.0.1:{config.WEB_PORT}",
                        help="base URL of the running DedDrop (default: %(default)s)")
    parser.add_argument("--key", default=config.MESHMAPPER_API_KEY or config.API_KEY,
                        help="API key (defaults to MESHMAPPER_API_KEY, then WDGWARS_API_KEY)")
    parser.add_argument("--self-test", action="store_true",
                        help="check the derivation locally instead of querying an instance")
    args = parser.parse_args(argv)

    if args.self_test:
        return _self_test()

    if not args.key:
        print("no API key: set WDGWARS_API_KEY or pass --key", file=sys.stderr)
        return 2

    try:
        report = _fetch_report(args.url, args.key)
    except urllib.error.HTTPError as e:
        print(f"{args.url} answered HTTP {e.code} — "
              f"{'the key was refused' if e.code == 401 else 'unexpected'}", file=sys.stderr)
        return 2
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
        print(f"could not read the ingest report from {args.url}: {e}", file=sys.stderr)
        return 2

    _print_report(report)
    if not report.get("pings"):
        print("\nNo push has arrived yet. Point MeshMapper at this instance and re-run.")
        return 3
    # Exit non-zero when the push carried nothing to derive an id from, so this
    # can gate a script rather than being read by eye.
    return 0 if report.get("pings_with_public_key") else 3


if __name__ == "__main__":
    raise SystemExit(main())
