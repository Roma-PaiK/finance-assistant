# Finance Assistant — Phase 2: Insights & Reporting

> **Phase 1 summary:** see `claude_phase_1.md`  
> **Phase 1 artifacts:** `core/`, `parsers/`, `config/`, `main.py`, `review.py`, `import_corrections.py`, `check_db.py`, `clear_db.py`

---

## How to use this doc

Each block has **Status**, **Role**, **What to do**, **Output**.

After a block is done, update:
- `Status:` → `✅ Done` (or `🟡 In progress`, `⬜ Not started`, `🔁 Needs revisit`)
- `Output:` → replace with actual result (file path, sample output, notes)

Status legend: ⬜ Not started · 🟡 In progress · ✅ Done · 🔁 Needs revisit

---

## Phase 2 "Done" Criteria

All three must hold:
- Monthly spend report generated in <10s with no manual correction needed
- Budget variance flags catch genuine overruns (not internal transfer noise)
- Two consecutive months of reports where category totals feel correct on gut check

---

## ⚠️ Pre-conditions Before Any Block Below

Two deferred Phase 1 blocks must be done (or explicitly accepted as gaps) before Phase 2 spend totals are trustworthy:

| Pre-condition | Why it matters |
|---|---|
| Block 5 — CC reconciliation | Without it, every CC bill payment on savings double-counts the spend |
| Block 5B — Statement re-upload dedup | Without it, re-uploading any statement silently duplicates rows in DB |

---

## Block 5 — Cross-Source CC Reconciliation (Deferred from Phase 1)

**Status:** ✅ Done

**Role:** De-duplicate spend across savings + credit cards. Without this, every CC bill payment double-counts the same spend — it's both on the CC statement (individual charges) and on the savings statement (the lump payment out). Category totals in Phase 2 are wrong until this is done.

**Confirmed behaviour:**
- Always pay one CC at a time — no multi-CC lump payments. Each outflow on savings maps to exactly one CC's bill.
- CC payments go out from **either Canara (`acc_canara_daily`) or HDFC savings (`acc_hdfc_emi`)** — both accounts must be checked, not just Canara.

**All active credit cards (4 total):**

| Card | Source ID | Statement format | Likely payment source |
|---|---|---|---|
| HDFC Moneyback | `cc_hdfc_moneyback` | HDFC CC PDF | Canara or HDFC savings |
| Amazon ICICI | `cc_amazon_icici` | ICICI CC PDF | Canara or HDFC savings |
| Axis Supermoney | `cc_supermoney_axis` | Axis CC PDF | Canara or HDFC savings |
| Tata Neu HDFC | `cc_hdfc_tataneu` | HDFC CC PDF (same format as Moneyback) | Canara or HDFC savings |

**What to do:**

- For each CC bill payment outflow on Canara or HDFC savings (CRED Club / BBPS near CC due date):
  - Identify which CC card it's for (from merchant pattern or amount match)
  - Find the matching CC statement "total due" or "minimum due" amount within a ±5-day window of the CC due date
  - Link them; mark savings-side transaction as `cc_settlement`, not genuine spend
- For each individual CC charge: count once, on the CC side only
- Classification taxonomy for every transaction: `genuine_spend` | `cc_settlement` | `internal_transfer` | `refund`
- Add `transaction_type` column to DB (migration needed); populate at reconciliation time

**Schema addition:**

```sql
-- Add via migration in core/db.py
ALTER TABLE transactions ADD COLUMN transaction_type TEXT DEFAULT 'genuine_spend';
ALTER TABLE transactions ADD COLUMN linked_statement_id INTEGER; -- FK to statement_log.id
-- transaction_type values: genuine_spend | cc_settlement | internal_transfer | refund | flagged
```

**CC statement format handling — two formats exist:**

| Format | When | What to extract |
|---|---|---|
| Single-month statement | Recent months | Billing period, total due, min due, due date — all from one PDF |
| Consolidated multi-cycle document | Older months | Multiple billing cycles in one file. Parser must split by cycle boundary, extract per-cycle: period start/end, total due, due date. Each cycle treated as a separate statement period in `statement_log`. |

