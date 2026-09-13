# Expense Manager Architecture

## Overview

This app is a standalone PyQt desktop expense manager.
It keeps repo-owned code, assets, defaults, and card definitions read-only. Writable runtime data lives in per-user OS app directories resolved by `src/app/files.py`:

- `config`: app config such as email accounts, Thunderbird selection, parser overrides, and script extractor config
- `data`: SQLite databases
- `state`: derived JSON snapshots
- `cache`: local caches
- `logs`: rotating logs

Set `EXPENSE_MANAGER_HOME` to force all five writable roots under one development/test directory.

The window mounts seven cards:

- overview: `panel.expenses`
- Thunderbird account config: `panel.expenses_config`
- local source management: `panel.expenses_sources`
- mail debug: `panel.expenses_debug`
- detailed analysis: `panel.expenses_tab`
- Insight Inbox: `panel.expense_insights`
- Settings & Storage: `panel.expenses_settings`

## Headless CLI and reports

`main.py` starts the GUI only with no arguments or the explicit `gui` command. `report`, `sources`, `duplicates`, `reconciliation`, `templates`, help, and version commands are dispatched to `src/app/cli.py` before UI imports. `ReportService` (`src/app/reports.py`) returns versioned JSON envelopes with bounded dashboard, analysis, ledger, vendor, insight, source, account, diagnostic, settings, storage, and capability data. The service reads domain services/repositories rather than Qt widgets, so the report contract remains usable in a terminal-only runtime. `templates mine` and `templates draft` use the same local mining service as Mail Debug and return masked templates/drafts, never a message body.

Reports omit original message bodies, source payloads/revisions, raw bank-rule text, and raw configuration files. Thunderbird account selection remains GUI-only; the CLI exposes setup status and exact GUI guidance. Source/duplicate management retains explicit confirmation for destructive operations.

The overview and analysis cards are derived from the same expense state payloads, so any mutation that changes expense rows or vendor mappings must refresh both cards. The config and debug cards stay mounted for reload consistency, but both are hidden by default and opened from the configuration flow instead of living permanently in the main scroll path.

## File Map

- `main.py`
  Launch entrypoint.
- `src/app/run.py`
  App bootstrap and logging setup.
- `src/app/logging_utils.py`
  Shared rotating file logging and uncaught-exception hook setup.
- `src/app/settings.py`
  Validated, atomically persisted application and analytics settings.
- `src/app/data_management.py`
  Storage inventory, checksummed plain backup/restore, cleanup, and retention operations.
- `src/app/uninstall.py`
  Standalone retention prompt used by packaged uninstallers.
- `src/app/window.py`
  Main window shell, card mounting, async job execution, and targeted card reload orchestration.
- `src/app/ui_kit/controls.py`
  Shared labels, buttons, pills, cards, and UI-kit theme configuration.
- `src/app/ui_kit/dialogs.py`
  Shared themed `QMessageBox` wrapper for warning and confirmation popups.
- `src/app/ui_kit/busy_overlay.py`
  Shared full-window blocking overlay used while long-running expenses jobs are active.
- `src/app/ui_kit/section_card.py`
  Shared titled section shell used by expense widgets.
- `src/app/files.py`
  Runtime path resolution plus JSON config/state stores.
- `src/expenses/bootstrap.py`
  Expense runtime assembly: repositories, services, jobs.
- `src/expenses/account_config.py`
  Thunderbird-provider config rewriting and per-account DB naming.
- `src/expenses/sources/`
  Versioned local provider registry: Thunderbird, EML folders, mapped CSV, documented SMS backups, and checksummed AxiosAlternative archives.
- `src/expenses/ui/sources_widget.py`
  Background-backed Sources grid, source setup dialog, read-only validation, import controls, duplicate-review queue, and exact-identity reconciliation queue.
- `src/expenses/ui/screen_api.py`
  UI-facing facade for expense and vendor actions.
- `src/expenses/ui/overview_widget.py`
  Compact overview card.
- `src/expenses/ui/config_widget.py`
  Thunderbird profile/account selection card plus account DB status UI.
- `src/expenses/ui/debug_table.py`
  Model/view data source used by the mail-debug candidate table.
- `src/expenses/ui/analysis_widget.py`
  Detailed ledger, vendor, chart, and mutation UI.
