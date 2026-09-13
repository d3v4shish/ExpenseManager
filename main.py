from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    """Dispatch the GUI, headless CLI, or data-retention uninstall helper."""

    if "--uninstall" in sys.argv[1:]:
        from src.app.uninstall import run_uninstall

        return run_uninstall()
    argv = sys.argv[1:]
    if not argv or argv == ["gui"]:
        from src.app.run import run

        return run()
    if argv:
        from src.app.cli import run_cli

        return run_cli(argv, Path(__file__).resolve().parent, program_name=Path(sys.argv[0]).name or "ExpenseManager")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
