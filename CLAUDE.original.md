# Finance Assistant — Phase 1: Tagging Workflow

**Goal:** Reliable, low-effort transaction categorization across savings + credit cards. Phase 2 (budgeting, savings advice, investment insights) needs clean data.

---

## How to use this doc

Each block has **Status**, **Role**, **What to do**, **Output**.

After block done, update:
- `Status:` → `✅ Done` (or `🟡 In progress`, `⬜ Not started`, `🔁 Needs revisit`)
- `Output:` → replace with actual artifact (file path, table name, sample output, notes)

Status legend:
- ⬜ Not started
- 🟡 In progress
- ✅ Done
- 🔁 Needs revisit

---

## Phase 1 "Done" Criteria

Complete when all three hold on real monthly data:

- [ ] >90% transactions tagged by cache/rules (no LLM call) on fresh month
- [ ] Dry-run corrections per month under ~20 rows
- [ ] Eval harness shows stable accuracy across two consecutive months without prompt changes

---

## Block 0 — Foundation: Canonical Taxonomy & Merchant Normalization

**Status:** ✅ Done

**Role:** Nothing downstream works if merchants aren't comparable across sources. Zomato on HDFC CC, Zomato via UPI, Zomato via Paytm must all resolve to same canonical name.

**What to do:**
- Lock final category list. Start from 9 in financeEnv. Decide:
  - Want `Internal Transfer` as own top-level category (recommended)?
  - Split `Other` into sub-buckets, or leave as review queue?
- Build/extend description cleaner: any raw string (UPI/X/Y/Z, POS CHARGE, NEFT-ABC-...) returns `canonical_merchant`.
- Handle common Indian statement noise: UPI prefixes, trip IDs, transaction ref numbers, trailing digits.
- Spot-check cleaner against sample of each statement type before trusting.

**Output:**

**Canonical Category List (locked 2026-04-25, source of truth: `config/categories.yaml`):**
| # | Category |
|---|----------|
| 1 | Food & Dining |
| 2 | Groceries |
| 3 | Fuel & Transport |
| 4 | Utilities & Bills |
| 5 | Rent |
| 6 | EMI & Loan |
| 7 | Health & Medical |
| 8 | Shopping & Apparel |
| 9 | Entertainment & Subscriptions |
| 10 | Education |
| 11 | Investment & SIP |
| 12 | Credit Card Payment |
| 13 | Internal Transfer |
| 14 | Internal Transfer — Self |
| 15 | Internal Transfer — Other |
| 16 | ATM & Cash |
| — | Other *(review queue — low-confidence fallback)* |

Notes:
- `Other` = NOT real category; review queue (Block 4 dry-run).
- `Internal Transfer — Self/Other` sub-types set at import time via `transfer_type` column in dry-run CSV.

**Merchant normalization — `core/description_cleaner.py`** (done 2026-04-16):

| Function | Output | Example |
|----------|--------|---------|
| `get_canonical_merchant(raw)` | Stable DB key | `"Zomato"`, `"Flipkart Paytm"`, `"Karkala Ro"` |
| `clean_description(raw)` | Human display | `"UPI - Zomato"`, `"IMPS - Karkala Ro"`, `"Auto Debit - HDFC Ergo"` |

Handled formats:
- HDFC savings: `UPI-MERCHANT-VPA@PSP-BANKREF`
- SBI/Canara savings: `UPI/DR|CR|DB/REFNUM/MERCHANT/...` and `MOB-IMPS-CR/PAYEE/BANK/...`
- Axis CC: `UPI/MERCHANT/VPA@PSP/TXNREF`
- ICICI/HDFC CC: plain merchant strings (fallback strip of location noise)
- All banks: `ACH DR/CR`, `ATM WDL`, `NEFT`, `CMP` (salary), `INT.CR`

`Transaction.canonical_merchant` field added to schema (populated at parse time).

---

## Block 1 — Ingestion & Extraction Hardening

**Status:** ✅ Done

**Role:** Fix "PDF extraction misses rows / grabs junk" before tagging. Garbage in = garbage tagged; you'll waste hours chasing tagging bugs that are really extraction bugs.

**What to do:**
- For each statement type (each bank PDF, each Excel), add **validation step**:
  - Count rows extracted vs. expected (use "total debit/credit count" on most Indian statements, or opening/closing balance math).
  - Mismatch → flag file, halt. No silent pass to tagging.
- Strip non-transaction filler (addresses, customer IDs, email headers) here, not later.
- Tag each row with `source_account` on ingestion.
- Standardize schema across sources: `date`, `raw_description`, `amount`, `direction (debit/credit)`, `source_account`, `bank_category` (nullable, for CCs).

**Output (done 2026-04-16):**

**Supported statement types + validation coverage:**
| Bank | Format | Validation |
|------|--------|-----------|
| SBI | XLSX | Balance math (opening/closing from metadata) |
| Canara | CSV / XLSX | Balance math (same metadata format as SBI) |
| HDFC savings | XLS | warn — no balance labels in header block |
| BOB | XLS | warn — no balance labels in header block |
| HDFC Moneyback CC | PDF | warn — no balance math for CCs |
| Amazon ICICI CC | PDF | warn — no balance math for CCs |
| Axis Supermoney CC | PDF | warn — no balance math for CCs |

