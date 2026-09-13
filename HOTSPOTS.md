# Hotspots

- Provider scanning and parsing run in background workers; do not move file I/O or parsing to the GUI thread.
- Batch source-record writes and transaction materialization before raising source limits.
- Duplicate matching is bounded by a 15-minute window and 100 candidate comparisons per transaction.
- EML fixtures incur one filesystem open and MIME parse per file; large folders stream records into 100-record repository batches, remain in worker jobs, and should use include/exclude patterns to reduce avoidable I/O.
- CSV and SMS parsing currently materialize one normalized record per source row/message before batched SQLite writes; benchmark duplicate-heavy files before increasing batch sizes or source limits.
- A changed CSV, SMS backup, EML folder, or Axios archive performs a complete snapshot scan so removed source records can be reconciled correctly. Keep those scans in worker jobs; the fingerprint no-op path avoids this work when the source is unchanged.
- Exact-identity reconciliation groups parsed facts in memory during ledger materialization and persists only conflicts. Large multi-source imports should be benchmarked with both matching and intentionally conflicting references before increasing source limits.
- Template mining is bounded to 2,000 stored email candidates and 96 normalized tokens per message. It must remain an explicit worker/CLI operation, never an ingestion-thread or GUI-thread activity; its cluster result contains masked templates and at most five source-record IDs per cluster.
- Cancellation is checked between normalized records, not during a provider's single-file decode/MIME parse. Very large individual archives remain an I/O and cancellation-latency boundary.
- Dashboard and analytics reports aggregate the active local ledger to preserve parity with the GUI. The prior-month trend uses SQLite month aggregation rather than a third full-ledger pass; keep dashboard payloads bounded and use paginated transaction/vendor/diagnostic endpoints instead of adding an unbounded export path.
