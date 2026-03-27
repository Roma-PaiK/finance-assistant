"""
SmartParser — one universal parser that handles all banks.

Each bank is just a config entry (BANK_CONFIGS) with:
  - detect_keywords: strings to match in filename
  - date_formats: list of strptime formats to try
  - date_cols / desc_cols / debit_cols / credit_cols: column name variants to try

The parse() method:
  1. Unlocks PDF/Excel if password-protected
  2. Reads raw rows (header=None)
  3. Scans for the header row dynamically
  4. Maps columns using the bank's config
  5. Returns normalised Transaction objects
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional
import re, os, io, tempfile
import pandas as pd
import pdfplumber
import pikepdf


# ─── Unified Transaction schema ────────────────────────────────────────────────

@dataclass
class Transaction:
    date: datetime.date
    description: str
    amount: float
    txn_type: str           # "debit" or "credit"
    source_id: str
    source_label: str
    raw_description: str
    category: Optional[str] = None
    is_internal_transfer: bool = False
    splitwise_candidate: bool = False
    splitwise_pushed: bool = False
    month: Optional[str] = None
    notes: str = ""

    def to_dict(self):
        return {
            "date": self.date.isoformat(),
            "month": self.month or self.date.strftime("%Y-%m"),
            "description": self.description,
            "raw_description": self.raw_description,
            "amount": self.amount,
            "txn_type": self.txn_type,
            "source_id": self.source_id,
            "source_label": self.source_label,
            "category": self.category,
            "is_internal_transfer": self.is_internal_transfer,
            "splitwise_candidate": self.splitwise_candidate,
            "splitwise_pushed": self.splitwise_pushed,
            "notes": self.notes,
        }


# ─── Per-bank config ────────────────────────────────────────────────────────────
# Only the things that actually differ between banks.

BANK_CONFIGS = {
    # ── Bank accounts ──────────────────────────────────────────────────────────
    "sbi": {
        "detect_keywords": ["sbi", "statebank", "accountstatement"],
        "password_key": "sbi",
        "account_id": "acc_sbi_salary",
        "date_formats": ["%d %b %Y", "%d/%m/%Y", "%d-%m-%Y", "%d %B %Y", "%d/%m/%y"],
        "date_cols":   ["date", "txn date", "transaction date", "value date"],
        "desc_cols":   ["details", "description", "narration", "particulars", "remarks"],
        "debit_cols":  ["debit", "withdrawal amt.", "withdrawal", "dr"],
        "credit_cols": ["credit", "deposit amt.", "deposit", "cr"],
    },
    "hdfc_bank": {
        "detect_keywords": ["hdfc"],
        "detect_exclude":  ["millenia", "cc", "credit"],   # exclude CC files
        "password_key": "hdfc_bank",
        "account_id": "acc_hdfc_emi",
        "date_formats": ["%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d/%m/%y"],
        "date_cols":   ["date", "value date"],
        "desc_cols":   ["narration", "description", "particulars", "details"],
        "debit_cols":  ["withdrawal amt.", "withdrawal", "debit", "dr"],
        "credit_cols": ["deposit amt.", "deposit", "credit", "cr"],
    },
    "bob": {
        "detect_keywords": ["bob", "baroda", "bankofbaroda"],
        "password_key": "bob",
        "account_id": "acc_bob_sip",
        "date_formats": ["%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d.%m.%Y"],
        "date_cols":   ["date", "txn date", "value date", "transaction date"],
        "desc_cols":   ["particulars", "narration", "description", "details", "remarks"],
        "debit_cols":  ["debit", "withdrawal", "dr amount", "dr"],
        "credit_cols": ["credit", "deposit", "cr amount", "cr"],
    },
    "canara": {
        "detect_keywords": ["canara"],
        "password_key": "canara",
        "account_id": "acc_canara_daily",
        "date_formats": ["%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d.%m.%Y", "%d/%m/%y"],
        "date_cols":   ["date", "txn date", "value date", "tran date"],
        "desc_cols":   ["particulars", "narration", "description", "details", "remarks", "transaction details"],
        "debit_cols":  ["withdrawals", "withdrawal", "debit", "dr"],
        "credit_cols": ["deposits", "deposit", "credit", "cr"],
    },

    # ── Credit cards ───────────────────────────────────────────────────────────
    "hdfc_cc": {
        "detect_keywords": ["hdfc"],
        "detect_require":  ["millenia", "cc", "credit"],   # must match one of these
        "password_key": "hdfc_cc",
        "account_id": "cc_hdfc_millenia",
        "date_formats": ["%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d/%m/%y", "%d %b '%y"],
        "date_cols":   ["date", "transaction date"],
        "desc_cols":   ["description", "narration", "particulars", "merchant", "details"],
        "amount_cols": ["amount", "transaction amount"],    # single amount col with Cr/Dr suffix
        "debit_cols":  ["debit", "dr"],
        "credit_cols": ["credit", "cr"],
    },
    "icici_cc": {
        "detect_keywords": ["icici", "amazon"],
        "password_key": "icici_cc",
        "account_id": "cc_amazon_icici",
        "date_formats": ["%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %b %y"],
        "date_cols":   ["date", "transaction date"],
        "desc_cols":   ["transaction details", "description", "narration", "merchant", "details"],
        "amount_cols": ["amount", "transaction amount"],
        "debit_cols":  ["debit", "dr"],
        "credit_cols": ["credit", "cr"],
    },
    "axis_cc": {
        "detect_keywords": ["axis", "supermoney"],
        "password_key": "axis_cc",
        "account_id": "cc_supermoney_axis",
        "date_formats": ["%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %b %y"],
        "date_cols":   ["date", "transaction date"],
        "desc_cols":   ["description", "narration", "merchant", "particulars", "details"],
        "amount_cols": ["amount", "transaction amount", "inr amount"],
        "debit_cols":  ["debit", "dr"],
        "credit_cols": ["credit", "cr"],
    },
}

# All column name variants we recognise as "header" rows
ALL_HEADER_KEYWORDS = {
    "date", "txn date", "transaction date", "value date", "tran date",
    "details", "description", "narration", "particulars", "remarks",
    "transaction details", "debit", "credit", "withdrawal", "deposit",
    "withdrawals", "deposits", "balance", "ref no", "cheque no",
    "amount", "dr", "cr",
}


# ─── SmartParser ────────────────────────────────────────────────────────────────

class SmartParser:
    """Universal parser. Instantiate with a bank config key."""

    def __init__(self, bank_key: str, source_id: str, source_label: str, cfg: dict):
        self.bank_key = bank_key
        self.source_id = source_id
        self.source_label = source_label
        self.cfg = cfg

    # ── Public API ─────────────────────────────────────────────────────────────

    def can_parse(self, file_path: str) -> bool:
        fp = file_path.lower()
        cfg = self.cfg
        # Must match at least one detect keyword
        if not any(kw in fp for kw in cfg["detect_keywords"]):
            return False
        # Must NOT match any exclude keyword
        for kw in cfg.get("detect_exclude", []):
            if kw in fp:
                return False
        # Must match at least one require keyword (if specified)
        require = cfg.get("detect_require", [])
        if require and not any(kw in fp for kw in require):
            return False
        return True

    def parse(self, file_path: str, password: str = "") -> list[Transaction]:
        ext = file_path.lower()
        if ext.endswith(".xlsx") or ext.endswith(".xls"):
            df = self._read_excel(file_path, password)
        else:
            df = self._read_pdf(file_path, password)

        if df is None or df.empty:
            print(f"   ⚠️  No data extracted from {os.path.basename(file_path)}")
            return []

        return self._extract_transactions(df)

    # ── File readers ───────────────────────────────────────────────────────────

    def _read_excel(self, path: str, password: str) -> Optional[pd.DataFrame]:
        """Read Excel, decrypting first if needed. Returns raw df (header=None)."""
        source = path

        # Decrypt if password-protected
        if password:
            try:
                import msoffcrypto
                with open(path, "rb") as f:
                    office = msoffcrypto.OfficeFile(f)
                    if office.is_encrypted():
                        buf = io.BytesIO()
                        office.load_key(password=password)
                        office.decrypt(buf)
                        buf.seek(0)
                        source = buf
            except ImportError:
                print("   ⚠️  msoffcrypto not installed — run: pip install msoffcrypto-tool")
            except Exception as e:
                print(f"   ⚠️  Decrypt failed: {e}")

        for engine in ["openpyxl", "xlrd"]:
            try:
                if isinstance(source, io.BytesIO):
                    source.seek(0)
                df = pd.read_excel(source, engine=engine, header=None)
                if not df.empty:
                    return df
            except Exception:
                continue

        # HTML-disguised-as-xlsx (SBI does this)
        try:
            with open(path, "rb") as f:
                snippet = f.read(512).lower()
            if b"<html" in snippet or b"<table" in snippet:
                dfs = pd.read_html(path)
                if dfs:
                    return dfs[0].reset_index(drop=True)
        except Exception:
            pass

        return None

    def _read_pdf(self, path: str, password: str) -> Optional[pd.DataFrame]:
        """Unlock PDF if needed, extract all tables, concatenate into one df."""
        working = path
        tmp = None

        if password:
            try:
                tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
                tmp.close()
                with pikepdf.open(path, password=password) as pdf:
                    pdf.save(tmp.name)
                working = tmp.name
            except pikepdf.PasswordError:
                raise ValueError(f"Wrong PDF password for {path}")

        frames = []
        try:
            with pdfplumber.open(working) as pdf:
                for page in pdf.pages:
                    tbl = page.extract_table()
                    if tbl:
                        frames.append(pd.DataFrame(tbl))
        finally:
            if tmp and os.path.exists(tmp.name):
                os.unlink(tmp.name)

        if not frames:
            return None

        combined = pd.concat(frames, ignore_index=True)
        return combined

    # ── Core extraction ────────────────────────────────────────────────────────

    def _extract_transactions(self, raw: pd.DataFrame) -> list[Transaction]:
        """Find the header row, map columns, parse each transaction row."""

        # 1. Find header row
        header_idx = self._find_header_row(raw)
        if header_idx is None:
            print("   ⚠️  Could not locate table header row. Check file format.")
            print(f"   First 5 rows: {raw.head().to_string()}")
            return []

        print(f"   Header row found at index {header_idx}")

        # 2. Set columns from that row
        df = raw.iloc[header_idx:].reset_index(drop=True)
        df.columns = [
            str(v).strip().lower() if pd.notna(v) else f"_col{i}"
            for i, v in enumerate(df.iloc[0])
        ]
        df = df.iloc[1:].reset_index(drop=True)

        # 3. Find which actual column names match our config
        date_col   = self._match_col(df, self.cfg["date_cols"])
        desc_col   = self._match_col(df, self.cfg["desc_cols"])
        debit_col  = self._match_col(df, self.cfg["debit_cols"])
        credit_col = self._match_col(df, self.cfg["credit_cols"])
        amount_col = self._match_col(df, self.cfg.get("amount_cols", []))

        if not date_col:
            print(f"   ⚠️  Date column not found. Columns seen: {list(df.columns)}")
            return []
        if not desc_col:
            print(f"   ⚠️  Description column not found. Columns seen: {list(df.columns)}")
            return []

        print(f"   Columns mapped — date:{date_col} | desc:{desc_col} | debit:{debit_col} | credit:{credit_col} | amount:{amount_col}")

        # 4. Parse rows
        transactions = []
        for _, row in df.iterrows():
            date = self._parse_date(str(row.get(date_col, "")))
            if not date:
                continue

            raw_desc = str(row.get(desc_col, ""))
            desc = re.sub(r'(?<!\s)\n\s*', '', raw_desc)   
            desc = re.sub(r'\s*\n\s*', ' ', desc)          
            desc = " ".join(desc.split())

            if not desc or desc.lower() in ("nan", "none", ""):
                continue

            debit  = self._clean_amount(row.get(debit_col))  if debit_col  else 0.0
            credit = self._clean_amount(row.get(credit_col)) if credit_col else 0.0

            # Credit card: single amount col with Cr/Dr suffix
            if amount_col and debit == 0 and credit == 0:
                raw_amt = str(row.get(amount_col, ""))
                if re.search(r'cr', raw_amt, re.IGNORECASE):
                    credit = self._clean_amount(raw_amt)
                else:
                    debit = self._clean_amount(raw_amt)

            if debit > 0:
                transactions.append(self._make(date, desc, debit, "debit"))
            elif credit > 0:
                transactions.append(self._make(date, desc, credit, "credit"))

        return transactions

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _find_header_row(self, df: pd.DataFrame) -> Optional[int]:
        """Scan rows top-down; return index of first row with 2+ header keywords."""
        for i, row in df.iterrows():
            vals = [str(v).strip().lower() for v in row.values if pd.notna(v)]
            hits = sum(1 for v in vals if any(kw in v for kw in ALL_HEADER_KEYWORDS))
            if hits >= 2:
                return i
        return None

    def _match_col(self, df: pd.DataFrame, candidates: list[str]) -> Optional[str]:
        """Return first df column that contains any of the candidate strings."""
        for candidate in candidates:
            for col in df.columns:
                if candidate in str(col).lower():
                    return col
        return None

    def _parse_date(self, val: str) -> Optional[datetime.date]:
        val = val.strip()
        for fmt in self.cfg["date_formats"]:
            try:
                return datetime.strptime(val, fmt).date()
            except ValueError:
                continue
        return None

    def _clean_amount(self, val) -> float:
        if val is None:
            return 0.0
        s = re.sub(r'[₹,\s]', '', str(val))
        s = re.sub(r'[CcDd][Rr]$', '', s).strip()
        try:
            return abs(float(s))
        except ValueError:
            return 0.0

    def _make(self, date, desc, amount, txn_type) -> Transaction:
        return Transaction(
            date=date,
            description=desc,
            raw_description=desc,
            amount=amount,
            txn_type=txn_type,
            source_id=self.source_id,
            source_label=self.source_label,
        )