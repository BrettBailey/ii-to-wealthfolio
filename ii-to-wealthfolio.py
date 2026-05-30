"""
ii-to-wealthfolio.py
Convert downloaded ii CSV transaction files to Wealthfolio import format.

Usage:
    python ii-to-wealthfolio.py

Reads all *.csv files from INPUT_DIR (default: ~/Downloads), as set in config.py.

For each file:
  - The filename stem is used as the account name (lowercased, up to the first
    hyphen, underscore, or digit). So "isa-2026-05.csv" → account "isa",
    "john.csv" → account "john", "sipp_export.csv" → account "sipp".
  - Structure is validated (fail-fast). BOMs (U+FEFF) are stripped from every
    field. All rows are classified into Wealthfolio activity types.
  - Output: output/{YYYYMMDD}-{account}.csv where YYYYMMDD is the last
    transaction date in the file and {account} is lowercase.
  - Source file is moved to DONE_DIR once processed.

Validation failures abort processing of that file immediately, without writing
any output or moving the source, so you can investigate and re-run.

Symbol mappings are stored in symbol-map.json next to this script. Unknown
symbols are looked up interactively via Yahoo Finance and saved for next time.
"""

import csv
import glob
import io
import json
import os
import re
import shutil
import sys
import urllib.request
from datetime import datetime

import config
from pathlib import Path

_SCRIPT_DIR = Path(__file__).parent

def _resolve(path: str) -> str:
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = _SCRIPT_DIR / p
    return str(p)

INPUT_DIR = _resolve(config.INPUT_DIR)
DONE_DIR = _resolve(config.DONE_DIR)
OUTPUT_DIR = _resolve(config.OUTPUT_DIR)
SYMBOL_MAP_PATH = _resolve(config.SYMBOL_MAP_PATH)

# ---------------------------------------------------------------------------
# Expected structure for new ii CSV files
# ii exports 11 columns (no ISIN), Date before Settlement Date
# ---------------------------------------------------------------------------

EXPECTED_COLUMNS = [
    "Date",
    "Settlement Date",
    "Symbol",
    "Sedol",
    "Quantity",
    "Price",
    "Description",
    "Reference",
    "Debit",
    "Credit",
    "Running Balance",
]
EXPECTED_COLUMN_COUNT = 11

# Columns that should contain £ currency values when not empty/n/a
CURRENCY_COLUMNS = ["Price", "Debit", "Credit", "Running Balance"]
DATE_COLUMNS = ["Date", "Settlement Date"]
DATE_PATTERN = re.compile(r"^\d{2}/\d{2}/\d{4}$")

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_structure(rows: list[dict], filename: str) -> bool:
    """
    Validate the structure of a newly downloaded ii CSV.
    Returns True if valid, False (and prints errors) if not.
    Never raises — always returns a bool.
    """
    is_valid = True

    if not rows:
        print(f"  ERROR: {filename} contains no data rows")
        return False

    # Column count check
    actual_columns = list(rows[0].keys())
    if len(actual_columns) != EXPECTED_COLUMN_COUNT:
        print(f"  ERROR: Column count mismatch in {filename}")
        print(f"    Expected: {EXPECTED_COLUMN_COUNT} columns")
        print(f"    Found:    {len(actual_columns)} columns: {actual_columns}")
        is_valid = False

    # Column names check
    missing_columns = [col for col in EXPECTED_COLUMNS if col not in actual_columns]
    extra_columns = [col for col in actual_columns if col not in EXPECTED_COLUMNS]
    if missing_columns:
        print(f"  ERROR: Missing columns in {filename}: {missing_columns}")
        is_valid = False
    if extra_columns:
        print(f"  ERROR: Unexpected columns in {filename}: {extra_columns}")
        is_valid = False

    if not is_valid:
        return False  # Skip further checks if column structure is wrong

    # Currency format check — values must start with £ (or be empty/n/a)
    for row_num, row in enumerate(rows, start=2):  # +2: header + 1-based
        for col in CURRENCY_COLUMNS:
            value = row.get(col, "").strip()
            if value and value.lower() != "n/a":
                if not value.startswith("£"):
                    print(f"  ERROR: Currency format changed in {filename}")
                    print(f"    Row {row_num}, column '{col}': got {value!r} (expected £...)")
                    print(f"    This may indicate ii has switched from GBP to pence, or changed currency.")
                    is_valid = False

    # Date format check
    for row_num, row in enumerate(rows, start=2):
        for col in DATE_COLUMNS:
            value = row.get(col, "").strip()
            if value and value.lower() != "n/a":
                if not DATE_PATTERN.match(value):
                    print(f"  ERROR: Date format changed in {filename}")
                    print(f"    Row {row_num}, column '{col}': got {value!r} (expected DD/MM/YYYY)")
                    is_valid = False

    return is_valid

