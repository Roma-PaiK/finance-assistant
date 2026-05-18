"""
Block 11 — Splitwise Local Reconciliation

Commands:
  python splitwise.py pending              # list unconfirmed splitwise candidates
  python splitwise.py pending --month YYYY-MM
  python splitwise.py confirm <id>         # interactive: set your share + optional group
  python splitwise.py dismiss <id>         # mark as not a split (full amount is yours)
  python splitwise.py receivables          # who owes you and how much
  python splitwise.py export               # CSV of confirmed splits for manual Splitwise entry
  python splitwise.py export --month YYYY-MM
  python splitwise.py summary --month YYYY-MM   # gross vs net spend for the month

Phase 3: Splitwise API sync (push directly to app) — not implemented here.
"""

import sys
import os
import csv
import argparse
from datetime import date

from core.db import (
    init_db, get_splitwise_candidates, get_splitwise_receivables,
    confirm_split, dismiss_splitwise, query,
)


# ── formatters ────────────────────────────────────────────────────────────────

def _fmt_inr(amount: float) -> str:
    if amount is None:
        return "—"
    int_part = str(int(abs(amount)))
    if len(int_part) <= 3:
        result = int_part
    else:
        result = int_part[-3:]
        int_part = int_part[:-3]
        while int_part:
            result = int_part[-2:] + "," + result
            int_part = int_part[:-2]
    return ("−" if amount < 0 else "") + "₹" + result


def _trunc(s: str, n: int) -> str:
    s = s or ""
    return s[:n] + "…" if len(s) > n else s


# ── commands ──────────────────────────────────────────────────────────────────

def cmd_pending(month: str | None):
    rows = get_splitwise_candidates(month)
    if not rows:
        label = f" for {month}" if month else ""
        print(f"\n  No pending splitwise candidates{label}.")
        print("  (Candidates come from contact-matched UPI transactions — Block 3)\n")
        return

    label = f" — {month}" if month else " — all months"
    print(f"\n{'─'*72}")
    print(f"  Splitwise Candidates{label}  ({len(rows)} pending)")
    print(f"{'─'*72}")
    print(f"  {'ID':>5}  {'Date':<11}  {'Description':<28}  {'Amount':>10}  {'Category':<20}")
    print(f"  {'─'*5}  {'─'*11}  {'─'*28}  {'─'*10}  {'─'*20}")

    for r in rows:
        print(f"  {r['id']:>5}  {r['date']:<11}  {_trunc(r['description'], 28):<28}  "
              f"{_fmt_inr(r['amount']):>10}  {(r['category'] or ''):<20}")

    print(f"{'─'*72}")
    print(f"\n  To confirm: python splitwise.py confirm <id>")
    print(f"  To dismiss: python splitwise.py dismiss <id>\n")


def cmd_confirm(txn_id: int):
    # Fetch the transaction
    rows = query(
        "SELECT * FROM transactions WHERE id = ?", (txn_id,)
    )
    if not rows:
        print(f"\n  ❌ No transaction found with id={txn_id}\n")
        return

    t = rows[0]
    total = t["amount"]

    print(f"\n{'─'*60}")
    print(f"  Transaction #{txn_id}")
    print(f"  Date        : {t['date']}")
    print(f"  Description : {t['description']}")
    print(f"  Amount      : {_fmt_inr(total)}")
    print(f"  Category    : {t['category']}")
    print(f"{'─'*60}")

    # Get split input
    print("\n  How to split?")
    print("  [1] 50/50  (you pay half)")
    print("  [2] Custom % (e.g. 33 for 1/3)")
    print("  [3] Fixed amount (enter your share in ₹)")
    print("  [q] Cancel\n")

    choice = input("  Choice: ").strip().lower()
    if choice == "q":
        print("  Cancelled.\n")
        return

    your_share: float | None = None

    if choice == "1":
        your_share = round(total / 2, 2)
    elif choice == "2":
        pct_str = input(f"  Your % of {_fmt_inr(total)}: ").strip()
        try:
            pct = float(pct_str)
            your_share = round(total * pct / 100, 2)
        except ValueError:
            print("  Invalid %. Cancelled.\n")
            return
    elif choice == "3":
        amt_str = input(f"  Your share amount (total = {_fmt_inr(total)}): ₹").strip()
        try:
            your_share = round(float(amt_str), 2)
        except ValueError:
            print("  Invalid amount. Cancelled.\n")
            return
    else:
        print("  Invalid choice. Cancelled.\n")
        return

    if your_share is None or your_share < 0 or your_share > total:
        print(f"  Share {_fmt_inr(your_share)} out of range. Cancelled.\n")
        return

    # Optional group label
    group = input("  Group label (e.g. 'Goa trip Jan 2025') — press Enter to skip: ").strip() or None

    owed = total - your_share
    print(f"\n  Your share  : {_fmt_inr(your_share)}")
    print(f"  Others owe  : {_fmt_inr(owed)}")
    if group:
        print(f"  Group       : {group}")

    confirm = input("\n  Save? [y/N]: ").strip().lower()
    if confirm != "y":
        print("  Cancelled.\n")
        return

    confirm_split(txn_id, your_share, group)
    print(f"  ✅ Saved. Transaction #{txn_id} — your share: {_fmt_inr(your_share)}\n")


