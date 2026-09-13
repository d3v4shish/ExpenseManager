"""Deterministic CPU benchmark for local masked message-template clustering."""

from __future__ import annotations

import argparse
import json
import time

from src.expenses.email.template_miner import BankAlertTemplateMiner


def run(records: int) -> dict:
    """Mine one deterministic synthetic alert set; fixture generation is excluded from timing."""

    count = max(1, int(records))
    fixture = [
        {
            "sourceRecordId": index + 1,
            "externalId": f"mail-{index + 1}",
            "sender": f"alerts@bank{index % 8}.example",
            "payload": {
                "subject": "Transaction alert",
                "textBody": (
                    f"Your A/c XX{1000 + index % 100:04d} has been debited for INR {index + 1}.00 "
                    f"at Merchant {index % 250}. UPI Ref: REF{index + 100000}"
                ),
            },
        }
        for index in range(count)
    ]
    miner = BankAlertTemplateMiner()
    started = time.perf_counter()
    result = miner.mine(fixture, min_support=2)
    elapsed = time.perf_counter() - started
    return {
        "records": count,
        "elapsedSeconds": elapsed,
        "templateCount": len(result["templates"]),
        "algorithm": result["algorithm"],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--records", type=int, default=10_000)
    arguments = parser.parse_args()
    print(json.dumps(run(arguments.records), sort_keys=True))
