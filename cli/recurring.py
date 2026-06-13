"""
Recurring Untagged Merchant Detector.

Finds merchants that land in 'Other' across multiple months — these are
recurring payees the rules/corrections DB hasn't learned yet. Tag them once
here; all future imports auto-categorize them via corrections DB.

Usage:
  uv run python recurring.py                        # list recurring Others (default: 2+ months)
  uv run python recurring.py --min-months 3         # raise threshold
  uv run python recurring.py --source acc_canara_daily  # single account
  uv run python recurring.py --tag                  # interactive: tag each merchant one by one
  uv run python recurring.py --save                 # commit tags to corrections DB (use with --tag)
"""

import argparse
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from collections import defaultdict
from datetime import date

from core.db import query
from core.corrections_db import upsert, get_all

VALID_CATEGORIES = [
    "Food & Dining", "Groceries", "Fuel & Transport", "Shopping & Apparel",
    "Entertainment & Subscriptions", "Health & Medical", "Utilities & Bills",
    "Rent", "EMI & Loan", "Investment & SIP", "Income", "Education",
    "ATM & Cash", "Refund", "Internal Transfer", "Other",
]


def find_recurring_others(min_months: int = 2, source_id: str = None) -> list[dict]:
    """
    Find canonical_merchants that appear in 'Other' across min_months+ distinct months.
    Returns list sorted by month_count desc, total_amount desc.
    """
    filters = ["category = 'Other'",
               "(transaction_type = 'genuine_spend' OR transaction_type = '' OR transaction_type IS NULL)",
               "txn_type = 'debit'"]
    params = []
    if source_id:
        filters.append("source_id = ?")
        params.append(source_id)

    rows = query(
        f"SELECT canonical_merchant, amount, date_parsed, source_id, description "
        f"FROM transactions WHERE {' AND '.join(filters)}",
        params if params else ()
    )

    # Group by canonical_merchant
    by_merchant = defaultdict(list)
    for r in rows:
        cm = r["canonical_merchant"] or r["description"] or ""
        if not cm.strip():
            continue
        by_merchant[cm].append(r)

    # Already in corrections DB — skip these
    known = {c["canonical_merchant"] for c in get_all()}

    results = []
    for merchant, txns in by_merchant.items():
        if merchant in known:
            continue
        months = {t["date_parsed"][:7] for t in txns if t.get("date_parsed")}
        if len(months) < min_months:
            continue
        results.append({
            "canonical_merchant": merchant,
            "month_count":        len(months),
            "txn_count":          len(txns),
            "total_amount":       sum(t["amount"] for t in txns),
            "avg_amount":         sum(t["amount"] for t in txns) / len(txns),
            "months":             sorted(months),
            "source_id":          txns[0]["source_id"],
            "sample_desc":        txns[0]["description"],
        })

    results.sort(key=lambda x: (-x["month_count"], -x["total_amount"]))
    return results


def print_table(merchants: list[dict]) -> None:
    if not merchants:
        print("No recurring untagged merchants found.")
        return

    print(f"\n{'Merchant':<30} {'Months':>6} {'Txns':>5} {'Total ₹':>10} {'Avg ₹':>8}  {'Source':<22}  Months seen")
    print("─" * 110)
    for m in merchants:
        months_str = ", ".join(m["months"])
        print(
            f"{m['canonical_merchant']:<30} "
            f"{m['month_count']:>6} "
            f"{m['txn_count']:>5} "
            f"{m['total_amount']:>10,.0f} "
            f"{m['avg_amount']:>8,.0f}  "
            f"{m['source_id']:<22}  "
            f"{months_str}"
        )
    print(f"\n{len(merchants)} recurring untagged merchant(s). Run with --tag to label them.")


def interactive_tag(merchants: list[dict], save: bool) -> None:
    if not merchants:
        print("Nothing to tag.")
        return

    tagged = []
    print(f"\nTag {len(merchants)} recurring merchants. Enter category number or blank to skip.\n")

    for m in merchants:
        print(f"\n  Merchant : {m['canonical_merchant']}")
        print(f"  Sample   : {m['sample_desc']}")
        print(f"  Source   : {m['source_id']}")
        print(f"  Seen     : {m['month_count']} months, {m['txn_count']} txns, avg ₹{m['avg_amount']:,.0f}")
        print(f"  Months   : {', '.join(m['months'])}")
        print()
        for i, cat in enumerate(VALID_CATEGORIES, 1):
            print(f"    {i:>2}. {cat}")
        choice = input("\n  Category [1-16, blank=skip]: ").strip()
        if not choice:
            print("  Skipped.")
            continue
        try:
            idx = int(choice) - 1
            category = VALID_CATEGORIES[idx]
        except (ValueError, IndexError):
            print("  Invalid — skipped.")
            continue

        tagged.append((m["canonical_merchant"], category, m["source_id"]))
        print(f"  → {category}")

    if not tagged:
        print("\nNothing tagged.")
        return

    print(f"\n{'─'*50}")
    print(f"{'PREVIEW' if not save else 'SAVING'} — {len(tagged)} merchant(s):")
    for merchant, category, source in tagged:
        print(f"  {merchant:<30} → {category}  (source: {source})")

    if save:
        for merchant, category, source in tagged:
            upsert(merchant, category, source_account_hint=source, notes="auto-seeded via recurring.py")
        print(f"\n✅ Saved to corrections DB. Future imports will auto-tag these merchants.")
    else:
        print("\nDry run — pass --save to commit.")


def main():
    parser = argparse.ArgumentParser(description="Recurring untagged merchant detector")
    parser.add_argument("--min-months", type=int, default=2,
                        help="Min distinct months a merchant must appear in Other (default: 2)")
    parser.add_argument("--source", help="Filter by source_id")
    parser.add_argument("--tag", action="store_true", help="Interactive tagging mode")
    parser.add_argument("--save", action="store_true", help="Commit tags to corrections DB")
    args = parser.parse_args()

    merchants = find_recurring_others(min_months=args.min_months, source_id=args.source)

    if args.tag:
        interactive_tag(merchants, save=args.save)
    else:
        print_table(merchants)


if __name__ == "__main__":
    main()
