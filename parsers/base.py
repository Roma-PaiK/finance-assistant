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
from core.description_cleaner import clean_description, get_canonical_merchant
from parsers.validator import ParseValidation, validate_balance


# ─── Unified Transaction schema ────────────────────────────────────────────────

@dataclass
class Transaction:
    date: datetime.date
    description: str        # human-readable (e.g. "UPI - Zomato")
    amount: float
    txn_type: str           # "debit" or "credit"
    source_id: str
    source_label: str
    raw_description: str    # original string from bank file
    canonical_merchant: str = ""   # stable key for corrections DB (e.g. "Zomato")
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
            "canonical_merchant": self.canonical_merchant,
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


@dataclass
class ParseResult:
    """Wraps extracted transactions + Block 1 validation outcome."""
    transactions: list          # list[Transaction]
    validation: ParseValidation


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
        "detect_exclude":  ["moneyback", "cc", "credit"],   # exclude CC files
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
        "date_cols":   ["value date", "date", "txn date", "tran date"],
        "desc_cols":   ["particulars", "narration", "description", "details", "remarks", "transaction details"],
        "debit_cols":  ["withdrawals", "withdrawal", "debit", "dr"],
        "credit_cols": ["deposits", "deposit", "credit", "cr"],
    },

    # ── Credit cards ───────────────────────────────────────────────────────────
    "hdfc_cc": {
        "detect_keywords": ["hdfc"],
        "detect_require":  ["moneyback", "cc", "credit"],   # must match one of these
        "password_key": "hdfc_cc",
        "account_id": "cc_hdfc_moneyback",
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
        # "amount (in`)" must come before generic "amount" to avoid matching
        # the "Intl.# amount" (reward points) column first
        "amount_cols": ["amount (in", "amount (in₹", "transaction amount", "amount"],
        "debit_cols":  ["debit", "dr"],
        "credit_cols": ["credit", "cr"],
        # ICICI CC PDFs render most transactions as plain text, not table elements.
        # pdfplumber extract_tables() only finds a fraction. Use text extraction instead.
        # Also: ICICI puts "CR" on ALL amounts (even charges) so direction is determined
        # by payment keywords, not by the CR suffix.
        "prefer_text_parser": True,
    },
    "axis_cc": {
        "detect_keywords": ["axis", "supermoney"],
        "password_key": "axis_cc",
        "account_id": "cc_supermoney_axis",
        "date_formats": ["%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %b %y"],
        "date_cols":   ["date", "txn date", "transaction date"],
        "desc_cols":   ["transaction details", "description", "narration", "merchant", "particulars", "details"],
        "amount_cols": ["amount (rs.", "amount (rs)", "amount", "transaction amount"],
        "debit_cols":  ["debit", "dr"],
        "credit_cols": ["credit", "cr"],
    },
}

