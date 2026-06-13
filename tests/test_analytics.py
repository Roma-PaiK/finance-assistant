"""
Layer A unit tests — each function tested against a known fixture DB.
All expected values are computed from the fixture dataset in conftest.py.
"""
import pytest
from core.analytics import (
    spend_by_category,
    monthly_trend,
    top_merchants,
    savings_rate,
    reconciled_totals,
    category_growth,
    _months_back,
)


# ── _months_back helper ───────────────────────────────────────────────────────

def test_months_back_basic():
    assert _months_back("2025-06", 3) == ["2025-04", "2025-05", "2025-06"]

def test_months_back_year_boundary():
    assert _months_back("2025-02", 3) == ["2024-12", "2025-01", "2025-02"]

def test_months_back_single():
    assert _months_back("2025-03", 1) == ["2025-03"]


# ── spend_by_category ─────────────────────────────────────────────────────────

def test_spend_by_category_totals(test_db):
    result = spend_by_category("2025-03")
    # Food: 1000 + 500 + 500 (your_share) = 2000
    assert result["Food & Dining"] == pytest.approx(2000.0)
    assert result["Groceries"] == pytest.approx(3000.0)
    assert result["Investment & SIP"] == pytest.approx(5000.0)

def test_spend_by_category_excludes_cc_settlement(test_db):
    result = spend_by_category("2025-03")
    # cc_settlement row should not appear
    assert "Credit Card Payment" not in result

def test_spend_by_category_excludes_credits(test_db):
    result = spend_by_category("2025-03")
    # Income credit should not appear
    assert "Income" not in result

def test_spend_by_category_empty_month(test_db):
    result = spend_by_category("2025-01")
    assert result == {}


# ── monthly_trend ─────────────────────────────────────────────────────────────

def test_monthly_trend_length(test_db):
    result = monthly_trend("Food & Dining", 2, as_of_month="2025-04")
    assert len(result) == 2

def test_monthly_trend_order(test_db):
    result = monthly_trend("Food & Dining", 2, as_of_month="2025-04")
    assert result[0]["month"] == "2025-03"
    assert result[1]["month"] == "2025-04"

def test_monthly_trend_values(test_db):
    result = monthly_trend("Food & Dining", 2, as_of_month="2025-04")
    assert result[0]["total"] == pytest.approx(2000.0)  # splitwise-aware
    assert result[1]["total"] == pytest.approx(1500.0)

def test_monthly_trend_zero_for_missing_month(test_db):
    # 2025-01 has no data
    result = monthly_trend("Food & Dining", 2, as_of_month="2025-02")
    assert result[0]["total"] == pytest.approx(0.0)
    assert result[1]["total"] == pytest.approx(0.0)


# ── top_merchants ─────────────────────────────────────────────────────────────

def test_top_merchants_sorted_desc(test_db):
    result = top_merchants("2025-03", n=10)
    totals = [r["total"] for r in result]
    assert totals == sorted(totals, reverse=True)

def test_top_merchants_values(test_db):
    result = top_merchants("2025-03", n=10)
    by_name = {r["merchant"]: r for r in result}
    # BigBasket: 3000 (1 txn)
    assert by_name["BigBasket"]["total"] == pytest.approx(3000.0)
    assert by_name["BigBasket"]["txn_count"] == 1
    # Swiggy: 1000 + 1000 = 2000 (2 txns, full amounts — not splitwise-adjusted)
    assert by_name["Swiggy"]["total"] == pytest.approx(2000.0)
    assert by_name["Swiggy"]["txn_count"] == 2
    # Zomato: 500 (1 txn)
    assert by_name["Zomato"]["total"] == pytest.approx(500.0)

def test_top_merchants_excludes_cc_settlement(test_db):
    result = top_merchants("2025-03", n=10)
    names = [r["merchant"] for r in result]
    assert "HDFC CC Payment" not in names

def test_top_merchants_n_limit(test_db):
    result = top_merchants("2025-03", n=2)
    assert len(result) == 2


# ── savings_rate ──────────────────────────────────────────────────────────────

def test_savings_rate_salary(test_db):
    result = savings_rate("2025-03")
    assert result["salary"] == pytest.approx(100000.0)

