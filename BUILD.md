# Build and validation

```bash
uv sync --frozen --extra dev
uv run python main.py
QT_QPA_PLATFORM=offscreen uv run python -m unittest discover -s tests -v
uv run python benchmarks/profile_analytics.py --rows 100000
uv run python benchmarks/profile_sources.py --provider csv --records 10000
uv run python benchmarks/profile_reports.py --rows 10000
```

Verify the dual-mode CLI without opening Qt:

```bash
EXPENSE_MANAGER_HOME=/tmp/expense-manager-build-check uv run python main.py --help
EXPENSE_MANAGER_HOME=/tmp/expense-manager-build-check uv run python main.py report dashboard --format json
EXPENSE_MANAGER_HOME=/tmp/expense-manager-build-check uv run python main.py sources setup-status --format json
EXPENSE_MANAGER_HOME=/tmp/expense-manager-build-check uv run python main.py reconciliation list --format json
EXPENSE_MANAGER_HOME=/tmp/expense-manager-build-check uv run python main.py templates mine --format json
```

After `./scripts/package_linux.sh`, run the same commands against `dist/ExpenseManager/ExpenseManager`. The Windows packaging script produces the same command surface through `ExpenseManager.exe`.