- `src/expenses/ui/analysis_workers.py`
  Background worker classes and vendor-detail aggregation logic used by the analysis pane.
- `src/expenses/email/thunderbird_profile.py`
  Thunderbird `prefs.js` inspector used to discover usable IMAP accounts and their default mailboxes.
- `src/expenses/email/bank_rule_catalog.py`
  Loader/merger for shipped bank email rules, user-local override JSON, and starter rule templates.
- `src/expenses/email/template_miner.py`
  Deterministic masked-token clustering used to produce review-only local message templates.
- `src/expenses/services/expense_service.py`
  Expense materialization, source reconciliation, derived payload generation, and vendor reconciliation.
- `src/expenses/services/template_mining.py`
  Shared GUI/CLI boundary for bounded template mining and unsaved rule-draft generation.
- `src/expenses/services/vendor_catalog.py`
  Vendor catalog service wrapper.
- `src/expenses/services/analytics.py`
  Deterministic, explainable local insight generation.
- `src/expenses/repositories/expenses_repository.py`
  Expense storage and derived query access.
- `src/expenses/repositories/vendor_catalog_repository.py`
  Vendor catalog persistence.
- `src/expenses/ui/insights_widget.py`
  Virtualized, asynchronously paged Insight Inbox and evidence view.
- `src/expenses/ui/settings_widget.py`
  Settings, file-level storage accounting, backups, cleanup, and uninstall retention UI.
- `src/expenses/ui/transaction_table.py`
  Recyclable model/view transaction table used by ledger drilldowns.

## Runtime Flow

1. `src/app/run.py` configures rotating file logging under the resolved `logs` directory before building the runtime.
2. `src/expenses/bootstrap.py` resolves the active Thunderbird account (when configured), binds the expenses repository to the active DB, and builds the source registry, parser chain, repositories, services, and async job specs.
3. If the active account has no dedicated DB yet but the shared app-data `expenses.db` exists, bootstrap copies that DB trio into the active account DB once and then uses the account DB going forward.
4. `src/app/window.py` loads `AppTheme`, configures the shared UI kit, runs startup tasks, and mounts overview/config/sources/debug/analysis/insight/settings widgets.
5. Widgets read prepared JSON-backed card data for overview/analysis, while config/debug pull live detail through `screen_api.py` for account and candidate-mail workflows.
6. Refresh, rebuild, analytics, backup, restore, cleanup, and storage scans run through worker threads and reload only the dependent cards.
7. While `expenses.refresh` or `expenses.rebuild` is active, the window can surface a blocking overlay with live sync status and captures input only for the blocking phases.

## UI Ownership

- `overview_widget.py` is read-only and only triggers background refresh/rebuild jobs.
- `config_widget.py` owns Thunderbird profile discovery, active-account switching, DB-status display, and save/update/rebuild affordances.
- `debug_widget.py` owns candidate-mail inspection, raw override editing, validation against a stored candidate, and the explicit reparse action.
  It also exposes the in-app entry point for appending a new regex-based bank rule template into the local override JSON.
- `analysis_widget.py` owns filters, ledger browsing, vendor inspection, and mutation controls.
- `insights_widget.py` owns filtering, pagination, evidence review, status changes, and manual recomputation.
- `settings_widget.py` owns validated settings and user-authorized data-management actions.
- `screen_api.py` is the boundary between widgets and domain logic.
- `window.py` also owns card visibility state so optional panes like the Thunderbird config and mail debug cards can stay hidden until requested, and it owns the blocking busy overlay for refresh/rebuild jobs.

## Thunderbird Account Config

- Thunderbird account selection is driven by `email_accounts.json` plus the local-only `local/thunderbird.json` in the resolved `config` directory.
- The app keeps exactly one managed Thunderbird provider:
  - `id: thunderbird_local`
  - `type: thunderbird`
- Saving a new account selection rewrites that managed provider, preserves `sync` and any non-Thunderbird providers, and keeps Thunderbird tuning fields that are not identity/path-specific.
- The local-only Thunderbird config stores the selected `profilePath` and intentionally clears local mailbox overrides so v1 stays in one-account auto-mailbox mode.
- Thunderbird account discovery parses real `prefs.js` mappings:
  - account -> identities
  - account -> server
  - server -> IMAP mail root
