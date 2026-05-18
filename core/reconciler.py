"""
Block 5 — Cross-Source Reconciliation.

Finds CC bill payments on savings accounts, matches them to CC monthly totals,
and marks the savings-side debit as 'Internal Transfer — CC Settlement'.

This prevents double-counting spend: each CC charge is counted once (CC side only).
CC bill payment on savings = internal transfer, not real spend.

Usage (via reconcile.py CLI — do not call directly):
  from core.reconciler import reconcile_all
  matches, unmatched = reconcile_all(dry_run=True)
"""

import re
import yaml
import os
from datetime import datetime, timedelta
from core.db import get_connection, query

CONFIG_DIR    = os.path.join(os.path.dirname(__file__), "..", "config")
ACCOUNTS_YAML = os.path.join(CONFIG_DIR, "accounts.yaml")

# Amount tolerance: payment must be within this % or ₹ amount of CC total to match
AMOUNT_TOLERANCE_PCT = 0.02   # 2%
AMOUNT_TOLERANCE_ABS = 150    # ₹150 flat

# How many days back from payment date to look for CC charges
BILLING_WINDOW_DAYS = 45

# Keyword patterns per CC source_id — matched against description + raw_description on savings account
CC_PAYMENT_PATTERNS: dict[str, list[str]] = {
    "cc_hdfc_moneyback": [
        # Must say "credit card" / "cc bill" / card name — NOT bare "hdfc" (appears in UPI VPAs)
        r"hdfc\s*(credit\s*card|cc\s*bill|moneyback|moneybk)",
        r"cc\s*bill.*hdfc",
        r"hdfc.*billpay.*credit",
    ],
    "cc_hdfc_tataneu": [
        r"tata\s*neu",
        r"hdfc\s*(credit\s*card|cc\s*bill|tataneu|tata\s*neu)",
        r"cc\s*bill.*tataneu",
    ],
    "cc_amazon_icici": [
        r"icici\s*(credit\s*card|cc\s*bill)",
        r"amazon\s*pay\s*icici\s*credit",
        r"cc\s*bill.*icici",
    ],
    "cc_supermoney_axis": [
        r"axis\s*(credit\s*card|cc\s*bill|supermoney\s*credit)",
        r"supermoney\s*axis",
        r"cc\s*bill.*axis",
    ],
}

# CRED Club = CC bill payment via CRED app. Cred Store / Cred Cash = purchases, not CC payments.
CRED_PATTERN = r"\bcred\s*club\b"


def _load_cc_accounts() -> list[dict]:
    with open(ACCOUNTS_YAML) as f:
        cfg = yaml.safe_load(f)
    return cfg.get("credit_cards", [])


def _amount_match(payment: float, cc_total: float) -> bool:
    delta = abs(payment - cc_total)
    pct   = delta / cc_total if cc_total else 1.0
    return delta <= AMOUNT_TOLERANCE_ABS or pct <= AMOUNT_TOLERANCE_PCT


def _confidence_label(payment: float, cc_total: float) -> str:
    delta = abs(payment - cc_total)
    if delta <= 2:
        return "exact"
    elif delta <= 50:
        return "near-exact"
    else:
        return "fuzzy"


def _detect_cc_for_payment(txn: dict, cc_accounts: list[dict]) -> list[str]:
    """
    Given a savings debit, return list of CC source_ids it might be paying.
    Returns multiple if CRED (ambiguous) or multiple patterns match.
    """
    text = f"{txn.get('description', '')} {txn.get('raw_description', '')}".lower()
    matched = []

    # Check specific CC patterns first
    for cc in cc_accounts:
        cc_id = cc["id"]
        for pattern in CC_PAYMENT_PATTERNS.get(cc_id, []):
            if re.search(pattern, text):
                matched.append(cc_id)
                break

    # CRED — ambiguous, return all CCs for amount matching
    if not matched and re.search(CRED_PATTERN, text):
        matched = [cc["id"] for cc in cc_accounts]

    return matched


def _parse_date(date_str: str) -> datetime:
    """Parse date string in YYYY-MM-DD or DD/MM/YYYY format."""
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    raise ValueError(f"Unrecognised date format: {date_str!r}")


def _cc_total_for_period(cc_source_id: str, before_date: str, days: int) -> tuple[float, str, int]:
    """
    Sum CC debits in the billing window ending on before_date.
    Returns (total, period_label, txn_count).
    Filters in Python to handle mixed date formats in DB (DD/MM/YYYY).
    """
    dt_end   = _parse_date(before_date)
    dt_start = dt_end - timedelta(days=days)

    rows = query(
        """SELECT amount, date FROM transactions
           WHERE source_id = ? AND txn_type = 'debit'
             AND (is_internal_transfer = 0 OR is_internal_transfer IS NULL)""",
        (cc_source_id,)
    )

    in_window = [
        r for r in rows
        if dt_start <= _parse_date(r["date"]) <= dt_end
    ]
    total        = sum(r["amount"] for r in in_window)
    period_label = f"{dt_start.strftime('%Y-%m-%d')} → {dt_end.strftime('%Y-%m-%d')}"
    return total, period_label, len(in_window)


