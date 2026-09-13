"""Locations shared by the app, independent of the launcher's working folder."""
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent
JSON_DIR = ROOT_DIR / "json"
RESULTS_DIR = ROOT_DIR / "results"
LAST_RUN_STATS_FILE = RESULTS_DIR / "kingshot_last_run_stats.json"
