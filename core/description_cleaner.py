"""
Cleans raw bank transaction descriptions into:
  - canonical_merchant : stable key for the corrections DB (e.g. "Zomato", "Flipkart")
  - clean_description  : human-readable display string (e.g. "UPI - Zomato")

Supported raw formats (from real statements):

  HDFC Bank savings  : UPI-FLIPKART-PAYTM-648052425@PTYBL-YESBOPT00000050022477817679
  SBI/Canara savings : UPI/DR/508458544571/KARKALA/CNRB/9381461575/NA
  SBI/Canara savings : UPI/DB/500264178779/CRED RENT/UTIB/** rent@axisb/payment //...
  SBI savings (IMPS) : MOB-IMPS-CR/Karkala Ro/The State /40550645841/ReqPay/...
  Axis CC            : UPI/ZOMATO/9845-9845@PTYBL/HDFC00315IG17S
  Axis CC            : UPI/SMALL MARKET PRATAPNA/UPI@PTYBL/STBC00123NG66
  ICICI / HDFC CC    : AMAZON-MINI CRED S AMAZON IN
  All banks          : NEFT, ACH DR/CR, ATM WDL, POS, CMP (salary), INT.CR
"""

import re
import yaml
import os

ACCOUNTS_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "accounts.yaml")

# IFSC-prefix → bank display name
BANK_CODE_MAP = {
    "cnrb": "Canara Bank",
    "hdfc": "HDFC Bank",
    "sbin": "SBI",
    "icic": "ICICI Bank",
    "utib": "Axis Bank",
    "barb": "Bank of Baroda",
    "punb": "Punjab National Bank",
    "kkbk": "Kotak Bank",
    "ioba": "Indian Overseas Bank",
    "ptybl": "Paytm Bank",
    "yesbopt": "Yes Bank",
}

# Trailing noise common in CC merchant strings
_CC_LOCATION_NOISE = re.compile(
    r'\s+(?:IN|LTD|PVT|PRIVATE|LIMITED|INDIA|NEW DELHI|MUMBAI|BANGALORE|'
    r'HYDERABAD|CHENNAI|PUNE|KOLKATA|CART|MINI|STORE)\.?$',
    re.IGNORECASE,
)

# Patterns to strip from any description
_LONG_REF = re.compile(r'\b\d{7,}\b')
_DATE_SUFFIX = re.compile(r'\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2}.*$')
_AT_BRANCH = re.compile(r'\s+AT\s+\d+\s+\w+.*$', re.IGNORECASE)
_OF_BRANCH = re.compile(r'\s+OF\s+\w+\s+AT.*$', re.IGNORECASE)

# SBI statement prefix tokens to drop after main extraction
_SBI_PREFIXES = ("WDL TFR", "DEP TFR", "INT CR", "INT.CR",
                 "ACH DR", "ACH CR", "ATM WDL", "POS DR", "CLG DR", "CLG CR")

# Patterns that indicate salary / payroll
_SALARY_HINTS = re.compile(
    r'bajaj\s*finance|salary|sal\s*cr|payroll|sal/|cmp\s+.+\s+ltd',
    re.IGNORECASE,
)


# ── Public API ─────────────────────────────────────────────────────────────────

def get_canonical_merchant(raw: str) -> str:
    """
    Returns a stable, title-cased merchant name for use as the corrections DB key.

    Examples:
      "UPI-FLIPKART-PAYTM-648052425@PTYBL-YESBOPT..." → "Flipkart Paytm"
      "UPI/DR/508458544571/KARKALA/CNRB/..."           → "Karkala"
      "UPI/DB/500264178779/CRED RENT/UTIB/..."         → "Cred Rent"
      "UPI/ZOMATO/9845-9845@PTYBL/HDFC..."             → "Zomato"
      "MOB-IMPS-CR/Karkala Ro/The State /..."          → "Karkala Ro"
      "INT.CR-SAVINGS BANK INTEREST"                    → "Interest Credit"
      "ATM WDL/..."                                     → "ATM Withdrawal"
    """
    raw_merchant, _ = _extract(raw)
    return _normalize(raw_merchant)


