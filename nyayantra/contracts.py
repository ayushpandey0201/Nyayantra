"""Contract review: split -> redact (incl. party names) -> screen -> classify -> retrieve the governing
law per clause -> analyse -> guardrail per clause.

A contract is a client document, so every call is "confidential": Groq only, never the Gemini free
tier, and if Groq is unavailable the contract goes to an advocate instead (DPDP Act s.8(5); BSA s.132).
Text that tries to instruct the model is still reviewed, as data, and the whole report is sent for
advocate review (OWASP LLM01).
"""
from __future__ import annotations

import hashlib
import re
import time
import uuid

from functools import lru_cache

import yaml

from nyayantra import llm, prompts
from nyayantra.config import ROOT
from nyayantra.guardrail import guardrail
from nyayantra.redact import redact
from nyayantra.retrieve import Filters, Options, fetch, retrieve
from nyayantra.screen import screen

CLAUSE_START = re.compile(r"^\s*(?:(?:clause|article|section)\s+)?(\d{1,2}(?:\.\d{1,2}){0,2})[.)]?\s+(?=\S)", re.I | re.M)
PARTY_DEF = re.compile(r"([A-Z][\w&.,'’ -]{2,120}?)\s*\((?:hereinafter[^)\"“]*|the\s+)?[\"“]([A-Z][\w ]{1,30})[\"”]\)")
BATCH = 4
LAW_PER_CLAUSE = 3


def split_clauses(text: str) -> list[dict]:
    starts = [m for m in CLAUSE_START.finditer(text)]
    if len(starts) >= 2:
        out = []
        for i, m in enumerate(starts):
            end = starts[i + 1].start() if i + 1 < len(starts) else len(text)
            body = text[m.end():end].strip()
            if body:
                out.append({"clause_id": m.group(1), "text": re.sub(r"\s+", " ", body)})
        return out
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    return [{"clause_id": str(i + 1), "text": re.sub(r"\s+", " ", p)} for i, p in enumerate(paras)]


def parties(text: str) -> list[str]:
    """Party names defined in the contract ('Acme Infotech Pvt Ltd (the "Company")'), to redact."""
    names = []
    for m in PARTY_DEF.finditer(text):
        name = re.sub(r"^.*?\b(?:between|and|with)\s+(?=[A-Z])", "", m.group(1).strip(" ,"))
        name = re.split(r",\s+(?:a|an|having|incorporated|registered|through|represented|residing|son|daughter|wife)\b", name)[0].strip(" ,")
        if len(name) > 3 and not name.lower().startswith(("the ", "this ")):
            names.append(name)
    return names


@lru_cache(maxsize=1)
def _clause_map() -> dict[str, list[str]]:
    return yaml.safe_load((ROOT / "ingest" / "clause_map.yaml").read_text())


def _clause_type_law(types: set[str]) -> dict[str, list[str]]:
    """Curated governing law per clause type, in the order written in ingest/clause_map.yaml."""
    return {t: (_clause_map().get(t) or [])[:LAW_PER_CLAUSE] for t in types}


