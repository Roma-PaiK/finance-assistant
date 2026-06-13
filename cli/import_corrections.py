"""
Block 2 — Dry-run correction importer.

Usage:
  uv run python import_corrections.py <corrected_csv> [...]         # preview summary only
  uv run python import_corrections.py <corrected_csv> [...] --save  # save to DB + corrections cache

Input: corrected dry-run CSV with two extra columns you added:
  corrected_category  — new category if wrong; blank = accept current category
  transfer_type       — for Internal Transfer rows: self | others | unknown | blank

Merge rule (applied to every row before DB insert):
  effective_category = corrected_category if filled, else original category
  Internal Transfer + self    → "Internal Transfer — Self"
  Internal Transfer + others  → "Internal Transfer — Other"
  Internal Transfer + unknown → "Internal Transfer"  (flagged in summary)
  transfer_type dropped after merge; only final_category stored in DB.

One correction upserts canonical_merchant → final_category into corrections
cache so all future transactions from that merchant are auto-tagged.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import yaml
import pandas as pd
from collections import Counter
from core.corrections_db import upsert, stats, init_corrections_table
from core.corrections_merge import merge_transfer_type
from core.db import get_connection, init_db
from core.description_cleaner import get_canonical_merchant

CONFIG_DIR = os.path.join(os.path.dirname(__file__), "..", "config")
ACCOUNTS_YAML    = os.path.join(CONFIG_DIR, "accounts.yaml")
CATEGORIES_YAML  = os.path.join(CONFIG_DIR, "categories.yaml")


def _load_label_to_id() -> dict[str, str]:
    """label → source_id from accounts.yaml (single source of truth)."""
    with open(ACCOUNTS_YAML) as f:
        cfg = yaml.safe_load(f)
    return {
        acc["label"].lower(): acc["id"]
        for acc in cfg.get("accounts", []) + cfg.get("credit_cards", [])
    }


def _load_valid_categories() -> set[str]:
    """Category names from categories.yaml (single source of truth)."""
    with open(CATEGORIES_YAML) as f:
        cfg = yaml.safe_load(f)
    return set(cfg.get("categories", {}).keys())



def resolve_row(row: pd.Series, label_to_id: dict, valid_categories: set) -> dict:
    """Compute final values for a single CSV row."""
    original_cat  = (row.get("category", "") or "").strip()
    corrected_cat = (row.get("corrected_category", "") or "").strip()
    transfer_type = (row.get("transfer_type", "") or "").strip()

    # Validate corrected_category if filled
    if corrected_cat and corrected_cat not in valid_categories:
        print(f"   ⚠️  Unknown category '{corrected_cat}' — treating as blank")
        corrected_cat = ""

    effective_cat = corrected_cat if corrected_cat else original_cat
    final_cat, needs_review = merge_transfer_type(effective_cat, transfer_type)

    # canonical_merchant: column if present, else re-derive from raw
    merchant = (row.get("canonical_merchant", "") or "").strip()
    if not merchant:
        raw = (row.get("raw_description", "") or "").strip()
        merchant = get_canonical_merchant(raw) if raw else ""

    # source_id: column if present, else derive via accounts.yaml
    source_id = (row.get("source_id", "") or "").strip()
    if not source_id:
        label = (row.get("source_label", "") or "").strip().lower()
        source_id = label_to_id.get(label, label)

    return {
        "date":                row.get("date", ""),
        "month":               row.get("month", ""),
        "txn_type":            (row.get("txn_type", "") or "").strip(),
        "amount":              row.get("amount", 0),
        "description":         row.get("description", ""),
        "raw_description":     row.get("raw_description", ""),
        "canonical_merchant":  merchant,
        "source_id":           source_id,
        "source_label":        row.get("source_label", ""),
        "category":            final_cat,
        "is_internal_transfer": final_cat.startswith("Internal Transfer"),
        "transaction_type":    row.get("transaction_type"),  # auto-detected from categorizer
        "splitwise_candidate": 0,
        "splitwise_pushed":    0,
        "notes":               row.get("notes", "") or "",
        # meta — not written to DB
        "_original_category": original_cat,
        "_was_corrected":     bool(corrected_cat),
        "_needs_review":      needs_review,
    }


# ── Summary ───────────────────────────────────────────────────────────────────

def print_summary(rows: list[dict], csv_path: str):
    corrected    = [r for r in rows if r["_was_corrected"]]
    needs_review = [r for r in rows if r["_needs_review"]]
    by_cat       = Counter(r["category"] for r in rows)

    print(f"\n{'='*64}")
    print(f"  {os.path.basename(csv_path)}")
    print(f"  {len(rows)} transactions | {len(corrected)} corrected | {len(needs_review)} flagged for review")
    print(f"{'='*64}")

    print(f"\n  Final category breakdown:")
    for cat, count in sorted(by_cat.items(), key=lambda x: -x[1]):
        print(f"    {cat:<44} {count:>4}")

    if corrected:
        print(f"\n  Corrections applied ({len(corrected)}):")
        for r in corrected:
            change = f"{r['_original_category']} → {r['category']}"
            merch  = r["canonical_merchant"] or r["description"][:30]
            print(f"    {merch:<32}  {change}")

    if needs_review:
        print(f"\n  ⚠️  Needs review — transfer_type=unknown ({len(needs_review)}):")
        for r in needs_review:
            print(f"    {r['date']}  {r['description'][:40]}  ₹{r['amount']}")

    print(f"{'='*64}\n")


# ── DB insert (with dedup) ────────────────────────────────────────────────────

def _existing_keys(conn) -> set:
    rows = conn.execute(
        "SELECT date, amount, txn_type, source_id FROM transactions"
    ).fetchall()
    return {(r[0], str(r[1]), r[2], r[3]) for r in rows}


def insert_rows(rows: list[dict]) -> tuple[int, int]:
    """Insert rows into transactions, skip dupes. Returns (inserted, skipped)."""
    conn = get_connection()
    existing = _existing_keys(conn)
    inserted = skipped = 0

    for r in rows:
        key = (r["date"], str(r["amount"]), r["txn_type"], r["source_id"])
        if key in existing:
            skipped += 1
            continue
        conn.execute("""
            INSERT INTO transactions
            (date, month, description, raw_description, canonical_merchant,
             amount, txn_type, source_id, source_label, category,
             is_internal_transfer, splitwise_candidate, splitwise_pushed, notes, transaction_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            r["date"], r["month"], r["description"], r["raw_description"],
            r["canonical_merchant"], r["amount"], r["txn_type"],
            r["source_id"], r["source_label"], r["category"],
            int(r["is_internal_transfer"]),
            r["splitwise_candidate"], r["splitwise_pushed"], r["notes"],
            r.get("transaction_type"),  # refund or None
        ))
        existing.add(key)
        inserted += 1

    conn.commit()
    conn.close()
    return inserted, skipped