def clean_description(raw: str) -> str:
    """
    Returns a human-readable description for display / transaction list.

    Examples:
      "UPI-FLIPKART-PAYTM-648052425@PTYBL-..."  → "UPI - Flipkart Paytm"
      "UPI/DR/508458544571/KARKALA/..."          → "UPI - Karkala"
      "MOB-IMPS-CR/Karkala Ro/..."               → "IMPS - Karkala Ro"
      "ACH DR HDFC ERGO GENERAL..."              → "Auto Debit - Hdfc Ergo General"
      "INT.CR-SAVINGS BANK INTEREST"             → "Interest Credit"
    """
    if not raw:
        return raw
    merchant, txn_type = _extract(raw)
    merchant = _normalize(merchant)

    prefix_map = {
        "upi":      "UPI - ",
        "imps":     "IMPS - ",
        "neft":     "NEFT - ",
        "ach_dr":   "Auto Debit - ",
        "ach_cr":   "Auto Credit - ",
        "pos":      "Card Purchase - ",
        "atm":      "",
        "interest": "",
        "salary":   "Salary - ",
        "payment":  "Payment from ",
        "other":    "",
    }
    return prefix_map.get(txn_type, "") + merchant


def load_account_hints() -> dict:
    """Load own-account identifiers for internal-transfer labelling."""
    try:
        with open(ACCOUNTS_PATH) as f:
            cfg = yaml.safe_load(f)
        hints = {}
        for acc in cfg.get("accounts", []):
            bank = acc.get("bank", "").lower()
            label = acc.get("label", bank)
            last4 = acc.get("last4", "")
            hints[bank] = label
            if last4 and last4 != "XXXX":
                hints[last4] = label
        return hints
    except Exception:
        return {}


# ── Core extraction ────────────────────────────────────────────────────────────

