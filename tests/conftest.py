"""
Shared pytest fixtures for Layer A analytics tests.
"""
import os
import sys
import sqlite3

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.db
import core.corrections_db
from core.db import init_db, get_connection


@pytest.fixture
def test_db(monkeypatch, tmp_path):
    """
    Temp SQLite DB with a known transaction set for analytics unit tests.

    2025-03:
      - Food & Dining: ₹1000 (Swiggy, full) + ₹500 (Zomato, full) + ₹1000 (Swiggy, splitwise your_share=500)
        → spend_by_category = 2000.0 ; top_merchants Swiggy total = 2000.0
      - Groceries: ₹3000 (BigBasket, full)
      - Investment & SIP: ₹5000 (acc_bob_sip debit)
      - Income: ₹100000 credit (acc_sbi_salary)
      - cc_settlement: ₹3000 debit (excluded from genuine_spend)
      - reconciliation_links: one row linking cc_hdfc_moneyback for 2025-03

    2025-04:
      - Food & Dining: ₹1500 (Zomato, full)
      - Groceries: ₹2000 (BigBasket, full)
    """
    db_file = str(tmp_path / "test_finance.db")
    monkeypatch.setattr(core.db, "DB_PATH", db_file)
    monkeypatch.setattr(core.corrections_db, "DB_PATH", db_file)

    init_db()

    txns = [
        # ── 2025-03 ───────────────────────────────────────────────────────────
        dict(date="2025-03-05", month="2025-03", description="Swiggy Order",
             raw_description="Swiggy Order", canonical_merchant="Swiggy",
             amount=1000.0, txn_type="debit", source_id="acc_canara_daily",
             source_label="Canara Daily", category="Food & Dining",
             transaction_type="genuine_spend"),
        dict(date="2025-03-10", month="2025-03", description="Zomato Order",
             raw_description="Zomato Order", canonical_merchant="Zomato",
             amount=500.0, txn_type="debit", source_id="acc_canara_daily",
             source_label="Canara Daily", category="Food & Dining",
             transaction_type="genuine_spend"),
        dict(date="2025-03-15", month="2025-03", description="Swiggy Order",
             raw_description="Swiggy Order", canonical_merchant="Swiggy",
             amount=1000.0, txn_type="debit", source_id="acc_canara_daily",
             source_label="Canara Daily", category="Food & Dining",
             transaction_type="genuine_spend"),
        dict(date="2025-03-12", month="2025-03", description="BigBasket",
             raw_description="BigBasket", canonical_merchant="BigBasket",
             amount=3000.0, txn_type="debit", source_id="cc_hdfc_moneyback",
             source_label="HDFC Moneyback", category="Groceries",
             transaction_type="genuine_spend"),
        dict(date="2025-03-01", month="2025-03", description="CMP Salary",
             raw_description="CMP Salary Ltd", canonical_merchant="CMP Salary",
             amount=100000.0, txn_type="credit", source_id="acc_sbi_salary",
             source_label="SBI Salary", category="Income",
             transaction_type="genuine_spend"),
        dict(date="2025-03-03", month="2025-03", description="SIP Deduction",
             raw_description="ACH DR SIP", canonical_merchant="SIP",
             amount=5000.0, txn_type="debit", source_id="acc_bob_sip",
             source_label="BOB SIP", category="Investment & SIP",
             transaction_type="genuine_spend"),
        dict(date="2025-03-20", month="2025-03", description="CC Payment HDFC",
             raw_description="CC Payment", canonical_merchant="HDFC CC Payment",
             amount=3000.0, txn_type="debit", source_id="acc_canara_daily",
             source_label="Canara Daily", category="Credit Card Payment",
             transaction_type="cc_settlement"),
        # ── 2025-04 ───────────────────────────────────────────────────────────
        dict(date="2025-04-08", month="2025-04", description="Zomato Order",
             raw_description="Zomato Order", canonical_merchant="Zomato",
             amount=1500.0, txn_type="debit", source_id="acc_canara_daily",
             source_label="Canara Daily", category="Food & Dining",
             transaction_type="genuine_spend"),
        dict(date="2025-04-15", month="2025-04", description="BigBasket",
             raw_description="BigBasket", canonical_merchant="BigBasket",
             amount=2000.0, txn_type="debit", source_id="cc_hdfc_moneyback",
             source_label="HDFC Moneyback", category="Groceries",
             transaction_type="genuine_spend"),
    ]

    from core.db import insert_transactions
    insert_transactions(txns)

    # Set splitwise_confirmed + your_share_amount on the 3rd Food transaction (Swiggy ₹1000 on 2025-03-15)
    conn = get_connection()
    conn.execute("""
        UPDATE transactions
        SET splitwise_confirmed = 1, your_share_amount = 500.0
        WHERE date = '2025-03-15' AND canonical_merchant = 'Swiggy' AND amount = 1000.0
    """)
    conn.commit()

    # Add reconciliation_links row for 2025-03
    cc_txn = conn.execute(
        "SELECT id FROM transactions WHERE transaction_type = 'cc_settlement' LIMIT 1"
    ).fetchone()
    if cc_txn:
        conn.execute("""
            INSERT INTO reconciliation_links
            (savings_txn_id, cc_source_id, cc_month, cc_total, savings_amount, delta, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (cc_txn["id"], "cc_hdfc_moneyback", "2025-03", 3000.0, 3000.0, 0.0, "exact"))
        conn.commit()

    conn.close()
    yield