# ---------------------------------------------------------------------------
# Symbol lookup (loaded from symbol-map.json)
# ---------------------------------------------------------------------------

def load_symbol_lookup(path: str) -> dict:
    lookup: dict = {}
    if not os.path.exists(path):
        return lookup
    try:
        with open(path, encoding="utf-8") as symbol_file:
            entries = json.load(symbol_file)
        for entry in entries:
            wf_symbol = entry.get("wealthfolioSymbol", "").strip()
            if wf_symbol:
                lookup[entry["symbol"].strip()] = wf_symbol
    except Exception as e:
        print(f"  WARNING: Could not load symbol-map.json: {e}")
    return lookup

SYMBOL_LOOKUP = load_symbol_lookup(SYMBOL_MAP_PATH)

# ---------------------------------------------------------------------------
# Yahoo Finance symbol lookup
# ---------------------------------------------------------------------------

def yahoo_search(query: str) -> list[dict]:
    """Search Yahoo Finance for a symbol/SEDOL/name. Returns list of {symbol, name} dicts."""
    try:
        url = f"https://query2.finance.yahoo.com/v1/finance/search?q={urllib.request.quote(query)}&lang=en-GB&region=GB&quotesCount=5"
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=5) as response:
            data = json.load(response)
        results = []
        for quote in data.get("quotes", []):
            symbol = quote.get("symbol", "")
            name = quote.get("longname") or quote.get("shortname") or ""
            if symbol:
                results.append({"symbol": symbol, "name": name})
        return results
    except Exception:
        return []

def resolve_unknown_symbol(ii_symbol: str, sedol: str) -> str | None:
    """
    Interactively resolve an unknown ii symbol/SEDOL to a Wealthfolio symbol.
    Searches Yahoo Finance and prompts the user to confirm or enter manually.
    Updates symbol-map.json if a mapping is confirmed.
    Returns the resolved Wealthfolio symbol, or None to skip.
    """
    print(f"\n  UNKNOWN SYMBOL: ii symbol={ii_symbol!r} SEDOL={sedol!r}")

    # Build list of queries to try
    queries = []
    if ii_symbol and ii_symbol not in ("n/a", ""):
        queries.append(ii_symbol + ".L")  # Try with .L suffix first
        queries.append(ii_symbol)
    if sedol and sedol not in ("n/a", ""):
        queries.append(sedol)

    found = []
    winning_query = None
    for query in queries:
        results = yahoo_search(query)
        for result in results:
            if result not in found:
                found.append(result)
        if found:
            winning_query = query
            break

    wf_name = ""
    if found:
        # Auto-select if the top result is an exact symbol match for the query
        top = found[0]
        if winning_query and top["symbol"].upper() == winning_query.upper():
            print(f"  Auto-selected: {top['symbol']} — {top['name']}")
            wf_symbol = top["symbol"]
            wf_name = top["name"]
        else:
            print(f"  Yahoo Finance results:")
            for i, result in enumerate(found[:5], 1):
                print(f"    {i}. {result['symbol']} — {result['name']}")
            print(f"  Enter number to select, W to enter Wealthfolio symbol manually, or S to skip:")
            choice = input("  > ").strip()
            if choice.isdigit() and 1 <= int(choice) <= len(found[:5]):
                wf_symbol = found[int(choice) - 1]["symbol"]
                wf_name = found[int(choice) - 1]["name"]
            elif choice.upper() == "W":
                wf_symbol = input("  Enter Wealthfolio symbol: ").strip()
                if not wf_symbol:
                    return None
                wf_name = input("  Enter description (company name): ").strip()
            elif choice.upper() == "S":
                return None
            else:
                return None
    else:
        print(f"  No Yahoo Finance results found.")
        print(f"  Enter Wealthfolio symbol manually, or S to skip:")
        choice = input("  > ").strip()
        if choice.upper() == "S" or not choice:
            return None
        wf_symbol = choice
        wf_name = input("  Enter description (company name): ").strip()

    # Save to symbol-map.json
    key = ii_symbol if ii_symbol and ii_symbol not in ("n/a", "") else sedol
    try:
        entries = []
        if os.path.exists(SYMBOL_MAP_PATH):
            with open(SYMBOL_MAP_PATH, encoding="utf-8") as symbol_file:
                entries = json.load(symbol_file)
        entries.append({"symbol": key, "name": wf_name, "wealthfolioSymbol": wf_symbol})
        with open(SYMBOL_MAP_PATH, "w", encoding="utf-8") as symbol_file:
            json.dump(entries, symbol_file, indent=2)
        print(f"  Added symbol map {key} => {wf_symbol} for {wf_name}")
    except Exception as e:
        print(f"  WARNING: Could not save to symbol-map.json: {e}")

    SYMBOL_LOOKUP[key] = wf_symbol
    return wf_symbol