For consolidated docs:
- Detect cycle boundaries by looking for repeating header patterns (e.g. "Statement Date", "Payment Due Date" appearing multiple times in the PDF)
- Split into logical cycles; parse each independently
- Ingest each cycle as its own `statement_log` entry with correct `period_start` / `period_end`
- Block 5B dedup logic applies per-cycle — re-uploading a consolidated doc that overlaps already-ingested cycles will warn per cycle, not block the whole file

**Matching logic:**

```
For each savings outflow tagged as potential CC settlement:
  1. Check CRED Club pattern → identifies CC card directly (high confidence)
  2. Check BBPS with biller name → identifies CC card (medium confidence)
  3. Amount match: savings outflow amount == CC statement "total due" for that card, within ±5 days of due date
  4. Match found → mark savings row as cc_settlement, link to CC statement period
  5. No match → flag as `flagged` for manual review (not silent pass)
```

**Edge cases to handle:**
- BBPS near CC due date = secondary signal (lower confidence than CRED Club — amount match required)
- Partial payment (paid minimum due, not full): amount matches `min_due` not `total_due` → flag as partial, still mark as `cc_settlement` but note outstanding balance
- Payment from HDFC savings for HDFC Moneyback or Tata Neu HDFC CC — same bank, different account type. Two HDFC CCs now exist; if payment comes from HDFC savings, must disambiguate by amount matching against each card's due amount, not by merchant name (which will just say "HDFC" for both)
- Tata Neu HDFC: statement PDF format is expected to be the same as HDFC Moneyback (both HDFC-issued). Reuse the same parser config in `BANK_CONFIGS`; differentiate by filename keyword `tataneu` or last-4 card digits in `config/accounts.yaml`

**Output:**

> **Done.** `core/reconciler.py` + `reconcile.py` CLI. Matching logic: CRED Club (high conf) → BBPS biller name (medium) → amount match within ±2% or ₹150, 45-day billing window. `reconciliation_links` table logs each match. Unmatched suspected CC payments flagged for manual review. CC settlement marking: savings-side row → `transaction_type = 'cc_settlement'`, `is_internal_transfer = 1`, `category = 'Internal Transfer — CC Settlement'`. Run: `uv run python reconcile.py` (preview) / `--save` (commit). Note: reconciler needs CC statements loaded (Block 12) before matches fire.

---

## Block 5B — Statement Re-upload Dedup (New — Gap from Phase 1)

**Status:** ✅ Done

**Role:** Prevent silent duplicate rows when the same statement period is uploaded more than once. Currently, re-uploading any file (e.g. re-downloading Feb 2025 Canara statement) will INSERT duplicates with no warning. This is a data integrity problem — and it's hard to detect after the fact because the duplicate rows look identical.

**Why this happens:** There is no unique constraint or ingestion registry. `deduplicator.py` handles cross-account internal transfer dedup, not re-upload dedup.

**What to do:**

- Add a `statement_log` table to track what has been ingested:

```sql
CREATE TABLE IF NOT EXISTS statement_log (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  source_account TEXT NOT NULL,       -- e.g. acc_canara_daily
  period_start  TEXT NOT NULL,        -- earliest date in file (DD/MM/YYYY)
  period_end    TEXT NOT NULL,        -- latest date in file
  file_hash     TEXT NOT NULL,        -- SHA256 of file bytes
  ingested_at   TEXT NOT NULL,        -- timestamp
  row_count     INTEGER,
  UNIQUE(source_account, period_start, period_end)
);
```

- At import time (`main.py`), before inserting:
  1. Compute file SHA256
  2. Check `statement_log` for matching `(source_account, period_start, period_end)`
  3. If exact file hash match → skip with message: "Already ingested. Use --force to re-import."
  4. If same period but different hash → warn: "Period overlap detected for `{source}` ({start}–{end}). Re-upload or amended file? Use --force to replace."
  5. `--force` mode: clears that `(source_account, period_start, period_end)` from transactions and re-inserts

- Dry-run (`--dry-run`) bypasses the log check — preview is always safe