- The config card only lists usable IMAP identities with a resolvable default mailbox. Mailbox fallback order is:
  1. `[Gmail].sbd/All Mail`
  2. `[Google Mail].sbd/All Mail`
  3. `Inbox`
  4. `INBOX`
- The config card is not always visible in the scroll stack. It is toggled from the small `MAIL CFG` trigger in the overview status pane and scrolls into view when opened.
- The config card now also exposes the entry point to the hidden mail-debug pane, which keeps parser/rule troubleshooting out of the default main flow.

## Candidate Mail Storage And Rules

- Expense mail ingestion is now candidate-first rather than parse-first.
- A Thunderbird email becomes a stored candidate when it matches the configured sender/subject bank rules.
- Candidate emails are written into `expense_source_records` before parse success is known.
- Parse output is split into two persisted layers:
  - `expense_parse_attempts`
    stores parser diagnostics such as matched rule id, status, failure reason, and extracted preview fields
  - `expense_parsed_facts`
    stores normalized facts emitted from successful parses
- Candidate emails that fail parsing remain visible in the active account DB and in the debug pane; they are no longer silently dropped.
- Rebuild/full scan stores every sender/subject-matched candidate mail from the configured mailbox.
- Incremental refresh stores new/overlap candidate mails from the latest candidate checkpoint window.
- Incremental refresh should skip reparsing and rewriting candidate rows whose stored `content_hash` is unchanged; rule edits and explicit reparse/rebuild flows remain the path for historical reparsing.
- The provider checkpoint now advances from the latest candidate email seen, not only from successful parsed rows.
- Shipped bank rules stay read-only in:
  - `src/expenses/defaults/bank_email_rules.json`
- User-local rule edits live in `bank_email_rules.overrides.json` in the resolved `config` directory.
- `bank_rule_catalog.py` merges overrides onto shipped defaults by stable bank rule `id`.
- Override-only bank rule ids are appended after the shipped defaults, so users can add entirely new banks through the local override payload without editing repo-owned JSON.
- Shared mail-text normalization now repeatedly decodes nested HTML entities and strips HTML/style blocks before regex extraction so HTML-only bank alerts still reach the parser as plain searchable text.
- The debug pane exposes:
  - shipped defaults as raw read-only JSON
  - local overrides as raw editable JSON
  - the effective merged rule payload
- The debug pane also exposes `Add Bank Template`, which appends a starter declarative rule with match filters plus regex arrays for direction, amount, transaction id, counterparty, account suffix, and date/time.
- Trusted Python extractor scripts can be enabled through `script_extractors.json` in the resolved `config` directory.
  Scripts are loaded from user-defined paths at app startup, run after built-in parsers, and return normalized transaction dictionaries.
  The debug pane reports script load status and import errors.
- Saving overrides reparses stored candidates for the active account immediately. If the change broadens sender/subject matching, a rebuild is still required to backfill older historical mails that were never stored as candidates.

## Local Source Ingestion And Deduplication