def map_symbol(symbol: str, sedol: str) -> str:
    clean_symbol = symbol.strip()
    if clean_symbol and clean_symbol not in ("n/a", ""):
        if clean_symbol in SYMBOL_LOOKUP:
            return SYMBOL_LOOKUP[clean_symbol]
        resolved = resolve_unknown_symbol(clean_symbol, sedol.strip())
        if resolved:
            return resolved
        return clean_symbol
    clean_sedol = sedol.strip()
    if clean_sedol and clean_sedol not in ("n/a", ""):
        if clean_sedol in SYMBOL_LOOKUP:
            return SYMBOL_LOOKUP[clean_sedol]
        resolved = resolve_unknown_symbol("", clean_sedol)
        if resolved:
            return resolved
        return clean_sedol
    return "$CASH-GBP"

# ---------------------------------------------------------------------------
# Amount parsing
# ---------------------------------------------------------------------------

def parse_amount(value: str) -> float:
    amount_str = value.strip().replace("£", "").replace(",", "")
    if amount_str in ("", "n/a"):
        return 0.0
    try:
        return float(amount_str)
    except ValueError:
        return 0.0

def is_empty(value: str) -> bool:
    return value.strip() in ("", "n/a")

def strip_bom(value: str) -> str:
    """Remove Unicode BOM characters (U+FEFF) embedded anywhere in a string."""
    return value.replace("\ufeff", "")

# ---------------------------------------------------------------------------
# Transaction classification (same logic as historical_convert.py)
# ---------------------------------------------------------------------------

