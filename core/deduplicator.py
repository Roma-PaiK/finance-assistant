"""
Deduplication + Internal Transfer Detection.
Handles SBI's verbose IMPS/UPI/NEFT description format to correctly
identify transfers between your own accounts.
"""

import re
import yaml
import os

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "accounts.yaml")

# SBI-specific: these bank codes in IMPS descriptions = your own accounts
# CNRB = Canara, HDFC = HDFC, BARB = Bank of Baroda
YOUR_BANK_CODES = {
    "cnrb": "acc_canara_daily",
    "hdfc": "acc_hdfc_emi",
    "barb": "acc_bob_sip",
    "bob":  "acc_bob_sip",
}

# Generic transfer keywords (for non-SBI banks)
TRANSFER_KEYWORDS = [
    "self transfer", "own account",
    "neft to self", "imps to self",
]

# Salary credit signals — never mark these as internal transfers
SALARY_SIGNALS = [
    r"bajaj finance",       # your salary source
    r"salary",
    r"sal\s*cr",
    r"payroll",
    r"cmp .+? ltd",         # company payments
]

# Patterns that are always internal transfers from SBI regardless of bank code
SBI_OWN_TRANSFER_PATTERNS = [
    r"imps/\d+/cnrb",      # IMPS to Canara (your daily account)
    r"imps/\d+/hdfc",      # IMPS to HDFC (your EMI account)
    r"imps/\d+/barb",      # IMPS to BOB (your SIP account)
    r"upi/dr/\d+/.+/cnrb", # UPI to Canara
    r"upi/dr/\d+/.+/hdfc", # UPI to HDFC
    r"neft.+cnrb",
    r"neft.+hdfc bank",
]


def load_transfer_rules() -> list[dict]:
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    return cfg.get("internal_transfers", [])


def _is_salary(desc: str) -> bool:
    desc_lower = desc.lower()
    return any(re.search(p, desc_lower) for p in SALARY_SIGNALS)


def _is_sbi_own_transfer(desc: str) -> bool:
    desc_lower = desc.lower()
    return any(re.search(p, desc_lower) for p in SBI_OWN_TRANSFER_PATTERNS)


def flag_internal_transfers(transactions: list[dict], source_id: str) -> list[dict]:
    """
    Mark transactions as internal transfers.
    Uses SBI-specific IMPS/UPI pattern matching + generic keywords.
    """
    transfer_source_ids = {"acc_sbi_salary", "acc_canara_daily", "acc_hdfc_emi", "acc_bob_sip"}

    for txn in transactions:
        if txn.get("is_internal_transfer"):
            continue

        desc = txn.get("description", "") or ""
        raw = txn.get("raw_description", "") or ""
        combined = f"{desc} {raw}"

        # Never flag salary credits as internal transfers
        if _is_salary(combined):
            continue

        # SBI-specific: check IMPS/UPI/NEFT patterns with your bank codes
        if source_id == "acc_sbi_salary" and _is_sbi_own_transfer(combined):
            txn["is_internal_transfer"] = True
            continue

        # Generic: description contains transfer keywords
        combined_lower = combined.lower()
        if any(kw in combined_lower for kw in TRANSFER_KEYWORDS):
            if source_id in transfer_source_ids:
                txn["is_internal_transfer"] = True

    return transactions


def dedup_transactions(existing: list[dict], new_txns: list[dict]) -> list[dict]:
    """
    Remove transactions already in DB.
    Match key: date + amount + txn_type + source_id
    """
    existing_keys = {
        (t["date"], str(t["amount"]), t["txn_type"], t["source_id"])
        for t in existing
    }
    return [
        t for t in new_txns
        if (t["date"], str(t["amount"]), t["txn_type"], t["source_id"]) not in existing_keys
    ]