- `SourceIngestionService` loads enabled provider definitions from `email_accounts.json`; providers expose validation, fingerprints, and record fetches, with optional `iter_records` streaming for large local folders. It records provider validation/fetch failures in `expense_import_journals` without silently skipping them.
- Every normalized source record has a persisted schema version (currently `1`), provider ID, record type, stable external ID, content hash, timestamp, source URI, and raw normalized payload. `expense_source_revisions` preserves changed content for the same identity.
- Built-in providers are local-only: Thunderbird, recursive EML folders (with include/exclude patterns), explicitly mapped CSV files, Android SMS Backup & Restore XML / documented JSON, and a checksummed AxiosAlternative archive (`manifest.json` + `transactions.json`, format version 1).
- CSV setup runs its delimiter/encoding/mapping preview in a widget worker and returns at most 25 normalized sample rows; it refuses ambiguous non-ISO dates until the user supplies `mapping.dateFormat`.
- CSV, SMS backup, EML-folder, and Axios archive providers use a complete local snapshot when their fingerprint changes. Every successfully scanned retained row is marked with the current import journal ID, then unseen records are pruned only after that scan completes. Thunderbird stays incremental and is never snapshot-pruned. Cancellation exits before pruning, so the old retained snapshot remains usable.
- The ledger stores many-to-one source provenance in `expense_transaction_sources`. A referenced transaction's strong identity is normalized bank name, account suffix, direction, transaction ID, and transaction minute. Equal source values share one canonical transaction and all source links. The database keeps a separate `expense_reconciliation_conflicts` record when sources with that identity disagree on amount, currency, or raw merchant. It records normalized candidates, selected value, policy outcome, and timestamps; it never silently last-write-wins.
- An unresolved exact-identity conflict creates one provisional ledger row with `reconciliationStatus: needs_review`, preventing double counting while keeping every source record as evidence. A user may select a candidate, producing `user_resolved`. Automatic choice is disabled by default; the explicit `email_accounts.json.reconciliation.exactConflictPolicy: provider_priority` policy uses its configured provider order and produces `auto_resolved`. A valid manual selection remains authoritative after later rematerialization.
- Bounded timestamp/vendor/amount matches between transactions without the same strong identity remain fuzzy duplicate review candidates; they are never silently merged. A user-marked fuzzy merge is reapplied on later materialization while retaining every source link.
- Import/rebuild, source validation, setup, and duplicate decisions use worker threads from the UI. Source removal only removes configuration; it does not delete retained transactions.
- The Sources pane exposes per-provider retained-record count and payload-storage estimate. Its separate destructive action deletes selected retained records/revisions only after confirmation, preserves the source definition/original file, then rematerializes the ledger from remaining facts.
- `email_accounts.json.sourceFeatures` provides local per-provider rollout flags. All built-in providers default enabled; a disabled provider receives a `disabled` import-journal record and is not scanned.
- `refresh_expenses(is_cancelled=...)` stops cooperatively between normalized records, retains already-written source records for idempotent retry, and leaves the derived ledger untouched. A cancelled full rebuild raises `SourceIngestionCancelled` and restores its SQLite snapshot rather than exposing a partially rebuilt ledger.
- AxiosAlternative archives are portable offline imports only. Device discovery, pairing, cloud upload, and live synchronization remain explicitly deferred.
- Parser learning is local and review-first: regex bank rules, rule templates, optional trusted Python scripts, plus bounded Drain-style clustering for stored email candidates. The miner masks values and non-financial text before returning templates, does not persist raw clusters, does not infer extractor regexes, and cannot activate a generated rule. It only creates an editable draft; the user must validate and save local overrides before parsing changes.

## Vendor Category Rules

- Vendor categories are treated as an alias-cluster property rather than a single-row property.
- A root vendor plus all of its merged descendants share one effective category set.
- The effective category set is the union of every category attached anywhere in that alias cluster.
- Category edits from the UI should rewrite the shared cluster category set, so changing categories on one vendor automatically applies to every vendor in the same alias cluster.
- The analysis UI should expose category editing from both places:
  - vendor editor
  - alias section
  Both paths mutate the same shared alias-cluster category set.

## Per-Account DB Layout

- The shared vendor catalog remains global at `data/databases/vendor_catalog.db`.
- Expense mail/source/fact/transaction state is account-specific under `data/databases/accounts/`.
- Account DB filenames are derived from normalized account email values.
- The active runtime repository can switch DB path in-process via `ExpensesRepository.switch_db_path(...)`; the mail-ingestion service follows automatically because it holds the same repository instance.
- Switching the active account immediately rewrites:
  - `state/expenses/expenses.json`
  - `state/expenses/expenses_tab.json`
  from the newly active account DB so cached data appears without restarting the app.

## Refresh And Rebuild Rules

- Cached account DBs use incremental refresh (`expenses.refresh`) from the last seen candidate mail plus the configured overlap window.
- A no-change refresh should stop after the mailbox scan when candidate hashes and the materialization version are already current; it should not recompute analytics or rewrite derived state just to confirm there were no updates.
- Empty/new account DBs do not auto-run a full scan on save or periodic refresh.
- First-time population of an empty account DB requires explicit rebuild (`expenses.rebuild`) from the config card.
- Rebuild performs the full mailbox rescan, stores all matched candidate mails, reparses them, and rolls back to the snapshot if the rebuild fails.
- Incremental refresh still uses `lookbackDays` as the safety fallback only when no provider checkpoint exists yet.
- The default sync block now treats:
  - `refreshMinutes`
    as the source of truth for the window-owned auto-refresh timer
  - `overlapHours`
    as the candidate-mail overlap window
