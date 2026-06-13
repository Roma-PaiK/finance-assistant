"""
Layer A — Deterministic Tool Library (Phase 3)

Read-only analytics functions over the post-tagging DB.
All functions return compact structured results (dicts/lists of numbers).
No LLM involvement — these are the ground truth the agent calls as tools.
"""

from datetime import date as _date
from core.db import query


def _months_back(from_month: str, n: int) -> list[str]:
    """n months ending at from_month inclusive, oldest first.
    Example: _months_back('2025-06', 3) → ['2025-04', '2025-05', '2025-06']"""
    y, m = int(from_month[:4]), int(from_month[5:7])
    months = []
    for _ in range(n):
        months.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return list(reversed(months))


def spend_by_category(month: str) -> dict[str, float]:
    """Genuine debit spend per category for YYYY-MM.
    Splitwise-aware: uses your_share_amount where a split is confirmed."""
    rows = query("""
        SELECT category,
               SUM(
                 CASE
                   WHEN splitwise_confirmed = 1 AND your_share_amount IS NOT NULL
                   THEN your_share_amount
                   ELSE amount
                 END
               ) as total
        FROM transactions
        WHERE month = ?
          AND transaction_type = 'genuine_spend'
          AND txn_type = 'debit'
        GROUP BY category
        ORDER BY total DESC
    """, (month,))
    return {r["category"]: r["total"] for r in rows if r["category"]}


def monthly_trend(
    category: str,
    n_months: int,
    as_of_month: str | None = None,
) -> list[dict]:
    """Spend for `category` over the last n_months ending at as_of_month.
    Returns [{"month": "YYYY-MM", "total": float}] oldest → newest.
    Months with no spend are included with total=0.0."""
    end = as_of_month or _date.today().strftime("%Y-%m")
    months = _months_back(end, n_months)
    placeholders = ",".join("?" * len(months))
    rows = query(f"""
        SELECT month, SUM(
            CASE
              WHEN splitwise_confirmed = 1 AND your_share_amount IS NOT NULL
              THEN your_share_amount
              ELSE amount
            END
        ) as total
        FROM transactions
        WHERE month IN ({placeholders})
          AND category = ?
          AND transaction_type = 'genuine_spend'
          AND txn_type = 'debit'
        GROUP BY month
        ORDER BY month
    """, (*months, category))
    totals = {r["month"]: r["total"] for r in rows}
    return [{"month": m, "total": totals.get(m, 0.0)} for m in months]


def top_merchants(month: str, n: int = 10) -> list[dict]:
    """Top n merchants by debit spend for YYYY-MM.
    Returns [{"merchant": str, "total": float, "txn_count": int}]."""
    rows = query("""
        SELECT
            COALESCE(NULLIF(canonical_merchant, ''), description) as merchant,
            SUM(amount) as total,
            COUNT(*) as txn_count
        FROM transactions
        WHERE month = ?
          AND transaction_type = 'genuine_spend'
          AND txn_type = 'debit'
        GROUP BY merchant
        ORDER BY total DESC
        LIMIT ?
    """, (month, n))
    return [dict(r) for r in rows]


def savings_rate(month: str) -> dict:
    """Salary inflow, SIP outflow, genuine spend, indicative savings rate for YYYY-MM.
    Returns {"salary": float, "genuine_spend": float, "sip_month": float,
             "sip_ytd": float, "savings_rate": float | None, "sip_count": int}.
    savings_rate is None when no salary found for the month."""
    year = month[:4]

    sip_rows = query("""
        SELECT SUM(amount) as total, COUNT(*) as n
        FROM transactions
        WHERE month = ?
          AND source_id = 'acc_bob_sip'
          AND txn_type = 'debit'
          AND category = 'Investment & SIP'
    """, (month,))
    sip_month = (sip_rows[0]["total"] or 0.0) if sip_rows else 0.0
    sip_count = (sip_rows[0]["n"] or 0) if sip_rows else 0

    ytd_rows = query("""
        SELECT SUM(amount) as total
        FROM transactions
        WHERE month LIKE ?
          AND source_id = 'acc_bob_sip'
          AND txn_type = 'debit'
          AND category = 'Investment & SIP'
    """, (f"{year}-%",))
    sip_ytd = (ytd_rows[0]["total"] or 0.0) if ytd_rows else 0.0

    sal_rows = query("""
        SELECT SUM(amount) as total
        FROM transactions
        WHERE month = ?
          AND source_id = 'acc_sbi_salary'
          AND txn_type = 'credit'
          AND category = 'Income'
    """, (month,))
    salary = (sal_rows[0]["total"] or 0.0) if sal_rows else 0.0

    spend_rows = query("""
        SELECT SUM(amount) as total
        FROM transactions
        WHERE month = ?
          AND transaction_type = 'genuine_spend'
          AND txn_type = 'debit'
    """, (month,))
    genuine_spend = (spend_rows[0]["total"] or 0.0) if spend_rows else 0.0

    rate = ((salary - genuine_spend) / salary * 100) if salary > 0 else None

    return {
        "salary": salary,
        "genuine_spend": genuine_spend,
        "sip_month": sip_month,
        "sip_ytd": sip_ytd,
        "savings_rate": rate,
        "sip_count": sip_count,
    }