def upsert_cache(rows: list[dict]) -> int:
    """Upsert corrections cache for explicitly corrected rows with a known merchant.
    Skip refunds — auto-detected by categorizer, not stored as merchant rule."""
    cached = 0
    for r in rows:
        if r["_was_corrected"] and r["canonical_merchant"] and r["category"] != "Refund":
            upsert(r["canonical_merchant"], r["category"], source_account_hint=r["source_id"])
            cached += 1
    return cached


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    args  = sys.argv[1:]
    save  = "--save" in args
    files = [a for a in args if not a.startswith("--")]

    if not files:
        print("Usage: uv run python import_corrections.py <csv> [...] [--save]")
        sys.exit(1)

    label_to_id      = _load_label_to_id()
    valid_categories = _load_valid_categories()
    init_corrections_table()
    if save:
        init_db()

    all_rows = []

    for path in files:
        if not os.path.exists(path):
            print(f"❌ Not found: {path}")
            continue
        if "corrected_category" not in pd.read_csv(path, nrows=0).columns:
            print(f"❌ No 'corrected_category' column in {path} — skipping")
            continue

        df = pd.read_csv(path, dtype=str).fillna("")
        rows = [resolve_row(row, label_to_id, valid_categories) for _, row in df.iterrows()]
        print_summary(rows, path)
        all_rows.extend(rows)

    if not all_rows:
        print("No rows to process.")
        return

    total_corrected = sum(1 for r in all_rows if r["_was_corrected"])
    total_review    = sum(1 for r in all_rows if r["_needs_review"])
    print(f"  Total across all files: {len(all_rows)} rows | {total_corrected} corrected | {total_review} flagged\n")

    if not save:
        print("  Preview only. Run with --save to insert into DB + update corrections cache.")
        print(f"  Corrections cache now: {stats()}")
        return

    inserted, skipped = insert_rows(all_rows)
    cached = upsert_cache(all_rows)
    s = stats()
    print(f"✅ Saved.")
    print(f"   DB: {inserted} inserted | {skipped} skipped (dupes)")
    print(f"   Cache: {cached} merchant mappings upserted")
    print(f"   Cache total: {s['total_merchants']} merchants | {s['high_confidence_3plus']} high-confidence (3+)")


if __name__ == "__main__":
    main()