- The shell no longer relies only on card JSON for `expenses.refresh` cadence; `window.py` resolves that timer from `email_accounts.json.sync.refreshMinutes`.

## Theme Structure

- `assets/theme/default_theme.json` is the shipped token source for colors, fonts, and surface radius.
- `assets/icons/expense_manager_matte.svg` is the editable source for the clean high-contrast matte app icon, `assets/icons/expense_manager_matte.png` is the shipped app/window icon, and `assets/icons/expense_manager_matte.ico` is used by the Windows PyInstaller executable. `assets/icons/expense_manager_matte_light.*` provides the inverted light-mode icon variant. Both variants use one muted shade in addition to black/off-white.
- The theme remains dark-only. There is no light theme or runtime theme switcher.
- The analysis pass added a few dark-role tokens that are reused instead of local literals:
  - `text_inverse`
  - `focus_ring`
  - `surface_panel`
  - `surface_panel_alt`
- The restrained follow-up pass adds shared state tokens so overview, analysis, and shared controls use the same dark interaction language:
  - `text_disabled`
  - `surface_active`
  - `surface_disabled`
  - `divider`
- `src/app/theme.py` exposes token reads through `hex()`, `color()`, and font helpers.
- `src/app/window.py` calls `configure_ui_kit_theme(...)` before mounting widgets so shared controls use the active theme.
- `src/app/window.py` also applies the shipped app icon to both `QApplication` and the main window when the icon asset is present. On Windows it sets a dedicated AppUserModelID before creating `QApplication` and prefers the `.ico` asset so taskbar grouping does not fall back to the Python executable icon.
- `src/app/ui_kit/controls.py` and `src/app/ui_kit/section_card.py` should be the first place to centralize shared button, label, pill, card, and section styling.
- Shared `SectionCard` headers now use a padded title band with slightly differentiated heading typography so major pane titles read distinctly from dense in-card labels.
- Shared `SectionCard` headers now use a heavier inset title band with subdued, engraved-looking typography so section titles stay readable without popping like action chrome.
- Standard warning and confirmation popups should go through `src/app/ui_kit/dialogs.py` so their body, buttons, and Windows title bar stay aligned with the dark app theme.
- Destructive confirmations that use `ThemedMessageBox` should explicitly mark their destructive button role so rebuild/delete actions keep red emphasis instead of inheriting the generic accent-primary button.
- Long-running blocking work should use `src/app/ui_kit/busy_overlay.py` rather than ad hoc per-card scrims when the intent is to pause the entire app window.
- `src/expenses/ui/analysis_widget.py` now uses file-local semantic helpers on top of `AppTheme` for dense analysis-only roles such as interactive text, debit/credit tones, destructive states, popup shells, and panel surfaces.
- `src/expenses/ui/overview_widget.py` now uses the same panel, divider, and button language as analysis instead of a separate matrix-specific visual style.
- `src/expenses/ui/debug_widget.py` follows the same dense desktop language but uses a split-pane layout: candidate table on the left, selected-mail detail and raw config editors on the right.
- The candidate table now runs through `QTableView` plus `debug_table.py` instead of `QTableWidget`, so large candidate lists no longer rebuild one widget item per cell on every reload.
- `src/expenses/ui/config_widget.py` should stay especially compact: row-form inputs, thin bordered controls, header-side saved-account status, and no redundant explanatory rows in the steady state.
- The config pane's DB summary belongs to the advanced path rather than the default short form. The default view should focus on profile/account/mailbox selection plus save/update actions; the `Advanced` toggle can reveal the DB summary and open the debug pane.
- Feature widgets can still pass explicit colors for data semantics, but normal presentation colors should resolve from theme tokens or those local semantic helpers rather than raw hex literals.

## Analysis Visual Conventions

- The analysis screen stays within the existing dark information architecture; this pass did not redesign workflows or add a light theme.
- The overview and analysis cards should now read as one feature-level visual system rather than two separate mini-themes.
- Primary analysis cards use flat, border-driven dark panels with restrained or square corners.
- Metric cards, chart cards, vendor sections, calendar cells, ledger tiles, and popups share the same surface ladder:
  - `surface_soft` / `surface_strong` for in-card surfaces
  - `surface_panel` / `surface_panel_alt` for denser popup and analysis shells