def classify(row: dict) -> dict | None:
    description = strip_bom(row.get("Description", "").strip())
    description_lower = description.lower()

    trade_date_raw = row.get("Date", "").strip()
    quantity_raw = row.get("Quantity", "").strip()
    price_raw = row.get("Price", "").strip()
    debit_raw = row.get("Debit", "").strip()
    credit_raw = row.get("Credit", "").strip()
    symbol_raw = row.get("Symbol", "").strip()
    sedol_raw = row.get("Sedol", "").strip()

    try:
        trade_date = datetime.strptime(trade_date_raw, "%d/%m/%Y")
        iso_date = trade_date.strftime("%Y-%m-%dT00:00:00.000Z")
    except ValueError:
        return None

    debit = parse_amount(debit_raw)
    credit = parse_amount(credit_raw)
    has_debit = not is_empty(debit_raw)
    has_credit = not is_empty(credit_raw)
    has_quantity = not is_empty(quantity_raw)
    price = parse_amount(price_raw)

    symbol = map_symbol(symbol_raw, sedol_raw)

    if description_lower.startswith("div "):
        return {
            "date": iso_date, "symbol": symbol, "quantity": 0,
            "activityType": "DIVIDEND", "unitPrice": 0, "currency": "GBP",
            "fee": 0, "amount": round(credit, 2), "fxRate": "", "comment": description,
        }

    if "subscription" in description_lower or "contribution" in description_lower:
        return {
            "date": iso_date, "symbol": "$CASH-GBP", "quantity": 0,
            "activityType": "DEPOSIT", "unitPrice": 0, "currency": "GBP",
            "fee": 0, "amount": round(credit, 2), "fxRate": "", "comment": description,
        }

    if "transfer" in description_lower or "trf" in description_lower or "tfr" in description_lower:
        return {
            "date": iso_date, "symbol": "$CASH-GBP", "quantity": 0,
            "activityType": "TRANSFER_IN", "unitPrice": 0, "currency": "GBP",
            "fee": 0, "amount": round(credit, 2), "fxRate": "", "comment": description,
        }

    if "interest" in description_lower:
        return {
            "date": iso_date, "symbol": "$CASH-GBP", "quantity": 0,
            "activityType": "INTEREST", "unitPrice": 0, "currency": "GBP",
            "fee": 0, "amount": round(credit, 2), "fxRate": "", "comment": description,
        }

    # LIQUIDATION: company wind-up — symbol present, no quantity, credit received
    if "liquidation" in description_lower and has_credit and symbol not in ("$CASH-GBP",):
        return {
            "date": iso_date, "symbol": symbol, "quantity": 0,
            "activityType": "SELL", "unitPrice": 0, "currency": "GBP",
            "fee": 0, "amount": round(credit, 2), "fxRate": "", "comment": description,
        }

    if has_quantity:
        try:
            quantity = float(quantity_raw.replace(",", ""))
        except ValueError:
            quantity = 0.0

        if has_debit:
            # If quantity is 0 (ii reports fractional unit trust purchases as 0),
            # back-calculate the true fractional quantity from debit ÷ price.
            if quantity == 0.0 and price > 0:
                quantity = round(debit / price, 6)
            # Always back-calculate unit_price from Debit ÷ quantity. Debit is the real
            # GBP cash movement and bakes in stamp duty, PTM levy, exec fees, and
            # FX markup. ii's Price column is unreliable (excludes fees; USD trades
            # are labelled £). Back-calc gives effective per-share cost, which is
            # what Wealthfolio needs to reconcile cash and what we want in cost basis.
            unit_price = round(debit / quantity, 5) if quantity else 0
            return {
                "date": iso_date, "symbol": symbol, "quantity": quantity,
                "activityType": "BUY", "unitPrice": unit_price,
                "currency": "GBP", "fee": 0, "amount": "", "fxRate": "", "comment": description,
            }
        elif has_credit:
            if quantity == 0.0 and price > 0:
                quantity = round(credit / price, 6)
            unit_price = round(credit / quantity, 5) if quantity else 0
            return {
                "date": iso_date, "symbol": symbol, "quantity": quantity,
                "activityType": "SELL", "unitPrice": unit_price,
                "currency": "GBP", "fee": 0, "amount": "", "fxRate": "", "comment": description,
            }

    if has_debit and is_empty(quantity_raw):
        return {
            "date": iso_date, "symbol": "$CASH-GBP", "quantity": 0,
            "activityType": "WITHDRAWAL", "unitPrice": 0, "currency": "GBP",
            "fee": 0, "amount": round(debit, 2), "fxRate": "", "comment": description,
        }

    print(f"  WARNING: Could not classify row: {description!r} | debit={debit_raw!r} credit={credit_raw!r} quantity={quantity_raw!r}")
    return None

# ---------------------------------------------------------------------------
# Account detection + last-transaction date
# ---------------------------------------------------------------------------

GUID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)

def detect_account(filename: str) -> str:
    """Return the account name from the filename stem (lowercase, before any hyphen/underscore/digit)."""
    filename_stem = os.path.splitext(os.path.basename(filename))[0].lower()
    if GUID_PATTERN.match(filename_stem):
        print(f"  WARNING: Filename looks like a GUID ({filename!r}). Account name will be meaningless.")
        print(f"    Rename the file to start with your account name, e.g. 'myaccount-{filename_stem[:8]}.csv'")
    # Strip any trailing date, number, or separator suffix: isa-2026-05 → isa
    return re.split(r"[-_\d]", filename_stem)[0]

def last_transaction_date(rows: list[dict]) -> str | None:
    """Return YYYYMMDD of the latest Date value across all rows, or None."""
    latest = None
    for row in rows:
        raw_date = row.get("Date", "").strip()
        if not raw_date or raw_date.lower() == "n/a":
            continue
        try:
            trade_date = datetime.strptime(raw_date, "%d/%m/%Y")
        except ValueError:
            continue
        if latest is None or trade_date > latest:
            latest = trade_date
    return latest.strftime("%Y%m%d") if latest else None

# ---------------------------------------------------------------------------
# Convert to Wealthfolio format
# ---------------------------------------------------------------------------

OUTPUT_COLUMNS = [
    "date", "symbol", "quantity", "activityType",
    "unitPrice", "currency", "fee", "amount", "fxRate", "comment",
]

