"""
Two-tier categorization:
1. Rule engine — keyword matching from categories.yaml (fast, free)
2. Ollama LLM fallback — for unknowns (local, free)

Also cleans raw descriptions before categorizing.
"""

import re
import yaml
import os
import requests
from core.description_cleaner import clean_description

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "categories.yaml")
OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "llama3"


def load_rules() -> dict[str, list[str]]:
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    return cfg.get("categories", {})


# Hard-coded high-confidence rules for SBI raw description patterns
# These run BEFORE the yaml rules, on the RAW description
SBI_RAW_RULES = [
    # Internal transfers — belt-and-suspenders with deduplicator
    (r"imps/\d+/cnrb",              "Internal Transfer"),
    (r"imps/\d+/hdfc",              "Internal Transfer"),
    (r"imps/\d+/barb",              "Internal Transfer"),
    (r"upi/dr/.+/cnrb",             "Internal Transfer"),
    (r"upi/dr/.+/hdfc",             "Internal Transfer"),
    (r"neft.+(canara|cnrb)",        "Internal Transfer"),

    # Salary
    (r"bajaj finance",              "Salary"),
    (r"cmp\s+\w.+\s+ltd",          "Salary"),

    # Interest
    (r"int\.cr|savings bank interest|int cr", "Interest"),

    # ATM
    (r"atm\s*wdl|atm/",            "ATM & Cash"),

    # Auto debits — SIP/EMI typically come as ACH DR
    (r"ach\s+dr.+sip",             "Investment & SIP"),
    (r"ach\s+dr.+mutual",          "Investment & SIP"),
    (r"ach\s+dr.+loan",            "EMI & Loan"),
    (r"ach\s+dr.+finance",         "EMI & Loan"),

    # UPI spends (not to own accounts) — let YAML rules handle merchant name
    # These are catch-alls only
    (r"upi/dr/.+/paytm",           "Utilities & Bills"),
    (r"upi/dr/.+/swiggy",          "Food & Dining"),
    (r"upi/dr/.+/zomato",          "Food & Dining"),
]


def categorize_by_raw_rules(raw_description: str) -> str | None:
    """Match against raw SBI description patterns first."""
    raw_lower = raw_description.lower()
    for pattern, category in SBI_RAW_RULES:
        if re.search(pattern, raw_lower):
            return category
    return None


def categorize_by_rules(description: str, rules: dict) -> str | None:
    """Match cleaned description against categories.yaml keywords."""
    desc_lower = description.lower()
    for category, keywords in rules.items():
        for kw in keywords:
            if kw.lower() in desc_lower:
                return category
    return None


def categorize_by_llm(description: str, raw: str = "") -> str:
    """Ask local Ollama to categorize. Uses cleaned description + raw as context."""
    prompt = f"""You are a personal finance categorizer for an Indian user.
Categorize this bank transaction into ONE of these categories:
Food & Dining, Groceries, Shopping, Fuel & Transport, Entertainment,
Utilities & Bills, Health & Medical, Education, EMI & Loan,
Investment & SIP, Rent, Salary, Interest, Credit Card Payment,
Internal Transfer, ATM & Cash, Subscriptions, Other.

Transaction description: "{description}"
Raw bank text: "{raw}"

Reply with ONLY the category name, nothing else."""

    try:
        resp = requests.post(OLLAMA_URL, json={
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False
        }, timeout=15)
        result = resp.json().get("response", "").strip()
        # Validate it's one of our known categories
        return result if result else "Other"
    except Exception:
        return "Other"


def categorize_transactions(transactions: list[dict], use_llm: bool = True) -> list[dict]:
    """
    Full categorization pipeline per transaction:
    1. Already flagged as internal transfer → done
    2. Raw description → SBI pattern rules
    3. Cleaned description → YAML keyword rules
    4. Ollama LLM fallback
    Also cleans the description in place.
    """
    rules = load_rules()

    for txn in transactions:
        raw = txn.get("raw_description", "") or txn.get("description", "")

        # Clean the description and store it back
        cleaned = clean_description(raw)
        txn["description"] = cleaned

        # Already marked internal transfer
        if txn.get("is_internal_transfer"):
            txn["category"] = "Internal Transfer"
            continue

        # Already has a category
        if txn.get("category") and txn["category"] != "Other":
            continue

        # Tier 1a: raw SBI pattern rules
        cat = categorize_by_raw_rules(raw)

        # Tier 1b: cleaned description vs yaml rules
        if not cat:
            cat = categorize_by_rules(cleaned, rules)

        # Tier 1c: also try raw vs yaml rules (catches merchant names in raw)
        if not cat:
            cat = categorize_by_rules(raw, rules)

        # Tier 2: LLM fallback
        if not cat and use_llm:
            cat = categorize_by_llm(cleaned, raw)

        txn["category"] = cat or "Other"

    # Sync flag: if category resolved to Internal Transfer, mark the flag too
    for txn in transactions:
        if txn.get("category") == "Internal Transfer":
            txn["is_internal_transfer"] = True

    return transactions