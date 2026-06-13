"""
Block 6 — Evaluation Harness

Measures categorization accuracy against ground truth stored in the DB.
Ground truth = current DB categories (manually verified via import_corrections + review).

Usage:
  uv run python eval.py                          # all sources, no LLM, no corrections DB
  uv run python eval.py --month 2025-01          # single month
  uv run python eval.py --source acc_canara_daily
  uv run python eval.py --with-corrections       # include corrections DB in pipeline
  uv run python eval.py --llm                    # enable LLM (slow — ~10 min for full DB)
  uv run python eval.py --include-internal       # include Internal Transfer rows in eval
  uv run python eval.py --out results.csv        # dump per-row results to CSV

Metrics reported:
  - Overall accuracy %
  - Per-category: count, correct, accuracy %
  - Category_source breakdown (which pipeline step won, correct vs wrong)
  - Top misclassification pairs (confusion matrix excerpt)
  - "Other" analysis: false positives + false negatives
"""

import argparse
import copy
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from collections import Counter, defaultdict

import pandas as pd

from core.categorizer import categorize_transactions
from core.db import get_connection


def load_ground_truth(month=None, source=None, include_internal=False) -> list[dict]:
    conn = get_connection()
    where = ["1=1"]
    params = []

    if month:
        where.append("month = ?")
        params.append(month)
    if source:
        where.append("source_id = ?")
        params.append(source)
    if not include_internal:
        where.append("is_internal_transfer = 0")

    sql = f"""
        SELECT id, date, month, raw_description, description, canonical_merchant,
               amount, txn_type, source_id, source_label, category,
               is_internal_transfer, transaction_type, notes
        FROM transactions
        WHERE {' AND '.join(where)}
          AND category IS NOT NULL
          AND category != ''
        ORDER BY date
    """
    rows = conn.execute(sql, params).fetchall()
    cols = [d[0] for d in conn.execute(sql, params).description] if False else [
        "id", "date", "month", "raw_description", "description", "canonical_merchant",
        "amount", "txn_type", "source_id", "source_label", "category",
        "is_internal_transfer", "transaction_type", "notes",
    ]
    conn.close()
    return [dict(zip(cols, r)) for r in rows]


def run_pipeline(ground_truth: list[dict], use_llm: bool, use_corrections: bool) -> list[dict]:
    """Re-categorize rows from scratch. Returns list with ground_truth + predicted fields."""
    # Deep copy so we don't mutate ground truth
    txns = []
    for row in ground_truth:
        t = {
            "raw_description": row["raw_description"] or row["description"] or "",
            "canonical_merchant": row["canonical_merchant"] or "",
            "amount": row["amount"],
            "txn_type": row["txn_type"],
            "source_id": row["source_id"],
            "source_label": row["source_label"],
            "is_internal_transfer": row["is_internal_transfer"],
            "notes": "",
            "_gt_id": row["id"],
            "_gt_category": row["category"],
            "_gt_transaction_type": row["transaction_type"],
        }
        txns.append(t)

    print(f"  Re-running pipeline on {len(txns)} transactions "
          f"(LLM={'on' if use_llm else 'off'}, corrections={'on' if use_corrections else 'off'})...")

    categorize_transactions(txns, use_llm=use_llm, use_corrections=use_corrections)

    results = []
    for t in txns:
        results.append({
            "id": t["_gt_id"],
            "raw_description": t["raw_description"],
            "canonical_merchant": t["canonical_merchant"],
            "amount": t["amount"],
            "txn_type": t["txn_type"],
            "source_id": t["source_id"],
            "ground_truth": t["_gt_category"],
            "predicted": t.get("category", "Other"),
            "category_source": t.get("category_source", "fallback"),
            "confidence": t.get("confidence", 0.0),
            "correct": t["_gt_category"] == t.get("category", "Other"),
        })
    return results