**This replaces the current manual workflow of:**
```bash
# What you currently have to do manually:
uv run clear_db.py --source acc_canara_daily --month 2025-01
uv run main.py canara_jan.pdf
```

**Output:**

> **Done.** `statement_log` table in `core/db.py`. SHA256 hash + `(source_account, period_start, period_end)` unique constraint. On re-upload: exact hash match → skip with message; same period diff hash → warn + block unless `--force`; `--force` deletes existing rows for that period then re-inserts. `--dry-run` bypasses check (preview always safe). DB cleared 2026-05-15; all future imports tracked from clean state.

---

## Block 6 — Evaluation Harness (Deferred from Phase 1)

**Status:** ✅ Done

**Role:** Answers "did my changes actually improve tagging?" with a number. Without this, every config/prompt tweak is guesswork.

**What to do:**

- Ground truth = current DB categories (manually verified; 1466 non-internal-transfer rows)
- `eval.py` re-runs pipeline on raw descriptions, compares predicted vs DB category
- Two modes: `--with-corrections` (production pipeline) vs default (rules-only honest eval)

**Commands:**
```
uv run python eval.py                    # rules only, no LLM (fast baseline)
uv run python eval.py --with-corrections # full pipeline incl. corrections DB
uv run python eval.py --llm              # enable LLM fallback (slow)
uv run python eval.py --month 2025-01    # single month
uv run python eval.py --source acc_canara_daily
uv run python eval.py --out results.csv  # per-row CSV dump
```

**Output:**

> **Done.** `eval.py`. Baseline scores on 1466 rows (2025, all sources, excl. internal transfers):
>
> | Mode | Accuracy |
> |---|---|
> | Rules only (no corrections, no LLM) | **78.0%** |
> | Rules + corrections DB (no LLM) | **86.0%** |
>
> Key findings from baseline run (2026-06-04):
> - `corrections_db` source: 118/118 correct (100%) — corrections DB is reliable
> - `fallback` (Other): 395/588 = 67% — largest error source; 193 rows land in Other when they shouldn't
> - Top gaps: Food & Dining (−53 to Other), Groceries (−34), Income (−34), Health & Medical (−22)
> - Refund detection: 0% in eval context (cross-txn matching can't fire on DB subset) — not a real accuracy gap
> - ATM & Cash: 28.6% — needs keyword coverage in categories.yaml
> - Income: 37% — Canara income credits not caught by raw_rules (SBI-only) or YAML keywords

---

## Block 6B — Pattern-Enriched LLM Context (Ambiguous UPI Transactions)

**Status:** ⬜ Not started

**Role:** Reduce the LLM fallback % for ambiguous UPI transactions (primarily auto-rickshaws and
similar cash-substitute payments) by passing structured pattern context alongside the raw 
description. The name alone is useless ("UPI/9876543210/PAY" tells the LLM nothing); the 
time + amount together are strong signals.

**The problem it solves:**
Auto-rickshaws on Canara frequently appear as bare UPI payment strings with no merchant name.
Hard-coding a rule ("₹60–150 between 7–10am = Auto") is brittle — amount ranges shift, 
occasional evening rides break it. Instead: detect that a transaction *looks like* a pattern 
match candidate, and pass that hypothesis to the LLM as context. The LLM then confirms or 
overrides based on the full picture.

**What to do:**

- Build a `PatternMatcher` class in `core/pattern_matcher.py`
- It runs *before* the LLM fallback step in the categorization decision tree (between YAML rules
  and LLM — new step 5B)
- For each transaction that reaches fallback, `PatternMatcher` checks:
  1. **Amount range match:** does the amount fall within a known range for a category?
  2. **Time-of-day match:** does the transaction time fall in a known window?
  3. **Source account match:** some patterns only make sense on specific accounts
  4. **Recurrence signal:** has a similar (amount ± ₹20, same time window) transaction appeared 
     3+ times before and been tagged consistently?
- If pattern fires → pass a `pattern_hint` string to the LLM prompt instead of calling it blind

**Updated LLM prompt when pattern fires:**

```python
# Instead of:
prompt = f"Categorize this transaction: '{raw_description}' for ₹{amount}"

# Pass:
prompt = f"""Categorize this transaction: '{raw_description}' for ₹{amount} at {time}.

Pattern hint: This matches a recurring pattern — {pattern_hint}.
Examples of similar past transactions tagged as {suggested_category}:
{similar_examples}  # 2-3 rows from corrections DB with same time+amount window

Does this match {suggested_category}? If yes, confirm. If not, suggest the correct category."""
```

**Seed patterns to start with (add more as you discover them):**

```yaml
# config/patterns.yaml
patterns:
  - id: auto_morning
    label: "Morning auto ride"
    category: Fuel & Transport
    canonical_merchant: Auto Rickshaw
    source_accounts: [acc_canara_daily, cc_hdfc_moneyback, cc_hdfc_tataneu]
    amount_range: [50, 200]
    time_window: ["06:30", "10:30"]
    min_occurrences: 3          # only activate after seen 3+ times
    confidence: 0.75            # hint confidence — LLM still makes final call

  - id: auto_evening
    label: "Evening auto ride"
    category: Fuel & Transport
    canonical_merchant: Auto Rickshaw
    source_accounts: [acc_canara_daily, cc_hdfc_moneyback, cc_hdfc_tataneu]
    amount_range: [50, 200]
    time_window: ["17:00", "22:00"]
    min_occurrences: 3
    confidence: 0.75
```

**Updated categorization decision tree (revised step 5):**

is_internal_transfer flag           → Internal Transfer (1.0)
Corrections DB lookup               → cached category (0.80–0.95)
Contact match (alias + VCF)         → Other + splitwise_candidate=1 (0.90–0.95)
SBI raw pattern rules               → category (0.90)
YAML keyword rules                  → category (0.80)
5B. PatternMatcher                     → pattern_hint passed to LLM (0.75 hint only)
LLM fallback (with or without hint) → category
"Other" fallback                    → review queue (0.0)


**Important constraint:** PatternMatcher never assigns a category directly — it only enriches
the LLM prompt. The LLM still makes the call. This avoids the brittleness of hard rules while 
giving the LLM enough context to stop guessing.

**When to build this:**
After Block 6 (eval harness) is done — the eval harness gives you a before/after accuracy score
so you can measure whether this actually moves the needle on Canara's ~37% fallback rate.
If Canara's fallback drops below 15% after corrections DB seeding alone (from loading 12 months 
of CC + bank statements), skip this block — it may not be needed.

