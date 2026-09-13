# ExpenseManager

ExpenseManager is a local-first PyQt desktop workstation for importing bank alerts from Thunderbird, `.eml` folders, mapped CSV files, Android SMS backups, and versioned AxiosAlternative archives. It maintains a categorized expense ledger and explainable spending insights; runtime data stays on the user's computer.

## Highlights

- Dense, flat desktop UI with virtualized transaction and insight tables.
- Incremental local-source ingestion with candidate diagnostics, provenance, editable bank rules, and explicit duplicate review.
- Local, review-first message-template mining for repeated bank-alert emails.
- Auditable cross-source reconciliation: exact reference conflicts stay visible until a user chooses a value or enables an explicit source-priority policy.
- Per-account SQLite ledgers plus a shared vendor/category catalog.
- Insight Inbox for possible duplicates, unusual amounts, month-to-date spikes, and recurring-payment changes or misses.
- Persisted `new`, `read`, `dismissed`, and `resolved` insight state with evidence transactions.
- GUI-accessible settings, file-by-file storage accounting, cleanup, backup, restore, and uninstall retention choices.
- Versioned SQLite migrations protected by automatic rollback snapshots.
- Rotating logs and opt-in desktop notifications.

Analytics are deterministic and fully local. They do not send financial data to an external service and are intended as review prompts, not financial advice.

## Development

