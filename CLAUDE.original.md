# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repo.

---

## What This Project Is

Local-first personal finance system for Indian user:
- 4 bank accounts (SBI, Canara, HDFC, Bank of Baroda)
- 3 credit cards (HDFC moneyback, Amazon Pay ICICI, SuperMoney Axis)
- Monthly statement uploads (PDF, password-protected PDF, XLSX, encrypted XLSX)
- Auto parse, clean, categorize, SQLite store
- CLI-first, dry-run preview before DB commit
- Planned: Streamlit UI + local LLM Q&A + Splitwise integration

**All local. No data leaves machine. Zero cloud cost.**

---

## Account & Card Registry

| ID | Label | Bank | Role |
|---|---|---|---|
| `acc_sbi_salary` | SBI Salary Account | SBI | Salary in. Source of all inter-account transfers |
| `acc_canara_daily` | Canara Daily Account | Canara | Daily spend, rent, CC payments |
| `acc_hdfc_emi` | HDFC EMI Account | HDFC | Education loan EMI only |
| `acc_bob_sip` | Bank of Baroda SIP Account | BOB | SIP/investments. Joint with father |
| `cc_hdfc_moneyback` | HDFC moneyback CC | HDFC | CC, paid from Canara |
| `cc_amazon_icici` | Amazon Pay ICICI CC | ICICI | CC, paid from Canara |
| `cc_supermoney_axis` | SuperMoney Axis CC | Axis | CC, paid from Canara |

### Internal Transfer Rules (NEVER count as spending)
- SBI → Canara (daily expense funding)
- SBI → HDFC (EMI funding)
- SBI → BOB (SIP funding)
- Detected via IMPS/NEFT/UPI patterns in raw description: `IMPS/xxx/CNRB`, `IMPS/xxx/HDFC`, `IMPS/xxx/BARB`
- Salary credits from Bajaj Finance must NEVER be flagged as internal transfers

---

## Architecture & Data Flow

```
CLI (main.py)
    │
    ▼
detector.py         — matches filename keywords → picks bank config from BANK_CONFIGS
                      (credit cards checked before bank accounts to avoid hdfc_cc/hdfc_bank ambiguity)
    │
    ▼
base.py SmartParser — decrypts file → extracts tables → finds header row dynamically
                      → maps columns → repairs line breaks → emits Transaction objects
    │
    ▼
deduplicator.py     — flags internal transfers (IMPS/UPI/NEFT pattern match)
                      → removes duplicates vs existing DB
    │
    ▼
categorizer.py      — cleans description (description_cleaner.py)
                      → Step 1: SBI raw pattern rules (SBI_RAW_RULES)
                      → Step 2: YAML keyword rules on cleaned description
                      → Step 3: YAML keyword rules on raw description
                      → Step 4: Ollama LLM fallback (requires Ollama running locally)
    │
    ├── --dry-run → timestamped CSV (no DB write)
    └── live run  → SQLite via db.py
```

---

## CLI Commands (uv run)

```bash
# Parse + dry run (CSV preview, no DB write)
uv run main.py <file> --dry-run
uv run main.py <file> --dry-run --csv custom_name.csv

# Parse + commit to DB
uv run main.py <file>
uv run main.py <file1> <file2> <file3>   # multiple files

# Inspect DB
uv run check_db.py
uv run check_db.py --month 2025-01
uv run check_db.py --category
uv run check_db.py --category --month 2025-01
uv run check_db.py --uncategorized
uv run check_db.py --all

# Fix categories (after editing config/categories.yaml)
uv run recategorize.py --no-llm
uv run recategorize.py
uv run recategorize.py --month 2025-01 --no-llm

# Clear DB
uv run clear_db.py --force
uv run clear_db.py --month 2025-01
uv run clear_db.py --source acc_sbi_salary
```

---

## File Naming Convention

Bank name in filename required for auto-detection:

| Source | Filename must contain |
|---|---|
| SBI | `sbi`, `statebank`, or `accountstatement` |
| HDFC bank (EMI account) | `hdfc` but NOT `moneyback`, `cc`, `credit` |
| HDFC moneyback CC | `hdfc` AND one of: `moneyback`, `cc`, `credit` |
| Bank of Baroda | `bob`, `baroda` |
| Canara | `canara` |
| Amazon ICICI CC | `icici` or `amazon` |
| SuperMoney Axis CC | `axis` or `supermoney` |

---

## Transaction Data Model

`Transaction` dataclass (parsers/base.py) — canonical schema:

| Field | Type | Notes |
|---|---|---|
| `date` | `datetime.date` | Parsed from statement |
| `month` | `str` | YYYY-MM derived from date |
| `description` | `str` | Cleaned human-readable (set by description_cleaner) |
| `raw_description` | `str` | Original bank text — used for internal transfer detection |
| `amount` | `float` | Always positive |
| `txn_type` | `str` | `"debit"` or `"credit"` |
| `source_id` | `str` | Account ID e.g. `acc_sbi_salary` |
| `source_label` | `str` | Human label from accounts.yaml |
| `category` | `str` | Set by categorizer |
| `is_internal_transfer` | `bool` | Set by deduplicator before categorizer |
| `splitwise_candidate` | `bool` | Future Splitwise integration |
| `notes` | `str` | Free text |

---

## Config Files

- **`config/accounts.yaml`** — account registry: `id`, `label`, `bank`, `last4`, `internal_transfers` rules
- **`config/passwords.yaml`** — PDF/Excel passwords per bank key (`pdf_passwords` dict); gitignored
- **`config/categories.yaml`** — keyword → category mapping; `categories` dict; first match wins

Ollama model/URL hardcoded in `core/categorizer.py`: `OLLAMA_MODEL = "llama3"`, `OLLAMA_URL = "http://localhost:11434/api/generate"`. Ollama must run locally for LLM fallback; fail → silent `"Other"`.

---

## Privacy & PII Rules for Claude Code

Bank statement files contain sensitive personal data. Follow these rules without exception:

- **Never print, log, or display any values read from statement files** — not to terminal,
  not to a CSV, not in debug output. No account numbers, names, amounts, descriptions,
  balances, IFSC codes, or any other cell values.
- **Never suggest or run commands that print raw file contents** — no `print(row)`,
  no `print(df.head())`, no `print(sheet.row_values(i))`, nothing that would cause
  actual data to appear in terminal output.
- **If you need to understand a file's structure to fix a parser**, ask the user to
  share a screenshot. Do not attempt to read or print the file yourself.
- **config/passwords.yaml and config/accounts.yaml are in .claudeignore** — do not
  attempt to read them. Their structure is documented in this CLAUDE.md.
- **The only safe output from any script** is structural metadata: row count, column
  count, column names, and whether a value is empty or not — never the value itself.

---

## Known Quirks & Gotchas

- **Statement header detection:** Parser scans dynamically for first row with 2+ matches from `ALL_HEADER_KEYWORDS` (`parsers/base.py`) — skips personal info rows above transaction table.
- **Description line breaks:** Cell values contain `\n` mid-word. Fixed with `re.sub(r'(?<!\s)\n\s*', '', raw_desc)` before word-boundary join.
- **Column name variants:** Each statement has own column nomenclature; `base.py` maps per-bank variants via `date_cols`, `desc_cols`, `debit_cols`, `credit_cols`, `amount_cols` lists.
- **Internal transfers:** Check `raw_description` not `description` — cleaner runs after dedup.
- **Dedup key:** `(date, amount, txn_type, source_id)` — no description. Same-day same-amount same-source = duplicate.
- **`recategorize.py`** re-runs description cleaning + categorization but NOT `flag_internal_transfers`. Existing `is_internal_transfer` flags preserved.
- **SBI HTML-as-XLSX:** SBI sometimes exports HTML disguised as `.xlsx`. Parser detects `<html>` magic bytes, falls back to `pd.read_html()`.