# All column name variants we recognise as "header" rows
ALL_HEADER_KEYWORDS = {
    "date", "txn date", "transaction date", "value date", "tran date",
    "details", "description", "narration", "particulars", "remarks",
    "transaction details", "merchant category",
    "debit", "credit", "withdrawal", "deposit",
    "withdrawals", "deposits", "balance", "ref no", "cheque no",
    "amount", "dr", "cr", "branch code",
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
        fp = os.path.basename(file_path).lower()
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

    def parse(self, file_path: str, password: str = "") -> "ParseResult":
        ext = file_path.lower()
        if ext.endswith(".xlsx") or ext.endswith(".xls"):
            df = self._read_excel(file_path, password)
        elif ext.endswith(".csv"):
            df = self._read_csv(file_path)
        else:
            df = self._read_pdf(file_path, password)

        fname = os.path.basename(file_path)

        if df is None or df.empty:
            print(f"   ⚠️  No data extracted from {fname}")
            validation = validate_balance([], None, None, fname)
            return ParseResult(transactions=[], validation=validation)

        print(f"   📥 Raw file content: {len(df)} rows read")
        transactions, opening, closing = self._extract_transactions(df)
        validation = validate_balance(transactions, opening, closing, fname)
        print(f"   {validation.summary()}")
        return ParseResult(transactions=transactions, validation=validation)

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

    def _read_csv(self, path: str) -> Optional[pd.DataFrame]:
        """Read CSV bank statement, preserving all rows.

        Problem: Indian bank CSVs mix two tricky formats:
          - Amounts like 15,000.00 (unquoted, thousand-separator comma)
          - Descriptions like "UPI/DR/..." (quoted, may contain commas internally)
        pandas read_csv drops rows when column count mismatches.

        Solution: use Python's csv.reader (handles quoted fields correctly),
        read all rows, find the header row, let _extract_transactions do the rest.
        """
        import csv as _csv
        try:
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                reader = _csv.reader(f)
                rows = list(reader)

            if not rows:
                return None

            # Find header row: first row with 2+ header keywords
            header_idx = None
            for i, row in enumerate(rows):
                row_text = ",".join(row).lower()
                hits = sum(1 for kw in ALL_HEADER_KEYWORDS if kw in row_text)
                if hits >= 2:
                    header_idx = i
                    break

            # Pad rows to consistent column count (use max width from header onwards)
            start = header_idx if header_idx is not None else 0
            data_rows = rows[start:]
            if not data_rows:
                return None
            max_cols = max(len(r) for r in data_rows)
            padded = [r + [""] * (max_cols - len(r)) for r in data_rows]

            df = pd.DataFrame(padded)
            return df if not df.empty else None

        except Exception as e:
            print(f"   ⚠️  CSV read failed: {e}")
            return None

    def _read_pdf(self, path: str, password: str) -> Optional[pd.DataFrame]:
        """Unlock PDF if needed, extract all tables, concatenate into one df.

        Uses extract_tables() (plural) instead of extract_table() so that
        statements where the transaction table is split into many mini-tables
        per page (e.g. ICICI Amazon CC) are fully captured.

        Single-column tables are filtered out — they are usually header banners
        or footnotes, not transaction data.  When ALL tables are single-column
        (e.g. HDFC Moneyback CC), fall back to text-based row extraction.
        """
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

        all_tables = []       # (ncols, DataFrame)
        text_lines = []       # fallback: raw text per page
        text_by_page = []     # [(page_num, [lines])] for prefer_text_parser banks

        try:
            with pdfplumber.open(working) as pdf:
                for page_num, page in enumerate(pdf.pages):
                    # Collect all tables on this page
                    for tbl in page.extract_tables():
                        if tbl and len(tbl) > 0:
                            ncols = max(len(row) for row in tbl)
                            if ncols >= 3:   # discard single/double-col noise
                                all_tables.append((ncols, pd.DataFrame(tbl)))
                    # Always collect text for fallback
                    txt = page.extract_text()
                    if txt:
                        lines = txt.splitlines()
                        text_lines.extend(lines)
                        text_by_page.append((page_num, lines))
        finally:
            if tmp and os.path.exists(tmp.name):
                os.unlink(tmp.name)

        # ICICI CC: most transactions are plain text, not table elements.
        # Use text-based extraction before attempting table logic.
        if self.cfg.get("prefer_text_parser") and text_by_page:
            df = self._parse_icici_cc_text(text_by_page)
            if df is not None and not df.empty:
                return df

        if all_tables:
            # Pick the column-count group whose best single header row has the
            # most keyword matches. Scoring entire table content is unreliable
            # when adjacent summary/charges tables share financial keywords
            # (e.g. Axis CC cashback+MAD tables outscore the real tx table).
            from collections import Counter
            col_counts = Counter(ncols for ncols, _ in all_tables)

            # Score each ncols group by the quality of its best header row.
            # Primary score: how many of {date col, desc col, amount col} are
            # present in one row (from bank config) — max 3.
            # Secondary score: total cells in that row matching any header keyword.
            # This avoids noise from merged cells, footnotes, EMI schedule tables
            # which share 'date'/'amount' keywords with the real transaction table.
            amt_kws = (
                self.cfg.get("amount_cols", [])
                + self.cfg.get("debit_cols", [])
                + self.cfg.get("credit_cols", [])
            )

            best_ncols = None
            best_primary = -1
            best_secondary = -1

            for ncols in col_counts:
                frames = [df for n, df in all_tables if n == ncols]
                max_primary = 0
                max_secondary = 0
                for df in frames:
                    for row in df.values:
                        cells = [str(v).lower() for v in row if pd.notna(v) and str(v).strip()]
                        has_date   = any(any(kw in c for kw in self.cfg["date_cols"])  for c in cells)
                        has_desc   = any(any(kw in c for kw in self.cfg["desc_cols"])  for c in cells)
                        has_amount = any(any(kw in c for kw in amt_kws)                for c in cells)
                        primary    = int(has_date) + int(has_desc) + int(has_amount)
                        secondary  = sum(1 for c in cells if any(kw in c for kw in ALL_HEADER_KEYWORDS))
                        if primary > max_primary or (primary == max_primary and secondary > max_secondary):
                            max_primary   = primary
                            max_secondary = secondary
                if max_primary > best_primary or (max_primary == best_primary and max_secondary > best_secondary):
                    best_primary   = max_primary
                    best_secondary = max_secondary
                    best_ncols     = ncols

            if best_primary >= 2:
                frames = [df for ncols, df in all_tables if ncols == best_ncols]
                return pd.concat(frames, ignore_index=True)

        # Fallback: tables are absent or decorative — parse raw text lines
        return self._parse_pdf_text_lines(text_lines)

    def _parse_pdf_text_lines(self, lines: list[str]) -> Optional[pd.DataFrame]:
        """Text-based fallback for PDFs where pdfplumber can't extract table columns.

        Handles HDFC CC statements where each row looks like:
            DD/MM/YYYY| HH:MM  DESCRIPTION  AMOUNT [Cr]
        Returns a 3-column df [date, description, amount_raw] that
        _extract_transactions can process via the standard column-mapping path.
        """
        DATE_PAT = re.compile(
            r"(\d{2}/\d{2}/\d{4})\s*\|\s*\d{2}:\d{2}\s+"  # date|time
            r"(.+?)\s+"                                       # description
            r"([\d,]+\.\d{2}\s*(?:Cr)?)"                     # amount [Cr]
            r"[\s\w]*$",                                      # trailing noise (points col etc.)
            re.IGNORECASE,
        )
        rows = [["date", "description", "amount"]]  # synthetic header
        for line in lines:
            m = DATE_PAT.search(line.strip())
            if m:
                rows.append([m.group(1), m.group(2).strip(), m.group(3).strip()])

        if len(rows) <= 1:
            return None
        return pd.DataFrame(rows)

    def _parse_icici_cc_text(self, text_by_page: list[tuple]) -> Optional[pd.DataFrame]:
        """Text-based parser for ICICI Amazon CC statements.

        ICICI CC PDFs render most transactions as plain text, not table markup.
        pdfplumber's extract_tables() only catches a handful of mini-tables.

        Transaction line format:
            DD/MM/YYYY  <10-13 digit serial>  <description>  <reward_pts>  [<intl_amt>]  <amount>  [CR]

        Direction logic:
        - ICICI puts "CR" on ALL amounts (including charges) — the suffix is meaningless.
        - Payment lines (BBPS, "payment received", refund, cashback) → credit.
        - Everything else → debit.
        - Lines without a serial number (e.g. EMI schedule rows) are skipped.

        Page strategy:
        - Page 1 text includes EMI reversal entries (same merchant/amount as real charges
          but different date/serial). These duplicate real transactions so we skip them.
          We accept only CR-flagged entries from page 1 (those are formal table rows).
        - Page 2+ text contains all real charges not captured in tables — always accepted.
        """
        PAYMENT_PAT = re.compile(
            r'payment\s+received|bbps|refund|cashback|reversal', re.IGNORECASE
        )
        # Anchor on the 10-13 digit serial number that follows the date.
        # This excludes EMI/summary rows which lack this field.
        TXN_PAT = re.compile(
            r'^(\d{2}/\d{2}/\d{4})\s+'   # date
            r'(\d{10,13})\s+'              # serial number
            r'(.+?)\s+'                    # description (non-greedy)
            r'(-?\d+)\s+'                  # reward points (integer, may be negative)
            r'(?:[\d,]+\.\d{2}\s+)?'      # optional intl amount (foreign currency txns)
            r'([\d,]+\.\d{2})\s*'         # transaction amount
            r'(CR)?\s*$',                  # optional CR suffix (ICICI adds it to all)
            re.IGNORECASE,
        )

        # Pattern to detect ICICI EMI fee breakdown lines (not standalone charges)
        EMI_DESC_PAT = re.compile(
            r'sgst-ci|cgst-ci|amortization', re.IGNORECASE
        )

        # Pre-compiled pattern to strip sidebar/promotional text that pdfplumber
        # sometimes prepends to transaction lines (e.g. "www.icicibank.com/offers 18/03/2026 ...")
        PREFIX_STRIP = re.compile(r'^.*?(?=\d{2}/\d{2}/\d{4})')

        # Pass 1: collect ALL parsed matches + build CR key sets.
        seen_serials:      set[str]   = set()
        all_matches:       list[tuple] = []           # (page_num, date, desc, amount, is_cr)
        cr_keys:           set[tuple]  = set()        # (desc, amount) for page-1 dedup
        cr_keys_with_date: set[tuple]  = set()        # (date, desc, amount) for page-2+ dedup

        for page_num, lines in text_by_page:
            for line in lines:
                clean = line.strip()
                m = TXN_PAT.match(clean)
                if not m:
                    clean = PREFIX_STRIP.sub('', clean)
                    m = TXN_PAT.match(clean)
                if not m:
                    continue
                serial = m.group(2)
                if serial in seen_serials:
                    continue
                seen_serials.add(serial)
                date_str = m.group(1)
                desc     = m.group(3).strip()
                amount   = m.group(5)
                is_cr    = bool(m.group(6))
                all_matches.append((page_num, date_str, desc, amount, is_cr))
                if is_cr:
                    cr_keys.add((desc, amount))
                    cr_keys_with_date.add((date_str, desc, amount))

        # Pass 2: build output rows with per-page filtering rules.
        #
        # Page 1 non-CR entries fall into three buckets:
        #   a) EMI fee breakdown (SGST-CI, CGST-CI, Amortization) → skip
        #   b) EMI reversal/reference: same (desc, amount) as a CR entry → skip
        #   c) Genuine new charges (utility on 13/03, processing fee on 16/03) → include
        #
        # Page 2+ non-CR: real charges not in any table. Only skip if
        #   (date, desc, amount) exactly matches a CR entry (same-day table-row duplicate).
        rows = [["date", "transaction details", "amount (in`)"]]
        for page_num, date_str, desc, amount, is_cr in all_matches:
            if not is_cr:
                # Always exclude ICICI EMI fee breakdown lines (SGST-CI, CGST-CI,
                # amortization) — they're sub-ledger noise, not standalone charges.
                if EMI_DESC_PAT.search(desc):
                    continue
                if page_num == 0:
                    # Page 1: also exclude entries whose (desc, amount) duplicates a
                    # formal CR entry — these are EMI reversal/reference lines.
                    if (desc, amount) in cr_keys:
                        continue
                else:
                    # Page 2+: exclude only if same date+desc+amount already in CR set
                    # (table row text also rendered as plain text on the same page).
                    if (date_str, desc, amount) in cr_keys_with_date:
                        continue

            # Determine direction from description, NOT from CR suffix
            if PAYMENT_PAT.search(desc):
                amt_col = f"{amount} CR"   # credit — payment received by card
            else:
                amt_col = amount           # debit — purchase/charge

            rows.append([date_str, desc, amt_col])

        if len(rows) <= 1:
            return None

        print(f"   ICICI CC text parser: {len(rows) - 1} transactions found")
        return pd.DataFrame(rows)

    # ── Core extraction ────────────────────────────────────────────────────────

    def _extract_transactions(
        self, raw: pd.DataFrame
    ) -> tuple[list["Transaction"], Optional[float], Optional[float]]:
        """Find header row, map columns, parse rows.
        Returns (transactions, opening_balance, closing_balance).
        opening/closing are None when not found in metadata.
        """

        # 1. Find header row
        header_idx = self._find_header_row(raw)
        if header_idx is None:
            print("   ⚠️  Could not locate table header row. Check file format.")
            print(f"   First 5 rows: {raw.head().to_string()}")
            return [], None, None

        print(f"   Header row found at index {header_idx}")

        # 2. Extract opening/closing balance from metadata rows above the header
        opening_balance, closing_balance = self._find_balance_metadata(raw, header_idx)
        if opening_balance is not None:
            print(f"   Balance metadata — opening: ₹{opening_balance:,.2f} | closing: ₹{closing_balance:,.2f}")

        # 3. Set columns from the header row
        df = raw.iloc[header_idx:].reset_index(drop=True)
        df.columns = [
            str(v).strip().lower() if pd.notna(v) else f"_col{i}"
            for i, v in enumerate(df.iloc[0])
        ]
        df = df.iloc[1:].reset_index(drop=True)
        print(f"   📋 Table extracted: {len(df)} data rows")

        # 4. Find which actual column names match our config
        date_col   = self._match_col(df, self.cfg["date_cols"])
        desc_col   = self._match_col(df, self.cfg["desc_cols"])
        debit_col  = self._match_col(df, self.cfg["debit_cols"])
        credit_col = self._match_col(df, self.cfg["credit_cols"])
        amount_col = self._match_col(df, self.cfg.get("amount_cols", []))

        if not date_col:
            print(f"   ⚠️  Date column not found. Columns seen: {list(df.columns)}")
            return [], opening_balance, closing_balance
        if not desc_col:
            print(f"   ⚠️  Description column not found. Columns seen: {list(df.columns)}")
            return [], opening_balance, closing_balance

        print(f"   Columns mapped — date:{date_col} | desc:{desc_col} | debit:{debit_col} | credit:{credit_col} | amount:{amount_col}")

        # 5. Parse rows — non-date rows (filler, totals, footnotes) are skipped automatically
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

        return transactions, opening_balance, closing_balance

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _find_balance_metadata(
        self, raw: pd.DataFrame, header_idx: int
    ) -> tuple[Optional[float], Optional[float]]:
        """Scan entire df for balance labels.

        - Rows ABOVE header: banks like Canara/HDFC put Opening/Closing Balance in metadata block.
        - Rows BELOW header: SBI puts a "Statement Summary" footer with
          "Brought Forward" (opening) and "Closing Balance" at the bottom.

        Returns (opening_balance, closing_balance) or (None, None) if not found.
        """
        opening: Optional[float] = None
        closing: Optional[float] = None

        for i in range(len(raw)):
            row = raw.iloc[i]
            all_cells = [str(v).strip() for v in row.values]
            vals = [v for v in all_cells if v and v.lower() not in ("nan", "none")]
            if not vals:
                continue

            cells_lower = [v.lower() for v in all_cells]

            # Case 1: label and value in same row (e.g. "Opening Balance | 38,124.31")
            for j, cell in enumerate(vals):
                cell_lower = cell.lower()
                if "opening balance" in cell_lower or "brought forward" in cell_lower:
                    amt = self._extract_adjacent_amount(vals, vals.index(cell))
                    if amt is not None:
                        opening = amt
                elif "closing balance" in cell_lower:
                    amt = self._extract_adjacent_amount(vals, vals.index(cell))
                    if amt is not None:
                        closing = amt

            # Case 2: this row is a header row with labels as columns;
            # values are in the NEXT row (SBI Statement Summary footer)
            has_bf = any("brought forward" in c for c in cells_lower)
            has_cb = any("closing balance" in c for c in cells_lower)
            if (has_bf or has_cb) and i + 1 < len(raw):
                next_row_vals = [str(v).strip() for v in raw.iloc[i + 1].values]
                for j, cell in enumerate(cells_lower):
                    if "brought forward" in cell or "opening balance" in cell:
                        amt = self._clean_amount(next_row_vals[j]) if j < len(next_row_vals) else None
                        if amt:
                            opening = amt
                    elif "closing balance" in cell:
                        amt = self._clean_amount(next_row_vals[j]) if j < len(next_row_vals) else None
                        if amt:
                            closing = amt

        return opening, closing

    def _extract_adjacent_amount(self, vals: list[str], label_idx: int) -> Optional[float]:
        """Given a list of cell strings and the index of a label, return the
        first numeric value found after that label in the same row."""
        for v in vals[label_idx + 1:]:
            amt = self._clean_amount(v)
            if amt > 0:
                return amt
        return None

    def _find_header_row(self, df: pd.DataFrame) -> Optional[int]:
        """Scan rows top-down; return index of first row with 2+ header keywords.
        Also look for rows with many non-null values (likely transaction table headers).
        """
        best_row = None
        best_score = 0

        for i, row in df.iterrows():
            non_null_count = pd.notna(row).sum()
            vals = [str(v).strip().lower() for v in row.values if pd.notna(v)]

            # Count exact keyword matches
            exact_hits = sum(1 for v in vals if v in ALL_HEADER_KEYWORDS)
            substring_hits = sum(1 for v in vals if any(kw in v for kw in ALL_HEADER_KEYWORDS))

            # Prefer rows with exact matches
            if exact_hits >= 2 and non_null_count >= 5:
                return i

            # Also look for rows with many columns and substring matches
            # (handles cases where keywords appear with different formatting)
            score = (exact_hits * 10) + (substring_hits * 2) + (non_null_count // 2)
            if score > best_score:
                best_score = score
                best_row = i

        # Return best row if it has reasonable keyword matches
        if best_score >= 5:
            return best_row
        return None

    def _match_col(self, df: pd.DataFrame, candidates: list[str]) -> Optional[str]:
        """Return first df column that matches any of the candidate strings.

        Tries word-boundary match first (e.g. "cr" must not match "description"),
        then falls back to substring match for multi-word candidates like "value date".
        """
        # Pass 1: word-boundary match (prevents "cr" ⊂ "description", "dr" ⊂ "address")
        for candidate in candidates:
            pat = re.compile(r'\b' + re.escape(candidate) + r'\b', re.IGNORECASE)
            for col in df.columns:
                if pat.search(str(col)):
                    return col
        # Pass 2: substring fallback only for longer candidates (≥ 4 chars).
        # Short codes like "cr" / "dr" must be whole-word only — "cr" ⊂ "description".
        for candidate in candidates:
            if len(candidate) < 4:
                continue
            for col in df.columns:
                if candidate in str(col).lower():
                    return col
        return None

    def _parse_date(self, val: str) -> Optional[datetime.date]:
        val = val.strip()
        # Remove Excel formula syntax if present (e.g., ="02 Jan 2025")
        val = re.sub(r'^=?"(.+)"$', r'\1', val)
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
        s = str(val)
        # Remove Excel formula syntax if present
        s = re.sub(r'^=?"(.+)"$', r'\1', s)
        # Remove currency symbols and formatting
        s = re.sub(r'[₹,\s]', '', s)
        # Remove Cr/Dr suffix
        s = re.sub(r'[CcDd][Rr]$', '', s).strip()
        try:
            return abs(float(s))
        except ValueError:
            return 0.0

    def _make(self, date, desc, amount, txn_type) -> Transaction:
        return Transaction(
            date=date,
            description=clean_description(desc),
            raw_description=desc,
            amount=amount,
            txn_type=txn_type,
            source_id=self.source_id,
            source_label=self.source_label,
            canonical_merchant=get_canonical_merchant(desc),
        )