def write_wealthfolio_csv(rows: list[dict], account: str, date_str: str) -> str:
    """Classify rows and write Wealthfolio CSV. Returns output path."""
    output_rows: list[dict] = []
    unclassified_rows: list[dict] = []

    for row in rows:
        result = classify(row)
        if result is not None:
            output_rows.append(result)
        else:
            unclassified_rows.append(row)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(OUTPUT_DIR, f"{date_str}-{account}.csv")
    with open(out_path, "w", newline="", encoding="utf-8") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(output_rows)

    print(f"  Wrote {len(output_rows)} rows → {out_path} (skipped {len(unclassified_rows)})")

    if unclassified_rows:
        print(f"\n  {'='*58}")
        print(f"  UNCLASSIFIED ROWS — not written to output ({len(unclassified_rows)} row(s)):")
        print(f"  {'-'*58}")
        print(f"  {'Date':<12} {'Debit':>10} {'Credit':>10}  Description")
        print(f"  {'-'*58}")
        for row in unclassified_rows:
            date_display = row.get("Date", "").strip()
            debit_display = row.get("Debit", "").strip() or "-"
            credit_display = row.get("Credit", "").strip() or "-"
            description_display = strip_bom(row.get("Description", "").strip())
            print(f"  {date_display:<12} {debit_display:>10} {credit_display:>10}  {description_display}")
        print(f"  {'='*58}")
        print(f"  Add handling for these in classify() if they should be imported.")

    return out_path

# ---------------------------------------------------------------------------
# Process one file
# ---------------------------------------------------------------------------

def process_file(filepath: str) -> bool:
    """
    Process a single ii download file.
    Returns True on success, False on validation failure.
    """
    filename = os.path.basename(filepath)
    print(f"\n--- {filename} ---")

    account = detect_account(filename)
    if not account:
        print(f"  ERROR: Could not derive account name from filename: {filename!r}")
        return False

    # Read file, stripping all BOM variants (ii stacks many BOMs throughout)
    try:
        with open(filepath, encoding="utf-8-sig") as input_file:
            content = input_file.read()
        content = content.replace("\ufeff", "")
    except Exception as e:
        print(f"  ERROR: Could not read {filename}: {e}")
        return False

    # Parse CSV
    reader = csv.DictReader(io.StringIO(content))
    if reader.fieldnames is None:
        print(f"  ERROR: {filename} appears to be empty or has no headers")
        return False

    # Normalise headers (strip quotes, whitespace, BOMs)
    reader.fieldnames = [h.strip().strip('"').replace("\ufeff", "") for h in reader.fieldnames]
    rows = []
    for row in reader:
        rows.append({k: (v or "").strip().replace("\ufeff", "") for k, v in row.items()})

    if not rows:
        print(f"  ERROR: {filename} has headers but no data rows")
        return False

    # Validate structure (fail-fast)
    if not validate_structure(rows, filename):
        print(f"  ABORTED: {filename} failed validation. No files written or moved.")
        return False

    date_str = last_transaction_date(rows)
    if date_str is None:
        print(f"  ERROR: {filename} has no valid transaction dates")
        return False

    print(f"  Validation passed. {len(rows)} rows. Account={account} LastDate={date_str}")

    # Convert to Wealthfolio format
    write_wealthfolio_csv(rows, account, date_str)

    # Move source to done/ with new name: {date}-{account}.csv
    os.makedirs(DONE_DIR, exist_ok=True)
    done_filename = f"{date_str}-{account}.csv"
    done_path = os.path.join(DONE_DIR, done_filename)
    if os.path.exists(done_path):
        # Keep both — append original stem so we don't overwrite
        original_stem = os.path.splitext(os.path.basename(filepath))[0]
        done_path = os.path.join(DONE_DIR, f"{date_str}-{account}__{original_stem}.csv")
    shutil.move(filepath, done_path)
    print(f"  Moved source → {done_path}")

    return True

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("Wealthfolio New Transactions Processor")
    print(f"Input:   {INPUT_DIR}")
    print(f"Output:  {OUTPUT_DIR}")
    print(f"Symbols: {SYMBOL_MAP_PATH}")

    os.makedirs(INPUT_DIR, exist_ok=True)

    # Find all .csv files in the input folder (not in done/)
    candidates = [
        f for f in glob.glob(os.path.join(INPUT_DIR, "*.csv"))
        if os.path.isfile(f)
    ]

    if not candidates:
        print("\nNo .csv files found in input folder.")
        print(f"  Expected location: {INPUT_DIR}")
        sys.exit(0)

    print(f"\nFound {len(candidates)} file(s) to process:")
    for candidate in candidates:
        print(f"  {os.path.basename(candidate)}")

    success = 0
    failed = 0
    for filepath in sorted(candidates):
        if process_file(filepath):
            success += 1
        else:
            failed += 1

    print(f"\n{'='*60}")
    print(f"Done. Success: {success}, Failed/Aborted: {failed}")
    if failed > 0:
        print("Review errors above before re-running.")
        sys.exit(1)


if __name__ == "__main__":
    main()
