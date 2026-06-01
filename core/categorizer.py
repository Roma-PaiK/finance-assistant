"""
Block 3 — Categorization Pipeline (Decision Tree)

Order (first hit wins):
1. is_internal_transfer flag
2. Corrections DB lookup (canonical_merchant)
3. SBI raw pattern rules
4. YAML keyword rules (cleaned desc, then raw)
   4a. Single match → assign directly (confidence 0.8)
   4b. Multiple matches, one keyword is more specific (longer) → longer wins, no LLM
   4c. Genuinely ambiguous (multiple matches, no clear winner) → LLM with constrained candidates
5. LLM fallback (Ollama) — full fallback or constrained conflict resolution
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
from core.contact_matcher import get_contacts, get_aliases, match_contact

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "categories.yaml")
OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "qwen2.5:7b"
OLLAMA_TIMEOUT = 30  # qwen 7B cold-load can take ~10-15s on first call

# Module-level counters for LLM call accounting (reset per categorize_transactions run)
_llm_stats = {"sent": 0, "returned_valid": 0, "returned_other": 0, "errors": 0}


def load_rules() -> dict[str, list[str]]:
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    result = {}
    for cat, val in cfg.get("categories", {}).items():
        if isinstance(val, dict):
            result[cat] = val.get("keywords") or []
        else:
            result[cat] = val or []
    return result


def load_hints() -> dict[str, str]:
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    return {
        cat: val["hint"]
        for cat, val in cfg.get("categories", {}).items()
        if isinstance(val, dict) and val.get("hint")
    }


def load_llm_excluded() -> set[str]:
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    return set(cfg.get("llm_excluded", []))


# SBI raw description pattern rules — match on raw string before cleaning
SBI_RAW_RULES = [
    (r"imps/\d+/cnrb",                        "Internal Transfer"),
    (r"imps/\d+/hdfc",                         "Internal Transfer"),
    (r"imps/\d+/barb",                         "Internal Transfer"),
    (r"upi/dr/.+/cnrb",                        "Internal Transfer"),
    (r"upi/dr/.+/hdfc",                        "Internal Transfer"),
    (r"neft.+(canara|cnrb)",                   "Internal Transfer"),
    (r"bajaj finance",                         "Income"),
    (r"cmp\s+\w.+\s+ltd",                     "Income"),
    (r"int\.cr|savings bank interest|int cr",  "Income"),
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
    """Single-match fast path — returns first matching category or None."""
    result = _yaml_rules_match_all(text, rules)
    if not result:
        return None
    if len(result) == 1:
        return result[0][1]
    # Multiple matches — return the one with the longest (most specific) keyword
    winner = _resolve_by_specificity(result)
    return winner


def _yaml_rules_match_all(text: str, rules: dict) -> list[tuple[str, str]]:
    """Returns list of (matched_keyword, category) for every matching keyword across all categories."""
    text_lower = text.lower()
    matches = []
    for category, keywords in rules.items():
        if not keywords:
            continue
        for kw in keywords:
            if kw.lower() in text_lower:
                matches.append((kw, category))
    return matches


def _resolve_by_specificity(matches: list[tuple[str, str]]) -> str | None:
    """
    Given multiple (keyword, category) matches, resolve by keyword length (longer = more specific).
    Returns the winning category if one keyword is strictly longer than all others,
    or None if there's a genuine tie (different keywords, same max length, different categories).
    """
    if not matches:
        return None
    max_len = max(len(kw) for kw, _ in matches)
    longest = [(kw, cat) for kw, cat in matches if len(kw) == max_len]
    # All longest hits agree on category → clear winner
    categories = {cat for _, cat in longest}
    if len(categories) == 1:
        return longest[0][1]
    return None  # genuine tie — caller should send to LLM


def _yaml_conflict_resolve(text: str, rules: dict) -> tuple[str | None, list[str]]:
    """
    Full match analysis for Step 4.
    Returns (resolved_category, candidate_categories).
    - resolved_category is set when specificity resolves the conflict (no LLM needed)
    - candidate_categories is the list to pass to LLM when resolution is ambiguous
    - Both None/empty → no matches at all
    """
    matches = _yaml_rules_match_all(text, rules)
    if not matches:
        return None, []

    unique_cats = list(dict.fromkeys(cat for _, cat in matches))  # preserve order, dedupe

    if len(unique_cats) == 1:
        return unique_cats[0], []  # single category, direct assign

    winner = _resolve_by_specificity(matches)
    if winner:
        return winner, []  # specificity resolved it

    return None, unique_cats  # genuine conflict → LLM needed


def _llm_batch_categorize(txn_batch: list[dict], valid_categories: set[str]) -> dict:
    """
    Batch LLM categorization for multiple transactions (10x faster than sequential).
    Args: txn_batch list of dicts with 'description', 'raw_description', 'index'
          Each item may optionally have 'candidate_categories': list[str] — constrained choice
          for conflict resolution. If absent, full valid_categories list is used.
    Returns: dict mapping index → (category, confidence, status)
    """
    if not txn_batch:
        return {}
    _llm_stats["sent"] += len(txn_batch)
    hints = load_hints()
    hints_lines = "\n".join(f"  - {cat}: {desc}" for cat, desc in hints.items())
    txn_lines = []
    for t in txn_batch:
        candidates = t.get("candidate_categories")
        cats_str = ", ".join(candidates) if candidates else ", ".join(sorted(valid_categories))
        txn_lines.append(
            f'{{"index": {t["index"]}, "desc": "{t.get("description", "")}", '
            f'"raw": "{t.get("raw_description", "") or t.get("description", "")}", '
            f'"candidates": "{cats_str}"}}'
        )
    prompt = f"""Categorize these {len(txn_batch)} bank transactions (Indian user).
