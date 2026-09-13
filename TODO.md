# Dual-mode binary and complete CLI JSON reporting

## Fixed setup entry points

- [x] Move source selection and Thunderbird configuration out of the scrolling dashboard.
  - Contract: persistent top-window **SOURCES**, **MAIL CFG**, and **SETTINGS** open their management flows in closable popups; none scroll, insert, or resize the dashboard.
  - Validation: offscreen UI test verifies the top-level actions and popup entry points; manual launch confirms the dashboard stays in place while each dialog opens and closes.
- [x] Exercise every file-based source with fresh sample data.
  - Contract: a clean runtime can rebuild from an EML folder, mapped CSV, Android SMS backup, and checksummed AxiosAlternative archive in one pass.
  - Validation: deterministic integration fixture verifies provider validation, source-record retention, materialized transactions, journals, and zero provider errors.
- [x] Make local-source setup type-safe and self-explanatory.
  - Contract: selecting an EML folder derives an `eml-…` source ID, hides CSV-only rows, and rejects a source ID that visibly identifies a different provider type.
  - Validation: offscreen source-dialog tests cover generated EML IDs, conditional-row visibility, and conflicting provider-ID rejection.
- [x] Discover Thunderbird profiles across readable `/home` directories.
  - Contract: Mail Configuration searches only known profile locations and automatically selects the first profile with a usable IMAP account; it never reads mailbox contents during discovery.
  - Validation: deterministic multi-home fixture proves profiles from separate home directories are found, while single-home discovery remains scoped for tests.

## Review-first message template mining

- [x] Add a deterministic, local Drain-style template miner.
  - Contract: stored email candidates are normalized, volatile financial values are masked, and structurally similar messages are clustered by sender without network access, mutable global state, or automatic parser activation.
  - Validation: fixed fixtures produce stable template keys, wildcard templates, support counts, and bounded source-record evidence.
- [x] Turn mined clusters into editable bank-rule drafts only.
  - Contract: a mined template suggests sender/subject matching and fills the existing declarative rule scaffold, but cannot alter active rules until the user reviews and saves the local override JSON.
  - Validation: tests ensure the draft is valid, unique, reviewable, and inactive until explicitly saved.
- [x] Expose template mining in the GUI and CLI JSON surface.
  - Contract: Mail Debug mines in a worker, shows safe template/support metadata, and inserts a selected draft; CLI provides versioned `templates mine` and `templates draft` output without message bodies.
  - Validation: offscreen UI, CLI, and privacy tests cover empty data, selection, generated drafts, deterministic ordering, and body exclusion.
- [x] Document the local template-mining model and limits.
  - Contract: README/architecture distinguish this review-first Drain-style clustering from a full adaptive Drain3 parser and state that no generated rule is automatically active.
  - Validation: documented commands match `--help` and capability output.

## Multi-source reconciliation

- [x] Reconcile complete local-source snapshots on refresh.
  - Contract: a successful changed CSV, SMS backup, EML folder, or Axios archive scan removes retained records that no longer exist in that source; Thunderbird remains incremental and is never pruned from a partial scan.
  - Validation: deterministic tests cover a deleted source row, a changed historical row, cancellation safety, and a no-op repeat refresh.
- [x] Preserve exact transaction identity while surfacing conflicts.
  - Contract: bank, account, direction, transaction reference, and normalized transaction minute form the strong identity; equal values share one ledger entry and provenance, while differing amount/currency/merchant values create a reviewable conflict instead of silently overwriting a transaction.
  - Validation: deterministic tests assert stable identity, one exact-match transaction with all source links, and no silent last-write-wins result for conflicting sources.
- [x] Add manual and opt-in automatic conflict resolution.
  - Contract: unresolved conflicts retain all candidates and show one clearly marked provisional ledger value; users can select a candidate, or explicitly enable provider-priority auto resolution using configured source order.
  - Validation: repository, service, CLI JSON, and GUI tests cover manual choice persistence, provider-priority resolution, re-materialization, and conflict cleanup after a source changes or is removed.
