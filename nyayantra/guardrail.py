"""Stage 6: check the draft before anyone sees it. Fails closed.

Deterministic checks (database lookups and string matching), then a policy model:
  ids_in_context        every [id] the draft cites was among the passages it was given
  points_cited          every point names at least one passage
  answer_cited          a substantive answer carries at least one citation
  no_invented_law       sections, cases and law-report citations written in the text exist in the corpus
  no_outside_law        ... and were among the passages (law cited from memory goes to review)
  quotes_verbatim       each quote is found, character for character (after normalising space and
                        punctuation), in a passage the point cites
  unverified_refused    if the question named an authority that does not exist, the draft says so
  status_disclosed      a repealed or not-yet-in-force provision is described as such (else a caveat
                        is added by the system)
  no_pii_in_output      no personal data in the draft
  no_prompt_leak        the system prompt is not echoed
  policy_guard          gpt-oss-safeguard-20b: no legal advice to a person, no guaranteed outcomes,
                        no help with crime, no claim to be an advocate (Advocates Act ss.29, 33)

Decision: "withhold" (the draft is not shown; the sources are), "review" (shown with a banner and
queued for an advocate), or "pass".
"""
from __future__ import annotations

import difflib
import re
import unicodedata

from nyayantra import citations, llm, prompts
from nyayantra.redact import redact

ID_RX = re.compile(r"\[([A-Z][A-Z0-9-]+:[A-Za-z0-9()]+)\]")
LOOSE_ID_RX = re.compile(r"\b([A-Z]{2,6}(?:-[A-Z0-9]+)*-\d{4}(?:-[A-Z0-9-]+)?:(?:s|r|p|sch)\d+[A-Z]{0,3})\b")
REFUSAL_RX = re.compile(r"not (?:be )?found|could not (?:be )?(?:found|locate|find|verify)|cannot (?:be )?(?:found|locate|find|verify)|"
                        r"no (?:such|record|judgment|provision)|does not (?:appear|exist|contain)|not (?:in|part of) the (?:corpus|passages|supplied)|"
                        r"unable to (?:find|locate|verify)|not (?:available|present|included)|no information|"
                        r"नहीं (?:मिल|है)|उपलब्ध नहीं", re.I)
REPEAL_RX = re.compile(r"repeal|replaced|no longer in force|superseded|निरस्त", re.I)
FUTURE_RX = re.compile(r"not (?:yet )?in force|not yet|comes? into force|in force from|from \d{1,2} \w+ 20\d\d|20(?:2[6-9]|3\d)|लागू नहीं", re.I)
LEAK_MARKERS = ["You are Nyayantra", "Rules:\n1.", "Return JSON only", "VIOLATION (flag)"]


def clean(s: str) -> str:
    """Model output as the checks (and the reader) should see it: NFKC, ASCII brackets and hyphens.
    gpt-oss often writes citations as 【ICA-1872:s27】 with non-breaking hyphens."""
    s = unicodedata.normalize("NFKC", s).replace("【", "[").replace("】", "]")
    return re.sub("[\u2010-\u2015\u2212]", "-", s)


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).lower()
    s = s.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'").replace("—", "-").replace("–", "-")
    s = re.sub(r"\[\.\.\.\]|\.\.\.|…", " ", s)
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def quote_found(quote: str, text: str) -> float:
    """How exactly the quote appears in the text, 0..1, after normalising case, spacing and punctuation.

    The quote is aligned word by word against the best-matching window of the text. Every word must
    match, except a one-for-one substitution of a similar word (an OCR repair such as 'writlng' ->
    'writing'). Changed, added or dropped words count as errors: 'unless it is oral' does not pass for
    'unless it is in writing'. An elided quote ("A ... B") is scored by its weakest fragment."""
    parts = [p for p in re.split(r"\.\.\.|…|\[\.\.\.\]", quote) if len(_norm(p).split()) >= 2]
    if len(parts) > 1:   # joining words at the cut ("... and shall specify") are not part of the source
        edge = {"and", "or", "but", "the", "that", "which", "a", "an", "of", "to"}
        frags = []
        for p in parts:
            w = _norm(p).split()
            while w and w[0] in edge:
                w = w[1:]
            while w and w[-1] in edge:
                w = w[:-1]
            if len(w) >= 2:
                frags.append(" ".join(w))
        return min(quote_found(f, text) for f in frags) if frags else 0.0
    q, t = _norm(quote).split(), _norm(text).split()
    if not q or not t:
        return 0.0
    sm = difflib.SequenceMatcher(None, t, q, autojunk=False)
    m = sm.find_longest_match(0, len(t), 0, len(q))
    if m.size == 0:
        return 0.0
    lo = max(0, m.a - m.b - 3)
    window = t[lo:min(len(t), m.a + (len(q) - m.b) + 3)]
    errors = 0
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, q, window, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        if op == "insert":                       # window words the quote skipped
            if 0 < i1 < len(q):
                errors += j2 - j1
            continue
        if op == "replace" and i2 - i1 == j2 - j1 and all(
                difflib.SequenceMatcher(None, a, b).ratio() >= 0.75 for a, b in zip(q[i1:i2], window[j1:j2])):
            continue                             # OCR-style repair, word for word
        errors += max(i2 - i1, j2 - j1)
    return round(max(0.0, 1 - errors / len(q)), 3)


