"""
Quick DB inspector — run anytime to see what's in your database.
Usage:
  python check_db.py                  # summary + last 30 transactions
  python check_db.py --all            # all transactions
  python check_db.py --month 2025-01  # specific month
  python check_db.py --category       # category breakdown
  python check_db.py --uncategorized  # only 'Other' / uncategorized
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.db import query

def print_divider(label=""):
    print(f"\n{'─'*70}")
    if label:
        print(f"  {label}")
        print(f"{'─'*70}")

def summary():
    total = query("SELECT COUNT(*) as c, SUM(amount) as s FROM transactions WHERE txn_type='debit' AND is_internal_transfer=0")
    credits = query("SELECT COUNT(*) as c, SUM(amount) as s FROM transactions WHERE txn_type='credit' AND is_internal_transfer=0")
    internal = query("SELECT COUNT(*) as c FROM transactions WHERE is_internal_transfer=1")
    months = query("SELECT DISTINCT month FROM transactions ORDER BY month DESC")

    print_divider("DATABASE SUMMARY")
    print(f"  Months loaded : {', '.join(r['month'] for r in months) or 'none'}")
    print(f"  Total debits  : {total[0]['c']} transactions  |  ₹{total[0]['s'] or 0:,.2f}")
    print(f"  Total credits : {credits[0]['c']} transactions  |  ₹{credits[0]['s'] or 0:,.2f}")
    print(f"  Internal xfers: {internal[0]['c']} (excluded from above)")

def category_breakdown(month=None):
    where = f"AND month='{month}'" if month else ""
    rows = query(f"""
        SELECT category, COUNT(*) as count, SUM(amount) as total
        FROM transactions
        WHERE txn_type='debit' AND is_internal_transfer=0 {where}
        GROUP BY category
        ORDER BY total DESC
    """)
    print_divider(f"CATEGORY BREAKDOWN {'(' + month + ')' if month else '(all time)'}")
    print(f"  {'Category':<28} {'Txns':>5}  {'Amount':>12}")
    print(f"  {'─'*28}  {'─'*5}  {'─'*12}")
    for r in rows:
        cat = r['category'] or 'Uncategorized'
        print(f"  {cat:<28} {r['count']:>5}  ₹{r['total']:>11,.2f}")

def list_transactions(limit=30, month=None, only_uncategorized=False):
    where_clauses = ["is_internal_transfer=0"]
    if month:
        where_clauses.append(f"month='{month}'")
    if only_uncategorized:
        where_clauses.append("(category='Other' OR category IS NULL)")
    where = "WHERE " + " AND ".join(where_clauses)

    limit_clause = f"LIMIT {limit}" if limit else ""
    rows = query(f"""
        SELECT id, date, txn_type, amount, category, description, source_label
        FROM transactions
        {where}
        ORDER BY date DESC
        {limit_clause}
    """)

    label = "UNCATEGORIZED TRANSACTIONS" if only_uncategorized else f"TRANSACTIONS (last {limit})"
    if month:
        label += f" — {month}"
    print_divider(label)
    print(f"  {'ID':>4}  {'Date':<12} {'Type':6} {'Amount':>10}  {'Category':<25} {'Description'}")
    print(f"  {'─'*4}  {'─'*12} {'─'*6} {'─'*10}  {'─'*25} {'─'*30}")
    for r in rows:
        cat = (r['category'] or 'None')[:25]
        desc = r['description'][:45] if r['description'] else ''
        typ = r['txn_type'].upper()
        print(f"  {r['id']:>4}  {r['date']:<12} {typ:<6} ₹{r['amount']:>9,.2f}  {cat:<25} {desc}")

    print(f"\n  Total shown: {len(rows)}")

def sources_breakdown():
    rows = query("""
        SELECT source_label, COUNT(*) as count, SUM(amount) as total
        FROM transactions WHERE is_internal_transfer=0
        GROUP BY source_label ORDER BY total DESC
    """)
    print_divider("SPEND BY SOURCE ACCOUNT / CARD")
    for r in rows:
        print(f"  {r['source_label']:<35} {r['count']:>4} txns  ₹{r['total']:>11,.2f}")

if __name__ == "__main__":
    args = sys.argv[1:]
    month = None
    for i, a in enumerate(args):
        if a == "--month" and i+1 < len(args):
            month = args[i+1]

    summary()
    sources_breakdown()
    category_breakdown(month)

    if "--uncategorized" in args:
        list_transactions(limit=None, month=month, only_uncategorized=True)
    elif "--category" not in args:
        limit = None if "--all" in args else 30
        list_transactions(limit=limit, month=month)