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

**Role:** Nothing downstream works without comparable merchants across sources. Zomato on HDFC CC, Zomato via UPI, Zomato via Paytm = same canonical name.

**What to do:**
- Lock final category list from financeEnv. Decide: want `Internal Transfer` as top-level? Split `Other` into sub-buckets or leave as review queue?
- Build/extend description cleaner: raw string → `canonical_merchant`.
- Handle Indian statement noise: UPI prefixes, trip IDs, ref numbers, trailing digits.
- Spot-check cleaner vs. samples before trusting.

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

**Status:** 🟡 In progress  

**Role:** Fix PDF extraction (misses rows/junk) before tagging. Garbage in = garbage tagged; hours wasted on extraction bugs masquerading as tagging bugs.

**What to do:**
- Add validation step per statement type: count extracted rows vs. expected (debit/credit totals or balance math). Mismatch → flag + halt; no silent pass.
- Strip filler (addresses, IDs, headers) now.
- Tag each row `source_account` on ingest.
- Standardize schema: `date`, `raw_description`, `amount`, `direction (debit/credit)`, `source_account`, `bank_category` (nullable, for CCs).

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

**Role:** System memory. Every correction lives here. Faster over time vs. re-writing same rules.

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
1. Generate dry-runs: `main.py --dry-run` on all statements
2. User corrects `corrected_category` column in dry-run CSV; blank = accept current
3. Internal Transfer rows: user adds `transfer_type` column (self/others/unknown)
4. Run `import_corrections.py <csv> [...]` to preview; `--save` commits to DB + cache
5. Merge rule at import time:
   - `Internal Transfer + self → "Internal Transfer — Self"`
   - `Internal Transfer + others → "Internal Transfer — Other"`
   - `Internal Transfer + unknown → "Internal Transfer"` (flagged for review)
   - `transfer_type` dropped after merge

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

**Status:** 🟡 In progress

**Role:** Tagging engine. Runs on every transaction. First hit wins — order matters.

**What to do — in this exact order:**

1. **Internal transfer / CC settlement check**
   - Rules on amount + counterparty account + date proximity.
   - Uses `accounts.yaml` for known account numbers.
   - Match → tag `Internal Transfer`, skip rest.

2. **Splitwise / contact-based check**
   - Uses contact names list.
   - UPI counterparty = known friend → tag per rules (e.g., `Social — pending split`).

3. **Corrections DB lookup** (Block 2)
   - Canonical merchant match. Hit → apply category.

4. **Time + amount pattern rules**
   - Context > merchant string cases.
   - Example: 7–11 AM auto-rickshaw ₹40–150 → `Transport & Commute`.

5. **Bank's default CC category**
   - Hint only (not decision). Passed to LLM below.

6. **LLM call with few-shot examples**
   - Retrieve 5–10 similar past corrections.
   - Input: raw description, canonical merchant, amount, time, source account, bank suggestion.
   - Returns: `category` + `confidence`.

7. **Fallback to "Other"**
   - LLM confidence below threshold → tag `Other`, flag for dry-run review.

**Every transaction gets:**
- `category`
- `category_source` (which of 7 paths tagged it)
- `confidence`

**Output:**
> _Fill in when done. Examples: confidence threshold settled, distribution of category_source on real month (e.g., "62% corrections DB, 18% LLM, 12% internal transfer, 5% rules, 3% Other"), LLM model used._

---

## Block 4 — Dry-Run Review Interface

**Status:** ⬜ Not started

**Role:** Monthly human-in-loop. Corrections captured + fed back to system.

**What to do:**
- Export dry-run to Excel/CSV.
- Sort: low-confidence + `Other` at top. Fix worst first.
- Include `corrected_category` column (blank default).
- Re-import script after review: reads corrected rows, updates transaction category in DB, upserts `canonical_merchant → category` to corrections DB.
- One correction = both transaction + rule update.

**Output:**
> _Fill in when done. Examples: path to dry-run script, sample Excel template, path to re-import script, average corrections per month after system stabilizes._

---

## Block 5 — Cross-Source Reconciliation

**Status:** ⬜ Not started

**Role:** De-duplicate spend across savings + CCs so totals are real. No CC bill payment double-count.

**What to do:**
- For each CC bill payment outflow on savings: find matching CC statement total (±3 days). Link + mark savings-side as `Internal Transfer — CC Settlement`.
- Count each CC charge once, CC-side only.
- Port reconciliation from financeEnv Task 2; classification taxonomy (`genuine_spend`, `cc_settlement`, `internal_transfer`, `refund`) already correct.

**Output:**
> _Fill in when done. Examples: count of linked settlements over year, any unmatched CC payments (and why), path to reconciliation module._

---

## Block 6 — Evaluation Harness (Where financeEnv Fits)

**Status:** ⬜ Not started

**Role:** Answers "did changes improve tagging?" with a number. Stops guesswork.

**What to do:**
- Freeze snapshot of corrected year → ground truth.
- Feed to financeEnv-style task: input raw transactions, output your labels.
- Run harness on prompt change, model swap, or decision tree tweak → compare scores.
- Track scores over time; catch regression.

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

---

## Next Phase (placeholder)

Phase 2 — Insights & Advice: budget planning, savings recommendations, investment suggestions. Not started until Phase 1 "Done" criteria met.