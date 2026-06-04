"""
Contact-based UPI payment detection.

Parses contacts from a VCF file and fuzzy-matches canonical merchant strings
against contact names. UPI descriptions truncate payee names (e.g. "Suresh Pa"
for "Suresh Pai"), so exact match isn't enough.

Match strategy (first hit wins, descending confidence):
  1. Token match     — any name token (≥4 chars) is substring of merchant → 0.90
  2. Fuzzy ratio     — difflib SequenceMatcher ≥ 0.72 on full name vs merchant → 0.80
  3. No match        → None

Usage:
  from core.contact_matcher import load_contacts, match_contact
  contacts = load_contacts()                          # call once, cache result
  result = match_contact("Sureshpai", contacts)       # → {"name": "Suresh pai", "confidence": 0.90}
  result = match_contact("Zomato", contacts)          # → None
"""

import os
import re
import yaml
from difflib import SequenceMatcher

CONTACTS_DIR  = os.path.join(os.path.dirname(__file__), "..", "contacts")
VCF_PATH      = os.path.join(CONTACTS_DIR, "contacts.vcf")
ALIASES_PATH  = os.path.join(os.path.dirname(__file__), "..", "config", "contact_aliases.yaml")

# Min token length to use in substring matching (avoids "Ed", "An" false hits)
MIN_TOKEN_LEN = 4

# Fuzzy match threshold — raised to eliminate similar-but-different names
# e.g. "Chaithra" vs "Chaitali" ≈ 0.75 (eliminated); truncations like
# "Suresh Pa" vs "Suresh Pai" ≈ 0.95 (safe)
FUZZY_THRESHOLD = 0.85


def _clean_name(name: str) -> str:
    """Lowercase, strip emoji and punctuation, collapse whitespace."""
    name = name.lower()
    # Remove emoji
    name = name.encode("ascii", "ignore").decode("ascii")
    # Remove non-alpha-space chars (apostrophes, ♥, etc.)
    name = re.sub(r"[^a-z\s]", " ", name)
    return re.sub(r"\s+", " ", name).strip()


def load_contacts(vcf_path: str = VCF_PATH) -> list[dict]:
    """
    Parse VCF and return list of contact dicts:
      {"raw": "Suresh pai", "clean": "suresh pai", "tokens": ["suresh", "pai"]}

    Tokens < MIN_TOKEN_LEN are excluded from token matching (too short = false hits).
    """
    if not os.path.exists(vcf_path):
        return []

    contacts = []
    with open(vcf_path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            if line.startswith("FN:"):
                raw = line[3:].strip()
                clean = _clean_name(raw)
                if not clean:
                    continue
                tokens = [t for t in clean.split() if len(t) >= MIN_TOKEN_LEN]
                contacts.append({"raw": raw, "clean": clean, "tokens": tokens})

    return contacts


def load_aliases(aliases_path: str = ALIASES_PATH) -> list[dict]:
    """
    Load contact_aliases.yaml — nickname/legal name mappings for family etc.
    Returns list of alias dicts with bank_pattern (lowercased), contact_name, relation.
    """
    if not os.path.exists(aliases_path):
        return []
    with open(aliases_path) as f:
        cfg = yaml.safe_load(f) or {}
    aliases = []
    for entry in cfg.get("aliases", []):
        if "bank_pattern" in entry and "contact_name" in entry:
            aliases.append({
                "pattern":      entry["bank_pattern"].lower(),
                "contact_name": entry["contact_name"],
                "relation":     entry.get("relation", "other"),
                "note":         entry.get("note", ""),
            })
    return aliases


def match_contact(merchant: str, contacts: list[dict], aliases: list[dict] | None = None) -> dict | None:
    """
    Try to match a canonical merchant string against known contacts.

    Alias lookup (exact substring) runs first — handles nickname ↔ legal name gap.
    Then token + fuzzy matching against VCF contact names.

    Returns {"name": str, "clean": str, "confidence": float, "relation": str} or None.
    """
    if not merchant:
        return None

    merchant_clean = _clean_name(merchant)
    if not merchant_clean:
        return None

    # Strategy 0: alias lookup (highest priority — explicit mapping)
    if aliases:
        for alias in aliases:
            if alias["pattern"] in merchant_clean:
                return {
                    "name":       alias["contact_name"],
                    "clean":      _clean_name(alias["contact_name"]),
                    "confidence": 0.95,
                    "relation":   alias["relation"],
                }

    if not contacts:
        return None

    best = None

    for contact in contacts:
        clean  = contact["clean"]
        tokens = contact["tokens"]

        # Strategy 1: token substring match (any long token inside merchant)
        token_hit = any(tok in merchant_clean for tok in tokens)
        if token_hit:
            score = 0.90
            if best is None or score > best["confidence"]:
                best = {"name": contact["raw"], "clean": clean, "confidence": score, "relation": "other"}
            continue

        # Strategy 2: fuzzy ratio on full name vs merchant
        ratio = SequenceMatcher(None, clean, merchant_clean).ratio()
        if ratio >= FUZZY_THRESHOLD:
            score = round(0.70 + ratio * 0.15, 2)  # 0.80–0.85 range
            if best is None or score > best["confidence"]:
                best = {"name": contact["raw"], "clean": clean, "confidence": score, "relation": "other"}

    return best


# ── Convenience: cached singletons ────────────────────────────────────────────

_CONTACTS_CACHE: list[dict] | None = None
_ALIASES_CACHE:  list[dict] | None = None


def get_contacts() -> list[dict]:
    """Return cached contacts list (loaded once per process)."""
    global _CONTACTS_CACHE
    if _CONTACTS_CACHE is None:
        _CONTACTS_CACHE = load_contacts()
    return _CONTACTS_CACHE


def get_aliases() -> list[dict]:
    """Return cached aliases list (loaded once per process)."""
    global _ALIASES_CACHE
    if _ALIASES_CACHE is None:
        _ALIASES_CACHE = load_aliases()
    return _ALIASES_CACHE