- Shared interactive states should come from `surface_active`, `surface_disabled`, `text_disabled`, and `divider` before introducing one-off variants.
- Shared `CardFrame` surfaces are now border-driven by default; ordinary overview and analysis cards should not depend on left accent strips for emphasis.
- Thin accent rails are reserved for a small number of high-priority summary cards where semantic color materially improves scanning.
- Shared buttons should stay dense and flat: smaller heights, tighter padding, and lighter border-driven emphasis closer to GTK desktop controls than oversized web-style buttons.
- Secondary row actions inside dense panes, such as merge/unmerge controls, should be especially compact so data remains primary and action chrome stays subordinate.
- Prefer calmer copy in dense panes: avoid repeating what a control already states, prefer human-facing titles over internal/debug labels, and keep steady-state helper text shorter than loading/error guidance.
- Dense alias/category metric strips should stay compact and uniform. If a metric also acts as the drilldown entry point, prefer making the top strip card clickable instead of rendering a second full-width duplicate card below the charts.
- Category drilldown charts should keep the monthly spend history as the primary full-width row, with yearly and breakdown charts grouped together beneath it instead of splitting the monthly history into a half-width card.
- The lower analysis block should stay visibly compact: top-vendor rows, right-side KPI cards, and month-calendar cells should prefer tighter padding, smaller text steps, and shorter row heights before adding more content or chrome.
- The month insight block should read like one flat GTK-style pane: `Top Vendors` should favor a dense grid/table treatment, and the month summary should prefer one unified facts table over several KPI mini-cards.
- Analysis sub-pane headers such as `Vendor Detail`, `Alias Mapping`, and `Category` should use the same subdued heading language rather than feature-specific accent colors; chart titles and other secondary headings should stay quieter than actionable controls.
- Alias/category metric cards and alias-merge cards should stay especially compact on sub-1080px layouts, and destructive secondary actions such as `Unmerge` should read as small right-aligned text actions rather than full-weight buttons.
- The overview chart strip should reflow by width instead of staying in one fixed shape: use one row only when the daily chart remains readable, fall back to a `1 + 2` split at medium widths, and stack charts at narrower widths instead of stretching them into oversized cards.
- The ledger date/search controls should live in the pane heading when width allows it. At `>=1000px`, keep year, month, search, and the ignored toggle grouped on the right side of the `Ledger Analysis` header; below that, stack the control group into two rows instead of leaving a long single-line filter strip.
- Month-calendar cells should prefer a one-line summary: day number plus spend for that day, with any row-count hint kept secondary. Do not expand each day cell into a mini ledger preview when a popup already provides the detailed drilldown.
- Destructive or non-obvious vendor actions should expose tooltip/help text, and destructive actions such as vendor deletion should confirm before execution.

## Background Work Rules

The following operations are intentionally off the GUI thread:

- mailbox refresh and rebuild jobs
- ledger group loading
- ledger row loading
- vendor detail loading
- vendor merge and unmerge actions
- vendor save and delete actions
- fuzzy alias accept actions that may create a vendor before merging
- ignore/restore transaction actions
- insight recomputation and page/detail loading
- settings writes and storage inventory scans
- backup, restore, cleanup, and uninstall-preparation work

The main thread should only coordinate state, render widgets, and dispatch background work.

## Analytics And Insight Lifecycle

- Analytics run locally after ingestion/materialization and can also be triggered from the Insight Inbox.
- Detection is deterministic and currency-isolated. Version `expense-insights-v1` covers possible duplicates, vendor-level unusual amounts, category/vendor month-to-date spikes, and recurring amount changes or missed charges.
- Amount outliers use a median and median absolute deviation with configurable floors, minimum history, and sensitivity.
- Recurrence uses observed intervals and rejects irregular series below the configured confidence threshold.
- Every insight contains a stable key, baseline, actual value, confidence, date period, and evidence transaction keys.
- Recomputing preserves `read` and `dismissed` choices. Findings that disappear become `resolved`; findings that recur retain their stable state.
- Insight rows are stored in the active account database. The table model fetches bounded pages and creates no per-row widgets.

## Schema, Backup, And Retention

