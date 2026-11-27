"""Measure knowledge-base retrieval on the query set in eval/retrieval_queries.yaml.

Usage (knowledge base ingested, see README):
    .venv/bin/python scripts/check_retrieval.py                  # auto: hybrid if embeddings exist
    .venv/bin/python scripts/check_retrieval.py --mode keyword   # keyword search only
    .venv/bin/python scripts/check_retrieval.py --mode hybrid    # requires GOOGLE_API_KEY + embeddings
    .venv/bin/python scripts/check_retrieval.py -v               # show the top 3 for every query

Reports hit@1, hit@3 and MRR (mean reciprocal rank of the first correct document),
overall and split into keyword-style and paraphrase queries, and saves the full
result to runs/<timestamp>-retrieval-<mode>.json.

The Week-3 "done" check from the project guide is the first query: "checkout errors"
must return the checkout runbook in the top 3.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from copilot.kb.search import MODES, SearchError, search  # noqa: E402
from copilot.kb.store import StoreError  # noqa: E402

QUERIES = ROOT / "eval" / "retrieval_queries.yaml"


def evaluate(mode: str, k: int = 3, queries_file: Path = QUERIES) -> dict:
    spec = yaml.safe_load(queries_file.read_text())
    rows, retrievals = [], set()
    for kind, items in spec.items():
        for item in items:
            result = search(kind, item["query"], k=max(k, 5), mode=mode)
            retrievals.add(result.get("retrieval"))
            ranked = [Path(r["source"]).stem for r in result["results"]]
            rank = next((i for i, doc in enumerate(ranked, start=1) if doc in item["expect"]), None)
            rows.append({"kind": kind, "query": item["query"], "style": item.get("style", "keyword"),
                         "expect": item["expect"], "rank": rank, "top": ranked[:k],
                         "warnings": result.get("warnings", [])})
    return {"mode": mode, "retrieval": sorted(r for r in retrievals if r), "k": k, "rows": rows,
            "summary": {group: _metrics([r for r in rows if group == "all" or r["style"] == group], k)
                        for group in ("all", "keyword", "paraphrase")}}


def _metrics(rows: list[dict], k: int) -> dict:
    n = len(rows)
    if n == 0:
        return {"queries": 0}
    return {"queries": n,
            "hit@1": round(sum(r["rank"] == 1 for r in rows) / n, 3),
            f"hit@{k}": round(sum(r["rank"] is not None and r["rank"] <= k for r in rows) / n, 3),
            "mrr": round(sum(1 / r["rank"] for r in rows if r["rank"]) / n, 3)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=MODES, default="auto")
    parser.add_argument("--k", type=int, default=3, help="cut-off for hit@k (default 3)")
    parser.add_argument("-v", "--verbose", action="store_true", help="print the top results for every query")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")

    try:
        report = evaluate(args.mode, args.k)
    except (StoreError, SearchError) as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return 2

    k = args.k
    for row in report["rows"]:
        ok = row["rank"] is not None and row["rank"] <= k
        if args.verbose or not ok:
            mark = "ok  " if ok else "MISS"
            print(f"{mark} [{row['kind'][:4]}/{row['style'][:5]}] rank={row['rank'] or '-':<2} {row['query']!r}")
            print(f"       expected {row['expect']}, got {row['top']}")
    print(f"\nmode={report['mode']} retrieval={','.join(report['retrieval'])}")
    for group, m in report["summary"].items():
        if m["queries"]:
            print(f"  {group:<10} n={m['queries']:<3} hit@1={m['hit@1']:.2f}  hit@{k}={m[f'hit@{k}']:.2f}  "
                  f"MRR={m['mrr']:.2f}")
    first = report["rows"][0]
    print(f"\nWeek-3 check: {first['query']!r} -> checkout runbook "
          f"{'in the top 3: PASS' if first['rank'] and first['rank'] <= 3 else 'NOT in the top 3: FAIL'}")

    runs = ROOT / "runs"
    runs.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = runs / f"{stamp}-retrieval-{args.mode}.json"
    path.write_text(json.dumps(report, indent=2))
    print(f"saved {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
