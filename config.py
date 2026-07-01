"""
config.py — user-editable settings for ii-to-wealthfolio.py

Edit this file to match your setup. Relative paths are resolved relative to
this script's directory. INPUT_DIR supports ~ for the home directory.
"""

from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# Where to look for downloaded ii CSV files.
INPUT_DIR = "~/Downloads"

# Processed files are moved here after a successful run.
DONE_DIR = "done"

# Where Wealthfolio-ready CSV files are written.
OUTPUT_DIR = "output"

# Symbol map — maps ii symbols/SEDOLs to Wealthfolio symbols.
SYMBOL_MAP_PATH = "symbol-map.json"

# Path to the Wealthfolio SQLite database (used to look up account names).
WEALTHFOLIO_DB_PATH = r"~\AppData\Roaming\com.teymz.wealthfolio\app.db"