- `app_schema_versions` records component schema versions in each SQLite database.
- Before an upgrade, the migration runner creates a SQLite-consistent rollback snapshot and restores it if migration or verification fails.
- `.expensemanager-backup` files are unencrypted ZIP archives containing a manifest, per-file SHA-256 checksums, configuration files, and online SQLite snapshots. Derived state, caches, and logs are excluded.
- Restore rejects absolute/traversal paths, validates every checksum, stages both config and data roots, and swaps them into place with rollback on failure.
- Settings & Storage reports each config/data/state/cache/log file and allows generated material to be removed independently.
- Packaged uninstall invokes `main.py --uninstall`, which explicitly asks whether databases and configuration should be retained. Direct OS package removal intentionally preserves per-user data.

## Release And Performance Verification

- `uv.lock` is the dependency source for development and both package scripts.
- `expense_manager_pyqt.spec` creates a one-directory executable with application assets, card definitions, icons, and lazy UI modules.
- `scripts/package_linux.sh` stages and validates the Debian application layout; `scripts/package_windows.ps1` builds the equivalent Windows directory and optional Inno Setup wrapper.
- `benchmarks/profile_analytics.py` seeds repeatable SQLite fixtures and measures only insight loading/generation/persistence. Optional `cProfile` and `tracemalloc` modes expose hot paths and peak allocations.

`expenses.rebuild` is treated as a fully blocking UI job at the window layer from the start of the rebuild.
`expenses.refresh` is only promoted to the blocking overlay once ingestion has finished and the app is entering analytics/materialization. Mailbox scanning itself stays interactive so routine reloads do not pause the whole window unnecessarily.
Long mailbox scans should cooperate with the GUI thread by yielding periodically from the worker loop rather than holding Python continuously; this keeps wheel and trackpad scrolling responsive while sync is reading or matching larger mail slices.

## Reload Contract

Expense mutations rewrite both state payloads:

- `expenses.json`
- `expenses_tab.json`

Because both cards depend on those files, mutation completion must refresh both expense data cards, not only the active card.

The Thunderbird config card also depends on:

- `config/email_accounts.json`
- `config/local/thunderbird.json`

The mail debug card also depends on:

- `config/email_accounts.json`
- `config/bank_email_rules.overrides.json`

so account-switch saves and rule edits should reload the config/debug cards alongside the overview and analysis cards.

The current reload scopes are intentionally tighter than a full-window reload:

- overview + analysis mutations reload `panel.expenses` and `panel.expenses_tab`
- account switches reload overview, analysis, and debug while the config card updates itself in place
- rule-edit saves and explicit reparses reload overview, analysis, and debug

## Extension Points

To add a new expense mutation:

1. Add or reuse a method on `screen_api.py`.
2. Keep repository and service work inside the screen API or lower layers.
3. Run the mutation from a worker in `analysis_widget.py`.
4. Reload the dependent expense cards after success instead of forcing a full-window refresh.

To add or adjust one mail parser rule without touching code:

1. Open the hidden `Mail Debug` pane from the config card.
2. Use `Add Bank Template` when starting a new bank, or select one stored candidate email when adjusting an existing rule.
3. Edit `bank_email_rules.overrides.json` in the resolved `config` directory through the raw override editor.
4. Update the sender/subject match filters and the regex arrays under `fields`.
5. Validate the override JSON on the selected stored candidate when one is available.
6. Save overrides to persist them and reparse stored candidates.
7. Rebuild if the change broadens sender/subject matching or introduces a new bank and you need historical backfill for emails that were never stored as candidates.

To add a new chart, KPI, or ledger surface:

1. Extend payload generation in `expense_service.py` if new derived data is needed.
2. Consume that prepared data in `overview_widget.py` or `analysis_widget.py`.
3. Avoid direct repository queries from paint or high-frequency UI callbacks.

## Current Tradeoffs

- The Thunderbird config card still assumes one managed Thunderbird provider; manual mailbox overrides and multi-account live views remain future work.
- `src/expenses/ui/analysis_widget.py` is smaller now that worker/detail aggregation moved into `analysis_workers.py`, but the main widget still owns a large amount of feature-specific rendering logic.
- There is no GPU-specific workload in the current expense UI; CPU-only behavior is expected and acceptable.
