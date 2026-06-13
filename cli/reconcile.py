"""
Block 5 — Cross-Source Reconciliation CLI.

Finds CC bill payments on savings accounts, matches to CC monthly totals,
marks savings-side as 'Internal Transfer — CC Settlement'.

Usage:
  uv run python reconcile.py              # preview matches (no DB changes)
  uv run python reconcile.py --save       # commit to DB
  uv run python reconcile.py --month 2025-01          # filter by month (preview)
  uv run python reconcile.py --source acc_canara_daily # filter by savings account

Why this matters:
  Without reconciliation, every CC bill payment on savings counts as spend AND
  all CC charges count as spend — same money double-counted.
  After reconciliation: savings-side = internal transfer (excluded from spend),
  CC-side charges = real spend (counted once).
"""

import sys
from collections import defaultdict
from core.reconciler import reconcile_all, apply_reconciliation
from core.db import init_db, query


def _print_matches(matches: list[dict], unmatched: list[dict]):
    print(f"\n{'='*68}")
    print(f"  Reconciliation — {len(matches)} matched | {len(unmatched)} unmatched suspected payments")
    print(f"{'='*68}")

    if matches:
        print(f"\n  Matched CC settlements ({len(matches)}):")
        by_cc = defaultdict(list)
        for m in matches:
            by_cc[m["cc_source_id"]].append(m)

        for cc_id, ms in sorted(by_cc.items()):
            total_savings = sum(m["savings_amount"] for m in ms)
            print(f"\n    {cc_id}  ({len(ms)} payments, ₹{total_savings:,.0f} total)")
            for m in sorted(ms, key=lambda x: x["savings_date"]):
                conf_tag = f"[{m['confidence']}]"
                delta_tag = f"Δ₹{m['delta']:.0f}" if m["delta"] > 2 else "exact"
                print(f"      {m['savings_date']}  ₹{m['savings_amount']:>10,.2f}  "
                      f"CC total ₹{m['cc_total']:>10,.2f}  {delta_tag:<10}  {conf_tag}")
                print(f"        {m['savings_desc'][:55]}")
                print(f"        CC period: {m['cc_period']}  ({m['cc_txn_count']} charges)")

    if unmatched:
        print(f"\n  Unmatched suspected CC payments ({len(unmatched)}) — review manually:")
        for u in sorted(unmatched, key=lambda x: x["savings_date"]):
            print(f"    {u['savings_date']}  ₹{u['savings_amount']:>10,.2f}  "
                  f"candidates: {', '.join(u['candidate_ccs'])}")
            print(f"      {u['savings_desc'][:60]}")

    print(f"\n{'='*68}")


def _print_cc_spend_summary():
    """Show spend by CC after reconciliation — real spend only (no settlements)."""
    print(f"\n  CC spend summary (genuine charges only):")
    cc_ids = ["cc_hdfc_millenia", "cc_amazon_icici", "cc_supermoney_axis"]
    for cc_id in cc_ids:
        rows = query(
            """SELECT month, SUM(amount) as total, COUNT(*) as count
               FROM transactions
               WHERE source_id = ? AND txn_type = 'debit'
                 AND (is_internal_transfer = 0 OR is_internal_transfer IS NULL)
               GROUP BY month ORDER BY month""",
            (cc_id,)
        )
        if rows:
            print(f"\n    {cc_id}:")
            for r in rows:
                print(f"      {r['month']}  ₹{r['total']:>10,.2f}  ({r['count']} txns)")


def main():
    args = sys.argv[1:]
    save = "--save" in args

    init_db()

    print("Running reconciliation...")
    matches, unmatched = reconcile_all(dry_run=not save)
    _print_matches(matches, unmatched)

    if not matches and not unmatched:
        print("\nNo CC bill payments detected on savings accounts.")
        print("Either no data loaded yet, or all already reconciled.")
        return

    if not save:
        print("\nPreview only. Run with --save to commit to DB.")
        return

    updated, linked = apply_reconciliation(matches)
    print(f"\nSaved.")
    print(f"  {updated} savings transactions marked as 'Internal Transfer — CC Settlement'")
    print(f"  {linked} reconciliation links logged")

    if unmatched:
        print(f"\n  {len(unmatched)} unmatched payments — use review.py apply to fix manually:")
        for u in unmatched:
            print(f"    id={u['savings_txn_id']}  {u['savings_date']}  ₹{u['savings_amount']}")

    _print_cc_spend_summary()


if __name__ == "__main__":
    main()