def print_report(results: list[dict], use_corrections: bool, use_llm: bool):
    total = len(results)
    correct = sum(1 for r in results if r["correct"])
    accuracy = correct / total * 100 if total else 0

    print(f"\n{'='*68}")
    print(f"  EVAL RESULTS  —  {total} transactions")
    print(f"  Pipeline: corrections={'ON' if use_corrections else 'OFF'}, LLM={'ON' if use_llm else 'OFF'}")
    print(f"{'='*68}")
    print(f"\n  Overall accuracy: {correct}/{total} = {accuracy:.1f}%\n")

    # Per-category breakdown
    by_cat = defaultdict(lambda: {"total": 0, "correct": 0})
    for r in results:
        gt = r["ground_truth"]
        by_cat[gt]["total"] += 1
        if r["correct"]:
            by_cat[gt]["correct"] += 1

    print(f"  {'Category':<38} {'Total':>6} {'Correct':>8} {'Acc%':>6}")
    print(f"  {'-'*60}")
    for cat in sorted(by_cat, key=lambda c: -by_cat[c]["total"]):
        t = by_cat[cat]["total"]
        c = by_cat[cat]["correct"]
        pct = c / t * 100 if t else 0
        flag = " ⚠️" if pct < 70 and t >= 5 else ""
        print(f"  {cat:<38} {t:>6} {c:>8} {pct:>6.1f}%{flag}")

    # Category_source breakdown for correct vs incorrect
    print(f"\n  Category source breakdown:")
    src_correct = Counter(r["category_source"] for r in results if r["correct"])
    src_wrong   = Counter(r["category_source"] for r in results if not r["correct"])
    all_sources = set(src_correct) | set(src_wrong)
    print(f"  {'Source':<28} {'Correct':>8} {'Wrong':>6}")
    print(f"  {'-'*44}")
    for src in sorted(all_sources, key=lambda s: -(src_correct[s] + src_wrong[s])):
        print(f"  {src:<28} {src_correct[src]:>8} {src_wrong[src]:>6}")

    # Top misclassification pairs
    wrong = [r for r in results if not r["correct"]]
    if wrong:
        confusion = Counter((r["ground_truth"], r["predicted"]) for r in wrong)
        print(f"\n  Top misclassifications ({len(wrong)} errors):")
        print(f"  {'Ground Truth':<32} {'Predicted':<32} {'Count':>6}")
        print(f"  {'-'*72}")
        for (gt, pred), count in confusion.most_common(15):
            print(f"  {gt:<32} {pred:<32} {count:>6}")

    # Other analysis
    other_fp = [r for r in results if r["ground_truth"] != "Other" and r["predicted"] == "Other"]
    other_fn = [r for r in results if r["ground_truth"] == "Other" and r["predicted"] != "Other"]
    gt_other_total = sum(1 for r in results if r["ground_truth"] == "Other")

    print(f"\n  'Other' analysis:")
    print(f"    Ground truth 'Other' rows     : {gt_other_total}")
    print(f"    False positives (wrongly Other): {len(other_fp)}")
    print(f"    False negatives (missed Other) : {len(other_fn)}")

    if other_fp:
        fp_cats = Counter(r["ground_truth"] for r in other_fp)
        print(f"    Categories most often predicted as Other:")
        for cat, n in fp_cats.most_common(5):
            print(f"      {cat:<38} {n}")

    print(f"\n{'='*68}\n")


def main():
    parser = argparse.ArgumentParser(description="Finance categorization eval harness (Block 6)")
    parser.add_argument("--month", help="Filter by month YYYY-MM")
    parser.add_argument("--source", help="Filter by source_id")
    parser.add_argument("--llm", action="store_true", help="Enable LLM fallback (slow)")
    parser.add_argument("--with-corrections", action="store_true",
                        help="Include corrections DB in pipeline (default: OFF for honest eval)")
    parser.add_argument("--include-internal", action="store_true",
                        help="Include Internal Transfer rows (excluded by default)")
    parser.add_argument("--out", help="Write per-row results to CSV path")
    args = parser.parse_args()

    print(f"\nLoading ground truth from DB...")
    ground_truth = load_ground_truth(
        month=args.month,
        source=args.source,
        include_internal=args.include_internal,
    )

    if not ground_truth:
        print("No rows matched filters.")
        sys.exit(0)

    print(f"  Loaded {len(ground_truth)} rows as ground truth.")

    results = run_pipeline(ground_truth, use_llm=args.llm, use_corrections=args.with_corrections)
    print_report(results, use_corrections=args.with_corrections, use_llm=args.llm)

    if args.out:
        df = pd.DataFrame(results)
        df.to_csv(args.out, index=False)
        print(f"  Per-row results written to: {args.out}")


if __name__ == "__main__":
    main()
