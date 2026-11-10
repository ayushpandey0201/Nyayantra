"""Stage 3: understand the question before searching.

The helper model (gpt-oss-20b) gives the language, an English restatement for search, the as-of date
and the authorities the user cites. Everything it says is checked deterministically: the as-of date
must parse, domains must come from the fixed list, and every cited authority is looked up in the
database. An authority that does not resolve is reported as unverified and passed to the generator,
which must not describe it; this is what stops an invented case name from being "summarised".
"""
from __future__ import annotations

import datetime as dt
import re

from nyayantra import citations, llm, prompts

CASE_NAME = re.compile(r"\b([A-Z][\w.&'’-]*(?:\s+(?:[A-Z][\w.&'’-]*|of|and|&|the))*)\s+(?:v\.?|vs\.?|versus)\s+"
                       r"([A-Z][\w.&'’-]*(?:\s+(?:[A-Z][\w.&'’-]*|of|and|&|the))*)")
REPORTER = re.compile(r"\(\d{4}\)\s*\d+\s*SCC\s*(?:OnLine\s*\w+\s*)?\d+|\[\d{4}\]\s*\d+\s*S\.?C\.?R\.?\s*\d+|"
                      r"AIR\s*\d{4}\s*[A-Z][a-z]*\s*\d+|\d{4}\s*SCC\s*OnLine\s*\w+\s*\d+", re.I)


def _split_joined(name: str) -> list[str]:
    """The case-name pattern allows 'and' inside a party name, so it can swallow text before the real
    name ("Indian Contract Act and Percept D'Mark v. Zaheer Khan") or join two authorities ("Percept
    D'Mark and Mehra Infotech v. Union of India"), where a real case would hide an invented one.
    Try each 'and' as a boundary: drop leading text that is not a case when the tail is one; otherwise split off a
    corpus case named first when the rest names a multi-word party."""
    parts = re.split(r"\s+(?:v\.?|vs\.?|versus)\s+", name, maxsplit=1)
    if len(parts) != 2:
        return [name]
    cuts = list(re.finditer(r"\s+(?:and|&)\s+(?=[A-Z])", parts[0]))
    if not cuts:
        return [name]
    for m in reversed(cuts):                      # shortest tail first
        tail = name[m.end():]
        if citations.find(tail).cases:
            # head is a case too ("Kailash Nath and Associates v. DDA"): one name, all of it resolves
            return [name] if citations.find(parts[0][:m.start()]).cases else [tail]
    m = cuts[0]
    head, tail = parts[0][:m.start()], name[m.end():]
    if len(re.findall(r"\b[A-Z][\w.'’-]*", parts[0][m.end():])) >= 2 and citations.find(head).cases:
        return [head, tail]
    return [name]


def _authorities(question: str, from_llm: list[str]) -> list[dict]:
    """Every authority the question names, each checked against the corpus."""
    found: list[str] = []
    for m in CASE_NAME.finditer(question):
        found += _split_joined(m.group(0).rstrip(" .,;:"))
    found += [m.group(0) for m in REPORTER.finditer(question)]
    for a in from_llm:
        if isinstance(a, str) and a.strip() and not any(a.strip() in f or f in a for f in found):
            found.append(a.strip())
    out = []
    for a in dict.fromkeys(found):
        c = citations.find(a)
        if c.unit_ids or c.cases:
            out.append({"text": a, "resolved": True, "units": c.unit_ids, "cases": c.cases})
        elif c.unresolved:
            out.append({"text": a, "resolved": False, "reason": f"{', '.join(c.unresolved)} is not in the corpus"})
        elif CASE_NAME.search(a) or REPORTER.search(a):
            out.append({"text": a, "resolved": False, "reason": "no judgment in the corpus matches this case or citation"})
        elif c.acts:
            out.append({"text": a, "resolved": True, "units": [], "cases": [], "acts": c.acts})
        else:
            out.append({"text": a, "resolved": False, "reason": "not an instrument in the corpus"})
    return out


def understand(question: str, today: dt.date | None = None, sensitivity: str = "public") -> dict:
    today = today or dt.date.today()
    call, parsed, error = None, None, None
    try:
        call = llm.chat("helper", prompts.UNDERSTAND_SYSTEM, prompts.understand_user(question, str(today)),
                        stage="understand", sensitivity=sensitivity, max_tokens=500, prompt_version=prompts.UNDERSTAND_V)
        parsed = llm.parse_json(call["raw"])
    except llm.LLMError as e:
        error = str(e)
    p = parsed or {}
    checks = {"schema_valid": parsed is not None}

    # the script decides Hindi vs English; the model is only trusted for other languages
    lang = "hi" if re.search("[ऀ-ॿ]", question) else (
        "en" if sum(c.isascii() for c in question) >= 0.95 * len(question) else p.get("language", "other"))
    eq = p.get("english_query") if isinstance(p.get("english_query"), str) and p["english_query"].strip() else question
    as_of = None
    if p.get("as_of"):
        try:
            d = dt.date.fromisoformat(str(p["as_of"])[:10])
            if dt.date(1860, 1, 1) <= d <= today + dt.timedelta(days=3650):
                as_of = d if d != today else None
        except ValueError:
            checks["as_of_parses"] = False
    domains = [d for d in p.get("domains", []) if d in prompts.DOMAINS] if isinstance(p.get("domains"), list) else []
    intent = p.get("intent") if p.get("intent") in ("legal_research", "citation_lookup", "out_of_scope") else "legal_research"
    auths = _authorities(question, p.get("cited_authorities") if isinstance(p.get("cited_authorities"), list) else [])

    notes = []
    for a in auths:
        if not a["resolved"]:
            notes.append(f'The question mentions "{a["text"]}": {a["reason"]}. It may not exist. Do not describe it; say it could not be found.')
    if p.get("foreign_law"):
        notes.append("The question concerns foreign law, which is outside this corpus of Indian law.")
    return {
        "language": lang, "search_query": question if lang == "en" else eq, "english_query": eq,
        "as_of": str(as_of) if as_of else None, "as_of_reason": p.get("as_of_reason") if as_of else None,
        "intent": intent, "domains": domains, "foreign_law": bool(p.get("foreign_law")),
        "authorities": auths, "unverified": [a for a in auths if not a["resolved"]], "notes": notes,
        "checks": checks, "llm_error": error, "llm": [call] if call else [],
        "controls": ["Accuracy: as-of date for time-varying law", "Hallucination: cited authorities checked against the corpus"],
    }