def _already_reconciled(savings_txn_id: int) -> bool:
    conn = get_connection()
    row  = conn.execute(
        "SELECT id FROM reconciliation_links WHERE savings_txn_id = ?",
        (savings_txn_id,)
    ).fetchone()
    conn.close()
    return row is not None


def reconcile_all(dry_run: bool = True) -> tuple[list[dict], list[dict]]:
    """
    Find CC bill payments on savings accounts and match to CC monthly totals.

    Returns:
      matches   — list of confirmed match dicts
      unmatched — list of suspected CC payments that couldn't be matched
    """
    cc_accounts = _load_cc_accounts()
    cc_ids      = {cc["id"] for cc in cc_accounts}

    # Find all savings-account debits that haven't already been reconciled
    # Check both linked_payment_account AND secondary_payment_account (HDFC CCs can pay from HDFC savings)
    linked_savings = set()
    for cc in cc_accounts:
        if cc.get("linked_payment_account"):
            linked_savings.add(cc["linked_payment_account"])
        if cc.get("secondary_payment_account"):
            linked_savings.add(cc["secondary_payment_account"])

    savings_debits = []
    for savings_id in linked_savings:
        rows = query(
            """SELECT * FROM transactions
               WHERE source_id = ? AND txn_type = 'debit'
               ORDER BY date""",
            (savings_id,)
        )
        savings_debits.extend(rows)

    matches   = []
    unmatched = []

    for txn in savings_debits:
        # Skip if already categorized as CC settlement
        if (txn.get("category") or "").startswith("Internal Transfer — CC"):
            continue
        # Skip already reconciled
        if not dry_run and _already_reconciled(txn["id"]):
            continue

        candidate_ccs = _detect_cc_for_payment(txn, cc_accounts)
        if not candidate_ccs:
            continue

        # Try to match against each candidate CC's monthly total
        best_match = None
        best_delta = float("inf")

        for cc_id in candidate_ccs:
            cc_total, period, count = _cc_total_for_period(
                cc_id, txn["date"], BILLING_WINDOW_DAYS
            )
            if cc_total <= 0:
                continue

            if _amount_match(txn["amount"], cc_total):
                delta = abs(txn["amount"] - cc_total)
                if delta < best_delta:
                    best_delta   = delta
                    best_match   = {
                        "savings_txn_id":  txn["id"],
                        "savings_date":    txn["date"],
                        "savings_amount":  txn["amount"],
                        "savings_source":  txn["source_id"],
                        "savings_desc":    txn["description"],
                        "cc_source_id":    cc_id,
                        "cc_total":        cc_total,
                        "cc_period":       period,
                        "cc_txn_count":    count,
                        "delta":           delta,
                        "confidence":      _confidence_label(txn["amount"], cc_total),
                        "original_category": txn.get("category", ""),
                    }

        if best_match:
            matches.append(best_match)
        else:
            # Suspected CC payment but no amount match — flag for manual review
            unmatched.append({
                "savings_txn_id":   txn["id"],
                "savings_date":     txn["date"],
                "savings_amount":   txn["amount"],
                "savings_source":   txn["source_id"],
                "savings_desc":     txn["description"],
                "candidate_ccs":    candidate_ccs,
                "reason":           "no_amount_match",
            })

    return matches, unmatched


def apply_reconciliation(matches: list[dict]) -> tuple[int, int]:
    """
    Commit matched CC settlements to DB:
    1. UPDATE savings-side txn → category = 'Internal Transfer — CC Settlement', is_internal_transfer = 1
    2. INSERT into reconciliation_links

    Returns (updated_txns, links_inserted).
    """
    conn    = get_connection()
    updated = 0
    linked  = 0

    for m in matches:
        # 1. Mark savings-side transaction
        conn.execute(
            """UPDATE transactions
               SET category = 'Internal Transfer — CC Settlement',
                   is_internal_transfer = 1,
                   transaction_type = 'cc_settlement',
                   category_source = 'reconciler',
                   confidence = 1.0
               WHERE id = ?""",
            (m["savings_txn_id"],)
        )
        updated += 1

        # 2. Log the link
        cc_month = m["cc_period"].split(" → ")[0][:7]  # YYYY-MM from start of window
        conn.execute(
            """INSERT OR IGNORE INTO reconciliation_links
               (savings_txn_id, cc_source_id, cc_month, cc_total, savings_amount, delta, confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (m["savings_txn_id"], m["cc_source_id"], cc_month,
             m["cc_total"], m["savings_amount"], m["delta"], m["confidence"])
        )
        linked += 1

    conn.commit()
    conn.close()
    return updated, linked
