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


# Raw description pattern rules — regex only, for patterns YAML substring matching can't express.
# Keep this list minimal. Simple keywords belong in config/categories.yaml instead.
# Redundant rules (imps/neft/atm/bajaj/int cr) removed — covered by YAML keywords.
# Broad UPI bank-name rules removed — matched merchant's bank, not own account (false positives).
RAW_RULES = [
    (r"cmp\s+\w.+\s+ltd",   "Income"),           # SBI salary: "CMP <company> LTD"
    (r"ach\s+dr.+sip",      "Investment & SIP"),  # SIP NACH auto-debit
    (r"ach\s+dr.+mutual",   "Investment & SIP"),  # Mutual fund NACH auto-debit
    (r"ach\s+dr.+loan",     "EMI & Loan"),         # Loan EMI NACH auto-debit
    (r"ach\s+dr.+finance",  "EMI & Loan"),         # Finance EMI NACH auto-debit
]


def _load_own_account_last4() -> set[str]:
    """Load last4 digits for all own bank accounts and credit cards from accounts.yaml."""
    accounts_path = os.path.join(os.path.dirname(__file__), "..", "config", "accounts.yaml")
    try:
        with open(accounts_path) as f:
            cfg = yaml.safe_load(f)
    except FileNotFoundError:
        return set()
    last4s = set()
    for section in ("accounts", "credit_cards"):
        for acct in cfg.get(section, []):
            val = acct.get("last4", "")
            if val:
                last4s.add(str(val).strip())
    return last4s


def _is_self_transfer_by_last4(raw: str, own_last4: set[str]) -> bool:
    """
    Return True if the description references an own account — detected via:
    1. Partial masked account number (xx972, xx183) whose last 3 digits match any own account last3/last4.
    2. Account label contains "my " (e.g. "My HDFC") — SBI IMPS labels own accounts this way.

    Handles both 3-digit and 4-digit last4 values in accounts.yaml.
    Only fires when one of these signals appears inside a BANK-xxDDD or BANK-xxDDDD pattern,
    not on bare digit sequences elsewhere in the description.
    """
    raw_lower = raw.lower()

    # Signal 2: "my " in description → user labelled this as their own account
    if re.search(r'\bmy\b', raw_lower):
        return True

    if not own_last4:
        return False

    # Build set of last3 digits from all own accounts (handles 3-digit and 4-digit last4)
    own_last3 = {v[-3:] for v in own_last4 if len(v) >= 3}

    # Signal 1: masked account patterns like xx972, xx183 inside BANK-xx...- segments
    # Pattern: optional x-prefix + 3 or 4 digits, anchored inside a word boundary or separator
    candidates = re.findall(r'[xX]{1,2}(\d{3,4})', raw)
    for c in candidates:
        if c[-3:] in own_last3:
            return True

    return False


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
    for pattern, category in RAW_RULES:
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
        contact_hint = t.get("contact_hint")
        hint_field = f', "contact_hint": "{contact_hint}"' if contact_hint else ""
        txn_type = t.get("txn_type", "")
        dir_field = f', "dir": "{txn_type}"' if txn_type else ""
        txn_lines.append(
            f'{{"index": {t["index"]}, "desc": "{t.get("description", "")}", '
            f'"raw": "{t.get("raw_description", "") or t.get("description", "")}", '
            f'"candidates": "{cats_str}"{hint_field}{dir_field}}}'
        )
    prompt = f"""Categorize these {len(txn_batch)} bank transactions (Indian user).
Each transaction has a "candidates" field listing the only valid categories for that transaction.
Choose from the candidates list — do not use any other category.
"dir" field: "credit" = money coming IN (cashback, salary, refund), "debit" = money going OUT.
Use "dir" to disambiguate: e.g. credit from Paytm/Supermoney/CRED = cashback → Income,
debit to same = subscription/utility payment → respective category.
If a transaction has "contact_hint", it means the payee matched a contact in the user's phone.
"Other" in candidates = personal payment to that contact (splitwise). Only use Other if the
description is clearly a person-to-person payment, not a merchant/service.

Category guide (use this to understand what each category covers):
{hints_lines}

Transactions:
[{", ".join(txn_lines)}]

Reply ONLY with JSON array:
[{{"index": <int>, "category": "<cat>", "confidence": <0.0-1.0>}}, ...]
Use "Other" only if none of the other candidates fit."""

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


