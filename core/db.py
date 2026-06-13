"""
SQLite database layer.
All transactions are stored here after parsing + categorization.
"""

import sqlite3
import os
from core.dateparse import parse_date_to_iso as _parse_date_to_iso

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "db", "finance.db")


def get_connection():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    from core.corrections_db import init_corrections_table
    init_corrections_table()
    conn = get_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS transactions (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            date                TEXT NOT NULL,
            month               TEXT NOT NULL,          -- YYYY-MM
            description         TEXT NOT NULL,
            raw_description     TEXT,
            canonical_merchant  TEXT,                   -- stable key for corrections DB
            amount              REAL NOT NULL,
            txn_type            TEXT NOT NULL,          -- debit / credit
            source_id           TEXT NOT NULL,          -- acc_sbi_salary etc.
            source_label        TEXT NOT NULL,
            category            TEXT,
            category_source     TEXT,                   -- which pipeline step tagged it
            confidence          REAL DEFAULT 0,         -- 0.0-1.0
            is_internal_transfer INTEGER DEFAULT 0,
            splitwise_candidate  INTEGER DEFAULT 0,
            splitwise_pushed     INTEGER DEFAULT 0,
            notes               TEXT,
            created_at          TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS category_rules (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword     TEXT NOT NULL UNIQUE,
            category    TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS upload_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            filename    TEXT NOT NULL,
            source_id   TEXT NOT NULL,
            month       TEXT,
            txn_count   INTEGER,
            uploaded_at TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_txn_month ON transactions(month);
        CREATE INDEX IF NOT EXISTS idx_txn_source ON transactions(source_id);
        CREATE INDEX IF NOT EXISTS idx_txn_category ON transactions(category);
        CREATE INDEX IF NOT EXISTS idx_txn_merchant ON transactions(canonical_merchant);

        CREATE TABLE IF NOT EXISTS reconciliation_links (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            savings_txn_id  INTEGER NOT NULL UNIQUE,
            cc_source_id    TEXT NOT NULL,
            cc_month        TEXT NOT NULL,
            cc_total        REAL NOT NULL,
            savings_amount  REAL NOT NULL,
            delta           REAL NOT NULL,
            confidence      TEXT NOT NULL,
            created_at      TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_recon_savings ON reconciliation_links(savings_txn_id);
        CREATE INDEX IF NOT EXISTS idx_recon_cc ON reconciliation_links(cc_source_id, cc_month);

        -- Block 5B: tracks ingested statement periods to prevent re-upload duplicates
        CREATE TABLE IF NOT EXISTS statement_log (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            source_account TEXT NOT NULL,
            period_start   TEXT NOT NULL,   -- DD/MM/YYYY (earliest date in file)
            period_end     TEXT NOT NULL,   -- DD/MM/YYYY (latest date in file)
            file_hash      TEXT NOT NULL,   -- SHA256 of file bytes
            ingested_at    TEXT NOT NULL DEFAULT (datetime('now')),
            row_count      INTEGER,
            UNIQUE(source_account, period_start, period_end)
        );
    """)
    # Migrations: add columns for existing DBs
    for col, ddl in [
        ("category_source",    "TEXT"),
        ("confidence",         "REAL DEFAULT 0"),
        # Block 5 (Phase 2): transaction classification + linking
        ("transaction_type",   "TEXT DEFAULT 'genuine_spend'"),
        ("linked_statement_id","INTEGER"),
        ("date_parsed",        "TEXT"),      # ISO 8601 (YYYY-MM-DD) for SQL date filtering
        # Block 11 (Phase 2): Splitwise local tracking
        ("splitwise_confirmed", "INTEGER DEFAULT 0"),
        ("your_share_amount",   "REAL"),     # NULL = full amount is yours; set after confirm
        ("splitwise_group",     "TEXT"),     # e.g. "Goa trip Jan 2025"
    ]:
        try:
            conn.execute(f"ALTER TABLE transactions ADD COLUMN {col} {ddl}")
            conn.commit()
        except Exception:
            pass  # column already exists
    conn.commit()
    _backfill_date_parsed(conn)
    _normalize_date_column(conn)
    _backfill_transaction_type(conn)
    conn.close()



def _backfill_date_parsed(conn: sqlite3.Connection):
    """One-time migration: populate date_parsed for existing rows that have it null."""
    rows = conn.execute(
        "SELECT id, date FROM transactions WHERE date_parsed IS NULL"
    ).fetchall()
    for row in rows:
        iso = _parse_date_to_iso(row["date"])
        if iso:
            conn.execute("UPDATE transactions SET date_parsed = ? WHERE id = ?", (iso, row["id"]))
    conn.commit()


def _normalize_date_column(conn: sqlite3.Connection):
    """Migration: ensure `date` column is ISO (YYYY-MM-DD) for all rows.
    Converts any DD/MM/YYYY values in-place using date_parsed as source of truth."""
    rows = conn.execute(
        "SELECT id, date_parsed FROM transactions WHERE date NOT LIKE '____-__-__' AND date_parsed IS NOT NULL"
    ).fetchall()
    for row in rows:
        conn.execute("UPDATE transactions SET date = ? WHERE id = ?", (row["date_parsed"], row["id"]))
    if rows:
        conn.commit()


def _backfill_transaction_type(conn: sqlite3.Connection):
    """One-time migration: set transaction_type for rows that still have NULL."""
    conn.execute("""
        UPDATE transactions
        SET transaction_type = 'internal_transfer'
        WHERE is_internal_transfer = 1 AND (transaction_type IS NULL OR transaction_type = 'genuine_spend')
    """)
    conn.execute("""
        UPDATE transactions
        SET transaction_type = 'genuine_spend'
        WHERE transaction_type IS NULL
    """)
    conn.commit()


# ── Statement log (Block 5B) ─────────────────────────────────────────────────

def check_statement_log(source_account: str, period_start: str, period_end: str) -> dict | None:
    """Return existing statement_log entry for this period, or None."""
    conn = get_connection()
    row = conn.execute(
        """SELECT * FROM statement_log
           WHERE source_account = ? AND period_start = ? AND period_end = ?""",
        (source_account, period_start, period_end)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def add_statement_log(source_account: str, period_start: str, period_end: str,
                      file_hash: str, row_count: int):
    """Record a successfully ingested statement period."""
    conn = get_connection()
    conn.execute(
        """INSERT OR REPLACE INTO statement_log
           (source_account, period_start, period_end, file_hash, ingested_at, row_count)
           VALUES (?, ?, ?, ?, datetime('now'), ?)""",
        (source_account, period_start, period_end, file_hash, row_count)
    )
    conn.commit()
    conn.close()


def delete_statement_period(source_account: str, period_start: str, period_end: str):
    """
    --force: delete all transactions for source_account in [period_start, period_end].
    Also removes the statement_log entry so re-insert can proceed cleanly.
    """
    start_iso = _parse_date_to_iso(period_start)
    end_iso   = _parse_date_to_iso(period_end)
    conn = get_connection()

    if start_iso and end_iso:
        conn.execute(
            """DELETE FROM transactions
               WHERE source_id = ?
                 AND date_parsed BETWEEN ? AND ?""",
            (source_account, start_iso, end_iso)
        )
    else:
        # Fallback: parse in Python (handles legacy rows without date_parsed)
        rows = conn.execute(
            "SELECT id, date FROM transactions WHERE source_id = ?",
            (source_account,)
        ).fetchall()
        start_dt = _parse_date_to_iso(period_start)
        end_dt   = _parse_date_to_iso(period_end)
        ids = [
            r["id"] for r in rows
            if start_dt <= (_parse_date_to_iso(r["date"]) or "") <= end_dt
        ]
        if ids:
            conn.execute(f"DELETE FROM transactions WHERE id IN ({','.join('?'*len(ids))})", ids)

    conn.execute(
        """DELETE FROM statement_log
           WHERE source_account = ? AND period_start = ? AND period_end = ?""",
        (source_account, period_start, period_end)
    )
    conn.commit()
    conn.close()


def insert_transactions(transactions: list[dict]) -> int:
    """Insert a list of transaction dicts. Returns count inserted."""
    conn = get_connection()
    inserted = 0
    for txn in transactions:
        try:
            is_transfer = int(txn.get("is_internal_transfer", 0))
            txn_type = txn.get("transaction_type") or (
                "internal_transfer" if is_transfer else "genuine_spend"
            )
            date_parsed = _parse_date_to_iso(txn.get("date", ""))
            date_iso = date_parsed or txn.get("date", "")  # always store ISO; fallback to raw if unparseable
            conn.execute("""
                INSERT INTO transactions
                (date, month, description, raw_description, canonical_merchant,
                 amount, txn_type, source_id, source_label, category,
                 category_source, confidence,
                 is_internal_transfer, splitwise_candidate, splitwise_pushed, notes,
                 transaction_type, date_parsed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                date_iso, txn["month"], txn["description"], txn["raw_description"],
                txn.get("canonical_merchant", ""),
                txn["amount"], txn["txn_type"], txn["source_id"], txn["source_label"],
                txn["category"], txn.get("category_source"), txn.get("confidence", 0.0),
                is_transfer,
                int(txn.get("splitwise_candidate", 0)), int(txn.get("splitwise_pushed", 0)),
                txn.get("notes", ""),
                txn_type, date_parsed
            ))
            inserted += 1
        except sqlite3.IntegrityError:
            pass  # skip dupes
    conn.commit()
    conn.close()
    return inserted


def query(sql: str, params: tuple = ()) -> list[dict]:
    conn = get_connection()
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_category(txn_id: int, category: str):
    conn = get_connection()
    conn.execute("UPDATE transactions SET category = ? WHERE id = ?", (category, txn_id))
    conn.commit()
    conn.close()


def update_splitwise_pushed(txn_id: int):
    conn = get_connection()
    conn.execute("UPDATE transactions SET splitwise_pushed = 1 WHERE id = ?", (txn_id,))
    conn.commit()
    conn.close()


def confirm_split(txn_id: int, your_share: float, group: str | None = None):
    """Mark a splitwise_candidate as confirmed with your share amount."""
    conn = get_connection()
    conn.execute(
        """UPDATE transactions
           SET splitwise_confirmed = 1, your_share_amount = ?, splitwise_group = ?
           WHERE id = ?""",
        (your_share, group, txn_id)
    )
    conn.commit()
    conn.close()


def dismiss_splitwise(txn_id: int):
    """Mark a candidate as dismissed (full amount is yours, not a split)."""
    conn = get_connection()
    conn.execute(
        "UPDATE transactions SET splitwise_candidate = 0 WHERE id = ?",
        (txn_id,)
    )
    conn.commit()
    conn.close()


def get_splitwise_candidates(month: str | None = None) -> list[dict]:
    """Return unconfirmed splitwise candidates, optionally filtered by month."""
    where = "WHERE splitwise_candidate = 1 AND splitwise_confirmed = 0"
    params: tuple = ()
    if month:
        where += " AND month = ?"
        params = (month,)
    rows = query(
        f"""SELECT id, date, month, description, canonical_merchant,
                   amount, category, source_label, splitwise_group, notes
            FROM transactions
            {where}
            ORDER BY date DESC""",
        params
    )
    return rows


def get_splitwise_receivables() -> list[dict]:
    """Return confirmed splits where you're owed money (amount > your_share_amount)."""
    rows = query(
        """SELECT id, date, month, description, canonical_merchant,
                  amount, your_share_amount, splitwise_group, category
           FROM transactions
           WHERE splitwise_confirmed = 1
             AND your_share_amount IS NOT NULL
             AND your_share_amount < amount
           ORDER BY date DESC"""
    )
    return rows


def log_upload(filename: str, source_id: str, month: str, txn_count: int):
    conn = get_connection()
    conn.execute(
        "INSERT INTO upload_log (filename, source_id, month, txn_count) VALUES (?, ?, ?, ?)",
        (filename, source_id, month, txn_count)
    )
    conn.commit()
    conn.close()