def test_savings_rate_sip(test_db):
    result = savings_rate("2025-03")
    assert result["sip_month"] == pytest.approx(5000.0)
    assert result["sip_count"] == 1

def test_savings_rate_sip_ytd(test_db):
    # only one month of SIP data in 2025
    result = savings_rate("2025-03")
    assert result["sip_ytd"] == pytest.approx(5000.0)

def test_savings_rate_genuine_spend(test_db):
    # 1000 + 500 + 1000 (full, not share) + 3000 + 5000 = 10500
    result = savings_rate("2025-03")
    assert result["genuine_spend"] == pytest.approx(10500.0)

def test_savings_rate_calculation(test_db):
    result = savings_rate("2025-03")
    expected = (100000.0 - 10500.0) / 100000.0 * 100
    assert result["savings_rate"] == pytest.approx(expected)

def test_savings_rate_none_when_no_salary(test_db):
    result = savings_rate("2025-04")
    assert result["salary"] == pytest.approx(0.0)
    assert result["savings_rate"] is None


# ── reconciled_totals ─────────────────────────────────────────────────────────

def test_reconciled_totals_genuine_spend(test_db):
    result = reconciled_totals("2025-03")
    # 1000 + 500 + 1000 + 3000 + 5000 = 10500 (cc_settlement excluded)
    assert result["genuine_spend"] == pytest.approx(10500.0)

def test_reconciled_totals_cc_count(test_db):
    result = reconciled_totals("2025-03")
    assert result["cc_reconciled_count"] == 1

def test_reconciled_totals_cc_link_content(test_db):
    result = reconciled_totals("2025-03")
    link = result["cc_links"][0]
    assert link["cc_source_id"] == "cc_hdfc_moneyback"
    assert link["cc_total"] == pytest.approx(3000.0)
    assert link["confidence"] == "exact"

def test_reconciled_totals_unreconciled_month(test_db):
    result = reconciled_totals("2025-04")
    assert result["cc_reconciled_count"] == 0
    assert result["cc_links"] == []


# ── category_growth ───────────────────────────────────────────────────────────

def test_category_growth_returns_list(test_db):
    result = category_growth(window=1, as_of_month="2025-04")
    assert isinstance(result, list)
    assert len(result) > 0

def test_category_growth_sorted_by_abs_pct(test_db):
    result = category_growth(window=1, as_of_month="2025-04")
    abs_pcts = [abs(r["growth_pct"]) for r in result if r["growth_pct"] is not None]
    assert abs_pcts == sorted(abs_pcts, reverse=True)

def test_category_growth_values(test_db):
    result = category_growth(window=1, as_of_month="2025-04")
    by_cat = {r["category"]: r for r in result}
    # Food: recent=1500, prior=2000 → -25%
    assert by_cat["Food & Dining"]["recent_avg"] == pytest.approx(1500.0)
    assert by_cat["Food & Dining"]["prior_avg"] == pytest.approx(2000.0)
    assert by_cat["Food & Dining"]["growth_pct"] == pytest.approx(-25.0)
    # Groceries: recent=2000, prior=3000 → -33.33%
    assert by_cat["Groceries"]["recent_avg"] == pytest.approx(2000.0)
    assert by_cat["Groceries"]["prior_avg"] == pytest.approx(3000.0)
    assert by_cat["Groceries"]["growth_pct"] == pytest.approx(-100 / 3, rel=1e-3)

def test_category_growth_none_when_no_prior(test_db):
    # 2025-01 and 2025-02 have no data, so prior_avg=0 for all categories
    result = category_growth(window=1, as_of_month="2025-03")
    # All categories in 2025-03 have no prior (2025-02 is empty)
    for r in result:
        assert r["growth_pct"] is None, f"{r['category']} should have None growth_pct"

def test_category_growth_investment_disappears(test_db):
    # Investment & SIP in 2025-03 but not 2025-04 → recent_avg=0, growth=-100%
    result = category_growth(window=1, as_of_month="2025-04")
    by_cat = {r["category"]: r for r in result}
    assert "Investment & SIP" in by_cat
    assert by_cat["Investment & SIP"]["recent_avg"] == pytest.approx(0.0)
    assert by_cat["Investment & SIP"]["growth_pct"] == pytest.approx(-100.0)
