"""Phase 8 benchmark: the same questions answered by four configurations, scored the same way.

  C1   LLM only: gpt-oss-120b answers from its own knowledge and names its authorities
  C2   dense RAG: top 5 passages by dense vectors only, same generator prompt, no checks
  C3   hybrid search + reranker + legal status (in force / repealed -> successor), same generator, no checks
  C4   Nyayantra: redact, screen, understand, C3 retrieval, generate, guardrail with one repair round
  C4R  C4 with the question first restated in legal terms (pipeline.rewrite_query), an ablation

Every configuration uses openai/gpt-oss-120b on Groq for the answer, with no Gemini fallback, and the
date is fixed (BENCH_TODAY) so prompts are identical across days and every reply replays from the cache.

  python -m eval.benchmark run [--configs C1 C4 ...] [--limit N]    # resumable; stops at Groq's daily limit
  python -m eval.benchmark score                                    # deterministic metrics -> scores.jsonl
  python -m eval.benchmark judge                                    # Qwen (Groq) grades correctness (public questions only)
  python -m eval.benchmark report                                   # eval/results/bench/report.md

Results go to eval/results/bench/ (git-ignored). Questions: a stratified 35 from eval/retrieval_gold.yaml,
the 10 invented-citation traps and 2 out-of-corpus questions from the reference set.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

os.environ["NYAYANTRA_NO_FALLBACK"] = "1"

import yaml  # noqa: E402

from nyayantra import llm, prompts  # noqa: E402
from nyayantra.generate import generate  # noqa: E402
from nyayantra.pipeline import research  # noqa: E402
from nyayantra.retrieve import Filters, Options, retrieve  # noqa: E402

HERE = Path(__file__).resolve().parent
OUT = HERE / "results" / "bench"
REF = HERE.parent / "docs" / "reference" / "legal_services_v1" / "poc" / "data" / "questions.json"
BENCH_TODAY = dt.date(2026, 11, 10)
CONFIGS = ["C1", "C4", "C3", "C2", "C4R"]        # run order: the core comparison first
SELECT = {
    "statute": ["Q01", "Q05", "Q11", "Q14", "Q17", "C07", "D03", "L02"],
    "case": ["Q19", "A06", "D08", "R08", "L03"],
    "mixed": ["Q02", "Q04", "Q09", "C03", "A03", "D07", "E01", "P02"],
    "repealed": ["R01", "R03", "R04", "R06"],
    "client": ["M01", "M02", "M03", "M04"],
    "hindi": ["H01", "H02", "H03"],
    "asof": ["T01", "T02", "T03"],
}
C4R_KINDS = {"client", "mixed", "hindi", "repealed"}   # the ablation runs where a rewrite could matter

BASELINE_V = "baseline-v1"
BASELINE_SYSTEM = """You are a legal research assistant for Indian advocates. Answer the question about Indian law accurately and concisely, as of the date given.
Name the specific statutory provisions (Act and section) and Supreme Court judgments (case name and citation) you rely on. If you quote, quote exactly. If you are not sure an authority exists, say so rather than guess.
Return JSON only: {"answer": string, "authorities": [string], "quotes": [{"authority": string, "quote": string}], "insufficient": boolean}"""


def questions() -> list[dict]:
    gold = {q["id"]: q for q in yaml.safe_load((HERE / "retrieval_gold.yaml").read_text())}
    out = [{"id": i, "kind": k, "q": gold[i]["q"], "gold": gold[i]["gold"], "as_of": str(gold[i]["as_of"]) if gold[i].get("as_of") else None}
           for k, ids in SELECT.items() for i in ids]
    if REF.exists():
        for q in json.loads(REF.read_text()):
            if q["kind"] == "trap":
                out.append({"id": "X" + q["id"][1:], "kind": "trap", "q": q["question"], "gold": []})
            elif q["id"] in ("Q20", "Q21"):
                out.append({"id": "F" + q["id"][1:], "kind": "outside", "q": q["question"], "gold": []})
    return out


def _lang(q: str) -> str:
    return "hi" if re.search("[ऀ-ॿ]", q) else "en"


class RateLimited(RuntimeError):
    pass


def _rate_limited(text: str) -> bool:
    return bool(re.search(r"429|rate limit|tokens per (?:day|minute)|TPD|circuit open|too large", text or "", re.I))


def _check_errors(r: dict) -> None:
    """A run degraded by a rate limit is not a result: discard it and resume later."""
    errs = []
    for st in r.get("trace", []):
        errs += [str(st.get("error") or ""), str((st.get("output") or {}).get("llm_error") or ""),
                 str((st.get("output") or {}).get("model_error") or ""), str((st.get("output") or {}).get("rewrite_error") or "")]
        for c in (st.get("output") or {}).get("checks", []) or []:
            if isinstance(c, dict) and c.get("name") == "policy_guard" and "unavailable" in str(c.get("detail")):
                errs.append(str(c["detail"]))
    bad = [e for e in errs if _rate_limited(e)]
    if bad:
        raise RateLimited(bad[0][:300])


def _calls(trace: list[dict]) -> list[dict]:
    return [c for s in trace for c in s.get("llm", [])]


def _usage(calls: list[dict]) -> dict:
    live = [c for c in calls if not c.get("cached")]
    return {"llm_calls": len(calls), "cached": len(calls) - len(live),
            "tokens_main": sum((c.get("prompt_tokens") or 0) + (c.get("completion_tokens") or 0) for c in calls if c["role"] == "main"),
            "tokens_all": sum((c.get("prompt_tokens") or 0) + (c.get("completion_tokens") or 0) for c in calls),
            "live_tokens_main": sum((c.get("prompt_tokens") or 0) + (c.get("completion_tokens") or 0) for c in live if c["role"] == "main")}


def _hits_view(hits: list[dict]) -> list[dict]:
    return [{"unit_id": h["unit_id"], "instrument_id": h["instrument_id"], "status": h["status"]} for h in hits]


# ---------- configurations ----------
def run_c1(q: dict) -> dict:
    user = f"Law as of: {BENCH_TODAY}\nAnswer in: {prompts.LANG[_lang(q['q'])]}\n<question>\n{q['q']}\n</question>"
    try:
        call = llm.chat("main", BASELINE_SYSTEM, user, stage="baseline", max_tokens=1800, prompt_version=BASELINE_V)
    except llm.LLMUnavailable as e:
        raise RateLimited(str(e)) if _rate_limited(str(e)) else e
    p = llm.parse_json(call["raw"]) or {}
    return {"answer": p.get("answer"), "authorities": p.get("authorities", []), "quotes": p.get("quotes", []),
            "insufficient": bool(p.get("insufficient")), "parsed_ok": bool(p), "context": [], "calls": [call]}


def run_rag(q: dict, opts: Options) -> dict:
    t = retrieve(q["q"], Filters(), opts)
    hits = t["hits"]
    try:
        g = generate(q["q"], str(BENCH_TODAY), hits, [], "public", language=_lang(q["q"]))
    except llm.LLMUnavailable as e:
        raise RateLimited(str(e)) if _rate_limited(str(e)) else e
    p = g["parsed"] or {}
    return {"answer": p.get("answer"), "points": p.get("points", []), "caveats": p.get("caveats", []),
            "insufficient": bool(p.get("insufficient")), "parsed_ok": bool(g["parsed"]), "context": _hits_view(hits),
            "calls": g["llm"]}


C2_OPTS = Options(k=5, retrievers=("dense",), rerank=False, legal_status=False, pin_citations=False, diversity=False, link_statutes=False)
C3_OPTS = Options(k=5)


def run_c4(q: dict, rewrite: bool = False) -> dict:
    r = research(q["q"], today=BENCH_TODAY, rewrite=rewrite)
    r.pop("_private", None)
    _check_errors(r)
    a = r["answer"] or {}
    drafts = [s for s in r["trace"] if s["stage"].startswith("generate")]
    last = (drafts[-1].get("output") or {}) if drafts else {}
    ret = next((s for s in r["trace"] if s["stage"] == "retrieve"), {})
    return {"answer": a.get("text"), "points": a.get("points", []), "caveats": a.get("caveats", []),
            "insufficient": bool(a.get("insufficient")), "parsed_ok": True, "decision": r["decision"], "reason": r["reason"],
            "context": [{"unit_id": h["unit_id"], "instrument_id": h["instrument_id"], "status": h["status"]} for h in r["sources"]],
            "query": (ret.get("output") or {}).get("query"),
            "repaired": any(s["stage"] == "generate_repair" for s in r["trace"]),
            "failed_checks": [s["output"]["failed"] for s in r["trace"] if s["stage"].startswith("guardrail")],
            "draft": {"answer": last.get("answer"), "points": last.get("points", []), "caveats": last.get("caveats", []),
                      "insufficient": bool(last.get("insufficient"))} if last else None,
            "calls": _calls(r["trace"])}


RUN = {"C1": run_c1, "C2": lambda q: run_rag(q, C2_OPTS), "C3": lambda q: run_rag(q, C3_OPTS),
       "C4": run_c4, "C4R": lambda q: run_c4(q, rewrite=True)}


def _done(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {json.loads(line)["id"] for line in path.read_text().splitlines() if line.strip()}


def cmd_run(configs: list[str], limit: int | None) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    qs = questions()
    if set(configs) - {"C1"}:
        retrieve("warm up", opts=Options(k=3))
    for cfg in configs:
        path = OUT / f"{cfg}.jsonl"
        done = _done(path)
        todo = [q for q in qs if q["id"] not in done and (cfg != "C4R" or q["kind"] in C4R_KINDS)]
        print(f"{cfg}: {len(done)} done, {len(todo)} to run", flush=True)
        for q in todo[:limit]:
            t0 = time.perf_counter()
            for attempt in range(2):
                try:
                    res = RUN[cfg](q)
                    break
                except RateLimited as e:
                    if "per day" in str(e) or "TPD" in str(e) or attempt == 1:
                        print(f"\nstopped: Groq limit reached ({str(e)[:160]}).\nResume later with the same command; "
                              f"finished rows are kept.", flush=True)
                        sys.exit(2)
                    print(f"  {q['id']}: rate limited, waiting 65 s", flush=True)
                    time.sleep(65)
            calls = res.pop("calls")
            row = {"id": q["id"], "kind": q["kind"], "config": cfg, "q": q["q"], **res,
                   "usage": _usage(calls), "latency_ms": round((time.perf_counter() - t0) * 1000),
                   "models": sorted({f"{c['provider']}:{c['model']}" for c in calls}),
                   "prompt_versions": sorted({c["prompt_version"] for c in calls if c.get("prompt_version")}),
                   "run_at": dt.datetime.now().isoformat(timespec="seconds")}
            with path.open("a") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(f"  {cfg} {q['id']:4s} {q['kind']:8s} {row.get('decision', 'answered' if row['answer'] else 'none'):12s} "
                  f"main tokens {row['usage']['tokens_main']:5d} (live {row['usage']['live_tokens_main']:5d}) "
                  f"{row['latency_ms'] / 1000:.1f}s", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--configs", nargs="*", default=CONFIGS)
    r.add_argument("--limit", type=int)
    sub.add_parser("score")
    sub.add_parser("judge")
    sub.add_parser("report")
    a = ap.parse_args()
    if a.cmd == "run":
        cmd_run(a.configs, a.limit)
    else:
        from eval import bench_score
        {"score": bench_score.score_all, "judge": bench_score.judge_all, "report": bench_score.report}[a.cmd]()


if __name__ == "__main__":
    main()