**Output:**

> *Fill in when done. Canara fallback % before and after, patterns file path, any patterns that 
> fired unexpectedly (false positives to tune).*

---

## Block 7 — Monthly Spend Report

**Status:** ✅ Done

**Pre-condition:** Block 5 (CC reconciliation) and Block 5B (re-upload dedup) must be done first. Totals are wrong otherwise.

**Role:** Core output of Phase 2. Single command, readable summary of the month — what was spent, where, vs. last month, vs. budget.

**What to do:**

- `report.py --month YYYY-MM` outputs:
  - Category totals (genuine spend only — excludes `cc_settlement`, `internal_transfer`, `refund`)
  - MoM delta per category (absolute ₹ + %)
  - Top 5 merchants by spend
  - `Other` row count (how many rows still in review queue)
- Output formats: terminal table (default) + `--csv` / `--excel` flags
- Full-year view: `report.py --year YYYY`

**Key query:**

```sql
SELECT category, SUM(amount) as total
FROM transactions
WHERE strftime('%Y-%m', date_parsed) = '2025-01'    -- requires date_parsed column (ISO format)
  AND transaction_type = 'genuine_spend'
GROUP BY category
ORDER BY total DESC;
```

> Note: this requires adding `date_parsed` (ISO 8601) column alongside existing DD/MM/YYYY `date` column — needed for reliable SQLite date filtering. Add in Block 5 migration.

**Output:**

