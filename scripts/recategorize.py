"""
Re-runs categorization + description cleaning on all existing transactions in the DB.
Run this after updating categories.yaml or after fixing categorizer logic.

Usage:
  python recategorize.py              # re-categorize all
  python recategorize.py --month 2025-01
  python recategorize.py --no-llm     # skip Ollama (rules only, faster)
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.db import query, get_connection
from core.categorizer import categorize_transactions
from core.description_cleaner import clean_description


def recategorize(month: str = None, use_llm: bool = True):
    where = f"WHERE month = '{month}'" if month else ""
    rows = query(f"SELECT * FROM transactions {where}")

    print(f"Found {len(rows)} transactions to re-process...")

    # Run through categorizer (also cleans descriptions)
    updated = categorize_transactions(rows, use_llm=use_llm)

    # Write back to DB
    conn = get_connection()
    for txn in updated:
        conn.execute(
            "UPDATE transactions SET category = ?, description = ? WHERE id = ?",
            (txn["category"], txn["description"], txn["id"])
        )
    conn.commit()
    conn.close()

    # Summary
    from collections import Counter
    cats = Counter(t["category"] for t in updated)
    print(f"\nDone! Category breakdown:")
    for cat, count in sorted(cats.items(), key=lambda x: -x[1]):
        print(f"  {cat:<30} {count:>4} transactions")


if __name__ == "__main__":
    args = sys.argv[1:]
    month = None
    for i, a in enumerate(args):
        if a == "--month" and i + 1 < len(args):
            month = args[i + 1]
    use_llm = "--no-llm" not in args

    recategorize(month=month, use_llm=use_llm)