- [x] Expose reconciliation safely in the Sources GUI and CLI.
  - Contract: Sources displays conflict count and candidate evidence before a decision; CLI has versioned JSON list, resolve, and policy commands; Validate remains read-only and does not persist UI-derived fields.
  - Validation: GUI/offscreen and CLI tests assert read-only validation, bounded conflict rows, action availability, and JSON contract stability.
- [x] Document reconciliation limits and template-mining status.
  - Contract: README/architecture describe snapshot retention, conflict policy, and that regex/script parsing exists but Drain3-style template mining is not yet implemented.
  - Validation: documentation commands and capability report identify the supported behavior without implying live sync or automatic learning.

## Baseline and dispatch

- [x] Record baseline report-query performance on fixed 10k/100k fixtures.
  - Acceptance: dashboard, ledger page, vendor detail, diagnostics, and source-query measurements plus hotspots are recorded before report optimizations.
- [x] Add deterministic root CLI dispatch.
  - Acceptance: no argument and `gui` start the desktop app; help/version/report/source/duplicate commands are headless; unknown arguments return usage errors.
- [ ] Validate the Windows GUI-and-CLI binary.
  - Acceptance: `ExpenseManager.exe --help` and report JSON work in PowerShell; Explorer launch opens the GUI without a visible console.
- [x] Package and validate the Linux GUI-and-CLI binary.
  - Acceptance: the packaged binary prints help, dashboard JSON, and Thunderbird guidance in a terminal.
- [x] Implement text and JSON CLI contracts.
  - Acceptance: text is the default, `--format json` works at any command position, JSON is versioned, diagnostics use stderr, and exit codes are tested.

## Safe GUI-data reports

- [x] Create versioned report DTOs shared by GUI and CLI.
  - Acceptance: reports return stable IDs, numeric values, timestamps, filter echoes, and pagination metadata without reading widget state.
- [x] Add dashboard and overview reports.
  - Acceptance: period totals, credits/net, ignored/unread counts, source health, top merchant, recent transaction summaries, and trend match fixed GUI fixtures.
- [x] Add complete analytics reports.
  - Acceptance: selected-period metrics, daily/weekly/monthly series, previous-`X`-month trends, top merchants, category totals, recurring patterns, and insight summaries are deterministic.
- [x] Add ledger and provenance reports.
  - Acceptance: paginated transaction lists, day groups, group rows, details, and source provenance honor all filters and pagination boundaries.
- [x] Add vendor, alias, and category reports.
  - Acceptance: catalog/search/detail, merges, categories, aggregates, series, suggestions, and transaction links equal GUI drill-down values.
- [x] Add Insight Inbox reports.
  - Acceptance: filtered/paginated insights, details, lifecycle state, confidence, algorithm metadata, and evidence transaction summaries equal the GUI Inbox.
- [x] Add source, account, diagnostic, settings, storage, and capability reports.
  - Acceptance: every safe GUI-visible value is JSON-accessible and GUI-only operations include precise setup guidance.
- [x] Enforce raw-data exclusion.
  - Acceptance: email/SMS bodies, source payloads/revisions, raw rules, and raw configuration never appear in a report, including details.

## CLI actions, documentation, and validation

- [x] Preserve source/duplicate management actions and add GUI-setup guidance.
  - Acceptance: existing actions return text/JSON; `sources setup-status` and `sources add-thunderbird` guide GUI setup without mutating data.
- [x] Document binary invocation and the complete CLI/report surface.
  - Acceptance: README, BUILD, architecture, benchmarks, and hotspots document every command, option, schema, privacy boundary, and GUI-only capability.
- [x] Add deterministic report and Linux package tests.
  - Acceptance: contract/privacy/filter/pagination/exit-code tests pass; Linux binary smoke tests pass; Windows package instructions include equivalent smoke coverage.
- [x] Benchmark final report queries.
  - Acceptance: fixed 10k/100k dashboard, analytics, transaction-page, vendor-list, and diagnostics measurements are in BENCHMARKS/HOTSPOTS.
- [ ] Complete the outstanding 100k EML/provider benchmark suite.
  - Acceptance: fixed 10k/100k provider/re-import measurements record CPU, memory, I/O, database growth, and responsiveness in BENCHMARKS/HOTSPOTS.