Python 3.11+ and [uv](https://docs.astral.sh/uv/) are required.

```bash
uv sync --frozen --extra dev
uv run python main.py
```

Run the complete test suite headlessly:

```bash
QT_QPA_PLATFORM=offscreen uv run python -m unittest discover -s tests -v
```

## CLI and compiled binary

The same `ExpenseManager` binary opens the desktop app with no arguments and runs a headless CLI when given a command. Development invocations use `uv run python main.py`; packaged Linux installs use `expense-manager`, and packaged Windows builds use `ExpenseManager.exe`.

```bash
# GUI (the default)
expense-manager
expense-manager gui

# Discover every command and emit machine-readable JSON
expense-manager --help
expense-manager report dashboard --format json
```

The CLI prints readable, indented output by default. Add `--format json` before or after any command for a stable JSON document. Success uses stdout; errors use stderr. Exit codes are `0` success, `1` provider/validation failure, `2` invalid input, and `3` GUI setup required.

Recommended local-source sequence:

```bash
uv run python main.py sources list --format json
uv run python main.py sources add-eml --id archived-mail --path /data/mail --format json
uv run python main.py sources add-csv --id bank-csv --path /data/transactions.csv --mapping-json '{"date":"Date","amount":"Amount","direction":"Direction","description":"Description"}' --format json
uv run python main.py sources add-sms-backup --id phone-sms --path /data/sms-backup.xml --format json
uv run python main.py sources add-axios-archive --id phone-export --path /data/phone-export.zip --format json
uv run python main.py sources validate --format json
uv run python main.py sources rebuild --format json
uv run python main.py sources inspect --id bank-csv --limit 100 --format json
uv run python main.py reconciliation list --format json
# Copy a candidate signature from the preceding JSON, then choose it explicitly.
uv run python main.py reconciliation resolve --key RECONCILIATION_KEY --signature CANDIDATE_SIGNATURE --format json
# Optional: use one configured provider first for future exact-identity conflicts.
uv run python main.py reconciliation set-policy --mode provider_priority --prefer-provider phone-export --format json
uv run python main.py templates mine --min-support 2 --format json
# Copy a template key from the previous JSON; this prints an unsaved rule draft.
uv run python main.py templates draft --key TEMPLATE_KEY --format json
uv run python main.py duplicates list --format json
```

Use `sources refresh` for routine incremental imports and `sources rebuild` for a full historical scan. `sources remove --confirm` removes only configuration; `sources delete-data --confirm` removes retained imported records and rematerializes the ledger. `sources set-enabled --enabled|--disabled` toggles one configured source.

`reconciliation list` returns every disagreement between source records that share the same normalized bank, account, direction, transaction reference, and transaction minute. Matching values share one ledger row and all provenance links. A disagreement in amount, currency, or merchant is kept as one clearly marked provisional row to prevent double counting. `reconciliation resolve` selects a candidate explicitly. The default policy is `manual`; `provider_priority` is opt-in and uses the configured priority order, with `--prefer-provider` moving one configured source to the front. A manual choice remains authoritative if that candidate still exists.

`sources setup-status` identifies CLI-ready providers. EML folders, mapped CSV, Android SMS backups, and AxiosAlternative archives can be added from the CLI. Thunderbird profile/account selection is deliberately GUI-only: run `ExpenseManager gui`, open **Mail Configuration**, choose the profile/account, then run `sources validate` or `sources refresh`. `sources add-thunderbird` is a non-mutating guidance command and exits `3`. Reconciliation configuration and resolutions work in both the Sources pane and the CLI.

Mail Configuration automatically checks known Thunderbird profile locations for the current user and readable `/home/<user>` directories, then selects the first profile with a usable IMAP account. This discovery reads profile metadata (`prefs.js`) and checks mailbox paths only; it does not scan email messages until an import is requested. Use **Find Profiles** to repeat the search or **Browse** to choose a profile manually.

`templates mine` clusters stored email candidates into masked repeated-message templates. It is local-only and emits sender-domain hints, support counts, safe templates, and bounded source-record IDs—never message bodies. `templates draft` turns one current template into an unsaved editable declarative bank-rule draft. Review its match/extraction fields, validate it in **Mail Debug**, and use **Save Overrides** to activate it; mining never enables or changes a rule by itself.

### JSON reports

All safe, user-visible GUI data is also available through `report` commands:

```bash
expense-manager report dashboard --months 6 --top 10 --format json
expense-manager report overview --format json
expense-manager report analytics --year 2026 --month 9 --months 12 --top 10 --format json
expense-manager report transactions list --year 2026 --month 9 --limit 200 --offset 0 --format json
expense-manager report transactions groups --year 2026 --month 9 --format json
expense-manager report transactions group --year 2026 --month 9 --group-key 2026-09-09 --format json
expense-manager report transactions get --key TRANSACTION_KEY --format json
expense-manager report vendors list --query cafe --format json
expense-manager report vendors get --identity CAFE --format json
expense-manager report insights list --status new --severity high --format json
expense-manager report insights get --key INSIGHT_KEY --format json
expense-manager report sources --format json
expense-manager report account --format json
expense-manager report diagnostics --provider-id thunderbird_local --format json
expense-manager report settings --format json
expense-manager report storage --format json
expense-manager report capabilities --format json
```

`dashboard` is bounded and includes overview, selected-period analytics, previous-month trend, top merchants, insight count, sources, and storage. Ledger, insights, vendors, diagnostics, and source records are separate bounded/paginated queries. Every report includes `schemaVersion`, `report`, `generatedAt`, `filters`, and `data`.

Reports intentionally never include original email/SMS bodies, source payloads or revisions, raw bank-rule JSON, or raw configuration files. GUI-only editing, backup/restore, cleanup, vendor changes, and parser-rule changes are listed by `report capabilities`; they are not automated by this CLI pass.

`report sources` includes the same safe reconciliation policy and conflict summary shown in the Sources pane. Conflict candidates expose only normalized values, source IDs, and record IDs—not original message bodies or source payloads.

In the desktop Sources pane, CSV setup includes a bounded background preview of detected columns and normalized sample rows before the mapping is saved. Ambiguous non-ISO dates require an explicit `dateFormat` mapping (for example `"%d/%m/%Y"`).

SMS imports accept Android SMS Backup & Restore XML, plus JSON containing either `messages` or `sms`; each item needs sender/address, body, and timestamp/date. A JSON item may use a `parts` array for one explicitly grouped multipart message.

Source removal deletes configuration only and requires `--confirm`; it never deletes retained transactions. All imports are local and offline. AxiosAlternative archives are imports, not device pairing or sync.

The Sources pane separately offers **Delete Imported Data** after confirmation. It removes only the selected provider's retained records, revisions, and derived ledger entries, then rebuilds from the remaining sources; it never deletes the original file or its configuration. Per-provider local feature flags live in `email_accounts.json.sourceFeatures` and default to enabled.

CSV, SMS backup, EML-folder, and Axios archive sources are complete local snapshots: after a successful scan caused by a source change, retained records that no longer exist in that source are removed. Thunderbird remains incremental and is never pruned from a partial mailbox scan. A cancellation never performs snapshot pruning.

An AxiosAlternative archive is a ZIP containing `manifest.json`, `transactions.json`, and `source_sms_identities.json`. Its version-1 manifest includes `formatVersion: 1`, a non-empty `parserVersion`, and SHA-256 values for both JSON payload files. ExpenseManager rejects an archive before import when this contract or either checksum is invalid.

## Parser learning limits

ExpenseManager has declarative regex bank rules, rule templates, optional trusted local Python extractors, and a deterministic local Drain-style template miner. The miner masks volatile values, clusters structurally similar stored email candidates, and proposes review-only drafts. It is deliberately smaller than a full adaptive Drain3 pipeline: it does not learn online during ingestion, persist raw message clusters, infer extraction regexes, or activate generated rules automatically. Parsing and reconciliation remain deterministic and locally controlled.

Run the deterministic analytics benchmark:

```bash
uv run python benchmarks/profile_analytics.py --rows 100000
uv run python benchmarks/profile_analytics.py --rows 100000 --profile build/analytics-100k.prof
uv run python -m pstats build/analytics-100k.prof
```

Fixture creation is excluded from the reported analytics time. Add `--measure-memory` for allocation tracing; it substantially increases runtime.

## Runtime data and privacy

Writable data uses per-user OS application directories rather than the checkout. Set `EXPENSE_MANAGER_HOME` to redirect all writable files for development or tests:

```bash
EXPENSE_MANAGER_HOME=/tmp/expense-manager-runtime uv run python main.py
```

The runtime roots are:

- `config/`: application, analytics, logging, notification, and mail settings
- `data/`: account and vendor SQLite databases
- `state/`: derived JSON views
- `cache/`: disposable caches
- `logs/`: rotating logs

Open **Settings & Storage** in the application to see every created file and its size, remove generated state/cache/log files, delete selected databases, or prepare for uninstall. Database deletion and uninstall always require an explicit keep/delete decision.

Backups use the `.expensemanager-backup` extension. They contain a manifest, checksums, configuration, and consistent SQLite snapshots. Backups are intentionally plain, unencrypted ZIP archives; store them accordingly. Restore validates paths and checksums and uses staged directory swaps with rollback protection.

## Packaging

Build the Ubuntu/Debian x86-64 package on Linux:

```bash
./scripts/package_linux.sh
```

This produces `dist/expense-manager_0.2.0_amd64.deb` and its SHA-256 file. The package includes the executable, application icon, desktop entry, launcher, and retention-aware uninstall launcher. Direct package-manager removal leaves per-user data untouched; the uninstall launcher asks whether to retain databases/config first.

Build the Windows x64 executable directory and, when Inno Setup is installed, the installer:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\package_windows.ps1
```

The deterministic build uses `uv.lock`. PyInstaller produces `dist\ExpenseManager\ExpenseManager.exe`; Inno Setup produces `dist\installer\ExpenseManager-0.2.0-Setup-x64.exe`. The Windows uninstaller launches the same retention dialog before package removal.

After a Linux package build, verify the binary contract with:

```bash
EXPENSE_MANAGER_HOME=/tmp/expense-manager-smoke dist/ExpenseManager/ExpenseManager --help
EXPENSE_MANAGER_HOME=/tmp/expense-manager-smoke dist/ExpenseManager/ExpenseManager report dashboard --format json
```

## Logs

Rotating logs live under the resolved `logs/` root. Log level, file size, and retention count are editable in Settings. `NOC_LOG_LEVEL=DEBUG` can override the startup level for troubleshooting.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for component and data-flow details.
