#!/usr/bin/env python3
"""
PartView Trip Legs Throughput Test
Elliott Workspace -> [APIs] PartView (10dcd2ae) -> Postman Mock -> Throughput Blast

Real endpoints from the collection:
  GET  /partview/api/tripleg/{uuid}
  GET  /partview/api/search/search-trip-leg?partNumber=...
  GET  /partview/search/trip_leg/{id}?creator_organization_fv_id=...&carrier_organization_fv_id=...
  GET  /partview/app/search?status=active&pageNumber=0&pageSize=N

Usage:
  python3 scripts/partview_throughput_test.py --auto
  python3 scripts/partview_throughput_test.py --auto --concurrency 50 --duration 60
  python3 scripts/partview_throughput_test.py --create-mock
  python3 scripts/partview_throughput_test.py --run-test --url https://your-mock.pstmn.io
  python3 scripts/partview_throughput_test.py --list-mocks
"""

import argparse
import json
import statistics
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import List

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
POSTMAN_API_KEY = "PMAK-69822a0b5fd1860001549c1a-9b5dd9594f4ad58c4aa766a6be36383b1c"
ELLIOTT_WS_ID = "b8e91624-ef75-45e4-a26a-a9435391be74"
PARTVIEW_COL_ID = "10dcd2ae-0689-4d8a-9261-975d675a4b40"
OWNER_ORG_FV_ID = "FV3221778A"
CARRIER_ORG_FV_ID = "FV0834338A"

DEFAULT_CONCURRENCY = 20
DEFAULT_DURATION_S = 30
DEFAULT_RAMP_S = 5

# Real trip-leg scenarios from [APIs] PartView collection
TRIP_LEG_SCENARIOS = [
    ("/partview/api/tripleg/9eeadee9-3ed3-48f7-96aa-e191d969c29a", {}),
    ("/partview/api/tripleg/05bca1c0-0c28-419b-96ac-2da586b89a90", {}),
    ("/partview/api/search/search-trip-leg", {"partNumber": "TRUCK-PART1"}),
    ("/partview/api/search/search-trip-leg", {"partNumber:contains": "PART"}),
    ("/partview/api/search/search-trip-leg", {"partNumber": "TRUCK-PART2"}),
    (
        "/partview/search/trip_leg/AZ99UM2QAWJ",
        {
            "creator_organization_fv_id": "FV3221778A",
            "carrier_organization_fv_id": "FV0834338A",
        },
    ),
    ("/partview/app/search", {"status": "active", "pageNumber": "0", "pageSize": "10"}),
    ("/partview/app/search", {"status": "active", "pageNumber": "0", "pageSize": "25"}),
]

# ---------------------------------------------------------------------------
# Postman API helper
# ---------------------------------------------------------------------------


def _postman_request(method, path, body=None):
    url = "https://api.getpostman.com" + path
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"X-Api-Key": POSTMAN_API_KEY, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


# ---------------------------------------------------------------------------
# Mock server management
# ---------------------------------------------------------------------------


def create_mock(env_id=None):
    """Create a Postman mock in the Elliott workspace, return its URL."""
    print(
        f"[mock] Creating mock from collection {PARTVIEW_COL_ID} in workspace {ELLIOTT_WS_ID}..."
    )
    payload = {
        "mock": {
            "name": f"partview-tripleg-throughput-{int(time.time())}",
            "collection": PARTVIEW_COL_ID,
            "workspace": ELLIOTT_WS_ID,
        }
    }
    if env_id:
        payload["mock"]["environment"] = env_id
    result = _postman_request("POST", "/mocks", payload)
    mock = result.get("mock", {})
    mock_id = mock.get("id", "")
    mock_url = mock.get("mockUrl") or f"https://{mock_id}.mock.pstmn.io"
    print(f"[mock] Created: {mock_url}  (id={mock_id})")
    return mock_url


def list_mocks():
    """List all mocks in the Elliott workspace."""
    result = _postman_request("GET", f"/mocks?workspace={ELLIOTT_WS_ID}")
    mocks = result.get("mocks", [])
    if not mocks:
        print("[mocks] No mock servers found.")
        return
    print(f"[mocks] {len(mocks)} mock(s):")
    for m in mocks:
        print(f"  {m.get('id')}  {m.get('mockUrl', '?')}  name={m.get('name', '?')}")


# ---------------------------------------------------------------------------
# Stats collector
# ---------------------------------------------------------------------------


