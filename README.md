# Expense Manager PyQt

Standalone desktop expense manager.

## Run

```powershell
python main.py
```

## Runtime Data

The app stores writable data in per-user OS app directories, not inside the repo checkout.

Set `EXPENSE_MANAGER_HOME` to force all writable paths under one directory for development or tests:

```powershell
$env:EXPENSE_MANAGER_HOME = "D:\ExpenseManagerRuntime"
python main.py
```

With the override set, the app uses:

- `config/` for app config
- `data/` for SQLite databases
- `state/` for derived JSON snapshots
- `cache/` for local caches
- `logs/` for rotating logs

Without the override, the same categories resolve to the normal per-user app locations for the current OS. Existing repo-local runtime folders are not migrated automatically; configure Thunderbird in the app and rebuild the database when starting fresh.

## Logs

The app writes rotating logs to the resolved `logs/` directory.

You can override the startup log level with:

- `NOC_LOG_LEVEL=DEBUG`

## Verification

Run the Python test suite with:

```powershell
python -m unittest discover -s tests -v
```

## Packaging

Windows packaging scaffolding is included:

- `expense_manager_pyqt.spec`
- `scripts/package_windows.ps1`

The Windows package embeds `assets/icons/expense_manager_matte.ico` as the executable icon and bundles `assets/` for runtime icon/theme loading.

## Theme

The shipped theme tokens live in `assets/theme/default_theme.json`.

- `src/app/theme.py` loads the token file.
- `src/app/window.py` applies the app font and configures the shared UI kit.
- `src/app/ui_kit/controls.py` and `src/app/ui_kit/section_card.py` consume those shared tokens for common controls and section shells.

Feature widgets may still contain some local color overrides. The shared UI kit is the preferred place to centralize future theme work.
