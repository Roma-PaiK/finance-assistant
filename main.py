"""
Main pipeline entrypoint.

Usage:
  python main.py <file>                  # parse + categorize + save to DB
  python main.py <file> --dry-run        # parse + categorize + export to CSV only (no DB write)
  python main.py <file> --dry-run --csv out.csv   # custom CSV output path
"""

import sys
import os
import pandas as pd
from parsers.detector import get_parser_and_password
from core.categorizer import categorize_transactions
from core.deduplicator import flag_internal_transfers, dedup_transactions
from core.db import init_db, insert_transactions, query, log_upload


def process_statement(file_path: str, dry_run: bool = False, csv_path: str = None) -> dict:
    print(f"\n📄 Processing: {file_path}")

    # 1. Detect parser
    parser, password = get_parser_and_password(file_path)
    print(f"✅ Detected: {parser.source_label}")

    # 2. Parse + validate (Block 1)
    result = parser.parse(file_path, password)
    validation = result.validation
    raw_txns = result.transactions

    print(f"   Parsed {len(raw_txns)} transactions")

    if not validation.is_ok():
        print(f"\n❌ HALTED — {validation.message}")
        print("   Fix the extraction issue or check the file format before proceeding.")
        return {"file": file_path, "parsed": 0, "inserted": 0, "halted": True, "reason": validation.message}

    if not raw_txns:
        return {"file": file_path, "parsed": 0, "inserted": 0}

    # 3. Convert to dicts
    txn_dicts = [t.to_dict() for t in raw_txns]

    # 4. Flag internal transfers
    txn_dicts = flag_internal_transfers(txn_dicts, parser.source_id)
    _flagged = sum(1 for t in txn_dicts if t.get("is_internal_transfer"))
    print(f"   🏷️  Tagging: {_flagged} internal transfers flagged | {len(txn_dicts) - _flagged} spendable rows")

    # 5. Dedup against existing DB (skip in dry-run — DB may be empty/irrelevant)
    if not dry_run:
        existing = query("SELECT date, amount, txn_type, source_id FROM transactions")
        before = len(txn_dicts)
        txn_dicts = dedup_transactions(existing, txn_dicts)
        dupes = before - len(txn_dicts)
        if dupes:
            print(f"   Deduped: {dupes} duplicates removed")

    # 6. Categorize
    txn_dicts = categorize_transactions(txn_dicts)

    if dry_run:
        # ── DRY RUN: export to CSV ─────────────────────────────────────────
        from datetime import datetime as dt
        timestamp = dt.now().strftime("%Y%m%d_%H%M%S")
        default_name = f"dry_run_{os.path.splitext(os.path.basename(file_path))[0]}_{timestamp}.csv"
        out = csv_path or default_name
        df = pd.DataFrame(txn_dicts)

        # Add blank review columns
        df["corrected_category"] = ""
        df["transfer_type"] = ""

        # Sort: Other first → low confidence → rest (fix worst first)
        df["_sort_other"] = (df["category"] == "Other").astype(int)
        df = df.sort_values(["_sort_other", "confidence"], ascending=[False, True])
        df = df.drop(columns=["_sort_other"])

        # Reorder columns for readability — review cols up front
        cols = ["date", "month", "txn_type", "amount",
                "corrected_category", "transfer_type",
                "category", "category_source", "confidence",
                "is_internal_transfer", "description", "raw_description",
                "canonical_merchant", "source_id", "source_label", "notes"]
        df = df[[c for c in cols if c in df.columns]]
        df.to_csv(out, index=False)
 
        print(f"\n📊 DRY RUN — {len(txn_dicts)} transactions exported to: {out}")
        print(f"   Review the CSV, tweak categories.yaml or categorizer.py, then re-run without --dry-run")
        _print_category_summary(txn_dicts)
        return {"file": file_path, "parsed": len(raw_txns), "csv": out, "dry_run": True}

    else:
        # ── LIVE RUN: save to DB ───────────────────────────────────────────
        inserted = insert_transactions(txn_dicts)
        month = txn_dicts[0]["month"] if txn_dicts else "unknown"
        log_upload(os.path.basename(file_path), parser.source_id, month, inserted)
        print(f"   ✅ Inserted {inserted} transactions into DB")
        _print_category_summary(txn_dicts)
        return {"file": file_path, "parsed": len(raw_txns), "inserted": inserted}


def _print_category_summary(txn_dicts: list[dict]):
    from collections import Counter
    spendable = [t for t in txn_dicts if not t.get("is_internal_transfer")]
    cats = Counter(t["category"] for t in spendable)
    sources = Counter(t.get("category_source", "unknown") for t in spendable)
    internal = len(txn_dicts) - len(spendable)

    print(f"\n   Category breakdown:")
    for cat, count in sorted(cats.items(), key=lambda x: -x[1]):
        print(f"     {cat:<30} {count:>4}")
    if internal:
        print(f"     {'Internal Transfer (excluded)':<30} {internal:>4}")

    print(f"\n   Source breakdown:")
    for src, count in sorted(sources.items(), key=lambda x: -x[1]):
        pct = count / len(spendable) * 100 if spendable else 0
        print(f"     {src:<28} {count:>4}  ({pct:.0f}%)")


if __name__ == "__main__":
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    csv_path = None

    # Extract --csv <path> if provided
    if "--csv" in args:
        idx = args.index("--csv")
        if idx + 1 < len(args):
            csv_path = args[idx + 1]

    # Get file paths (everything that isn't a flag)
    files = [a for a in args if not a.startswith("--") and a != csv_path]

    if not files:
        print("Usage: python main.py <statement_file> [--dry-run] [--csv output.csv]")
        sys.exit(1)

    if not dry_run:
        init_db()

    for path in files:
        result = process_statement(path, dry_run=dry_run, csv_path=csv_path)
        print(f"\n{'📋 DRY RUN complete' if dry_run else '✅ Done'}: {result}")