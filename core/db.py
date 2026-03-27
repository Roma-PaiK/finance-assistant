"""
SQLite database layer.
All transactions are stored here after parsing + categorization.
"""

import sqlite3
import os
from datetime import date

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "db", "finance.db")


def get_connection():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS transactions (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            date                TEXT NOT NULL,
            month               TEXT NOT NULL,          -- YYYY-MM
            description         TEXT NOT NULL,
            raw_description     TEXT,
            amount              REAL NOT NULL,
            txn_type            TEXT NOT NULL,          -- debit / credit
            source_id           TEXT NOT NULL,          -- acc_sbi_salary etc.
            source_label        TEXT NOT NULL,
            category            TEXT,
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
    """)
    conn.commit()
    conn.close()


def insert_transactions(transactions: list[dict]) -> int:
    """Insert a list of transaction dicts. Returns count inserted."""
    conn = get_connection()
    inserted = 0
    for txn in transactions:
        try:
            conn.execute("""
                INSERT INTO transactions
                (date, month, description, raw_description, amount, txn_type,
                 source_id, source_label, category, is_internal_transfer,
                 splitwise_candidate, splitwise_pushed, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                txn["date"], txn["month"], txn["description"], txn["raw_description"],
                txn["amount"], txn["txn_type"], txn["source_id"], txn["source_label"],
                txn["category"], int(txn["is_internal_transfer"]),
                int(txn["splitwise_candidate"]), int(txn["splitwise_pushed"]),
                txn.get("notes", "")
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


def log_upload(filename: str, source_id: str, month: str, txn_count: int):
    conn = get_connection()
    conn.execute(
        "INSERT INTO upload_log (filename, source_id, month, txn_count) VALUES (?, ?, ?, ?)",
        (filename, source_id, month, txn_count)
    )
    conn.commit()
    conn.close()
