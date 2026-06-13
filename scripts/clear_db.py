"""
Clears transactions from the DB.

Usage:
  python clear_db.py                        # clears ALL transactions (asks for confirmation)
  python clear_db.py --month 2025-01        # clears one month only
  python clear_db.py --source acc_sbi_salary  # clears one account only
  python clear_db.py --force                # skip confirmation prompt
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.db import get_connection, query


def clear(month: str = None, source: str = None, force: bool = False):
    # Build WHERE clause
    conditions = []
    if month:
        conditions.append(f"month = '{month}'")
    if source:
        conditions.append(f"source_id = '{source}'")
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    # Show what will be deleted
    rows = query(f"SELECT COUNT(*) as c, source_id, month FROM transactions {where} GROUP BY source_id, month ORDER BY month")
    if not rows:
        print("No transactions found matching criteria.")
        return

    print("\nTransactions to be deleted:")
    total = 0
    for r in rows:
        print(f"  {r['month']}  {r['source_id']:<25}  {r['c']} transactions")
        total += r['c']
    print(f"\n  Total: {total} transactions")

    if not force:
        confirm = input("\n⚠️  Are you sure? Type 'yes' to confirm: ").strip().lower()
        if confirm != "yes":
            print("Aborted.")
            return

    conn = get_connection()
    conn.execute(f"DELETE FROM transactions {where}")
    conn.execute(f"DELETE FROM upload_log {where.replace('month', 'month') if where else ''}")
    conn.commit()
    conn.close()
    print(f"✅ Deleted {total} transactions.")


if __name__ == "__main__":
    args = sys.argv[1:]
    month = None
    source = None
    force = "--force" in args

    for i, a in enumerate(args):
        if a == "--month" and i + 1 < len(args):
            month = args[i + 1]
        if a == "--source" and i + 1 < len(args):
            source = args[i + 1]

    clear(month=month, source=source, force=force)