from __future__ import annotations

import argparse
import cProfile
import json
import random
import sys
import tempfile
import time
import tracemalloc
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.expenses.repositories.expenses_repository import ExpensesRepository
from src.expenses.services.analytics import AnalyticsService


def seeded_transactions(count: int) -> list[dict]:
    randomizer = random.Random(20260817)
    start = datetime(2021, 1, 1, tzinfo=timezone.utc)
    rows = []
    for index in range(count):
        stamp = start + timedelta(minutes=index * 47)
        vendor_index = index % 500
        amount = round(randomizer.lognormvariate(5.0, 0.8), 2)
        rows.append(
            {
                "transactionKey": f"bench-{index}",
                "providerId": "benchmark",
                "externalId": f"bench-{index}",
                "parserId": "benchmark",
                "bankName": "Benchmark Bank",
                "accountSuffix": f"{index % 4:04d}",
                "direction": "debit",
                "amount": amount,
                "currency": "INR",
                "transactionId": f"ref-{index}",
                "counterparty": f"Vendor {vendor_index}",
                "rawCounterparty": f"Vendor {vendor_index}",
                "resolvedVendor": f"Vendor {vendor_index}",
                "canonicalVendor": f"Vendor {vendor_index}",
                "canonicalAlias": f"Vendor {vendor_index}",
                "aliasKey": f"vendor-{vendor_index}",
                "category": f"Category {vendor_index % 20}",
                "subcategory": "",
                "vendorMatchSource": "benchmark",
                "timestamp": stamp.isoformat(),
                "year": stamp.year,
                "month": stamp.month,
                "title": "Benchmark transaction",
                "sender": "benchmark@example.invalid",
            }
        )
    return rows


def run(count: int, profile_path: Path | None, *, measure_memory: bool = False) -> dict:
    with tempfile.TemporaryDirectory() as temp_dir:
        repository = ExpensesRepository(Path(temp_dir) / "expenses.db")
        rows = seeded_transactions(count)
        repository.upsert_transactions(rows)
        service = AnalyticsService(repository)
        profiler = cProfile.Profile()
        if measure_memory:
            tracemalloc.start()
        started = time.perf_counter()
        if profile_path is not None:
            profile_path.parent.mkdir(parents=True, exist_ok=True)
            profiler.enable()
        report = service.recompute()
        if profile_path is not None:
            profiler.disable()
            profiler.dump_stats(profile_path)
        elapsed = time.perf_counter() - started
        peak = 0
        if measure_memory:
            _current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
        return {
            "transactions": count,
            "insights": int(report.get("generated", 0)),
            "elapsedSeconds": elapsed,
            "peakMemoryBytes": peak,
            "profile": str(profile_path or ""),
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=10_000)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--measure-memory", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(max(1, args.rows), args.profile, measure_memory=args.measure_memory), indent=2))
