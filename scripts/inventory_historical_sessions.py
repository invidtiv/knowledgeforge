#!/usr/bin/env python3
"""Inventory local historical AI session sources without ingesting content."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from knowledgeforge.ingestion.historical_sessions import build_local_inventory, write_json


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", default=str(Path.home()), help="Home directory to inspect")
    parser.add_argument(
        "--output",
        default=str(ROOT / "data/historical_ingestion/local_inventory.json"),
        help="Where to write the JSON inventory report",
    )
    parser.add_argument("--no-write", action="store_true", help="Print only; do not write output")
    args = parser.parse_args()

    os.environ.setdefault("KNOWLEDGEFORGE_CONFIG", str(ROOT / "config.yaml"))
    report = build_local_inventory(Path(args.home))

    if not args.no_write:
        write_json(Path(args.output), report)

    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