def _check(checks: list, name: str, ok: bool, severity: str, detail=None) -> None:
    checks.append({"name": name, "ok": bool(ok), "severity": severity, "detail": detail})


def guardrail(parsed: dict | None, hits: list[dict], question: str, understood: dict,
              sensitivity: str = "public", use_policy_model: bool = True) -> dict:
    checks: list[dict] = []
    by_id = {h["unit_id"]: h for h in hits}
    ctx_instruments = {h["instrument_id"] for h in hits}
    calls: list[dict] = []
    added_caveats: list[str] = []

    schema_ok = (isinstance(parsed, dict) and isinstance(parsed.get("answer"), str) and parsed["answer"].strip()
                 and isinstance(parsed.get("points"), list))
    _check(checks, "schema_valid", schema_ok, "withhold")
    if not schema_ok:
        return _decide(checks, calls, added_caveats)

    parsed["answer"] = clean(parsed["answer"])
    parsed["caveats"] = [clean(str(c)) for c in parsed.get("caveats", [])]
    for pt in parsed["points"]:
        if isinstance(pt, dict):
            for k in ("statement", "quote"):
                if isinstance(pt.get(k), str):
                    pt[k] = clean(pt[k])
            pt["ids"] = [clean(i).strip("[] ") for i in pt.get("ids", []) if isinstance(i, str)]
    answer = parsed["answer"]
    insufficient = parsed.get("insufficient", False)
    points = [p for p in parsed["points"] if isinstance(p, dict)]
    body = answer + " " + " ".join(str(p.get("statement", "")) for p in points) + " " + " ".join(map(str, parsed.get("caveats", [])))
    cited = set(ID_RX.findall(body)) | set(LOOSE_ID_RX.findall(body))
    for p in points:
        cited |= {i for i in p.get("ids", []) if isinstance(i, str)}
    outside = sorted(cited - by_id.keys())
    _check(checks, "ids_in_context", not outside, "withhold", outside or None)

    uncited = [] if insufficient else [p.get("statement", "")[:80] for p in points if not [i for i in p.get("ids", []) if i in by_id]]
    _check(checks, "points_cited", not uncited, "withhold", uncited or None)
    # inline [id] citations are preferred; a draft whose every point is cited is accepted without them
    inline = bool(ID_RX.search(answer))
    _check(checks, "answer_cited", insufficient or inline or (points and not uncited), "withhold")
    _check(checks, "inline_citations", insufficient or inline, "info")

    # law written out in words: "section 27B of the Contract Act", "(2022) 8 SCC 614", "X v. Y"
    plain = ID_RX.sub(" ", body)
    c = citations.find(plain)
    from nyayantra.understand import CASE_NAME, REPORTER
    invented = list(c.unresolved)
    for m in REPORTER.finditer(plain):
        if not citations.find(m.group()).cases:
            invented.append(m.group())
    unverified_texts = {a["text"] for a in understood.get("unverified", [])}
    # the user's own unverified authorities, in every form the checks below produce, may be named to refuse them
    refused_forms, refused_cases = set(unverified_texts), set()
    for u in unverified_texts:
        cu = citations.find(u)
        refused_forms |= set(cu.unresolved) | {m.group() for m in REPORTER.finditer(u)}
        refused_cases |= set(cu.cases)
    for m in CASE_NAME.finditer(plain):
        if not citations.find(m.group()).cases and not any(m.group() in u or u in m.group() for u in unverified_texts):
            invented.append(m.group())
    # naming the user's invented authority in order to refuse it is fine
    invented = [x for x in invented if x not in refused_forms and not any(x in u or u in x for u in unverified_texts)]
    _check(checks, "no_invented_law", not invented, "withhold", invented or None)
    outside_law = [u for u in c.unit_ids if u not in by_id] + [k for k in c.cases if k not in ctx_instruments and k not in refused_cases]
    _check(checks, "no_outside_law", not outside_law, "review", outside_law or None)

    bad_quotes = []
    for p in points:
        q = str(p.get("quote", "")).strip()
        ids = [i for i in p.get("ids", []) if i in by_id]
        if not q or not ids:
            if not insufficient:
                bad_quotes.append({"statement": str(p.get("statement", ""))[:80], "score": 0.0, "reason": "no quote"})
            continue
        score = max(quote_found(q, (by_id[i].get("heading") or "") + "\n" + by_id[i]["text"]) for i in ids)
        if score < 0.95:
            bad_quotes.append({"quote": q[:120], "ids": ids, "score": score})
    _check(checks, "quotes_verbatim", not bad_quotes, "withhold", bad_quotes or None)

    if understood.get("unverified"):
        _check(checks, "unverified_refused", bool(REFUSAL_RX.search(answer)) or insufficient, "withhold",
               [a["text"] for a in understood["unverified"]])

    text_all = answer + " " + " ".join(map(str, parsed.get("caveats", [])))
    missing = []
    for i in sorted(cited & by_id.keys()):
        h = by_id[i]
        if h["status"] == "repealed" and not REPEAL_RX.search(text_all):
            missing.append(i)
            succ = ", ".join(h.get("replaced_by") or []) or "no successor provision in the corpus"
            added_caveats.append(f"{h['citation']} was {h['status_note']}; replaced by: {succ}.")
        elif h["status"] == "not_yet_in_force" and not FUTURE_RX.search(text_all):
            missing.append(i)
            added_caveats.append(f"{h['citation']} is enacted but not yet in force ({h['status_note']}).")
    _check(checks, "status_disclosed", not missing, "info", missing or None)

    _check(checks, "no_pii_in_output", not redact(answer).found, "withhold")
    _check(checks, "no_prompt_leak", not any(m in answer for m in LEAK_MARKERS), "withhold")

    if use_policy_model:
        try:
            call = llm.chat("safeguard", prompts.POLICY_SYSTEM, prompts.policy_user(question, answer), stage="policy_guard",
                            sensitivity=sensitivity, max_tokens=400, reasoning="low", prompt_version=prompts.POLICY_V)
            calls.append(call)
            v = llm.parse_json(call["raw"])
            if v is None:
                _check(checks, "policy_guard", False, "review", "policy model output unreadable")
            else:
                _check(checks, "policy_guard", not v.get("violation"), "review",
                       {"categories": v.get("categories"), "rationale": v.get("rationale")})
        except llm.LLMError as e:
            _check(checks, "policy_guard", False, "review", f"policy model unavailable: {e}")
    return _decide(checks, calls, added_caveats)


def _decide(checks: list[dict], calls: list[dict], added: list[str]) -> dict:
    failed = [c for c in checks if not c["ok"]]
    if any(c["severity"] == "withhold" for c in failed):
        decision = "withhold"
    elif any(c["severity"] == "review" for c in failed):
        decision = "review"
    else:
        decision = "pass"
    repairable = {"ids_in_context", "points_cited", "answer_cited", "quotes_verbatim", "no_invented_law",
                  "unverified_refused", "schema_valid"}
    return {"decision": decision, "checks": checks, "failed": [c["name"] for c in failed],
            "repairable": decision == "withhold" and all(c["name"] in repairable for c in failed if c["severity"] == "withhold"),
            "added_caveats": added, "llm": calls,
            "controls": ["Hallucination: citations resolved against the database, quotes matched to source text",
                         "OWASP LLM05 improper output handling / LLM09 misinformation: fail-closed output checks",
                         "Advocates Act 1961 ss.29, 33: policy guard against legal advice"]}
