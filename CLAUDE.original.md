# Finance Assistant — Phase 1: Tagging Workflow

**Goal:** Reliable, low-effort transaction categorization across savings accounts and credit cards, so Phase 2 (budgeting, savings advice, investment insights) operates on clean data.

---

## How to use this doc

Each block has a **Status**, **Role**, **What to do**, and **Output** section.

When you finish a block, update:
- `Status:` → `✅ Done` (or `🟡 In progress`, `⬜ Not started`, `🔁 Needs revisit`)
- `Output:` → replace with the actual artifact produced (file path, table name, sample output, notes on what you learned, etc.)

Status legend:
- ⬜ Not started
- 🟡 In progress
- ✅ Done
- 🔁 Needs revisit

---

## Phase 1 "Done" Criteria

Phase 1 is complete when all three hold on real monthly data:

- [ ] >90% of transactions tagged by cache/rules (no LLM call) on a fresh month
- [ ] Dry-run corrections per month are under ~20 rows
- [ ] Eval harness shows stable accuracy across two consecutive months without prompt changes

---

## Block 0 — Foundation: Canonical Taxonomy & Merchant Normalization

**Status:** ✅ Done

**Role:** Nothing downstream works if merchants aren't comparable across sources. Zomato on HDFC CC, Zomato via UPI, and Zomato via Paytm must all resolve to the same canonical name.

**What to do:**
- Lock down the final category list. Start from the 9 in financeEnv. Decide:
  - Do you want `Internal Transfer` as its own top-level category (recommended)?
  - Do you want to split `Other` into sub-buckets, or leave it as a review queue?
- Build/extend the description cleaner so that any raw string (UPI/X/Y/Z, POS CHARGE, NEFT-ABC-...) returns a `canonical_merchant`.
- Handle the common Indian statement noise: UPI prefixes, trip IDs, transaction reference numbers, trailing digits.
- Spot-check the cleaner against a sample of each statement type before trusting it.

**Output:**

**Canonical Category List (locked 2026-04-19):**
| # | Category |
|---|----------|
| 1 | Food & Dining |
| 2 | Grocery |
| 3 | Transport & Commute |
| 4 | Utilities & Bills *(electricity, water, gas, phone, internet)* |
| 5 | Rent & Housing |
| 6 | EMI & Loan Repayment |
| 7 | Insurance *(health, life, car, home)* |
| 8 | Healthcare *(doctor visits, medicine, hospital bills)* |
| 9 | Shopping & Apparel *(clothing, electronics, personal care: haircuts, skincare, grooming)* |
| 10 | Entertainment & Subscriptions *(movies, concerts, events, OTT, apps, memberships)* |
| 11 | Education *(school/college fees, coaching, courses)* |
| 12 | Travel & Vacation *(trips, flights, hotels, pilgrimages)* |
| 13 | Gifts & Donations *(wedding gifts, charity, religious donations)* |
| 14 | Savings & Investment *(FD, SIP, stocks, PPF, etc.)* |
| 15 | Internal Transfer *(between own accounts)* |
| 16 | Cash Withdrawal/Cash Expenses |
| — | Other *(review queue — low-confidence fallback)* |

Notes:
- `Other` is NOT a real category; it's a review queue (Block 4 dry-run).
- `Internal Transfer` covers both savings↔savings and CC settlement flows (Block 5 will sub-tag as `Internal Transfer — CC Settlement`).

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

**Role:** Fix the "PDF extraction misses rows / grabs junk" problem before tagging. Garbage in = garbage tagged, and you'll waste hours chasing tagging bugs that are really extraction bugs.

**What to do:**
- For each statement type (each bank PDF, each Excel), add a **validation step**:
  - Count rows extracted vs. count expected (use the "total debit count" / "total credit count" on most Indian statements, or opening/closing balance math).
  - If counts don't match, flag the file and halt — don't silently pass to tagging.
- Strip non-transaction filler (addresses, customer IDs, email headers) here, not later.
- Tag each row with its `source_account` immediately on ingestion.
- Standardize the schema across sources: `date`, `raw_description`, `amount`, `direction (debit/credit)`, `source_account`, `bank_category` (nullable, for CCs).

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

**Status:** ⬜ Not started

**Role:** The memory of the system. Every correction you ever make lives here. This is what makes the system get faster over time instead of needing the same rules re-written.

**What to do:**
- Create a `corrections` table keyed on `canonical_merchant`.
- Fields:
  - `canonical_merchant` (primary key or unique index)
  - `category`
  - `confidence_count` (how many times this mapping has been confirmed)
  - `last_seen_date`
  - `source_account_hint` (optional — only if same merchant means different things on different cards)
  - `notes` (optional)
