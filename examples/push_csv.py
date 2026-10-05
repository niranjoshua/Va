"""Send a site's CSV export to a Vaticore API (staging or production), in batches.

For loading a site's history from your own computer: the databases accept
connections only from Vaticore's services, so readings go in through the API
with the operator's key, like any other push.

    uv run python examples/push_csv.py export.csv \\
        --api https://vaticore-api-staging.onrender.com \\
        --operator example-towerco --site lag-ikd-0142 --key vk_... \\
        --timezone Africa/Lagos \\
        --map "Time=timestamp" --map "Load (kW)=load_kw" --map "Genset (kW)=genset_kw"

Columns already named like the internal schema (timestamp, load_kw,
generation_kw, grid_available, battery_soc_pct, genset_kw, fuel_level_l) need
no --map. Watts are not converted: map only columns in kW, percent and
litres. Use --dry-run first to see what would be sent.
"""

from __future__ import annotations

import argparse
import math
import sys

import httpx
import pandas as pd

FIELDS = (
    "load_kw",
    "generation_kw",
    "grid_available",
    "battery_soc_pct",
    "genset_kw",
    "fuel_level_l",
)
BATCH = 5000


def readings(frame: pd.DataFrame) -> list[dict[str, object]]:
    """Rows as API readings: timestamp text plus the known numeric fields."""
    present = [f for f in FIELDS if f in frame.columns]
    if "timestamp" not in frame.columns:
        raise SystemExit("no timestamp column: add --map 'YourColumn=timestamp'")
    if not present:
        raise SystemExit(f"none of {', '.join(FIELDS)} found: add --map for your columns")
    out = []
    for row in frame[["timestamp", *present]].itertuples(index=False):
        item: dict[str, object] = {"timestamp": str(row[0])}
        for name, value in zip(present, row[1:], strict=True):
            if value is not None and not (isinstance(value, float) and math.isnan(value)):
                item[name] = float(value)
        out.append(item)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("csv")
    parser.add_argument("--api", required=True, help="the API's address")
    parser.add_argument("--operator", required=True)
    parser.add_argument("--site", required=True)
    parser.add_argument("--key", required=True, help="the operator's key (or the admin token)")
    parser.add_argument("--timezone", help="for timestamps without an offset, e.g. Africa/Lagos")
    parser.add_argument("--map", action="append", default=[], help="'CSV column=internal name'")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    frame = pd.read_csv(args.csv)
    renames = {}
    for item in args.map:
        source, sep, target = item.partition("=")
        if not sep or target not in ("timestamp", *FIELDS):
            raise SystemExit(
                f"--map {item!r}: use 'CSV column=one of timestamp, {', '.join(FIELDS)}'"
            )
        renames[source] = target
    frame = frame.rename(columns=renames)
    rows = readings(frame)
    print(f"{len(rows):,} readings from {args.csv}; first {rows[0]}, last {rows[-1]}")
    if args.dry_run:
        return 0

    url = f"{args.api.rstrip('/')}/ingest/{args.operator}/{args.site}"
    headers = {"Authorization": f"Bearer {args.key}"}
    with httpx.Client(timeout=120.0) as client:
        for start in range(0, len(rows), BATCH):
            body: dict[str, object] = {"readings": rows[start : start + BATCH]}
            if args.timezone:
                body["timezone"] = args.timezone
            response = client.post(url, json=body, headers=headers)
            if response.status_code >= 300:
                print(f"rejected at row {start}: HTTP {response.status_code} {response.text[:300]}")
                return 1
            print(
                f"sent rows {start:,} to {min(start + BATCH, len(rows)) - 1:,}: {response.json()}"
            )
    print("done. Next: python -m vaticore.pipeline health --days 60 (in the service Shell)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
