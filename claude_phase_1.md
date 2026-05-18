# Finance Assistant — Phase 1 Summary (Completed)

> **Compressed reference.** Full block-by-block notes were in the original `claude.md`.  
> Phase 1 status: Blocks 0–4 ✅ done. Block 5 (CC reconciliation) and Block 6 (eval harness) deferred — carried into Phase 2 as pre-conditions.

---

## What Phase 1 Built

| Block | Name | Status | Key artifact |
|---|---|---|---|
| 0 | Canonical taxonomy + merchant normalization | ✅ Done | `core/description_cleaner.py` |
| 1 | Ingestion + extraction hardening | ✅ Done | `parsers/base.py`, `parsers/validator.py` |
| 2 | Corrections database (cache) | ✅ Done | `core/corrections_db.py`, `import_corrections.py` |
| 3 | Categorization pipeline (decision tree) | ✅ Done | `core/categorizer.py`, `core/contact_matcher.py` |
| 4 | Dry-run review interface | ✅ Done | `review.py`, Excel export with dropdowns |
| 5 | Cross-source CC reconciliation | ⬜ Deferred | — |
| 6 | Evaluation harness | ⬜ Deferred | — |

---

## Locked Decisions

**Category list** (source of truth: `config/categories.yaml`):
Food & Dining, Groceries, Fuel & Transport, Utilities & Bills, Rent, EMI & Loan, Health & Medical, Shopping & Apparel, Entertainment & Subscriptions, Education, Investment & SIP, Credit Card Payment, Internal Transfer, Internal Transfer — Self, Internal Transfer — Other, ATM & Cash, Other *(review queue only)*

**Supported banks:**

| Bank | Format | Validation |
|---|---|---|
| SBI | XLSX | Balance math ✅ |
| Canara | CSV/XLSX | Balance math ✅ |
| HDFC savings | XLS | warn (no header balance) |
| BOB | XLS | warn (no header balance) |
| HDFC Moneyback CC | PDF | warn |
| Amazon ICICI CC | PDF | warn |
| Axis Supermoney CC | PDF | warn |

**Categorization decision tree (priority order):**
1. `is_internal_transfer` flag → Internal Transfer (confidence 1.0)
2. Corrections DB lookup → cached category (0.80–0.95, scales with confidence_count)
3. Contact match — alias file + VCF fuzzy → Other + `splitwise_candidate=1` (0.90–0.95)
4. SBI raw pattern rules (0.90)
5. YAML keyword rules (`config/categories.yaml`) (0.80)
6. LLM fallback (Ollama llama3)
7. "Other" fallback — review queue (0.0)

**Two review workflows (never mix):**
- Pre-insert: `main.py --dry-run` → `import_corrections.py --save` (INSERT flow)
- Post-insert: `review.py export` → `review.py apply --save` (UPDATE + corrections upsert)

---

## Seed Data State (as of 2026-04-27)

- 6 statement files corrected; 398 transactions seeded; 61 unique merchants cached; 8 at high confidence (3+)
- **1 year bank statements ingested** (SBI, Canara, HDFC savings, BOB)
- **1 month CC statements ingested** (HDFC Moneyback, Amazon ICICI, Axis Supermoney)

---

## Key Design Choices to Remember

- `canonical_merchant` is the stable DB key — all matching goes through `description_cleaner.get_canonical_merchant()`
- Corrections DB always wins over contact match
- `Other` = review queue, NOT a real category. High `Other` % = corrections DB needs seeding
- Dates stored as DD/MM/YYYY — always filter in Python via `_parse_date()`, not SQLite string comparison
- Alias file bridges nickname ↔ legal name gap (`config/contact_aliases.yaml`) — grows incrementally
- CRED Club = CC bill payment. CRED Store = genuine spend. CRED Rent = Rent. Patterns must be specific.
- BBPS = payment rail, tag as Utilities & Bills by default
- BOB joint account = Savings & Investment (not family transfer)
- All 3 CCs pay from `acc_canara_daily`

---

## Known Gaps Carried Forward

- Canara: ~37% fallback/Other — mostly UPI person payments needing more corrections seeding
- CC spend + CC bill on savings currently double-counted (Block 5 deferred)
- No automated accuracy measurement (Block 6 deferred)
- Mom's bank name variant not yet in alias file — add when it appears
- Time + amount pattern rules not implemented — moved to Block 6B in Phase 2 as pattern-enriched LLM context (not hard rules). Build after eval harness confirms it's needed.
