"""
Cleans raw SBI (and other bank) transaction descriptions into human-readable form.

SBI raw format examples:
  "WDL TFR   IMPS/508417989983/CNRB-xx183-Roma Can/0098288162094 AT 21216 DOMALGUDA"
  "WDL TFR   UPI/DR/508458544571/KARKALA/CNRB/9381461575/NA"
  "DEP TFR   CMP BAJAJ FINANCE LTD   0041425814297 OF BAJAJ FINANCE LTD AT 21216"
  "INT.CR-SAVINGS BANK INTEREST"

Cleaned output:
  "IMPS to Roma Canara (CNRB)"
  "UPI to KARKALA"
  "Salary - BAJAJ FINANCE LTD"
  "Interest Credit"
"""

import re
import yaml
import os

ACCOUNTS_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "accounts.yaml")


def load_account_hints() -> dict:
    """
    Load account identifiers so we can name internal transfers properly.
    Returns dict like: {"cnrb": "Canara Daily", "hdfc": "HDFC EMI", "bob": "BOB SIP"}
    """
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


# Bank code → readable name (for IMPS/NEFT descriptions)
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
}

# SBI prefix codes → human meaning
SBI_PREFIX_MAP = {
    "wdl tfr": "Transfer Out",
    "dep tfr": "Transfer In",
    "int.cr": "Interest Credit",
    "int cr": "Interest Credit",
    "ach dr": "Auto Debit",
    "ach cr": "Auto Credit",
    "atm wdl": "ATM Withdrawal",
    "pos dr": "POS Purchase",
    "clg dr": "Cheque Debit",
    "clg cr": "Cheque Credit",
    "cmp": "Company Payment",
}

# Salary / payroll indicators
SALARY_PATTERNS = [
    r"bajaj finance",
    r"salary",
    r"sal cr",
    r"payroll",
    r"sal/",
    r"cmp .+ ltd",   # "CMP SOME COMPANY LTD" = company payment = likely salary
]


def clean_description(raw: str) -> str:
    """
    Main cleaner. Returns a short, readable description.
    Also returns a hint for categorization.
    """
    if not raw:
        return raw

    text = " ".join(raw.split())  # collapse whitespace/newlines
    text_lower = text.lower()

    # --- Interest credit ---
    if "int.cr" in text_lower or "int cr" in text_lower or "savings bank interest" in text_lower:
        return "Interest Credit"

    # --- ATM withdrawal ---
    if "atm wdl" in text_lower or "atm/" in text_lower:
        location = _extract_after(text, ["ATM/", "ATM WDL"], stop_at=["AT ", "0098", "0041"])
        return f"ATM Withdrawal{' - ' + location if location else ''}"

    # --- IMPS transfer ---
    imps_match = re.search(r"IMPS/\d+/([^/\s]+)-?(?:xx\d+)?-?([^/\s]*)", text, re.IGNORECASE)
    if imps_match:
        bank_code = imps_match.group(1).lower()
        name_part = imps_match.group(2).strip()
        bank_name = BANK_CODE_MAP.get(bank_code[:4], bank_code.upper())
        direction = "to" if "wdl" in text_lower else "from"
        label = f"IMPS {direction} {name_part}" if name_part else f"IMPS {direction} {bank_name}"
        return label.title()

    # --- UPI transfer ---
    upi_match = re.search(r"UPI/(?:DR|CR)/\d+/([^/\s]+)", text, re.IGNORECASE)
    if upi_match:
        payee = upi_match.group(1).strip()
        direction = "to" if "/DR/" in text.upper() else "from"
        return f"UPI {direction} {payee}".title()

    upi_match2 = re.search(r"UPI-([^-\s/]+)", text, re.IGNORECASE)
    if upi_match2:
        payee = upi_match2.group(1).strip()
        return f"UPI - {payee}".title()

    # --- NEFT ---
    neft_match = re.search(r"NEFT\*[^*]+\*[^*]+\*([^*\d]+?)(?:\s+\d{6,}|\s+AT\s|$)", text, re.IGNORECASE)
    if neft_match:
        payee = neft_match.group(1).strip()
        direction = "to" if any(x in text_lower[:20] for x in ["wdl", "dr"]) else "from"
        return f"NEFT {direction} {payee}".title()

    # --- Company/Salary payment (CMP prefix) ---
    cmp_match = re.search(r"CMP\s+(.+?)(?:\s{2,}|\s+\d{10,})", text, re.IGNORECASE)
    if cmp_match:
        company = cmp_match.group(1).strip()
        # Check if it looks like salary
        if any(re.search(p, company, re.IGNORECASE) for p in SALARY_PATTERNS):
            return f"Salary - {company.title()}"
        return f"Payment from {company.title()}"

    # --- ACH (auto debit/credit — EMI, SIP etc.) ---
    ach_match = re.search(r"ACH\s+(DR|CR)\s+(.+?)(?:\s{2,}|\s+\d{8,})", text, re.IGNORECASE)
    if ach_match:
        direction = "Debit" if ach_match.group(1).upper() == "DR" else "Credit"
        entity = ach_match.group(2).strip()
        return f"Auto {direction} - {entity.title()}"

    # --- POS / card swipe ---
    pos_match = re.search(r"POS\s+(?:DR\s+)?(.+?)(?:\s{2,}|\s+\d{6,})", text, re.IGNORECASE)
    if pos_match:
        merchant = pos_match.group(1).strip()
        return f"Card Purchase - {merchant.title()}"

    # --- Fallback: strip noise, return meaningful part ---
    # Remove reference numbers (long digit sequences), branch codes, AT XXXX patterns
    cleaned = re.sub(r'\b\d{8,}\b', '', text)           # long ref numbers
    cleaned = re.sub(r'AT \d+ \w+.*$', '', cleaned)     # "AT 21216 DOMALGUDA..."
    cleaned = re.sub(r'OF \w+ AT.*$', '', cleaned)      # "OF BAJAJ... AT..."
    cleaned = re.sub(r'\s{2,}', ' ', cleaned).strip()

    # Remove SBI prefixes like "WDL TFR", "DEP TFR"
    for prefix in SBI_PREFIX_MAP:
        if cleaned.lower().startswith(prefix):
            cleaned = cleaned[len(prefix):].strip()

    return cleaned.title() if cleaned else raw.strip()


def _extract_after(text: str, markers: list, stop_at: list = None) -> str:
    for marker in markers:
        idx = text.upper().find(marker.upper())
        if idx != -1:
            after = text[idx + len(marker):].strip()
            if stop_at:
                for stop in stop_at:
                    stop_idx = after.upper().find(stop.upper())
                    if stop_idx != -1:
                        after = after[:stop_idx].strip()
            return after[:30].strip()
    return ""