**Validation statuses:** `pass` (delta ≤ ₹2) / `warn` (no metadata, row count > 0) / `fail` (mismatch or 0 rows → pipeline halts)

**Artifacts:**
- `parsers/validator.py` — `ParseValidation` dataclass + `validate_balance()` fn
- `parsers/base.py` — `ParseResult` wrapper; `parse()` returns `ParseResult`; `_find_balance_metadata()` scans pre-header rows
- `main.py` — halts on `fail`, skips categorization + DB insert
- `core/db.py` — `canonical_merchant` column + index added to transactions table

**Filler stripping:** non-date rows (totals, addresses, footnotes) skipped by date-parse guard in `_extract_transactions` — no separate strip pass needed.

---

## Block 2 — Corrections Database (The Cache)

**Status:** ✅ Done

**Role:** System memory. Every correction lives here. Makes system faster over time instead of re-writing same rules.

**Output (done 2026-04-21):**

**Schema — `core/corrections_db.py`:**
```python
corrections (
  canonical_merchant  TEXT PRIMARY KEY,
  category            TEXT NOT NULL,
  confidence_count    INTEGER DEFAULT 1,   # increments on repeat corrections
  last_seen_date      TEXT,
  source_account_hint TEXT,                # e.g. "cc_hdfc_moneyback" if merchant ≠ across accounts
  notes               TEXT
)
```

**Operations:**
- `lookup(canonical_merchant)` → returns category or None
- `upsert(canonical_merchant, category, source_account_hint, notes)` → inserts or increments confidence
- `get_all()` → list of all rows sorted by confidence desc
- `stats()` → {"total_merchants": N, "high_confidence_3plus": M}

**Seeding workflow:**
1. Generate dry-runs with `main.py --dry-run` on all statements
2. User corrects `corrected_category` column in each dry-run CSV (add column; blank = accept current)
3. For Internal Transfer rows: user adds `transfer_type` column (self/others/unknown)
4. Run `import_corrections.py <csv> [...]` to preview; `--save` to commit to DB + cache
5. Merge rule applied at import time:
   - `Internal Transfer + self → "Internal Transfer — Self"`
   - `Internal Transfer + others → "Internal Transfer — Other"`
   - `Internal Transfer + unknown → "Internal Transfer"` (flagged for review)
   - `transfer_type` column dropped after merge

**Seed snapshot (2026-04-21) from all corrected dry-runs:**
- **6 statement files corrected** (BOB, SBI, HDFC savings, HDFC CC, Amazon CC, Axis CC)
- **398 transactions seeded into DB**
- **61 unique merchants cached**
- **8 merchants at high confidence (3+ occurrences)**
- **0 rows flagged for review** (all transfer_type decisions made)

**Artifacts:**
- `core/corrections_db.py` — cache ops
- `import_corrections.py` — CSV importer with preview + `--save` mode
- `config/categories.yaml` — extended with `Internal Transfer — Self/Other` sub-types
- `config/accounts.yaml` — label→source_id mapping (loaded at import time)

**Key design:**
- Single source of truth: categories from `categories.yaml`, source IDs from `accounts.yaml`
- Transfer type merge happens at import, not display — DB stores only final merged category
- Merchant string normalization happens in `description_cleaner.get_canonical_merchant()` at parse time

---

## Block 3 — Categorization Pipeline (The Decision Tree)

**Status:** ✅ Done

**Role:** Tagging engine. Runs on every transaction. First hit wins — order matters.

**Implemented decision tree (in order):**

1. **`is_internal_transfer` flag** — set by deduplicator pre-categorization → `Internal Transfer`, confidence 1.0
2. **Corrections DB lookup** — `canonical_merchant` match → apply cached category, confidence 0.80–0.95 (scales with `confidence_count`)
3. **SBI raw pattern rules** — regex on raw description (ACH DR, IMPS bank codes, ATM WDL, etc.) → confidence 0.90
4. **YAML keyword rules** — `categories.yaml` keywords vs cleaned + raw description → confidence 0.80
5. **LLM fallback** — Ollama (`llama3`), prompted with canonical category list from yaml, returns `{category, confidence}` JSON
6. **"Other" fallback** — LLM returns Other or fails → `category_source: fallback`, confidence 0.0

**Every transaction gets:** `category`, `category_source`, `confidence`

**Output (done 2026-04-27):**

**Artifacts:**
- `core/categorizer.py` — full decision tree implementation
- `core/db.py` — `category_source TEXT` + `confidence REAL` columns added (with migration for existing DBs)
- `main.py` — dry-run CSV includes `category_source` + `confidence`; terminal prints source % breakdown

**category_source distribution across all statements (post Canara seeding):**
| Source | Example statements |
|--------|-------------------|
| `internal_transfer_flag` | SBI: 62% of rows |
| `corrections_db` | BOB: 98% / ICICI CC: 60% / Canara: 50% / HDFC: 38% |
| `raw_rules` | SBI spendable: 47% |
| `yaml_rules` | ICICI CC: 40% / Canara: 12% |
| `fallback` (Other) | Canara: 37% — mostly UPI person payments, one-off vendors |

