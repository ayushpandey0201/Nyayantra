"""The research flow: redact -> screen -> understand -> retrieve -> generate -> guardrail.

Every stage is also callable on its own (the API exposes each as a route). This function chains them
and returns the answer plus a full trace: each stage's output, latency, the exact prompts sent, the raw
model replies and the compliance controls applied. The trace holds only redacted text.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
import time
import uuid

from nyayantra import llm
from nyayantra.generate import generate
from nyayantra.guardrail import guardrail
from nyayantra.redact import redact
from nyayantra.retrieve import Filters, Options, fetch, retrieve
from nyayantra.screen import screen
from nyayantra.understand import understand

DISCLAIMER = ("Research aid for advocates, generated from the cited sources. It is not legal advice "
              "(Advocates Act 1961, ss. 29 and 33); verify against the official text before relying on it.")
CONTEXT_K = 5
# "PAN [PAN_1]", "Aadhaar [AADHAAR_1]", "(+91 [PHONE_1])": the placeholders and their labels say nothing about the
# law, and words like "Aadhaar" pull in unrelated judgments, so they are removed from the search query only
PLACEHOLDER = re.compile(r"(?:\b(?:e-?mail|phone|mobile|tel|PAN|Aadhaar|a/c|account|IFSC|GSTIN|passport|voter id|card)"
                         r"(?:\s+(?:no\.?|number|id))?\s*[:#]?\s*)?(?:\+91\s*)?\[[A-Z_]+_\d+\]", re.I)


def search_text(q: str) -> str:
    q = PLACEHOLDER.sub("", q)
    q = re.sub(r"\(\s*(?:[,;]\s*)*\)", "", q)           # brackets left empty
    return re.sub(r"\s+([,.;?])", r"\1", re.sub(r"\s{2,}", " ", q)).strip()


def _stage(trace: list, name: str, t0: float, out: dict) -> dict:
    trace.append({"stage": name, "latency_ms": round((time.perf_counter() - t0) * 1000, 1), **out})
    return out


def _hit_view(h: dict) -> dict:
    keep = ("rank", "unit_id", "instrument_id", "kind", "title", "citation", "heading", "status", "status_note",
            "via", "replaced_by", "source", "opinion_author", "decided_on", "reporter_citations", "scores")
    return {k: h.get(k) for k in keep} | {"text": h["text"][:600] + (" ..." if len(h["text"]) > 600 else "")}


def research(question: str, as_of: dt.date | None = None, mode: str = "accurate", repair: bool = True,
             use_policy_model: bool = True, today: dt.date | None = None) -> dict:
    rid = "R-" + uuid.uuid4().hex[:12]
    today = today or dt.date.today()
    trace: list[dict] = []
    t_all = time.perf_counter()
    result = {"request_id": rid, "kind": "research", "input_sha256": hashlib.sha256(question.encode()).hexdigest(),
              "disclaimer": DISCLAIMER}

    # 1. redact
    t0 = time.perf_counter()
    red = redact(question)
    sensitivity = "confidential" if red.sensitive else "public"
    _stage(trace, "redact", t0, {"output": {"redacted_text": red.text, "found": red.found, "sensitivity": sensitivity},
                                 "controls": ["DPDP Act 2023 s.8(5): personal data removed before any LLM call",
                                              "BSA 2023 s.132: client confidentiality",
                                              "Routing: confidential requests stay on Groq (no free-tier fallback)" if red.sensitive else
                                              "Routing: public request; Gemini fallback permitted"]})
    q = red.text
    # kept in memory for the caller (the store re-redacts with it); never sent to a model or logged
    result["_private"] = {"mapping": red.mapping, "redacted_input": q, "sensitivity": sensitivity, "as_of": None}

    # 2. screen
    t0 = time.perf_counter()
    scr = screen(q, sensitivity)
    _stage(trace, "screen", t0, {"output": {k: scr[k] for k in ("flagged", "rules", "model_score", "threshold", "model_error")},
                                 "llm": scr["llm"], "controls": scr["controls"]})
    if scr["flagged"]:
        return _finish(result, trace, t_all, decision="blocked", answer=None,
                       reason="The question looks like an attempt to override the system's instructions, so it was not sent to the model.")

    # 3. understand
    t0 = time.perf_counter()
    und = understand(q, today, sensitivity)
    _stage(trace, "understand", t0, {"output": {k: und[k] for k in ("language", "search_query", "as_of", "as_of_reason", "intent",
                                                                    "domains", "foreign_law", "authorities", "notes", "checks", "llm_error")},
                                     "llm": und["llm"], "controls": und["controls"]})
    if und["intent"] == "out_of_scope":
        return _finish(result, trace, t_all, decision="out_of_scope", answer=None,
                       reason="This does not look like a question about Indian law.")
    eff_as_of = as_of or (dt.date.fromisoformat(und["as_of"]) if und["as_of"] else None)
    result["_private"]["as_of"] = str(eff_as_of) if eff_as_of else None

    # 4. retrieve
    t0 = time.perf_counter()
    ret = retrieve(search_text(und["search_query"]), Filters(as_of=eff_as_of), Options(k=CONTEXT_K, rerank=(mode == "accurate")))
    hits = ret["hits"]
    successors: dict[str, list[str]] = {}
    for h in hits:
        if (h.get("via") or {}).get("replaces"):
            successors.setdefault(h["via"]["replaces"], []).append(h["unit_id"])
    for h in hits:
        if h["status"] == "repealed":
            # successors in the context first; otherwise the mapping table, so a caveat never says "no successor" wrongly
            h["replaced_by"] = successors.get(h["unit_id"]) or h.get("replaced_by") or \
                next((f.get("replaced_by", []) for f in fetch([h["unit_id"]], eff_as_of)), [])
    _stage(trace, "retrieve", t0, {"output": {"hits": [_hit_view(h) for h in hits], "trace": ret["trace"]},
                                   "controls": ["Duty of competence: current law only, as of the stated date",
                                                "Repealed provisions replaced by their mapped successors (IPC -> BNS)",
                                                "Overruled judgments excluded"]})
    if not hits:
        return _finish(result, trace, t_all, decision="insufficient", answer=None, reason="No source in the corpus matched.")

    # 5. generate (+ 6. guardrail, one repair round if the failures are fixable)
    as_of_str = str(eff_as_of or today)
    gen = gr = None
    attempt_repair = None
    for attempt in range(2 if repair else 1):
        t0 = time.perf_counter()
        try:
            gen = generate(q, as_of_str, hits, und["notes"], sensitivity, repair=attempt_repair, language=und["language"])
        except llm.LLMUnavailable as e:
            _stage(trace, "generate", t0, {"error": str(e)})
            return _finish(result, trace, t_all, decision="review", answer=None, hits=hits,
                           reason="No permitted model was available" + (" (confidential request: Groq only)" if sensitivity != "public" else "")
                                  + "; the sources are shown and the question is queued for an advocate.")
        _stage(trace, "generate" if attempt == 0 else "generate_repair", t0,
               {"output": gen["parsed"], "llm": gen["llm"], "controls": gen["controls"]})
        t0 = time.perf_counter()
        gr = guardrail(gen["parsed"], hits, q, und, sensitivity, use_policy_model)
        _stage(trace, "guardrail" if attempt == 0 else "guardrail_repair", t0,
               {"output": {k: gr[k] for k in ("decision", "failed", "checks", "added_caveats")}, "llm": gr["llm"],
                "controls": gr["controls"]})
        if gr["decision"] != "withhold" or not gr["repairable"]:
            break
        problems = [f"{c['name']}: {c['detail']}" if c["detail"] else c["name"] for c in gr["checks"] if not c["ok"] and c["severity"] == "withhold"]
        attempt_repair = (gen["llm"][0]["raw"][:4000], problems)

    p = gen["parsed"] or {}
    decision = gr["decision"]
    if decision == "pass" and p.get("insufficient"):
        decision = "insufficient"
    answer = None
    if decision != "withhold":
        # text, points and caveats were normalised by the guardrail (ASCII brackets and hyphens)
        answer = {"text": red.restore(p.get("answer", "")), "points": p.get("points", []),
                  "caveats": list(p.get("caveats", [])) + gr["added_caveats"], "insufficient": p.get("insufficient", False)}
    if decision == "pass" and scr["model_error"]:
        # the rules passed but Prompt Guard could not be asked: show the answer, but an advocate checks it
        decision = "review"
        gr["failed"] = gr["failed"] + ["injection_classifier_unavailable"]
    reason = {"withhold": "The draft failed the citation checks, so it is not shown. The retrieved sources are listed; an advocate should review.",
              "review": "Shown with a warning and queued for advocate review: " + ", ".join(
                  "injection classifier unavailable" if f == "injection_classifier_unavailable" else f for f in gr["failed"]),
              "insufficient": "The sources do not answer this question."}.get(decision)
    return _finish(result, trace, t_all, decision=decision, answer=answer, hits=hits, reason=reason)


def _finish(result: dict, trace: list, t_all: float, decision: str, answer, reason: str | None = None,
            hits: list | None = None) -> dict:
    calls = [c for s in trace for c in s.get("llm", [])]
    result.update({
        "decision": decision, "review_required": decision in ("review", "withhold", "blocked"), "reason": reason,
        "answer": answer, "sources": [_hit_view(h) for h in (hits or [])],
        "usage": {"llm_calls": len(calls), "cached_calls": sum(c.get("cached", False) for c in calls),
                  "prompt_tokens": sum(c.get("prompt_tokens") or 0 for c in calls if not c.get("cached")),
                  "completion_tokens": sum(c.get("completion_tokens") or 0 for c in calls if not c.get("cached")),
                  "providers": sorted({f"{c['provider']}:{c['model']}" for c in calls}),
                  "fallback_used": any(c.get("fallback_from") for c in calls)},
        "latency_ms": round((time.perf_counter() - t_all) * 1000, 1), "trace": trace})
    return result
