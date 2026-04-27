"""
Block 3 — Categorization Pipeline (Decision Tree)

Order (first hit wins):
1. is_internal_transfer flag
2. Corrections DB lookup (canonical_merchant)
3. SBI raw pattern rules
4. YAML keyword rules (cleaned desc, then raw)
5. LLM fallback (Ollama)
6. "Other" fallback

Every txn gets: category, category_source, confidence
"""

import re
import yaml
import os
import json
import requests
from core.description_cleaner import clean_description
import core.corrections_db as corrections_db

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "categories.yaml")
OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "llama3"


def load_rules() -> dict[str, list[str]]:
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    return cfg.get("categories", {})


# SBI raw description pattern rules — match on raw string before cleaning
SBI_RAW_RULES = [
    (r"imps/\d+/cnrb",                        "Internal Transfer"),
    (r"imps/\d+/hdfc",                         "Internal Transfer"),
    (r"imps/\d+/barb",                         "Internal Transfer"),
    (r"upi/dr/.+/cnrb",                        "Internal Transfer"),
    (r"upi/dr/.+/hdfc",                        "Internal Transfer"),
    (r"neft.+(canara|cnrb)",                   "Internal Transfer"),
    (r"bajaj finance",                         "Salary"),
    (r"cmp\s+\w.+\s+ltd",                     "Salary"),
    (r"int\.cr|savings bank interest|int cr",  "Interest"),
    (r"atm\s*wdl|atm/",                       "ATM & Cash"),
    (r"ach\s+dr.+sip",                        "Investment & SIP"),
    (r"ach\s+dr.+mutual",                     "Investment & SIP"),
    (r"ach\s+dr.+loan",                       "EMI & Loan"),
    (r"ach\s+dr.+finance",                    "EMI & Loan"),
    (r"upi/dr/.+/paytm",                      "Utilities & Bills"),
    (r"upi/dr/.+/swiggy",                     "Food & Dining"),
    (r"upi/dr/.+/zomato",                     "Food & Dining"),
]


def _corrections_lookup(canonical_merchant: str) -> tuple[str | None, float]:
    """Returns (category, confidence). Confidence scales with correction count."""
    if not canonical_merchant:
        return None, 0.0
    cat = corrections_db.lookup(canonical_merchant)
    if not cat:
        return None, 0.0
    # Get confidence_count for this merchant to scale confidence
    for row in corrections_db.get_all():
        if row["canonical_merchant"] == canonical_merchant:
            count = row.get("confidence_count", 1)
            conf = 0.95 if count >= 3 else (0.85 if count >= 2 else 0.80)
            return cat, conf
    return cat, 0.80


def _raw_rules_match(raw: str) -> str | None:
    raw_lower = raw.lower()
    for pattern, category in SBI_RAW_RULES:
        if re.search(pattern, raw_lower):
            return category
    return None


def _yaml_rules_match(text: str, rules: dict) -> str | None:
    text_lower = text.lower()
    for category, keywords in rules.items():
        if not keywords:
            continue
        for kw in keywords:
            if kw.lower() in text_lower:
                return category
    return None


def _llm_categorize(description: str, raw: str, valid_categories: set[str]) -> tuple[str, float]:
    cats_list = "\n".join(f"- {c}" for c in sorted(valid_categories))
    prompt = f"""You are a personal finance categorizer for an Indian user.
Categorize this bank transaction into EXACTLY ONE of these categories:
{cats_list}

Transaction: "{description}"
Raw bank text: "{raw}"

Reply with JSON only: {{"category": "<category>", "confidence": <0.0-1.0>}}
Use "Other" if genuinely unsure. No explanation, no extra text."""

    try:
        resp = requests.post(OLLAMA_URL, json={
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
        }, timeout=15)
        text = resp.json().get("response", "").strip()
        match = re.search(r'\{.*?\}', text, re.DOTALL)
        if match:
            data = json.loads(match.group())
            cat = data.get("category", "Other")
            conf = float(data.get("confidence", 0.6))
            if cat not in valid_categories:
                return "Other", 0.0
            return cat, conf
    except Exception:
        pass
    return "Other", 0.0


def categorize_transactions(transactions: list[dict], use_llm: bool = True) -> list[dict]:
    rules = load_rules()
    valid_cats = set(rules.keys())
    corrections_db.init_corrections_table()

    for txn in transactions:
        raw = txn.get("raw_description", "") or txn.get("description", "")
        cleaned = clean_description(raw)
        txn["description"] = cleaned
        canonical = txn.get("canonical_merchant", "")

        # Step 1: internal transfer flag (set by deduplicator)
        if txn.get("is_internal_transfer"):
            txn["category"] = "Internal Transfer"
            txn["category_source"] = "internal_transfer_flag"
            txn["confidence"] = 1.0
            continue

        # Step 2: corrections DB — canonical merchant match
        cat, conf = _corrections_lookup(canonical)
        if cat:
            txn["category"] = cat
            txn["category_source"] = "corrections_db"
            txn["confidence"] = conf
            continue

        # Step 3: SBI raw pattern rules
        cat = _raw_rules_match(raw)
        if cat:
            txn["category"] = cat
            txn["category_source"] = "raw_rules"
            txn["confidence"] = 0.9
            continue

        # Step 4: YAML keyword rules (try cleaned desc first, then raw)
        cat = _yaml_rules_match(cleaned, rules) or _yaml_rules_match(raw, rules)
        if cat:
            txn["category"] = cat
            txn["category_source"] = "yaml_rules"
            txn["confidence"] = 0.8
            continue

        # Step 5: LLM fallback
        if use_llm:
            cat, conf = _llm_categorize(cleaned, raw, valid_cats)
            if cat != "Other":
                txn["category"] = cat
                txn["category_source"] = "llm"
                txn["confidence"] = conf
                continue

        # Step 6: Other fallback
        txn["category"] = "Other"
        txn["category_source"] = "fallback"
        txn["confidence"] = 0.0

    # Sync is_internal_transfer flag post-categorization
    for txn in transactions:
        if txn.get("category") == "Internal Transfer":
            txn["is_internal_transfer"] = True

    return transactions