- Expose two operations:
  - `lookup(canonical_merchant)` → returns category or None
  - `upsert(canonical_merchant, category)` → inserts or increments confidence
- Seed it by: running current logic on the year of savings data → exporting → correcting in Excel → importing back.

**Output:**
> _Fill in when done. Examples: table schema, path to DB file, seed size (e.g., "seeded with 487 unique merchants from 2024 savings data"), notes on surprises found during seeding._

---

## Block 3 — Categorization Pipeline (The Decision Tree)

**Status:** ⬜ Not started

**Role:** The actual tagging engine. Runs on every transaction. First hit wins — order matters.

**What to do — in this exact order:**

1. **Internal transfer / CC settlement check**
   - Rules on amount + counterparty account + date proximity between your own accounts.
   - Uses your `accounts.yaml` config for known account numbers.
   - If matched → tag as `Internal Transfer`, skip remaining steps.

2. **Splitwise / contact-based check**
   - Uses the contact names list you'll provide.
   - If UPI counterparty is a known friend → tag per your contact rules (e.g., `Social — pending split`).

3. **Corrections DB lookup** (Block 2)
   - Canonical merchant match. If hit → apply category.

4. **Time + amount pattern rules**
   - Narrow cases where context > merchant string.
   - Example: morning auto-rickshaws ₹40–150, 7–11 AM → `Transport & Commute`.

5. **Bank's default CC category**
   - NOT a decision. Passed as a *hint* into the LLM call below.

6. **LLM call with few-shot examples**
   - Retrieve 5–10 most similar past corrections from DB.
   - Input to LLM: raw description, canonical merchant, amount, time, source account, bank's suggested category.
   - Returns: `category` + `confidence`.

7. **Fallback to "Other"**
   - If LLM confidence is below threshold → tag as `Other`, flag for dry-run review.

**Every transaction gets:**
- `category`
- `category_source` (which of the 7 paths tagged it)
- `confidence`

**Output:**
> _Fill in when done. Examples: confidence threshold you settled on, distribution of category_source values on a real month (e.g., "62% corrections DB, 18% LLM, 12% internal transfer, 5% rules, 3% Other"), LLM model used._

---

## Block 4 — Dry-Run Review Interface

**Status:** ⬜ Not started

**Role:** The monthly human-in-the-loop step. Where corrections get captured and fed back into the system.

**What to do:**
- Dry-run exports to Excel/CSV.
- Sort order: low-confidence and `Other` rows at the top. Fix worst first.
- Include a `corrected_category` column, blank by default.
- After review, the re-import script:
  - Reads corrected rows.
  - Updates the transaction's category in the main DB.
  - **Upserts** the `canonical_merchant → category` mapping into the corrections DB (Block 2).
- **Key principle:** one correction updates both the transaction AND the rule for all future transactions.

**Output:**
> _Fill in when done. Examples: path to dry-run script, sample Excel template, path to re-import script, average corrections per month after the system stabilizes._

---

## Block 5 — Cross-Source Reconciliation

**Status:** ⬜ Not started

**Role:** De-duplicate spend across savings and credit cards so category totals are real. Without this, every CC bill payment gets double-counted.

**What to do:**
- For each CC bill payment outflow on savings:
  - Find matching CC statement total within a date window (±3 days typically).
  - Link them; mark savings-side as `Internal Transfer — CC Settlement`.
- For each individual CC charge:
  - Ensure it's counted once, on the CC side only.
- Port the reconciliation logic from financeEnv Task 2 — the classification taxonomy (`genuine_spend`, `cc_settlement`, `internal_transfer`, `refund`) is already right.

**Output:**
> _Fill in when done. Examples: count of linked settlements over the year, any unmatched CC payments (and why), path to reconciliation module._

---

## Block 6 — Evaluation Harness (Where financeEnv Fits)

**Status:** ⬜ Not started

**Role:** Answers "did my changes actually improve tagging?" with a number, so iteration stops being guesswork.

**What to do:**
- Freeze a snapshot of your corrected year of data → this is ground truth.
- Feed it into a financeEnv-style task:
  - Input: raw transactions.
  - Expected output: your labels.
- Every time you change a prompt, swap a model (llama3 → qwen → Haiku), or tweak the decision tree → run the harness → compare scores.
- Track scores over time so you see regression if a change makes things worse.

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

> _Scratchpad for things you learn as you go — prompt tweaks, weird edge cases, merchant aliases, ideas for Phase 2._

- BOB joint account — my share = sum of my outflows to that account. Dad's contributions don't appear in my statements. 

---

## Next Phase (placeholder)

Phase 2 — Insights & Advice: budget planning, savings recommendations, investment suggestions. Not to be started until Phase 1 "Done" criteria are met.