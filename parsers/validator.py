"""
Block 1 — Extraction validation.

After parsing, check that extracted rows are internally consistent.
For savings accounts: opening + credits - debits ≈ closing balance.
For CCs / when metadata unavailable: warn but don't fail.

Validation statuses:
  pass  — balance math checks out (delta ≤ BALANCE_TOLERANCE)
  warn  — no balance metadata found; row count > 0; proceed with caution
  fail  — balance mismatch OR 0 rows extracted from non-empty file
"""

from dataclasses import dataclass
from typing import Optional
from core.settings import SETTINGS

BALANCE_TOLERANCE: float = SETTINGS["validator"]["balance_tolerance"]


@dataclass
class ParseValidation:
    source_file: str
    row_count: int
    status: str                          # "pass" | "warn" | "fail"
    message: str = ""
    opening_balance: Optional[float] = None
    closing_balance: Optional[float] = None
    computed_closing: Optional[float] = None
    delta: Optional[float] = None

    def is_ok(self) -> bool:
        """True if pipeline should continue (pass or warn)."""
        return self.status in ("pass", "warn")

    def summary(self) -> str:
        icon = {"pass": "✅", "warn": "⚠️ ", "fail": "❌"}[self.status]
        base = f"{icon} Validation {self.status.upper()} — {self.row_count} rows"
        if self.status == "pass":
            return f"{base}, balance delta ₹{self.delta:.2f}"
        if self.message:
            return f"{base}. {self.message}"
        return base


def validate_balance(
    transactions: list,
    opening_balance: Optional[float],
    closing_balance: Optional[float],
    source_file: str,
    source_id: str = "",
) -> ParseValidation:
    """
    Core balance-math check for savings accounts.

    transactions : list of Transaction objects (must have .amount and .txn_type)
    opening_balance : extracted from statement metadata (or None)
    closing_balance : extracted from statement metadata (or None)
    """
    row_count = len(transactions)

    if row_count == 0:
        return ParseValidation(
            source_file=source_file,
            row_count=0,
            status="fail",
            message="0 transactions extracted. File may be malformed, unreadable, or use an unsupported format.",
        )

    if opening_balance is None or closing_balance is None:
        return ParseValidation(
            source_file=source_file,
            row_count=row_count,
            status="warn",
            message="Opening/closing balance not found in metadata — balance check skipped.",
        )

    # CC statements don't follow bank balance equation (payment rows absent from file)
    if source_id.startswith("cc_"):
        return ParseValidation(
            source_file=source_file,
            row_count=row_count,
            status="warn",
            message="Credit card statement — balance check skipped (CC uses different equation).",
        )

    debits  = sum(t.amount for t in transactions if t.txn_type == "debit")
    credits = sum(t.amount for t in transactions if t.txn_type == "credit")
    computed = round(opening_balance + credits - debits, 2)
    delta    = round(abs(computed - closing_balance), 2)

    if delta <= BALANCE_TOLERANCE:
        return ParseValidation(
            source_file=source_file,
            row_count=row_count,
            status="pass",
            opening_balance=opening_balance,
            closing_balance=closing_balance,
            computed_closing=computed,
            delta=delta,
        )

    return ParseValidation(
        source_file=source_file,
        row_count=row_count,
        status="fail",
        message=(
            f"Balance mismatch: opening ₹{opening_balance:,.2f} + credits ₹{credits:,.2f} "
            f"- debits ₹{debits:,.2f} = ₹{computed:,.2f}, "
            f"expected ₹{closing_balance:,.2f} (delta ₹{delta:,.2f}). "
            "Possible missed rows or extraction error."
        ),
        opening_balance=opening_balance,
        closing_balance=closing_balance,
        computed_closing=computed,
        delta=delta,
    )