def cmd_dismiss(txn_id: int):
    rows = query("SELECT id, date, description, amount FROM transactions WHERE id = ?", (txn_id,))
    if not rows:
        print(f"\n  ❌ No transaction found with id={txn_id}\n")
        return
    t = rows[0]
    print(f"\n  Dismiss #{txn_id}: {t['description']} — {_fmt_inr(t['amount'])} on {t['date']}")
    confirm = input("  Mark as NOT a split (full amount is yours)? [y/N]: ").strip().lower()
    if confirm == "y":
        dismiss_splitwise(txn_id)
        print(f"  ✅ Dismissed.\n")
    else:
        print("  Cancelled.\n")


def cmd_receivables():
    rows = get_splitwise_receivables()
    if not rows:
        print("\n  No outstanding receivables.\n")
        return

    # Group by splitwise_group
    groups: dict[str, list] = {}
    for r in rows:
        g = r["splitwise_group"] or "Ungrouped"
        groups.setdefault(g, []).append(r)

    total_owed = sum(r["amount"] - r["your_share_amount"] for r in rows)

    print(f"\n{'─'*72}")
    print(f"  Outstanding Receivables  (total owed to you: {_fmt_inr(total_owed)})")
    print(f"{'─'*72}")

    for group, txns in sorted(groups.items()):
        group_owed = sum(t["amount"] - t["your_share_amount"] for t in txns)
        print(f"\n  📂 {group}  — {_fmt_inr(group_owed)} owed")
        print(f"  {'Date':<11}  {'Description':<30}  {'Total':>10}  {'Your share':>10}  {'Owed':>10}")
        print(f"  {'─'*11}  {'─'*30}  {'─'*10}  {'─'*10}  {'─'*10}")
        for t in txns:
            owed = t["amount"] - t["your_share_amount"]
            print(f"  {t['date']:<11}  {_trunc(t['description'], 30):<30}  "
                  f"{_fmt_inr(t['amount']):>10}  {_fmt_inr(t['your_share_amount']):>10}  "
                  f"{_fmt_inr(owed):>10}")

    print(f"\n{'─'*72}")
    print(f"  Total owed to you: {_fmt_inr(total_owed)}")
    print(f"  Phase 3: Splitwise API sync will auto-close settled receivables.\n")