**Known gaps (not blocking):**
- Splitwise / contact-based check: not implemented (no contacts list yet)
- Time + amount pattern rules: not implemented (low ROI vs corrections DB)
- Bank CC category hint: not passed to LLM (minimal signal given corrections DB coverage)
- Canara internal transfers: 0 flagged — Canara↔SBI/CC flows need Block 5 reconciliation

---

## Block 4 — Dry-Run Review Interface

**Status:** ⬜ Not started

**Role:** Monthly human-in-the-loop step. Where corrections get captured and fed back into system.

**What to do:**
- Dry-run exports to Excel/CSV.
- Sort: low-confidence + `Other` rows at top. Fix worst first.
- Include `corrected_category` column, blank by default.
- Re-import script after review:
  - Reads corrected rows.
  - Updates transaction's category in main DB.
  - **Upserts** `canonical_merchant → category` mapping into corrections DB (Block 2).
- **Key principle:** one correction updates both transaction AND rule for all future transactions.

**Output:**
> _Fill in when done. Examples: path to dry-run script, sample Excel template, path to re-import script, average corrections per month after system stabilizes._

---

## Block 5 — Cross-Source Reconciliation

**Status:** ⬜ Not started

**Role:** De-duplicate spend across savings + credit cards so category totals are real. Without this, every CC bill payment double-counts.

**What to do:**
- For each CC bill payment outflow on savings:
  - Find matching CC statement total within date window (±3 days typically).
  - Link them; mark savings-side as `Internal Transfer — CC Settlement`.
- For each individual CC charge:
  - Count once, on CC side only.
- Port reconciliation logic from financeEnv Task 2 — classification taxonomy (`genuine_spend`, `cc_settlement`, `internal_transfer`, `refund`) already right.

**Output:**
> _Fill in when done. Examples: count of linked settlements over year, any unmatched CC payments (and why), path to reconciliation module._

---

## Block 6 — Evaluation Harness (Where financeEnv Fits)

**Status:** ⬜ Not started

**Role:** Answers "did my changes actually improve tagging?" with a number. Stops iteration being guesswork.

**What to do:**
- Freeze snapshot of corrected year of data → ground truth.
- Feed into financeEnv-style task:
  - Input: raw transactions.
  - Expected output: your labels.
- Every prompt change, model swap (llama3 → qwen → Haiku), or decision tree tweak → run harness → compare scores.
- Track scores over time; catch regression if change makes things worse.

**Output:**
> _Fill in when done. Examples: baseline accuracy score, best-performing prompt/model combo, path to eval script, history of score changes per iteration._

---

## The Big-Picture Flow

```
Statement files (PDF/Excel)
        │
   [Block 1] Extract + validate rows
        │
   [Block 0] Clean descriptions → canonical_merchant
        │
   [Block 3] Categorization pipeline
        │   ├─ internal transfer check
        │   ├─ contact/Splitwise check
        │   ├─ corrections DB lookup     ◄── reads from [Block 2]
        │   ├─ time/amount rules
        │   ├─ bank category as hint
        │   └─ LLM + few-shot            ◄── reads from [Block 2]
        │
   [Block 5] Reconcile across sources
        │
   [Block 4] Dry-run Excel → you correct → re-import
        │                         │
        │                         └──► updates [Block 2] corrections DB
        │
   Final DB state
        │
   [Block 6] Eval harness measures accuracy → informs next iteration
```

---

## Working Notes

> _Scratchpad for things learned as you go — prompt tweaks, weird edge cases, merchant aliases, ideas for Phase 2._

- BOB joint account — my share = sum of my outflows to that account. Dad's contributions don't appear in my statements. 
- BBPS = payment rail not merchant. Tag as Utilities & Bills by default. Sub-biller detail not available from bank statements — enrich from biller apps in Phase 2 if needed. On savings account side, large BBPS amounts near CC due date = CC settlement.
- NEFT/IMPS keyword too broad for internal transfer detection — needs contact check first. Known contacts: dad (suresh pai variants), self (roma pa variants). BOB joint account = Savings & Investment, not family transfer."

---

## Next Phase (placeholder)

Phase 2 — Insights & Advice: budget planning, savings recommendations, investment suggestions. Not started until Phase 1 "Done" criteria met.

## Phase 3 (Future — multi-agent advisory system)

**Status:** ⬜ Not started

**Role:** Once Phase 1 & 2 are stable, build specialized agents for different financial tasks:
- Categorization Agent (debate-based)
- Reconciliation Agent
- Budget Planning Agent
- Tax Optimization Agent
- Investment Advisor Agent
- Coordinator (orchestrates, synthesizes)

**Why later, not now:** Multi-agent adds complexity. Value shows up when you have enough data (12+ months clean history) and independent problems to solve. Phase 1-2 is better as a clean pipeline.

**Output:**
> _To be filled in later._