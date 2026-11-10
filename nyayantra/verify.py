"""Citation check for an advocate's own draft (a brief, notice or opinion): every section, case name and
reporter citation in the text is looked up in the database and reported with its status on the as-of
date. Deterministic, no LLM, so nothing leaves the machine."""
from __future__ import annotations

import datetime as dt
import hashlib
import re
import time
import uuid

import psycopg

from nyayantra import citations
from nyayantra.config import DSN
from nyayantra.redact import redact
from nyayantra.retrieve import fetch
from nyayantra.understand import CASE_NAME, _authorities


def _cases(ids: list[str], as_of: dt.date) -> dict[str, dict]:
    if not ids:
        return {}
    with psycopg.connect(DSN) as conn:
        rows = conn.execute("""select instrument_id, short_title, reporter_citations, decided_on, treatment, overruled_by, overruled_on
                               from instrument where instrument_id = any(%s)""", (ids,)).fetchall()
    out = {}
    for iid, title, reps, decided, treatment, by, on in rows:
        overruled = bool(by) and (on is None or on <= as_of)
        out[iid] = {"instrument_id": iid, "title": title, "citation": (reps or [None])[0], "decided_on": str(decided) if decided else None,
                    "status": "overruled" if overruled else "good_law",
                    "status_note": f"overruled by {by}" + (f" on {on}" if on else "") if overruled else treatment}
    return out


def _loaded(instrument_ids: set[str]) -> dict[str, tuple[str, int]]:
    """instrument_id -> (title, number of units). An Act listed in sources.yaml may not be ingested yet."""
    if not instrument_ids:
        return {}
    with psycopg.connect(DSN) as conn:
        rows = conn.execute("""select i.instrument_id, i.short_title, count(u.unit_id) from instrument i
                               left join unit u using (instrument_id) where i.instrument_id = any(%s) group by 1, 2""",
                            (list(instrument_ids),)).fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


def check(text: str, as_of: dt.date | None = None) -> dict:
    t0 = time.perf_counter()
    d = as_of or dt.date.today()
    red = redact(text)
    c = citations.find(red.text)
    units = {h["unit_id"]: h for h in fetch(c.unit_ids, d)}
    cases = _cases(c.cases, d)
    loaded = _loaded({m["unresolved"].split(" ")[0] for m in c.mentions if m.get("unresolved")})
    # one party word can name two corpus cases ("Kailash"): keep the case whose title best matches the draft
    words = set(re.findall(r"[a-z]{3,}", red.text.lower()))
    by_text: dict[str, list[str]] = {}
    for m in c.mentions:
        if m.get("case"):
            by_text.setdefault(m["text"], []).append(m["case"])
    drop = set()
    for t, ids in by_text.items():
        if len(ids) > 1:
            score = {i: len(words & set(re.findall(r"[a-z]{3,}", cases.get(i, {}).get("title", "").lower()))) for i in ids}
            best = max(score.values())
            drop |= {(t, i) for i in ids if score[i] < best}
    items = []
    for m in c.mentions:
        if m.get("case") and (m["text"], m["case"]) in drop:
            continue
        if m.get("unit_id"):
            h = units.get(m["unit_id"])
            if h is None:
                items.append({"text": m["text"], "type": "provision", "found": True, "unit_id": m["unit_id"],
                              "status": "overruled", "ok": False, "problem": "excluded on this date"})
                continue
            ok = h["status"] == "in_force"
            items.append({"text": m["text"], "type": "provision", "found": True, "unit_id": h["unit_id"], "citation": h["citation"],
                          "heading": h["heading"], "status": h["status"], "status_note": h["status_note"],
                          "replaced_by": h.get("replaced_by", []), "ok": ok,
                          "problem": None if ok else ("repealed; cite " + ", ".join(h.get("replaced_by") or ["the successor provision"])
                                                      if h["status"] == "repealed" else h["status_note"] or h["status"])})
        elif m.get("case"):
            k = cases.get(m["case"], {})
            ok = k.get("status") == "good_law"
            shown = m["text"] if any(ch.isdigit() for ch in m["text"]) else k.get("title", m["text"])   # a reporter citation, else the case name
            items.append({**k, "text": shown, "type": "case", "found": True, "instrument_id": m["case"], "ok": ok,
                          "problem": None if ok else k.get("status_note")})
        elif m.get("unresolved"):
            iid = m["unresolved"].split(" ")[0]
            title, n = loaded.get(iid, (iid, 0))
            items.append({"text": m["text"], "type": "provision", "found": False, "ok": False,
                          "problem": f"{m['unresolved']} is not in the corpus; it may not exist" if n else
                                     f"{title} is not loaded in the corpus yet, so this could not be checked"})
    seen = " ".join(i["text"] for i in items)
    for a in _authorities(red.text, []):
        if not a["resolved"] and a["text"] not in seen:
            scc = bool(re.search(r"\bSCC\b", a["text"])) and not CASE_NAME.search(a["text"])
            items.append({"text": a["text"].rstrip(" .,;"), "type": "case", "found": False, "ok": False,
                          "problem": "no judgment in the corpus carries this SCC citation (the corpus records S.C.R. and neutral "
                                     "citations, so check the case name)" if scc else a["reason"]})
    seen_keys, unique = set(), []
    for i in items:
        key = (i["type"], i.get("unit_id") or i.get("instrument_id") or i["text"])
        if key not in seen_keys:
            seen_keys.add(key)
            unique.append(i)
    items = unique
    bad = [i for i in items if not i["ok"]]
    return {"request_id": "V-" + uuid.uuid4().hex[:12], "kind": "citation_check",
            "input_sha256": hashlib.sha256(text.encode()).hexdigest(), "as_of": str(d),
            "decision": "pass" if items and not bad else ("insufficient" if not items else "review"),
            "review_required": False,
            "reason": None if not bad else f"{len(bad)} of {len(items)} citations need attention",
            "summary": {"citations": len(items), "ok": len(items) - len(bad), "not_found": sum(not i["found"] for i in items),
                        "repealed_or_overruled": sum(i["found"] and not i["ok"] for i in items)},
            "items": [{**i, "text": red.restore(i["text"])} for i in items],
            "usage": {"llm_calls": 0}, "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            "trace": [{"stage": "citations", "output": {"mentions": c.mentions, "unresolved": c.unresolved},
                       "controls": ["Every citation looked up in the database; no model involved",
                                    "Status on the as-of date: repealed provisions point to successors, overruled cases flagged"]}],
            "_private": {"mapping": red.mapping, "redacted_input": red.text,
                         "sensitivity": "confidential" if red.sensitive else "public", "as_of": str(d)}}