def cmd_export(month: str | None):
    rows = query(
        f"""SELECT id, date, month, description, canonical_merchant,
                   amount, your_share_amount, splitwise_group, category, source_label
            FROM transactions
            WHERE splitwise_confirmed = 1
              {"AND month = ?" if month else ""}
            ORDER BY date DESC""",
        (month,) if month else ()
    )

    if not rows:
        label = f" for {month}" if month else ""
        print(f"\n  No confirmed splits{label} to export.\n")
        return

    suffix = f"_{month}" if month else ""
    out = f"splitwise_export{suffix}.csv"

    with open(out, "w", newline="") as f:
        fields = ["id", "date", "month", "description", "canonical_merchant",
                  "total_amount", "your_share", "others_owe",
                  "splitwise_group", "category", "source_label"]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in rows:
            share = r["your_share_amount"] if r["your_share_amount"] is not None else r["amount"]
            writer.writerow({
                "id":                 r["id"],
                "date":               r["date"],
                "month":              r["month"],
                "description":        r["description"],
                "canonical_merchant": r["canonical_merchant"],
                "total_amount":       r["amount"],
                "your_share":         round(share, 2),
                "others_owe":         round(r["amount"] - share, 2),
                "splitwise_group":    r["splitwise_group"] or "",
                "category":           r["category"],
                "source_label":       r["source_label"],
            })

    print(f"\n  ✅ Exported {len(rows)} splits → {out}")
    print(f"  Use this CSV to manually add expenses in Splitwise web/app.\n")


def cmd_summary(month: str):
    """Gross vs net spend for a month — how much splitwise reduces your effective spend."""
    gross_rows = query(
        """SELECT SUM(amount) as total
           FROM transactions
           WHERE month = ? AND transaction_type = 'genuine_spend' AND txn_type = 'debit'""",
        (month,)
    )
    gross = gross_rows[0]["total"] or 0.0 if gross_rows else 0.0

    # Net = (genuine_spend where not confirmed split) + sum(your_share_amount where confirmed)
    unconfirmed_rows = query(
        """SELECT SUM(amount) as total
           FROM transactions
           WHERE month = ? AND transaction_type = 'genuine_spend' AND txn_type = 'debit'
             AND (splitwise_confirmed = 0 OR splitwise_confirmed IS NULL)""",
        (month,)
    )
    confirmed_rows = query(
        """SELECT SUM(your_share_amount) as total, SUM(amount) as gross_total
           FROM transactions
           WHERE month = ? AND splitwise_confirmed = 1
             AND transaction_type = 'genuine_spend' AND txn_type = 'debit'""",
        (month,)
    )

    unconfirmed = unconfirmed_rows[0]["total"] or 0.0 if unconfirmed_rows else 0.0
    share_sum   = confirmed_rows[0]["total"] or 0.0 if confirmed_rows else 0.0
    net = unconfirmed + share_sum
    saved = gross - net

    print(f"\n{'─'*52}")
    print(f"  Splitwise Summary — {month}")
    print(f"{'─'*52}")
    print(f"  Gross spend   : {_fmt_inr(gross)}")
    print(f"  Your net spend: {_fmt_inr(net)}")
    print(f"  Splitwise adj : −{_fmt_inr(saved)}  (others owe you this)")
    print(f"{'─'*52}\n")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Splitwise local reconciliation (Block 11)")
    sub = parser.add_subparsers(dest="cmd")

    p_pending = sub.add_parser("pending", help="List unconfirmed candidates")
    p_pending.add_argument("--month", help="Filter by YYYY-MM")

    p_confirm = sub.add_parser("confirm", help="Confirm split for a transaction")
    p_confirm.add_argument("id", type=int, help="Transaction ID")

    p_dismiss = sub.add_parser("dismiss", help="Dismiss a candidate (not a split)")
    p_dismiss.add_argument("id", type=int, help="Transaction ID")

    sub.add_parser("receivables", help="Show outstanding receivables")

    p_export = sub.add_parser("export", help="Export confirmed splits to CSV")
    p_export.add_argument("--month", help="Filter by YYYY-MM")

    p_summary = sub.add_parser("summary", help="Gross vs net spend for a month")
    p_summary.add_argument("--month", default=date.today().strftime("%Y-%m"))

    args = parser.parse_args()

    if not args.cmd:
        parser.print_help()
        return

    init_db()

    if args.cmd == "pending":
        cmd_pending(getattr(args, "month", None))
    elif args.cmd == "confirm":
        cmd_confirm(args.id)
    elif args.cmd == "dismiss":
        cmd_dismiss(args.id)
    elif args.cmd == "receivables":
        cmd_receivables()
    elif args.cmd == "export":
        cmd_export(getattr(args, "month", None))
    elif args.cmd == "summary":
        cmd_summary(args.month)


if __name__ == "__main__":
    main()