def analyze(text: str, use_policy_model: bool = True) -> dict:
    rid = "C-" + uuid.uuid4().hex[:12]
    t_all = time.perf_counter()
    trace: list[dict] = []

    def stage(name, t0, out):
        trace.append({"stage": name, "latency_ms": round((time.perf_counter() - t0) * 1000, 1), **out})

    t0 = time.perf_counter()
    names = parties(text)
    red = redact(text, {"PARTY": names})
    clauses = split_clauses(red.text)
    stage("redact", t0, {"output": {"found": red.found, "clauses": len(clauses), "sensitivity": "confidential"},
                         "controls": ["DPDP Act 2023 s.8(5) and BSA 2023 s.132: personal data and party names removed",
                                      "Routing: confidential, Groq only; no free-tier fallback"]})

    t0 = time.perf_counter()
    scr = screen(red.text, "confidential")
    stage("screen", t0, {"output": {k: scr[k] for k in ("flagged", "rules", "model_score", "windows", "model_error")},
                         "llm": scr["llm"], "controls": scr["controls"]})

    # classify cheaply first, to know which governing provisions to fetch
    t0 = time.perf_counter()
    types_by_clause: dict[str, list[str]] = {}
    try:
        cls_sys = prompts.CLAUSES_SYSTEM.split("For each clause")[0] + (
            'For each clause in <clauses> return only its types. Return JSON only: {"clauses": [{"clause_id": string, "types": [string]}]}')
        call = llm.chat("helper", cls_sys, prompts.clauses_user(clauses, []), stage="classify", sensitivity="confidential",
                        max_tokens=1500, prompt_version=prompts.CLAUSES_V + "-classify")
        for c in (llm.parse_json(call["raw"]) or {}).get("clauses", []):
            if isinstance(c, dict):
                types_by_clause[str(c.get("clause_id"))] = [t for t in c.get("types", []) if t in prompts.CLAUSE_TYPES]
        stage("classify", t0, {"output": types_by_clause, "llm": [call]})
    except llm.LLMUnavailable as e:
        stage("classify", t0, {"error": str(e)})
        return _done(rid, text, red, trace, t_all, "review", [], f"Groq unavailable; a confidential contract is not sent elsewhere ({e}).")

    # governing law per clause: curated clause-type provisions + a search on the clause itself
    t0 = time.perf_counter()
    curated = _clause_type_law({t for ts in types_by_clause.values() for t in ts if t != "ok"})
    law_by_clause: dict[str, list[dict]] = {}
    all_ids: dict[str, dict] = {}
    for c in clauses:
        ts = [t for t in types_by_clause.get(c["clause_id"], []) if t != "ok"]
        if not ts:
            continue
        want = [u for t in ts for u in curated.get(t, [])]
        r = retrieve(c["text"], Filters(kinds=["act", "rules", "judgment"]), Options(k=LAW_PER_CLAUSE, rerank=False))
        got = {h["unit_id"]: h for h in fetch(list(dict.fromkeys(want)))}
        for h in r["hits"]:
            got.setdefault(h["unit_id"], h)
        hits = list(got.values())[:LAW_PER_CLAUSE + 1]
        law_by_clause[c["clause_id"]] = hits
        all_ids.update({h["unit_id"]: h for h in hits})
    stage("retrieve", t0, {"output": {k: [h["unit_id"] for h in v] for k, v in law_by_clause.items()},
                           "controls": ["Clause types mapped to governing provisions (ingest/clause_map.yaml)",
                                        "Current law only; repealed provisions replaced by successors"]})

    # analyse in batches; each batch sees only the law retrieved for its clauses
    t0 = time.perf_counter()
    results: dict[str, dict] = {}
    calls = []
    for i in range(0, len(clauses), BATCH):
        batch = clauses[i:i + BATCH]
        law = list({h["unit_id"]: h for c in batch for h in law_by_clause.get(c["clause_id"], [])}.values())
        try:
            call = llm.chat("main", prompts.CLAUSES_SYSTEM, prompts.clauses_user(batch, law), stage="analyse",
                            sensitivity="confidential", max_tokens=1600, prompt_version=prompts.CLAUSES_V)
        except llm.LLMUnavailable as e:
            stage("analyse", t0, {"error": str(e), "llm": calls})
            return _done(rid, text, red, trace, t_all, "review", [], f"Groq unavailable; a confidential contract is not sent elsewhere ({e}).")
        calls.append(call)
        for c in (llm.parse_json(call["raw"]) or {}).get("clauses", []):
            if isinstance(c, dict) and c.get("clause_id") is not None:
                results[str(c["clause_id"])] = c
    stage("analyse", t0, {"output": results, "llm": calls})

    # guardrail each finding against the law that clause was given
    t0 = time.perf_counter()
    report, decisions = [], []
    for c in clauses:
        f = results.get(c["clause_id"], {})
        types = [t for t in f.get("types", []) if t in prompts.CLAUSE_TYPES] or ["ok"]
        risk = f.get("risk") if f.get("risk") in ("high", "medium", "low", "none") else "none"
        item = {"clause_id": c["clause_id"], "text": red.restore(c["text"]), "types": types, "risk": risk,
                "issue": red.restore(str(f.get("issue", ""))), "ids": f.get("ids", []), "quote": f.get("quote", ""),
                "instruction_in_text": bool(f.get("instruction_in_text"))}
        if not f:
            item["check"] = {"decision": "withhold", "failed": ["missing_from_model_output"]}
        elif types != ["ok"] and risk != "none":
            draft = {"answer": str(f.get("issue", "")) or "-", "insufficient": not f.get("ids"),
                     "points": [{"statement": f.get("issue", ""), "ids": f.get("ids", []), "quote": f.get("quote", "")}] if f.get("ids") else [],
                     "caveats": []}
            g = guardrail(draft, law_by_clause.get(c["clause_id"], []), c["text"], {"unverified": []}, "confidential",
                          use_policy_model=False)
            item["check"] = {"decision": g["decision"], "failed": g["failed"], "added_caveats": g["added_caveats"]}
        else:
            item["check"] = {"decision": "pass", "failed": []}
        decisions.append(item["check"]["decision"])
        report.append(item)
    policy = None
    if use_policy_model:
        flagged_text = "\n".join(f"{r['clause_id']}: {r['issue']}" for r in report if r["risk"] != "none")
        if flagged_text:
            try:
                pc = llm.chat("safeguard", prompts.POLICY_SYSTEM, prompts.policy_user("Review of a contract", flagged_text),
                              stage="policy_guard", sensitivity="confidential", max_tokens=400, prompt_version=prompts.POLICY_V)
                policy = {"llm": [pc], "parsed": llm.parse_json(pc["raw"])}
            except llm.LLMError as e:
                policy = {"error": str(e)}
    stage("guardrail", t0, {"output": {"per_clause": {r["clause_id"]: r["check"] for r in report},
                                       "policy": (policy or {}).get("parsed")},
                            "llm": (policy or {}).get("llm", []),
                            "controls": ["Every finding's citations and quotes checked against the law it was given",
                                         "Advocates Act 1961 ss.29, 33: flags issues, does not advise signing"]})

    decision = "withhold" if "withhold" in decisions else "review"   # a contract report always goes to an advocate
    reason = "Contract reports are always reviewed by an advocate before release."
    if scr["flagged"] or any(r["instruction_in_text"] for r in report):
        reason += " The contract contains text that tries to instruct the AI reviewer."
    if (policy or {}).get("parsed", {}) and policy["parsed"].get("violation"):
        reason += " Policy guard: " + str(policy["parsed"].get("rationale", ""))
    return _done(rid, text, red, trace, t_all, decision, report, reason)


def _done(rid, text, red, trace, t_all, decision, report, reason) -> dict:
    calls = [c for s in trace for c in s.get("llm", [])]
    return {"request_id": rid, "kind": "contract", "input_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "decision": decision, "review_required": True, "reason": reason,
            "summary": {"clauses": len(report), "high": sum(r["risk"] == "high" for r in report),
                        "medium": sum(r["risk"] == "medium" for r in report),
                        "withheld_findings": sum(r.get("check", {}).get("decision") == "withhold" for r in report)},
            "clauses": report,
            "usage": {"llm_calls": len(calls), "cached_calls": sum(c.get("cached", False) for c in calls),
                      "prompt_tokens": sum(c.get("prompt_tokens") or 0 for c in calls if not c.get("cached")),
                      "completion_tokens": sum(c.get("completion_tokens") or 0 for c in calls if not c.get("cached")),
                      "providers": sorted({f"{c['provider']}:{c['model']}" for c in calls})},
            "latency_ms": round((time.perf_counter() - t_all) * 1000, 1), "trace": trace}