def categorize_transactions(transactions: list[dict], use_llm: bool = True, use_corrections: bool = True) -> list[dict]:
    rules      = load_rules()
    valid_cats = set(rules.keys())
    llm_cats   = valid_cats - load_llm_excluded()  # LLM never assigns excluded categories
    contacts   = get_contacts()
    aliases    = get_aliases()
    own_last4  = _load_own_account_last4()
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
            # Confirm self-transfer via alias patterns ("Roma Can", "Roma BoB" etc.)
            # or by matching a partial account number in the description against own accounts.
            raw_lower = (raw or "").lower()
            is_self = (
                any(a["pattern"] in raw_lower for a in aliases if a.get("relation") == "self")
                or _is_self_transfer_by_last4(raw, own_last4)
            )
            txn["category"] = "Internal Transfer — Self" if is_self else "Internal Transfer"
            txn["category_source"] = "internal_transfer_flag"
            txn["confidence"] = 1.0
            continue

        # Step 2: corrections DB — canonical merchant match
        if use_corrections:
            cat, conf = _corrections_lookup(canonical)
            if cat:
                txn["category"] = cat
                txn["category_source"] = "corrections_db"
                txn["confidence"] = conf
                continue

        # Step 2.5: contact match — UPI P2P payments to known people.
        # Self-transfers are definitive (no conflict possible).
        # Non-self matches are stored as contact_hit and resolved in Step 4:
        # if YAML also fires, the conflict goes to LLM; otherwise direct → Other.
        source = txn.get("source_id", "")
        is_savings = source.startswith("acc_")
        contact_hit = None
        if is_savings and canonical:
            contact = match_contact(canonical, contacts, aliases)
            if contact:
                if contact.get("relation") == "self":
                    txn["category"] = "Internal Transfer — Self"
                    txn["category_source"] = "contact_match"
                    txn["confidence"] = contact["confidence"]
                    txn["is_internal_transfer"] = True
                    txn["notes"] = (txn.get("notes") or "") + f" [contact: {contact['name']}]"
                    continue
                else:
                    contact_hit = contact  # defer — check for YAML conflict in Step 4

        # Step 2.7: own-account last-4 digit match — catches masked account refs in description
        if is_savings and _is_self_transfer_by_last4(raw, own_last4):
            txn["category"] = "Internal Transfer — Self"
            txn["category_source"] = "account_last4_match"
            txn["confidence"] = 0.9
            txn["is_internal_transfer"] = True
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
            # YAML cleanly resolved → trust it regardless of contact match.
            # Businesses saved as contacts (e.g. "handmade cafe") should not
            # override a clear YAML keyword rule.
            txn["category"] = cat
            txn["category_source"] = "yaml_rules"
            txn["confidence"] = 0.8
            continue

        txn_type = txn.get("txn_type", "")

        if conflict_cats and not contact_hit:
            # YAML ambiguous, no contact → LLM with constrained candidates
            if use_llm:
                llm_candidates.append({
                    "index": idx,
                    "description": cleaned,
                    "raw_description": raw,
                    "candidate_categories": conflict_cats,
                    "txn_type": txn_type,
                })
            continue

        if conflict_cats and contact_hit:
            # YAML genuinely ambiguous + contact fired → LLM resolves
            if use_llm:
                candidates = list(dict.fromkeys(conflict_cats + ["Other"]))
                llm_candidates.append({
                    "index": idx,
                    "description": cleaned,
                    "raw_description": raw,
                    "candidate_categories": candidates,
                    "contact_hint": contact_hit["name"],
                    "txn_type": txn_type,
                })
            else:
                txn["category"] = conflict_cats[0]
                txn["category_source"] = "yaml_rules"
                txn["confidence"] = 0.7
            continue

        if contact_hit:
            # Only contact match fired, no YAML signal → direct assign Other
            txn["category"] = "Other"
            txn["category_source"] = "contact_match"
            txn["confidence"] = contact_hit["confidence"]
            txn["splitwise_candidate"] = 1
            txn["notes"] = (txn.get("notes") or "") + f" [contact: {contact_hit['name']}]"
            continue

        # Step 5: LLM fallback — no YAML match, no contact
        if use_llm:
            llm_candidates.append({"index": idx, "description": cleaned, "raw_description": raw, "txn_type": txn_type})

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
                    src = "llm_conflict" if item.get("candidate_categories") else "llm"
                else:
                    src = "fallback"
                transactions[idx]["category_source"] = src
                transactions[idx]["confidence"] = conf
                # Contact+YAML conflict resolved as Other → mark splitwise candidate
                if cat == "Other" and item.get("contact_hint"):
                    transactions[idx]["splitwise_candidate"] = 1
                    transactions[idx]["notes"] = (
                        (transactions[idx].get("notes") or "") +
                        f" [contact: {item['contact_hint']}]"
                    )

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
        if (txn.get("category") or "").startswith("Internal Transfer"):
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
        if txn.get("is_internal_transfer"):
            continue  # self-transfers are not refunds

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