Each transaction has a "candidates" field listing the only valid categories for that transaction.
Choose from the candidates list for each transaction — do not use any other category.

Category guide (use this to understand what each category covers):
{hints_lines}

Transactions:
[{", ".join(txn_lines)}]

Reply ONLY with JSON array:
[{{"index": <int>, "category": "<cat>", "confidence": <0.0-1.0>}}, ...]
Use "Other" only if none of the candidates fit."""

    try:
        resp = requests.post(OLLAMA_URL, json={
            "model": OLLAMA_MODEL, "prompt": prompt, "stream": False
        }, timeout=OLLAMA_TIMEOUT * 2)
        if resp.status_code != 200:
            return {t["index"]: ("Other", 0.0, "http_error") for t in txn_batch}
        text = resp.json().get("response", "").strip()
        match = re.search(r'\[.*?\]', text, re.DOTALL)
        if not match:
            return {t["index"]: ("Other", 0.0, "no_json") for t in txn_batch}
        results = json.loads(match.group())
        result_map = {}
        for res in results:
            idx, cat, conf = res.get("index"), res.get("category", "Other"), float(res.get("confidence", 0.6))
            if cat == "Other":
                _llm_stats["returned_other"] += 1
            elif cat not in valid_categories:
                _llm_stats["errors"] += 1
                cat = "Other"
            else:
                _llm_stats["returned_valid"] += 1
            result_map[idx] = (cat, conf, "valid" if cat != "Other" else "other")
        return {t["index"]: result_map.get(t["index"], ("Other", 0.0, "missing")) for t in txn_batch}
    except requests.Timeout:
        return {t["index"]: ("Other", 0.0, "timeout") for t in txn_batch}
    except requests.ConnectionError:
        return {t["index"]: ("Other", 0.0, "conn_error") for t in txn_batch}
    except Exception as e:
        return {t["index"]: ("Other", 0.0, "exception") for t in txn_batch}


def _llm_categorize(description: str, raw: str, valid_categories: set[str]) -> tuple[str, float, str]:
    """Call Ollama. Returns (category, confidence, status).

    status values:
      - "valid"        : LLM returned a category in valid_categories (not Other)
      - "other"        : LLM legitimately returned "Other"
      - "invalid_cat"  : LLM returned a string not in valid_categories
      - "no_json"      : Response had no JSON object
      - "http_error"   : Non-200 from Ollama
      - "timeout"      : Request timed out
      - "conn_error"   : Could not reach Ollama
      - "exception"    : Any other error
    """
    _llm_stats["sent"] += 1

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
            "format": "json",  # qwen2.5 supports forced JSON output
        }, timeout=OLLAMA_TIMEOUT)
        if resp.status_code != 200:
            _llm_stats["errors"] += 1
            print(f"   ⚠️  LLM HTTP {resp.status_code}: {resp.text[:120]}")
            return "Other", 0.0, "http_error"
        text = resp.json().get("response", "").strip()
        match = re.search(r'\{.*?\}', text, re.DOTALL)
        if not match:
            _llm_stats["errors"] += 1
            print(f"   ⚠️  LLM no JSON in response: {text[:120]}")
            return "Other", 0.0, "no_json"
        data = json.loads(match.group())
        cat = data.get("category", "Other")
        conf = float(data.get("confidence", 0.6))
        if cat == "Other":
            _llm_stats["returned_other"] += 1
            return "Other", conf, "other"
        if cat not in valid_categories:
            _llm_stats["errors"] += 1
            print(f"   ⚠️  LLM invalid category '{cat}' not in taxonomy")
            return "Other", 0.0, "invalid_cat"
        _llm_stats["returned_valid"] += 1
        return cat, conf, "valid"
    except requests.Timeout:
        _llm_stats["errors"] += 1
        print(f"   ⚠️  LLM timeout after {OLLAMA_TIMEOUT}s — model loading? skipping txn")
        return "Other", 0.0, "timeout"
    except requests.ConnectionError:
        _llm_stats["errors"] += 1
        print(f"   ⚠️  LLM connection failed — is Ollama running on {OLLAMA_URL}?")
        return "Other", 0.0, "conn_error"
    except Exception as e:
        _llm_stats["errors"] += 1
        print(f"   ⚠️  LLM exception: {type(e).__name__}: {e}")
        return "Other", 0.0, "exception"


def categorize_transactions(transactions: list[dict], use_llm: bool = True) -> list[dict]:
    rules      = load_rules()
    valid_cats = set(rules.keys())
    llm_cats   = valid_cats - load_llm_excluded()  # LLM never assigns excluded categories
    contacts   = get_contacts()
    aliases    = get_aliases()
    corrections_db.init_corrections_table()

    # Reset LLM stats for this run
    _llm_stats["sent"] = 0
    _llm_stats["returned_valid"] = 0
    _llm_stats["returned_other"] = 0
    _llm_stats["errors"] = 0

    # PASS 1: apply rules, collect LLM fallback candidates
    llm_candidates = []  # list of (idx, cleaned, raw) for batch LLM call

    for idx, txn in enumerate(transactions):
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

        # Step 2.5: contact match — UPI P2P payments to known people
        # Skip if YAML keyword rules already match — prevents merchant names that
        # share a word with a contact name (e.g. "Bajaj Finance" / "Bhavna Bajaj")
        # from being misclassified as a person payment.
        source = txn.get("source_id", "")
        is_savings = source.startswith("acc_")
        _yaml_pre, _ = _yaml_conflict_resolve(raw, rules)
        if not _yaml_pre:
            _yaml_pre, _ = _yaml_conflict_resolve(cleaned, rules)
        if is_savings and canonical and not _yaml_pre:
            contact = match_contact(canonical, contacts, aliases)
            if contact:
                if contact.get("relation") == "self":
                    txn["category"] = "Internal Transfer — Self"
                    txn["category_source"] = "contact_match"
                    txn["confidence"] = contact["confidence"]
                    txn["is_internal_transfer"] = True
                    txn["notes"] = (txn.get("notes") or "") + f" [contact: {contact['name']}]"
                else:
                    txn["category"] = "Other"
                    txn["category_source"] = "contact_match"
                    txn["confidence"] = contact["confidence"]
                    txn["splitwise_candidate"] = 1
                    txn["notes"] = (txn.get("notes") or "") + f" [contact: {contact['name']}]"
                continue

        # Step 3: SBI raw pattern rules
        cat = _raw_rules_match(raw)
        if cat:
            txn["category"] = cat
            txn["category_source"] = "raw_rules"
            txn["confidence"] = 0.9
            continue

        # Step 4: YAML keyword rules (try cleaned desc first, then raw)
        cat, conflict_cats = _yaml_conflict_resolve(cleaned, rules)
        if not cat and not conflict_cats:
            cat, conflict_cats = _yaml_conflict_resolve(raw, rules)

        if cat:
            # Single match or specificity resolved it — no LLM needed
            txn["category"] = cat
            txn["category_source"] = "yaml_rules"
            txn["confidence"] = 0.8
            continue

        if conflict_cats:
            # Genuine ambiguity — send to LLM with constrained candidates
            if use_llm:
                llm_candidates.append({
                    "index": idx,
                    "description": cleaned,
                    "raw_description": raw,
                    "candidate_categories": conflict_cats,
                })
            continue

        # Step 5: LLM fallback — no YAML match at all
        if use_llm:
            llm_candidates.append({"index": idx, "description": cleaned, "raw_description": raw})

    # PASS 2: Batch LLM call for all fallback candidates
    if use_llm and llm_candidates:
        BATCH_SIZE = 15
        for batch_start in range(0, len(llm_candidates), BATCH_SIZE):
            batch = llm_candidates[batch_start:batch_start + BATCH_SIZE]
            results = _llm_batch_categorize(batch, llm_cats)
            for item in batch:
                idx = item["index"]
                cat, conf, status = results.get(idx, ("Other", 0.0, "missing"))
                transactions[idx]["category"] = cat
                if cat != "Other":
                    source = "llm_conflict" if item.get("candidate_categories") else "llm"
                else:
                    source = "fallback"
                transactions[idx]["category_source"] = source
                transactions[idx]["confidence"] = conf

    # Step 6: Verify ALL transactions have a category (safety check)
    for txn in transactions:
        if "category" not in txn or not txn["category"]:
            txn["category"] = "Other"
            txn["category_source"] = "fallback"
            txn["confidence"] = 0.0

    # Auto-detect refunds: credit same merchant + amount within N days of debit
    _detect_refunds(transactions)

    # Account-level filter: BOB is a joint account.
    # Only SIPs and self-transfers belong to the user — exclude everything else.
    for txn in transactions:
        if txn.get("source_id") == "acc_bob_sip":
            cat = txn.get("category", "")
            raw = (txn.get("raw_description", "") or "").lower()
            # Self-transfer: IMPS/P2A from Roma Pa — description cleaner strips the name,
            # leaving keyword like "Investments" which fires YAML rules → wrong category.
            # Catch by checking raw description directly.
            if "roma pa" in raw:
                txn["category"] = "Internal Transfer — Self"
                txn["category_source"] = "account_filter"
                txn["is_internal_transfer"] = True
            elif cat != "Investment & SIP" and not cat.startswith("Internal Transfer"):
                txn["category"] = "Internal Transfer"
                txn["category_source"] = "account_filter"
                txn["is_internal_transfer"] = True

    # Sync is_internal_transfer flag post-categorization
    for txn in transactions:
        if txn.get("category") == "Internal Transfer":
            txn["is_internal_transfer"] = True

    return transactions


def _detect_refunds(transactions: list[dict], days_window: int = 30):
    """
    Auto-detect refunds: for each credit, find matching debit with same merchant + amount.
    If found within days_window, mark credit as transaction_type = 'refund'.
    Handles cases like IPO allotment refunds (debit → credit after days, not same day).
    """
    from datetime import datetime, timedelta

    def parse_date(d: str) -> datetime | None:
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d"):
            try:
                return datetime.strptime(d, fmt)
            except ValueError:
                continue
        return None

    # Group debits by (merchant, amount) for fast lookup
    debits_by_key = {}
    for idx, txn in enumerate(transactions):
        if txn.get("txn_type") == "debit":
            key = (txn.get("canonical_merchant", ""), txn.get("amount", 0))
            if key not in debits_by_key:
                debits_by_key[key] = []
            debits_by_key[key].append((idx, txn))

    # Check each credit for refund match
    for idx, txn in enumerate(transactions):
        if txn.get("txn_type") != "credit":
            continue

        key = (txn.get("canonical_merchant", ""), txn.get("amount", 0))
        if key not in debits_by_key:
            continue

        credit_date = parse_date(txn.get("date", ""))
        if not credit_date:
            continue

        # Find matching debit within days_window
        for debit_idx, debit_txn in debits_by_key[key]:
            debit_date = parse_date(debit_txn.get("date", ""))
            if not debit_date:
                continue

            # Check if credit is within days_window AFTER debit
            if 0 <= (credit_date - debit_date).days <= days_window:
                # Match found — mark credit as refund
                txn["transaction_type"] = "refund"
                txn["category"] = "Refund"
                txn["category_source"] = "refund_detection"
                txn["confidence"] = 0.95
                break  # Only match once per credit
