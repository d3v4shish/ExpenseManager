# Benchmarks

Provider measurements use generated fixtures only; no user files or runtime data are read.

```bash
uv run python benchmarks/profile_sources.py --provider csv --records 10000
uv run python benchmarks/profile_sources.py --provider csv --records 100000
uv run python benchmarks/profile_sources.py --provider sms --records 10000
uv run python benchmarks/profile_sources.py --provider sms --records 100000
uv run python benchmarks/profile_sources.py --provider eml --records 10000
uv run python benchmarks/profile_sources.py --provider eml --records 100000
uv run python benchmarks/profile_sources.py --provider csv --records 100000 --measure-memory
```

The benchmark reports first import, repeat import, SQLite size, and optional traced peak allocation. Results are machine-specific; record the command output with a release rather than treating a timing as a product promise.

Analytics benchmarking remains available through `benchmarks/profile_analytics.py`; see `benchmarks/README.md`.

CLI report benchmarks use a deterministic synthetic ledger. Fixture creation is excluded from each reported query time:

```bash
uv run python benchmarks/profile_reports.py --rows 10000
uv run python benchmarks/profile_reports.py --rows 100000
uv run python benchmarks/profile_template_miner.py --records 10000
```

The report benchmark measures bounded dashboard, analytics, transaction-page, vendor-list, and diagnostic queries. Capture the JSON result alongside a release; it is a baseline, not a cross-machine performance promise.

Measured on 2026-09-09 with the deterministic fixture (fixture construction excluded from the query timings):

| Rows | Dashboard | Analytics | Transaction page | Vendor list | Diagnostics |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 10,000 | 0.317 s | 0.198 s | 0.005 s | 0.004 s | 0.001 s |
| 100,000 | 3.477 s | 2.326 s | 0.011 s | 0.049 s | 0.001 s |

The initial report baseline loaded all ledger rows once more to calculate the previous-month trend. Replacing that pass with a SQLite month aggregate reduced the 100k dashboard from 4.383 s to 3.477 s and analytics from 2.973 s to 2.326 s in the same deterministic run. These are local measurements, not a hardware-independent claim.

Measured on 2026-09-09 with the commands above (no allocation tracing):

| Provider | Records | First import | Repeat import | SQLite bytes |
| --- | ---: | ---: | ---: | ---: |
| CSV | 10,000 | 0.435 s | 0.371 s | 15,040,512 |
| SMS XML | 10,000 | 0.395 s | 0.335 s | 9,904,128 |
| EML | 10,000 | 2.050 s | 1.980 s | 9,977,856 |
| CSV | 100,000 | 4.404 s | 3.896 s | 148,684,800 |
| SMS XML | 100,000 | 3.944 s | 3.642 s | 97,300,480 |

The 100k EML fixture is intentionally much more I/O-heavy (100k files and MIME parses); run its command on the release target before setting an EML capacity budget. No performance improvement is claimed from these baseline measurements.

Reconciliation validation baseline, measured on 2026-09-13 with the same generated 10k fixtures after adding source snapshot bookkeeping (no allocation tracing):

| Provider | First import | Repeat import | SQLite bytes |
| --- | ---: | ---: | ---: |
| CSV | 0.466 s | 0.371 s | 14,311,424 |
| SMS XML | 0.405 s | 0.356 s | 9,424,896 |

These timings cover normalized provider ingestion and persisted source-record bookkeeping, not full parser/materialization reconciliation. They are a local regression baseline only; this change makes no performance-improvement claim.

Template mining uses an in-memory deterministic fixture and excludes fixture creation:

```bash
uv run python benchmarks/profile_template_miner.py --records 10000
```

Measured on 2026-09-13: 10,000 messages clustered into 8 masked templates in `0.070 s`. The miner runs only in a worker or headless CLI command; this is a local baseline, not a hardware-independent promise.