def reconciled_totals(month: str) -> dict:
    """Genuine spend total + CC reconciliation state for YYYY-MM.
    Returns {"month": str, "genuine_spend": float, "cc_reconciled_count": int,
             "cc_links": list[dict]} where cc_links contains one entry per
    reconciled CC bill for this month."""
    links = query("""
        SELECT cc_source_id, cc_total, savings_amount, delta, confidence
        FROM reconciliation_links
        WHERE cc_month = ?
    """, (month,))

    spend_rows = query("""
        SELECT SUM(amount) as total
        FROM transactions
        WHERE month = ?
          AND transaction_type = 'genuine_spend'
          AND txn_type = 'debit'
    """, (month,))
    genuine_spend = (spend_rows[0]["total"] or 0.0) if spend_rows else 0.0

    return {
        "month": month,
        "genuine_spend": genuine_spend,
        "cc_reconciled_count": len(links),
        "cc_links": [dict(r) for r in links],
    }


def category_growth(
    window: int = 3,
    as_of_month: str | None = None,
) -> list[dict]:
    """For each category, compare avg spend in last `window` months vs prior `window` months.
    Returns [{"category": str, "recent_avg": float, "prior_avg": float,
              "growth_pct": float | None}] sorted by abs(growth_pct) desc.
    growth_pct is None when prior_avg is 0 (no prior data to compare against)."""
    end = as_of_month or _date.today().strftime("%Y-%m")
    recent_months = _months_back(end, window)

    prior_end_y, prior_end_m = int(recent_months[0][:4]), int(recent_months[0][5:7])
    prior_end_m -= 1
    if prior_end_m == 0:
        prior_end_m = 12
        prior_end_y -= 1
    prior_end = f"{prior_end_y:04d}-{prior_end_m:02d}"
    prior_months = _months_back(prior_end, window)

    all_months = recent_months + prior_months
    placeholders = ",".join("?" * len(all_months))
    rows = query(f"""
        SELECT month, category,
               SUM(
                 CASE
                   WHEN splitwise_confirmed = 1 AND your_share_amount IS NOT NULL
                   THEN your_share_amount
                   ELSE amount
                 END
               ) as total
        FROM transactions
        WHERE month IN ({placeholders})
          AND transaction_type = 'genuine_spend'
          AND txn_type = 'debit'
        GROUP BY month, category
    """, tuple(all_months))

    recent_set = set(recent_months)
    prior_set = set(prior_months)
    recent_totals: dict[str, float] = {}
    prior_totals: dict[str, float] = {}
    for r in rows:
        cat, mo, total = r["category"], r["month"], r["total"]
        if not cat:
            continue
        if mo in recent_set:
            recent_totals[cat] = recent_totals.get(cat, 0.0) + total
        elif mo in prior_set:
            prior_totals[cat] = prior_totals.get(cat, 0.0) + total

    all_cats = set(recent_totals) | set(prior_totals)
    results = []
    for cat in all_cats:
        r_avg = recent_totals.get(cat, 0.0) / window
        p_avg = prior_totals.get(cat, 0.0) / window
        growth_pct = ((r_avg - p_avg) / p_avg * 100) if p_avg > 0 else None
        results.append({
            "category": cat,
            "recent_avg": round(r_avg, 2),
            "prior_avg": round(p_avg, 2),
            "growth_pct": round(growth_pct, 2) if growth_pct is not None else None,
        })

    results.sort(
        key=lambda x: abs(x["growth_pct"]) if x["growth_pct"] is not None else float("inf"),
        reverse=True,
    )
    return results