@dataclass
class Stats:
    lock: threading.Lock = field(default_factory=threading.Lock)
    latencies: List[float] = field(default_factory=list)
    errors: int = 0
    total: int = 0

    def record(self, latency_ms, ok):
        with self.lock:
            self.total += 1
            if ok:
                self.latencies.append(latency_ms)
            else:
                self.errors += 1

    def report(self, elapsed_s):
        with self.lock:
            lats = sorted(self.latencies)
            n = len(lats)
            rps = self.total / elapsed_s if elapsed_s > 0 else 0
            print("\n" + "=" * 60)
            print("  PartView Trip Legs Throughput Report")
            print("=" * 60)
            print(f"  Duration     : {elapsed_s:.1f}s")
            print(f"  Total reqs   : {self.total}")
            print(f"  Errors       : {self.errors}")
            print(f"  Success      : {n}")
            print(f"  RPS          : {rps:.2f}")
            if lats:
                print(f"  Latency p50  : {statistics.median(lats):.1f}ms")
                print(f"  Latency p95  : {lats[max(0, int(0.95 * n) - 1)]:.1f}ms")
                print(f"  Latency p99  : {lats[max(0, int(0.99 * n) - 1)]:.1f}ms")
                print(f"  Latency min  : {lats[0]:.1f}ms")
                print(f"  Latency max  : {lats[-1]:.1f}ms")
            print("=" * 60)


# ---------------------------------------------------------------------------
# Worker thread
# ---------------------------------------------------------------------------


def _worker(base_url, stats, stop_event, ramp_s, worker_idx, concurrency):
    time.sleep((worker_idx / concurrency) * ramp_s)
    idx = worker_idx % len(TRIP_LEG_SCENARIOS)
    while not stop_event.is_set():
        path, params = TRIP_LEG_SCENARIOS[idx % len(TRIP_LEG_SCENARIOS)]
        url = base_url.rstrip("/") + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url,
            headers={"x-api-key": POSTMAN_API_KEY, "Accept": "application/json"},
        )
        t0 = time.perf_counter()
        ok = False
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
                ok = resp.status < 500
        except urllib.error.HTTPError as e:
            ok = e.code < 500
        except Exception:
            ok = False
        stats.record((time.perf_counter() - t0) * 1000, ok)
        idx += 1


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------


def run_test(
    base_url,
    concurrency=DEFAULT_CONCURRENCY,
    duration_s=DEFAULT_DURATION_S,
    ramp_s=DEFAULT_RAMP_S,
):
    print(f"\n[test] Target   : {base_url}")
    print(f"[test] Threads  : {concurrency}")
    print(f"[test] Duration : {duration_s}s  (ramp={ramp_s}s)")
    print(f"[test] Scenarios: {len(TRIP_LEG_SCENARIOS)} trip-leg combos\n")

    stats = Stats()
    stop_event = threading.Event()
    threads = [
        threading.Thread(
            target=_worker,
            args=(base_url, stats, stop_event, ramp_s, i, concurrency),
            daemon=True,
        )
        for i in range(concurrency)
    ]
    for t in threads:
        t.start()

    t_start = time.perf_counter()
    try:
        while time.perf_counter() - t_start < duration_s:
            elapsed = time.perf_counter() - t_start
            with stats.lock:
                rps = stats.total / max(elapsed, 0.001)
            print(
                f"\r[test] {elapsed:.0f}s/{duration_s}s  reqs={stats.total}  rps={rps:.1f}  err={stats.errors}   ",
                end="",
                flush=True,
            )
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[test] Interrupted.")
    finally:
        stop_event.set()
        for t in threads:
            t.join(timeout=2)

    stats.report(time.perf_counter() - t_start)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="PartView Trip Legs Throughput Test")
    parser.add_argument(
        "--auto", action="store_true", help="Create mock + run test (one shot)"
    )
    parser.add_argument(
        "--create-mock", action="store_true", help="Create Postman mock only"
    )
    parser.add_argument(
        "--run-test", action="store_true", help="Run test against --url"
    )
    parser.add_argument(
        "--list-mocks", action="store_true", help="List mocks in Elliott workspace"
    )
    parser.add_argument("--url", default="", help="Mock base URL for --run-test")
    parser.add_argument(
        "--env-id", default="", help="Postman environment ID to attach to mock"
    )
    parser.add_argument(
        "--concurrency", type=int, default=DEFAULT_CONCURRENCY, help="Worker threads"
    )
    parser.add_argument(
        "--duration", type=int, default=DEFAULT_DURATION_S, help="Test duration seconds"
    )
    parser.add_argument(
        "--ramp", type=int, default=DEFAULT_RAMP_S, help="Ramp-up seconds"
    )
    args = parser.parse_args()

    if args.list_mocks:
        list_mocks()
    elif args.create_mock:
        url = create_mock(args.env_id or None)
        print(f"[done] Mock URL: {url}")
    elif args.auto:
        url = create_mock(args.env_id or None)
        time.sleep(2)
        run_test(url, args.concurrency, args.duration, args.ramp)
    elif args.run_test:
        if not args.url:
            parser.error("--run-test requires --url")
        run_test(args.url, args.concurrency, args.duration, args.ramp)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
