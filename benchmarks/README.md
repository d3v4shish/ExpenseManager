# ExpenseManager benchmarks

Run deterministic analytics profiling without touching user data:

```bash
uv run python benchmarks/profile_analytics.py --rows 10000 --profile build/analytics-10k.prof
uv run python benchmarks/profile_analytics.py --rows 100000 --profile build/analytics-100k.prof
uv run python benchmarks/profile_analytics.py --rows 1000000 --profile build/analytics-1m.prof
uv run python benchmarks/profile_analytics.py --rows 1000000 --measure-memory
```

Release latency budgets on the reference runner are 100k insight recomputation under two seconds and 1M under fifteen seconds. Record the JSON output with release artifacts.

Reference result on 2026-08-17:

| Rows | Analytics time | Traced peak allocation |
| ---: | ---: | ---: |
| 100,000 | 1.19 s | not traced |
| 1,000,000 | 14.16 s | not traced |
| 1,000,000 | 64.46 s with tracing | 1,254,446,015 bytes |

`tracemalloc` intentionally runs as a separate diagnostic because allocation tracing changes latency substantially. The million-row memory result is a scalability boundary: normal account-sized datasets are well below it, while future very large ledgers should move analytics loading to a dictionary-encoded columnar representation.
