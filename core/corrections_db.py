"""
Block 2 — Corrections Database (cache).

Stores canonical_merchant → category mappings learned from dry-run corrections.
Each upsert increments confidence_count so high-confidence mappings are visible.

Operations:
  lookup(canonical_merchant) → category or None
  upsert(canonical_merchant, category, source_account_hint, notes)
  get_all() → list of all rows (for inspection)
"""

import sqlite3
import os
from datetime import date

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "db", "finance.db")


def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_corrections_table():
    """Create corrections table if not exists. Called from init_db()."""
    conn = _conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS corrections (
            canonical_merchant   TEXT PRIMARY KEY,
            category             TEXT NOT NULL,
            confidence_count     INTEGER DEFAULT 1,
            last_seen_date       TEXT,
            source_account_hint  TEXT,   -- e.g. "cc_hdfc_moneyback" if merchant means different things on diff cards
            notes                TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_corrections_merchant ON corrections(canonical_merchant);
    """)
    conn.commit()
    conn.close()


def lookup(canonical_merchant: str) -> str | None:
    """Return cached category for merchant, or None if not cached."""
    if not canonical_merchant:
        return None
    conn = _conn()
    row = conn.execute(
        "SELECT category FROM corrections WHERE canonical_merchant = ?",
        (canonical_merchant.strip(),)
    ).fetchone()
    conn.close()
    return row["category"] if row else None


def upsert(
    canonical_merchant: str,
    category: str,
    source_account_hint: str = "",
    notes: str = "",
):
    """
    Insert or update a merchant → category mapping.
    Increments confidence_count on repeat corrections.
    """
    if not canonical_merchant or not category:
        return
    today = date.today().isoformat()
    conn = _conn()
    existing = conn.execute(
        "SELECT confidence_count FROM corrections WHERE canonical_merchant = ?",
        (canonical_merchant.strip(),)
    ).fetchone()

    if existing:
        conn.execute("""
            UPDATE corrections
            SET category = ?, confidence_count = confidence_count + 1,
                last_seen_date = ?, source_account_hint = ?, notes = ?
            WHERE canonical_merchant = ?
        """, (category, today, source_account_hint, notes, canonical_merchant.strip()))
    else:
        conn.execute("""
            INSERT INTO corrections
            (canonical_merchant, category, confidence_count, last_seen_date, source_account_hint, notes)
            VALUES (?, ?, 1, ?, ?, ?)
        """, (canonical_merchant.strip(), category, today, source_account_hint, notes))

    conn.commit()
    conn.close()


def get_all() -> list[dict]:
    """Return all corrections rows sorted by confidence desc."""
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM corrections ORDER BY confidence_count DESC, canonical_merchant"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def stats() -> dict:
    conn = _conn()
    total = conn.execute("SELECT COUNT(*) as c FROM corrections").fetchone()["c"]
    high = conn.execute("SELECT COUNT(*) as c FROM corrections WHERE confidence_count >= 3").fetchone()["c"]
    conn.close()
    return {"total_merchants": total, "high_confidence_3plus": high}
