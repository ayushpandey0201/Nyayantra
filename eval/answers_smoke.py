"""Live end-to-end run of the research pipeline on a small set: real questions, invented-citation traps
and out-of-corpus questions. Groq only (no fallback), paced for the free tier, every reply cached so a
re-run costs nothing.  python -m eval.answers_smoke
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time
from pathlib import Path

os.environ["NYAYANTRA_NO_FALLBACK"] = "1"

import yaml  # noqa: E402

from eval.retrieval_eval import matches  # noqa: E402
from nyayantra.pipeline import research  # noqa: E402

HERE = Path(__file__).resolve().parent
REF = HERE.parent / "docs" / "reference" / "legal_services_v1" / "poc" / "data" / "questions.json"
GOLD_IDS = ["Q02", "Q04", "Q16", "R01", "R03", "T01", "H01", "D01"]
PACE_S = 40   # gpt-oss-120b free tier: 8,000 tokens per minute


def cases() -> list[dict]:
    gold = {q["id"]: q for q in yaml.safe_load((HERE / "retrieval_gold.yaml").read_text())}
    out = [{"id": i, "kind": "gold", "q": gold[i]["q"], "as_of": gold[i].get("as_of"), "gold": gold[i]["gold"]} for i in GOLD_IDS]
    if REF.exists():
        for q in json.loads(REF.read_text()):
            if q["kind"] == "trap":
                out.append({"id": "X" + q["id"][1:], "kind": "trap", "q": q["question"]})
            elif q["id"] in ("Q20", "Q21"):
                out.append({"id": q["id"], "kind": "outside", "q": q["question"]})
    return out


def main() -> None:
    rows = []
    for n, c in enumerate(cases()):
        t0 = time.time()
        r = research(c["q"], as_of=dt.date.fromisoformat(str(c["as_of"])) if c.get("as_of") else None)
        cited = sorted({i for p in (r["answer"] or {}).get("points", []) for i in p.get("ids", [])})
        row = {"id": c["id"], "kind": c["kind"], "q": c["q"], "decision": r["decision"], "reason": r["reason"],
               "answer": (r["answer"] or {}).get("text"), "caveats": (r["answer"] or {}).get("caveats"), "cited": cited,
               "failed_checks": [s["output"]["failed"] for s in r["trace"] if s["stage"].startswith("guardrail")],
               "repaired": any(s["stage"] == "generate_repair" for s in r["trace"]),
               "usage": r["usage"], "latency_ms": r["latency_ms"]}
        if c.get("gold"):
            row["gold_cited"] = [any(matches(i, i.split(":")[0], g) for i in cited for g in grp) for grp in c["gold"]]
        rows.append(row)
        print(f"{c['id']:4s} {c['kind']:7s} {r['decision']:12s} repaired={row['repaired']!s:5s} "
              f"tokens={r['usage']['prompt_tokens'] + r['usage']['completion_tokens']:5d} cached={r['usage']['cached_calls']}/{r['usage']['llm_calls']} "
              f"{row.get('gold_cited', '')}", flush=True)
        if r["usage"]["cached_calls"] < r["usage"]["llm_calls"] and n < len(cases()) - 1:
            time.sleep(max(0, PACE_S - (time.time() - t0)))
    out = HERE / "results"
    out.mkdir(exist_ok=True)
    (out / "answers_smoke.json").write_text(json.dumps({"run_at": dt.datetime.now().isoformat(timespec="seconds"), "rows": rows},
                                                       indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