> **Done.** `report.py`. Commands: `uv run python report.py --month YYYY-MM` (terminal table), `--compare YYYY-MM` (explicit MoM), `--csv` / `--excel` (export), `--year YYYY` (full-year view). Filters: `transaction_type = 'genuine_spend'` + `txn_type = 'debit'` only. Shows: category totals, MoM delta, bar chart, top 5 merchants, Other review queue count. Ready to test once Block 12 data committed.

---

## Block 8 — Budget Tracking

**Status:** ✅ Done

**Role:** Compare actuals to targets per category. Surface overruns early in the month, not at month-end.

**What to do:**

- Add `config/budget.yaml` — monthly targets per category
- `report.py --month YYYY-MM --budget` overlays targets on actuals
- Flag categories where actual > 110% of budget (configurable threshold in budget.yaml)
- Distinguish fixed vs. variable categories — different alert logic:
  - Fixed (Rent, EMI): informational only, no alert
  - Variable (Food, Shopping): alert on overrun
- No rollover logic for now — each month resets

**`config/budget.yaml` structure:**

```yaml
thresholds:
  alert_pct: 110          # alert if actual > this % of budget
  warn_pct: 90            # warn (amber) if actual > this % of budget

monthly_targets:
  Food & Dining: 8000
  Groceries: 4000
  Fuel & Transport: 3000
  Entertainment & Subscriptions: 2000
  Shopping & Apparel: 3000
  Health & Medical: 1500

fixed_categories:         # informational only, no overrun alert
  - Rent
  - EMI & Loan
  - Investment & SIP
```

**Output:**

> **Done.** `config/budget.yaml` + `--budget` flag on `report.py`. Variable categories get 🔴/🟡/🟢 status. Fixed categories (Rent, EMI, SIP) shown informational only. Alert summary printed at bottom if any category OVER/WARN. Thresholds: alert=110%, warn=90% (configurable in budget.yaml). Run: `uv run python report.py --month YYYY-MM --budget`.

---

## Block 9 — Anomaly Detection

**Status:** ⬜ Skipped — not needed

**Why skipped:** Banks (HDFC, ICICI, Axis, Canara) already flag anomalies and fraud in-app. Duplicate UPI retry detection has limited value given low false-negative rate from banks. Block 9 adds no splitwise signal — that comes from contact match (Block 3). May revisit if cross-bank pattern detection becomes useful in Phase 3.

**Role:** Flag unusual transactions without needing budget config. Catches one-offs, duplicate charges, suspiciously large amounts.

**What to do:**

- Flag: amount > 3× category median for that merchant (from corrections DB history)
- Flag: same-amount duplicate within 3-day window, same source account (UPI retry pattern)
- Flag: new merchant (not in corrections DB) with amount > ₹2,000
- Surface in dry-run CSV as `anomaly = 1` column — separate from category corrections
- Anomalies = data quality / one-off signals, not budget signals. Keep separate from Block 8 alerts.

**Output:**

> *Fill in when done. Example: anomaly count on first run, false positive rate.*

---

## Block 10 — Savings & Investment Awareness

**Status:** ✅ Done

**Role:** Passive awareness layer — not advice. "Here's what went to SIPs vs. what you spent vs. what hit your account." Sets up Phase 3 investment reasoning.

**What to do:**

- Monthly summary of `Investment & SIP` category flows (from BOB SIP account)
- Track `acc_bob_sip` outflows — sum by month, running YTD total
- Compute indicative savings rate: `(salary_inflow - genuine_spend) / salary_inflow`
  - Salary inflow = `CMP`-tagged transactions on SBI
  - Genuine spend = Block 7 total
  - Label as "indicative only" — tax, insurance, investment flows skew this
- Surface as a section within Block 7 report, not a separate command

**Output:**

> **Done.** Section added to `report.py` month report (always shown, no flag needed). Shows: salary inflow (SBI `acc_sbi_salary` credits), SIP outflow + count (`acc_bob_sip` debits), SIP YTD, genuine spend, indicative savings rate with low/good marker. Labelled "indicative only — excludes tax, insurance, inter-account flows".

---

## Block 11 — Splitwise Reconciliation

**Status:** ✅ Done

**Role:** Handle the "paid full upfront, recoup later" pattern. Without this, Food & Dining / Entertainment totals are inflated on months you front group expenses.

