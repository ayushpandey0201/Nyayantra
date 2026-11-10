"""Measure Stage 4 on eval/retrieval_gold.yaml, configuration by configuration (an ablation).

  python -m eval.retrieval_eval            # all configurations
  python -m eval.retrieval_eval --only full --show-misses

Writes eval/results/retrieval.json (every question, every configuration) and retrieval.md (tables).
No LLM is involved, except in full+rewrite (gpt-oss-20b on Groq restates each question; replies cached).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
import time
from pathlib import Path

import psycopg
import yaml

from nyayantra.config import DSN
from nyayantra.pipeline import rewrite_query
from nyayantra.retrieve import Filters, Options, retrieve

HERE = Path(__file__).resolve().parent
K = 10

CONFIGS = {
    "dense":         Options(k=K, retrievers=("dense",), rerank=False, legal_status=False, pin_citations=False, diversity=False),
    "sparse":        Options(k=K, retrievers=("sparse",), rerank=False, legal_status=False, pin_citations=False, diversity=False),
    "fts":           Options(k=K, retrievers=("fts",), rerank=False, legal_status=False, pin_citations=False, diversity=False),
    "hybrid":        Options(k=K, retrievers=("dense", "sparse", "fts"), rerank=False, legal_status=False, pin_citations=False, diversity=False),
    "hybrid+legal":  Options(k=K, rerank=False),
    "dense+rerank":  Options(k=K, retrievers=("dense",), rerank=True, legal_status=False, pin_citations=False, diversity=False),
    "full":          Options(k=K),
    "full+rewrite":  Options(k=K),    # the question first restated in legal terms by gpt-oss-20b (pipeline.rewrite_query)
    "full+both":     Options(k=K),    # the question followed by its legal-terms restatement
}


def matches(unit_id: str, instrument_id: str, gold: str) -> bool:
    return instrument_id == gold[:-2] if gold.endswith(":*") else unit_id == gold


def score(hits: list[dict], groups: list[list[str]]) -> dict:
    first = []  # rank at which each group is first satisfied (None = never)
    for g in groups:
        r = next((h["rank"] for h in hits if any(matches(h["unit_id"], h["instrument_id"], x) for x in g)), None)
        first.append(r)
    hit = [r for r in first if r]
    out = {"first_ranks": first}
    for k in (1, 3, 5, 10):
        out[f"hit@{k}"] = int(any(r <= k for r in hit))
        out[f"recall@{k}"] = sum(1 for r in hit if r <= k) / len(groups)
    out["rr"] = 1 / min(hit) if hit else 0.0
    return out


def check_gold(items: list[dict]) -> None:
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("select unit_id from unit")
        units = {r[0] for r in cur.fetchall()}
        cur.execute("select instrument_id from instrument")
        insts = {r[0] for r in cur.fetchall()}
    bad = [(q["id"], g) for q in items for grp in q["gold"] for g in grp
           if (g.endswith(":*") and g[:-2] not in insts) or (not g.endswith(":*") and g not in units)]
    if bad:
        raise SystemExit(f"gold units not in the database: {bad}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--show-misses", action="store_true")
    args = ap.parse_args()
    items = yaml.safe_load((HERE / "retrieval_gold.yaml").read_text())
    check_gold(items)
    chosen = [n for n in CONFIGS if not args.only or n in args.only]
    retrieve("warm up the models", opts=Options(k=3, rerank=any(CONFIGS[n].rerank for n in chosen)))  # load weights before timing

    results = {}
    for name, opts in CONFIGS.items():
        if args.only and name not in args.only:
            continue
        rows = []
        for q in items:
            as_of = q.get("as_of")
            f = Filters(as_of=dt.date.fromisoformat(str(as_of)) if as_of else None)
            t0 = time.perf_counter()
            text = (rewrite_query(q["q"], "public")[0] if name.endswith("+rewrite") else
                    q["q"] + "\n" + rewrite_query(q["q"], "public")[0] if name.endswith("+both") else q["q"])
            r = retrieve(text, f, opts)
            ms = (time.perf_counter() - t0) * 1000
            s = score(r["hits"], q["gold"])
            rows.append({"id": q["id"], "kind": q["kind"], "ms": round(ms, 1), **s,
                         "top": [f'{h["unit_id"]}{"" if h["status"] in ("in_force", "unchecked") else " [" + h["status"] + "]"}'
                                 for h in r["hits"][:5]]})
            if args.show_misses and not s["hit@5"]:
                print(f"  MISS {name} {q['id']}: {q['q'][:70]}\n       gold {q['gold']}\n       top  {rows[-1]['top']}")
        results[name] = rows
        print(f"{name:14s} " + "  ".join(f"{m} {summ(rows, m):.3f}" for m in ("hit@1", "hit@5", "recall@5", "recall@10", "rr"))
              + f"  p50 {statistics.median(r['ms'] for r in rows):.0f} ms")

    out = HERE / "results"
    out.mkdir(exist_ok=True)
    (out / "retrieval.json").write_text(json.dumps({"run_at": dt.datetime.now().isoformat(timespec="seconds"),
                                                    "k": K, "n": len(items), "results": results}, indent=1))
    write_md(out / "retrieval.md", items, results)


def summ(rows: list[dict], m: str) -> float:
    return sum(r[m] for r in rows) / len(rows)


def write_md(path: Path, items: list[dict], results: dict) -> None:
    kinds = sorted({q["kind"] for q in items})
    lines = [f"# Stage 4 retrieval: {len(items)} gold questions", "",
             f"Run {dt.date.today()}. hit@k: a gold unit in the top k. recall@k: share of gold groups found. "
             "MRR: mean reciprocal rank of the first gold unit. All local; no LLM.", "",
             "| Configuration | hit@1 | hit@5 | recall@5 | recall@10 | MRR | p50 ms | p95 ms |", "|---|---|---|---|---|---|---|---|"]
    for name, rows in results.items():
        ms = sorted(r["ms"] for r in rows)
        lines.append(f"| {name} | {summ(rows, 'hit@1'):.2f} | {summ(rows, 'hit@5'):.2f} | {summ(rows, 'recall@5'):.2f} | "
                     f"{summ(rows, 'recall@10'):.2f} | {summ(rows, 'rr'):.2f} | {statistics.median(ms):.0f} | "
                     f"{ms[int(0.95 * (len(ms) - 1))]:.0f} |")
    lines += ["", "## recall@5 by question kind", "", "| Configuration | " + " | ".join(f"{k} (n={sum(q['kind'] == k for q in items)})" for k in kinds) + " |",
              "|---|" + "---|" * len(kinds)]
    for name, rows in results.items():
        lines.append(f"| {name} | " + " | ".join(f"{summ([r for r in rows if r['kind'] == k], 'recall@5'):.2f}" for k in kinds) + " |")
    last = list(results)[-1]
    misses = [r for r in results[last] if r["recall@5"] < 1]
    if misses:
        q = {x["id"]: x for x in items}
        lines += ["", f"## Not fully found in the top 5 ({last})", "", "| ID | Question | Gold | Top 5 |", "|---|---|---|---|"]
        for r in misses:
            lines.append(f"| {r['id']} | {q[r['id']]['q']} | {'; '.join(' or '.join(g) for g in q[r['id']]['gold'])} | {', '.join(r['top'])} |")
    path.write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
