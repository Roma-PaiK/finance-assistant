# Personal Finance Manager — Command Reference

## Setup (one time)

```bash
# Install dependencies
uv sync

# Install Ollama (for AI categorization)
brew install ollama
ollama pull llama3

# Initialise the database
uv run main.py --init
```

---

## Daily Use — Processing Statements

### Dry run (preview only, no DB write)
```bash
# Outputs a timestamped CSV so you can review categories before committing
uv run main.py your_statement.xlsx --dry-run

# Custom CSV output path
uv run main.py your_statement.xlsx --dry-run --csv my_review.csv
```

### Commit to DB (after you're happy with dry run)
```bash
uv run main.py your_statement.xlsx
```

### Process multiple files at once
```bash
uv run main.py sbi_jan.xlsx canara_jan.pdf hdfc_cc_jan.pdf
```

> **File naming convention** — include the bank name so auto-detection works:
> | Bank / Card | Example filename |
> |---|---|
> | SBI salary | `sbi_jan2025.xlsx` |
> | HDFC EMI account | `hdfc_bank_jan2025.pdf` |
> | Bank of Baroda SIP | `bob_jan2025.pdf` |
> | Canara daily | `canara_jan2025.pdf` |
> | HDFC Millennia CC | `hdfc_millenia_jan2025.pdf` or `hdfc_cc_jan2025.pdf` |
> | Amazon ICICI CC | `amazon_icici_jan2025.pdf` |
> | SuperMoney Axis CC | `axis_jan2025.pdf` or `supermoney_jan2025.pdf` |

---

## Inspecting the Database

```bash
# Summary + last 30 transactions
uv run check_db.py

# All transactions (no limit)
uv run check_db.py --all

# Filter by month
uv run check_db.py --month 2025-01

# Category spend breakdown
uv run check_db.py --category

# Category breakdown for a specific month
uv run check_db.py --category --month 2025-01

# Show only uncategorized / 'Other' transactions
uv run check_db.py --uncategorized

# Uncategorized for a specific month
uv run check_db.py --uncategorized --month 2025-01
```

---

## Fixing Categories

### Re-run categorization on existing DB data (after editing rules)
```bash
# Rules only (fast, no Ollama)
uv run recategorize.py --no-llm

# Rules + Ollama LLM fallback
uv run recategorize.py

# Re-categorize one month only
uv run recategorize.py --month 2025-01 --no-llm
```

> **Workflow for fixing bad categories:**
> 1. Run `uv run check_db.py --uncategorized` to see what's wrong
> 2. Add keywords to `config/categories.yaml` under the right category
> 3. Run `uv run recategorize.py --no-llm` to apply
> 4. Run `uv run check_db.py --category` to verify

---

## Clearing the Database

```bash
# Clear ALL transactions (will ask for confirmation)
uv run clear_db.py

# Clear without confirmation prompt
uv run clear_db.py --force

# Clear one specific month only
uv run clear_db.py --month 2025-01

# Clear one specific account only
uv run clear_db.py --source acc_sbi_salary

# Clear one account for one month
uv run clear_db.py --source acc_sbi_salary --month 2025-01
```

> **Source IDs for --source flag:**
> | Account | source ID |
> |---|---|
> | SBI salary | `acc_sbi_salary` |
> | HDFC EMI | `acc_hdfc_emi` |
> | Bank of Baroda SIP | `acc_bob_sip` |
> | Canara daily | `acc_canara_daily` |
> | HDFC Millennia CC | `cc_hdfc_millenia` |
> | Amazon ICICI CC | `cc_amazon_icici` |
> | SuperMoney Axis CC | `cc_supermoney_axis` |

---

## Config Files

| File | What to edit |
|---|---|
| `config/accounts.yaml` | Your account numbers, last 4 digits, labels |
| `config/passwords.yaml` | PDF/Excel passwords per bank |
| `config/categories.yaml` | Keyword rules for auto-categorization |

> **Never commit:** `config/passwords.yaml`, `data/db/finance.db`, and `dry_run_*.csv` files.
> These are already in `.gitignore`.

---

## Typical Monthly Workflow

```bash
# 1. Dry run all new statements
uv run main.py sbi_feb.xlsx canara_feb.pdf hdfc_cc_feb.pdf --dry-run

# 2. Open the CSV, check categories look right

# 3. If anything's wrong, edit config/categories.yaml then dry run again
uv run main.py sbi_feb.xlsx --dry-run

# 4. Happy? Commit everything to DB
uv run main.py sbi_feb.xlsx canara_feb.pdf hdfc_cc_feb.pdf

# 5. Verify
uv run check_db.py --month 2025-02 --category
```

---

## Project Structure

```
finance_assistant/
├── main.py                  # Entry point — parse + categorize + DB write
├── check_db.py              # Inspect what's in the DB
├── clear_db.py              # Delete transactions from DB
├── recategorize.py          # Re-run categorization on existing DB data
├── pyproject.toml           # uv project config + dependencies
├── uv.lock                  # uv lockfile (auto-managed)
├── requirements.txt         # pip-compatible dependency list
├── .python-version          # Python version pin for uv
│
├── config/
│   ├── accounts.yaml        # Your accounts + card details + transfer rules
│   ├── passwords.yaml       # PDF/Excel passwords (never commit this)
│   └── categories.yaml      # Keyword → category mapping rules
│
├── parsers/
│   ├── base.py              # SmartParser + BANK_CONFIGS (all banks in one place)
│   └── detector.py          # Auto-selects the right config per file
│
├── core/
│   ├── db.py                # SQLite read/write layer
│   ├── categorizer.py       # Rules engine + Ollama LLM fallback
│   ├── deduplicator.py      # Internal transfer detection + dedup
│   └── description_cleaner.py  # Cleans raw bank description strings
│
└── data/
    └── db/
        └── finance.db       # Your SQLite database (never commit this)
```