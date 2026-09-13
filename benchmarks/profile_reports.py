"""Deterministic local benchmark for bounded CLI report queries."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from pathlib import Path

from src.expenses.bootstrap import build_runtime


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=10_000)
    args = parser.parse_args()
    rows = max(1, int(args.rows))
    with tempfile.TemporaryDirectory(prefix="expense_manager_report_benchmark_") as temp_dir:
        root = Path(temp_dir)
        previous_home = os.environ.get("EXPENSE_MANAGER_HOME")
        os.environ["EXPENSE_MANAGER_HOME"] = str(root / "runtime")
        try:
            runtime = build_runtime(root)
            runtime.repositories["expenses"].upsert_transactions([_transaction(index) for index in range(rows)])
            reports = runtime.services["reports"]
            results = {
                "dashboard": _measure(lambda: reports.dashboard(months=6, top=10)),
                "analytics": _measure(lambda: reports.analytics(months=12, top=10)),
                "transactionPage": _measure(lambda: reports.transactions(limit=200)),
                "vendorList": _measure(lambda: reports.vendors(limit=100)),
                "diagnostics": _measure(lambda: reports.diagnostics(limit=200)),
            }
        finally:
            if previous_home is None:
                os.environ.pop("EXPENSE_MANAGER_HOME", None)
            else:
                os.environ["EXPENSE_MANAGER_HOME"] = previous_home
    print(json.dumps({"rows": rows, "results": results}, sort_keys=True))
    return 0


def _measure(action) -> dict[str, float]:
    started = time.perf_counter()
    action()
    return {"seconds": time.perf_counter() - started}


def _transaction(index: int) -> dict:
    month = (index % 12) + 1
    day = (index % 28) + 1
    vendor = f"Vendor {index % 50:02d}"
    return {
        "transactionKey": f"bench-{index:06d}",
        "providerId": "benchmark",
        "externalId": f"bench-{index:06d}",
        "parserId": "benchmark",
        "bankName": "Benchmark Bank",
        "accountSuffix": "1234",
        "direction": "debit",
        "amount": float((index % 1000) + 1),
        "currency": "INR",
        "transactionId": f"bench-{index:06d}",
        "counterparty": vendor,
        "rawCounterparty": vendor,
        "resolvedVendor": vendor,
        "canonicalVendor": vendor,
        "canonicalAlias": vendor,
        "aliasKey": f"vendor-{index % 50:02d}",
        "category": f"Category {index % 8}",
        "subcategory": "",
        "vendorMatchSource": "benchmark",
        "timestamp": f"2026-{month:02d}-{day:02d}T10:00:00+05:30",
        "year": 2026,
        "month": month,
        "title": "benchmark",
        "sender": "benchmark@example.test",
    }


if __name__ == "__main__":
    raise SystemExit(main())