def _extract(raw: str) -> tuple[str, str]:
    """
    Returns (merchant_string, txn_type_key).
    txn_type_key is one of: upi | imps | neft | ach_dr | ach_cr |
                             pos | atm | interest | salary | payment | other
    """
    if not raw:
        return raw, "other"

    text = " ".join(raw.split())   # collapse whitespace / newlines

    # ── 1. Interest credit ─────────────────────────────────────────────────
    if re.search(r'INT\.?CR|SAVINGS BANK INTEREST', text, re.IGNORECASE):
        return "Interest Credit", "interest"

    # ── 2. ATM / cash withdrawal ───────────────────────────────────────────
    if re.search(r'ATM\s*WDL|ATM/|CASH WITHDRAWAL', text, re.IGNORECASE):
        return "ATM Withdrawal", "atm"

    # ── 3. HDFC savings UPI ────────────────────────────────────────────────
    #   Format: UPI-MERCHANT-VPA@PSP-BANKREF
    #   e.g.    UPI-FLIPKART-PAYTM-648052425@PTYBL-YESBOPT00000050022477817679
    #           UPI-SWIGGY-6789@OKICICI-HDFC0001XYZ
    m = re.match(r'UPI-(.+?)-\d{7,}@', text, re.IGNORECASE)
    if m:
        return m.group(1).replace('-', ' ').strip(), "upi"

    # Variant: no numeric segment before @  (e.g. UPI-MERCHANTNAME@VPA-REF)
    m = re.match(r'UPI-([^@\d][^@]*?)@', text, re.IGNORECASE)
    if m:
        return m.group(1).replace('-', ' ').strip(), "upi"

    # ── 4. SBI / Canara savings UPI ────────────────────────────────────────
    #   Format: UPI/DR|CR|DB/REFNUM/MERCHANT NAME/BANKCODE/VPA/...
    #   e.g.    UPI/DR/508458544571/KARKALA/CNRB/9381461575/NA
    #           UPI/DB/500264178779/CRED RENT/UTIB/** rent@axisb/payment //...
    m = re.match(r'UPI/(?:DR|CR|DB)/\d+/([^/]+)', text, re.IGNORECASE)
    if m:
        return m.group(1).strip(), "upi"

    # ── 5. Axis CC / HDFC CC UPI ───────────────────────────────────────────
    #   Format: UPI/MERCHANT NAME/VPA@PSP/TXNREF
    #   e.g.    UPI/ZOMATO/9845-9845@PTYBL/HDFC00315IG17S
    #           UPI/SMALL MARKET PRATAPNA/UPI@PTYBL/STBC00123NG66
    #   Key difference from savings: no DR/CR/DB keyword, merchant comes first
    m = re.match(r'UPI/([A-Za-z][^/]+)/[^/]*@[^/]*/[A-Z0-9]+', text, re.IGNORECASE)
    if m:
        return m.group(1).strip(), "upi"
    # Looser fallback for CC UPI (fewer segments)
    m = re.match(r'UPI/([A-Za-z][^/\d][^/]*)/', text, re.IGNORECASE)
    if m:
        return m.group(1).strip(), "upi"

    # ── 6. MOB-IMPS (SBI savings) ──────────────────────────────────────────
    #   Format: MOB-IMPS-CR|DR/PAYEE NAME/BANK NAME/ACCT/ReqPay/...
    #   e.g.    MOB-IMPS-CR/Karkala Ro/The State /40550645841/ReqPay/...
    m = re.match(r'MOB-IMPS-(?:CR|DR)/([^/]+)/', text, re.IGNORECASE)
    if m:
        return m.group(1).strip(), "imps"

    # ── 7. Classic IMPS ────────────────────────────────────────────────────
    #   Format: IMPS/REFNUM/BANKCODE-xxLAST4-PAYEE/...
    m = re.search(r'IMPS/\d+/([^/\s]+)-?(?:xx\d+)?-?([^/\s]*)', text, re.IGNORECASE)
    if m:
        bank_code = m.group(1).lower()
        name_part = m.group(2).strip()
        if name_part:
            return name_part, "imps"
        return BANK_CODE_MAP.get(bank_code[:4], bank_code.upper()), "imps"

    # ── 8. NEFT ────────────────────────────────────────────────────────────
    m = re.search(r'NEFT\*[^*]+\*[^*]+\*([^*\d]+?)(?:\s+\d{6,}|\s+AT\s|$)', text, re.IGNORECASE)
    if m:
        return m.group(1).strip(), "neft"

    # ── 9. ACH auto-debit / auto-credit (SIP, EMI, insurance premiums) ────
    m = re.search(r'ACH\s+(DR|CR)\s+(.+?)(?:\s{2,}|\s+\d{8,}|$)', text, re.IGNORECASE)
    if m:
        direction = m.group(1).upper()
        entity = m.group(2).strip()
        return entity, "ach_dr" if direction == "DR" else "ach_cr"

    # ── 10. POS (card swipe at terminal) ──────────────────────────────────
    m = re.search(r'POS\s+(?:DR\s+)?(.+?)(?:\s{2,}|\s+\d{6,}|$)', text, re.IGNORECASE)
    if m:
        return m.group(1).strip(), "pos"

    # ── 11. CMP (company payment / salary — SBI) ──────────────────────────
    m = re.search(r'CMP\s+(.+?)(?:\s{2,}|\s+\d{10,}|$)', text, re.IGNORECASE)
    if m:
        company = m.group(1).strip()
        txn_key = "salary" if _SALARY_HINTS.search(company) else "payment"
        return company, txn_key

    # ── 12. Fallback: strip noise, return what's left ─────────────────────
    cleaned = _DATE_SUFFIX.sub('', text)
    cleaned = _AT_BRANCH.sub('', cleaned)
    cleaned = _OF_BRANCH.sub('', cleaned)
    cleaned = _LONG_REF.sub('', cleaned).strip()
    cleaned = _CC_LOCATION_NOISE.sub('', cleaned).strip()

    # Strip URL noise common in CC descriptions (e.g. "Http://Www.Am", "Https://...")
    cleaned = re.sub(r'\s+https?://\S*', '', cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r'\s+www\.\S*', '', cleaned, flags=re.IGNORECASE).strip()

    # Drop leading SBI prefix tokens
    for prefix in _SBI_PREFIXES:
        if cleaned.upper().startswith(prefix):
            cleaned = cleaned[len(prefix):].strip()
            break

    return (cleaned or raw.strip()), "other"


_ACRONYMS = re.compile(r'\b(atm|upi|sbi|hdfc|icici|axis|emi|sip|neft|imps|ppf|fd|otp|kyc|ltd|pvt)\b', re.IGNORECASE)

def _normalize(merchant: str) -> str:
    """Title-case, collapse whitespace, restore known acronyms to uppercase."""
    if not merchant:
        return merchant
    result = re.sub(r'\s+', ' ', merchant).strip().title()
    # Restore acronyms that title() wrongly lowercased
    result = _ACRONYMS.sub(lambda m: m.group(0).upper(), result)
    return result