**What to do:**

- `splitwise_candidate = 1` rows (from Block 3 contact match) enter a pending pool
- `splitwise.py` CLI: review candidates, confirm split ratio (or skip)
- Your effective share replaces full amount in spend totals for Block 7
- Track outstanding receivables (who owes you, how much)
- Splitwise API integration deferred to Phase 3 — manual CSV input for now

**Schema addition (migration):**

```sql
ALTER TABLE transactions ADD COLUMN splitwise_confirmed INTEGER DEFAULT 0;
ALTER TABLE transactions ADD COLUMN your_share_amount REAL;      -- NULL = full amount
ALTER TABLE transactions ADD COLUMN splitwise_group TEXT;         -- e.g. "Goa trip Jan 2025"
```

**Output:**

> **Done.** `splitwise.py` CLI + 3 DB columns (`splitwise_confirmed`, `your_share_amount`, `splitwise_group`). Commands: `pending` (list candidates), `confirm <id>` (interactive: 50/50, custom %, or fixed ₹), `dismiss <id>`, `receivables` (who owes you), `export` (CSV for manual Splitwise entry), `summary --month` (gross vs net). `report.py` category totals auto-use `your_share_amount` when split confirmed. Phase 3: Splitwise API sync.

---

## Block 12 — CC Statement Catchup (Ongoing Data Task)

**Status:** ✅ Done (2025 scope)

**Role:** Phase 1 only ingested 1 month of CC statements. Block 5 reconciliation needs the matching CC statements to work. This is a data loading task, not a code task — but it needs to be tracked.

**Scope decision:** 2025 data only for Phase 2. 2026 statements added later. Axis Supermoney and Tata Neu HDFC both activated in 2026 — no 2025 statements exist for these cards.

**Tracking table (⬜ = not done, ✅ = ingested, N/A = card not active that month):**

| Month | HDFC Moneyback | Amazon ICICI | Axis Supermoney | Tata Neu HDFC |
|---|---|---|---|---|
| 2025-01 | ✅ | ✅ | N/A | N/A |
| 2025-02 | ✅ | ✅ | N/A | N/A |
| 2025-03 | ✅ | ✅ | N/A | N/A |
| 2025-04 | ✅ | ✅ | N/A | N/A |
| 2025-05 | ✅ | ✅ | N/A | N/A |
| 2025-06 | ✅ | ✅ | N/A | N/A |
| 2025-07 | ✅ | ✅ | N/A | N/A |
| 2025-08 | ✅ | ✅ | N/A | N/A |
| 2025-09 | ✅ | ✅ | N/A | N/A |
| 2025-10 | ✅ | ✅ | N/A | N/A |
| 2025-11 | ✅ | ✅ | N/A | N/A |
| 2025-12 | ✅ | ✅ | N/A | N/A |
| 2026-01 | ⬜ | ⬜ | ⬜ | ⬜ |
| 2026-02 | ⬜ | ⬜ | ⬜ | ⬜ |
| 2026-03 | ⬜ | ⬜ | ⬜ | ⬜ |
| 2026-04 | ⬜ | ⬜ | ⬜ | ⬜ |

> 2026 rows: do after Phase 2 is stable. Tata Neu + Axis setup notes preserved below for when needed.

**⚠️ One-time setup required for Tata Neu HDFC CC (do before loading any 2026 statements):**
1. Add to `config/accounts.yaml` — label `Tata Neu HDFC CC`, source ID `cc_hdfc_tataneu`, last 4 digits of card
2. Add PDF password to `config/passwords.yaml` under `cc_hdfc_tataneu`
3. Add `tataneu` keyword in `parsers/detector.py` → maps to HDFC CC parser in `BANK_CONFIGS`
4. Dry-run one statement to confirm before committing

**Output:**

> All 2025 months for HDFC Moneyback (610 rows) and Amazon ICICI (186 rows) ingested via import_corrections.py from dry-run CSVs. DB total at completion: 1602 rows across all sources.

---

## Phase 2 Execution Order

