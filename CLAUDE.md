# CLAUDE.md

- Install: `pip install -e ".[dev]"`. Test: `pytest`. Lint: `ruff check .`. CI runs both on Python 3.11 and 3.12.
- Bluetooth hangs from any shell but the user's own Terminal window on macOS, so never run `scan`, `probe`, `run`, `drill`
  or `eco-off` from an agent shell; hand those steps to the user. `score`, `calibrate` and the tests need no hardware.
- This repo is public. Never commit a device address, serial number, credentials or a real status or log file unscrubbed.
- `tests/test_regression.py` holds the estimate to its recorded answers on a real drain. Don't change the answers to make
  it pass; a change to the estimate must be deliberate and say why the numbers moved.
- The rules in CONTRIBUTING.md about writes aren't negotiable.
- Plans go in `docs/roadmap.md`, findings in `docs/history.md`, drills and their results in `docs/drills.md`.
