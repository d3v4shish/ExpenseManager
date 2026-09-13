"""Deterministic local provider scan/storage benchmark; no user runtime data is read."""

from __future__ import annotations

import argparse
import json
import tempfile
import time
import tracemalloc
from pathlib import Path

from src.expenses.repositories.expenses_repository import ExpensesRepository
from src.expenses.sources.providers import CsvProvider, EmlFolderProvider, SmsBackupProvider


def run(provider_name: str, records: int, *, measure_memory: bool = False) -> dict:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        config, provider = _fixture(provider_name, root, max(1, records))
        repository = ExpensesRepository(root / "expenses.db")
        if measure_memory:
            tracemalloc.start()
        started = time.perf_counter()
        _store_records(repository, provider(config))
        first_elapsed = time.perf_counter() - started
        started = time.perf_counter()
        _store_records(repository, provider(config))
        repeat_elapsed = time.perf_counter() - started
        peak = 0
        if measure_memory:
            _current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
        return {
            "provider": provider_name,
            "records": records,
            "firstImportSeconds": first_elapsed,
            "repeatImportSeconds": repeat_elapsed,
            "sourceRecords": repository.count_source_records(),
            "databaseBytes": (root / "expenses.db").stat().st_size,
            "peakMemoryBytes": peak,
        }


def _store_records(repository: ExpensesRepository, provider, *, batch_size: int = 500) -> None:
    iterator = provider.iter_records() if callable(getattr(provider, "iter_records", None)) else iter(provider.fetch_records())
    batch = []
    for record in iterator:
        batch.append((record, [], []))
        if len(batch) >= batch_size:
            repository.upsert_source_record_parse_results(batch)
            batch.clear()
    if batch:
        repository.upsert_source_record_parse_results(batch)


def _fixture(provider_name: str, root: Path, records: int):
    if provider_name == "csv":
        path = root / "transactions.csv"
        rows = ["Date,Amount,Direction,Description,Reference"]
        rows.extend(f"2026-01-01T00:{index % 60:02d}:00+00:00,{index + 1}.00,debit,Vendor {index},REF{index}" for index in range(records))
        path.write_text("\n".join(rows), encoding="utf-8")
        return ({"id": "bench", "type": "csv", "path": str(path), "mapping": {"date": "Date", "amount": "Amount", "direction": "Direction", "description": "Description", "reference": "Reference"}}, CsvProvider)
    if provider_name == "sms":
        path = root / "backup.xml"
        messages = "".join(f'<sms _id="{index}" address="AXISBK" body="A/c XX1234 debited INR {index + 1}.00 to Vendor UPI Ref: REF{index}" date="1767225600000" />' for index in range(records))
        path.write_text(f"<smses>{messages}</smses>", encoding="utf-8")
        return ({"id": "bench", "type": "sms_backup", "path": str(path)}, SmsBackupProvider)
    if provider_name == "eml":
        folder = root / "mail"
        folder.mkdir()
        for index in range(records):
            (folder / f"{index}.eml").write_text(f"From: alerts@example.test\nSubject: Alert {index}\nDate: Thu, 01 Jan 2026 00:00:00 +0000\n\nA/c XX1234 debited INR {index + 1}.00", encoding="utf-8")
        return ({"id": "bench", "type": "eml_folder", "path": str(folder)}, EmlFolderProvider)
    raise ValueError("provider must be csv, sms, or eml")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=("csv", "sms", "eml"), required=True)
    parser.add_argument("--records", type=int, default=10_000)
    parser.add_argument("--measure-memory", action="store_true")
    arguments = parser.parse_args()
    print(json.dumps(run(arguments.provider, arguments.records, measure_memory=arguments.measure_memory), sort_keys=True))