Do these in sequence — each block depends on the previous:

```
[Block 5B] Re-upload dedup          ← data integrity, do first
      │
[Block 5]  CC reconciliation        ← spend totals are wrong until this is done
      │
[Block 12] CC statement catchup     ← load all 2025 CC data so Block 5 has something to match
      │
[Block 6]  Eval harness             ← freeze ground truth before adding more data
      │
[Block 7]  Monthly spend report     ← first real Phase 2 output
      │
[Block 8]  Budget tracking          ← overlay targets on Block 7 output
      │
[Block 9]  Anomaly detection        ← flag issues in dry-run before they hit DB
      │
[Block 10] Savings awareness        ← add section to Block 7 report
      │
[Block 11] Splitwise reconciliation ← last because it needs contact match (Phase 1) + spend report (Block 7)
```

---

## Working Notes

> *Scratchpad — add discoveries, edge cases, prompt tweaks as you go.*

### Ongoing data tasks to stay on top of
- Download CC statements monthly (all 4 cards now: HDFC Moneyback, Amazon ICICI, Axis Supermoney, Tata Neu HDFC) — name per convention, run dry-run before committing
- Re-export `contacts/contacts.vcf` when you add/rename contacts in phone
- Add new aliases to `config/contact_aliases.yaml` whenever a dry-run shows an unknown person payment

### Pending keyword additions
- **Rent — Kusum Devi**: rent is paid via UPI from Canara bank account to Kusum Devi. Once Canara stmt dry-run is done, check the exact description format and add her canonical name as a keyword under `Rent` in `config/categories.yaml`.

### Things to NOT forget
- `--dry-run` → `import_corrections.py --save` for fresh months (INSERT flow)
- `review.py export` → `review.py apply --save` for retroactive fixes (UPDATE flow)
- Never commit `config/passwords.yaml`, `data/db/finance.db`, or `dry_run_*.csv`

---

---

## Phase 3 Placeholder — UI, Dashboard & Natural Language Q&A

**Status:** ⬜ Not started — start after Phase 2 "Done" criteria are met.

**What goes here:** This is the phase where you stop running CLI commands and start interacting with your data through a proper interface. Three components:

**Component A — Dashboard (read-only, visual)**
- Monthly spend by category (bar/donut chart)
- MoM trend lines per category
- Budget vs actual gauges (from Block 8)
- Savings rate over time (from Block 10)
- Outstanding Splitwise receivables (from Block 11)
- Tech choice: local web app (Flask/FastAPI + simple HTML) or Streamlit — decide when you get here

**Component B — Natural language Q&A over your DB**
- Ask questions in plain English: "How much did I spend on food in March?", "Which month had the highest shopping spend?", "What's my average monthly Zomato bill?"
- Answer comes from the DB — not from Claude's memory
- Implementation: Claude API (claude-sonnet) with function calling / tool use. Claude interprets the question, generates the SQL query, runs it against `finance.db`, returns the answer in plain English
- Queries must respect `transaction_type = 'genuine_spend'` filter automatically — never return raw totals that include internal transfers

**Component C — Splitwise transaction management UI**
- Surface all `splitwise_candidate = 1` rows in a simple table
- For each: confirm split ratio, assign to a group (e.g. "Goa trip"), or dismiss
- Show outstanding receivables grouped by person
- This replaces the `splitwise.py` CLI from Block 11 — same data, better UX
- Optional: Splitwise API sync (pull settlements from Splitwise app to auto-close receivables)

> Phase 3 plan will be written in `claude_phase_3.md` when Phase 2 is complete.

---

## Phase 4 Placeholder — Multi-Agent Advisory System

**Status:** ⬜ Not started — do not start until Phase 3 is stable and 12+ months of clean data exists.

**Why this late:** Multi-agent adds significant complexity. Payoff only shows up with enough clean history (12+ months) and independent sub-problems to solve. Agents planned: Categorization, Reconciliation, Budget Planning, Spend Pattern, Tax Awareness, Investment Advisor, Coordinator.

> Phase 4 plan will be written in `claude_phase_4.md` when Phase 3 is complete.
