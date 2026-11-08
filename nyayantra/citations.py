"""Find the law a query names explicitly: 'section 27 of the Contract Act', 's.420 IPC', 'rule 7 of the
DPDP Rules', '(2017) 10 SCC 1', 'Percept D'Mark'. Deterministic, no LLM: every match is a lookup in the
database, so an invented section ('Section 27B') simply does not resolve.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import psycopg
import yaml

from nyayantra.config import DSN, ROOT

SECTION = re.compile(r"\b(?:sections?|secs?\.?|ss?\.|u/s\.?|s(?=\s?\d))\s*(\d{1,3}[A-Z]{0,2})((?:\(\w{1,4}\))*)", re.I)
RULE = re.compile(r"\b(?:rules?|r\.)\s*(\d{1,3}[A-Z]?)((?:\(\w{1,4}\))*)", re.I)
BARE = re.compile(r"\b(\d{1,3}[A-Z]{0,2})\b")          # "IPC 420", "420 IPC"
REPORTER = re.compile(r"\(\d{4}\)\s*\d+\s*SCC\s*\d+|\[\d{4}\]\s*\d+\s*S\.?C\.?R\.?\s*\d+|AIR\s*\d{4}\s*SC\s*\d+", re.I)

CASE_STOP = {"union", "india", "state", "states", "limited", "ltd", "company", "corporation", "corp", "government",
             "govt", "council", "board", "central", "commission", "others", "justice", "retd", "privacy", "aadhaar",
             "international", "industries", "engineering", "construction", "technologies", "technical", "service",
             "services", "trading", "association", "organisation", "national", "electricity", "regulatory",
             "high", "court", "punjab", "haryana", "bihar", "karnataka", "maharashtra", "kerala", "consumer",
             "education", "research", "centre", "medical", "india)", "sons", "spinning", "manufacturing",
             "architects", "aluminium", "between", "singh", "kumar", "kumari", "sharma", "gupta", "verma", "reddy",
             "mehta", "patel", "khanna", "agarwal", "jain", "rao", "iyer", "nair", "das", "ghosh", "bose", "indian", "contract", "section", "supreme", "india's", "agreements", "arbitration", "interplay", "stamp", "district"}


@dataclass
class Cited:
    unit_ids: list[str] = field(default_factory=list)        # resolved statutory units
    unresolved: list[str] = field(default_factory=list)      # named but not in the corpus (e.g. 'ICA s.27B')
    cases: list[str] = field(default_factory=list)           # judgment instrument_ids named by party or citation
    acts: list[str] = field(default_factory=list)            # instruments mentioned by name
    mentions: list[dict] = field(default_factory=list)       # what matched where, for the trace


@lru_cache(maxsize=1)
def _catalogue() -> dict:
    src = yaml.safe_load((ROOT / "ingest" / "sources.yaml").read_text())
    names = []  # (regex, instrument_id)
    for group in src.values():
        if not isinstance(group, list):
            continue
        for s in group:
            title = re.sub(r"^The\s+", "", s["title"]).split(" (")[0]
            forms = {title, re.sub(r",?\s*\d{4}$", "", title)}
            for a in s.get("aliases", []):
                forms.add(a)
            for f in forms:
                if f.isupper() and len(f) <= 5:          # IPC, BNS, DPDPA: case-sensitive, standalone
                    names.append((re.compile(rf"(?<![A-Za-z]){re.escape(f)}(?![A-Za-z])"), s["id"], f))
                elif len(f) >= 6:
                    pat = r"[\s,]+".join("(?:and|&)" if t == "and" else re.escape(t) for t in re.split(r"[\s,]+", f) if t).replace(r"\&", r"\s*(?:&|and)\s*")
                    names.append((re.compile(rf"\b{pat}\b", re.I), s["id"], f))
    names.sort(key=lambda t: -len(t[2]))
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("select unit_id from unit")
        units = {r[0] for r in cur.fetchall()}
        cur.execute("select instrument_id, kind::text from instrument")
        kinds = dict(cur.fetchall())
        cur.execute("select instrument_id, short_title from instrument where kind='judgment'")
        cases = cur.fetchall()
        cur.execute("select form, instrument_id from citation_index where unit_id is null")
        case_forms = dict(cur.fetchall())
    # distinctive party words: >= 5 letters, not generic, used by at most two case titles
    words: dict[str, set[str]] = {}
    for iid, title in cases:
        toks = set(re.findall(r"[a-z]{5,}", re.sub(r"[.'’]", "", title.lower()))) - CASE_STOP
        toks |= {t.lower() for t in iid.split("-")[2:] if len(t) >= 4 and t.lower() not in CASE_STOP}
        for t in toks:
            words.setdefault(t, set()).add(iid)
    words = {w: ids for w, ids in words.items() if len(ids) <= 2}
    return {"names": names, "units": units, "kinds": kinds, "case_words": words, "case_forms": case_forms}


def _norm(s: str) -> str:
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[^\w()]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def find(query: str) -> Cited:
    cat = _catalogue()
    out = Cited()
    acts = []  # (start, end, instrument_id)
    taken = []
    for rx, iid, form in cat["names"]:
        for m in rx.finditer(query):
            if any(a <= m.start() < b for a, b in taken):
                continue
            taken.append((m.start(), m.end()))
            acts.append((m.start(), m.end(), iid))
    acts.sort()
    out.acts = list(dict.fromkeys(a[2] for a in acts))

    def nearest_act(pos_start: int, pos_end: int) -> str | None:
        after = [a for a in acts if a[0] >= pos_end and a[0] - pos_end <= 40]
        before = [a for a in acts if a[1] <= pos_start and pos_start - a[1] <= 12]
        if after:
            return after[0][2]
        if before:
            return before[-1][2]
        return out.acts[0] if len(out.acts) == 1 else None

    seen_spans = []
    for rx, word in ((SECTION, "s"), (RULE, "r")):
        for m in rx.finditer(query):
            iid = nearest_act(m.start(), m.end())
            seen_spans.append((m.start(), m.end()))
            if not iid:
                continue
            _resolve(out, cat, iid, word, m.group(1), m.group(2), m.group(0))
    for a0, a1, iid in acts:  # "IPC 420", "420 IPC"
        for m in BARE.finditer(query):
            if any(s <= m.start() < e for s, e in seen_spans):
                continue
            if 0 <= m.start() - a1 <= 2 or 0 <= a0 - m.end() <= 2:
                if re.fullmatch(r"(19|20)\d\d", m.group(1)):
                    continue
                _resolve(out, cat, iid, "r" if cat["kinds"].get(iid) == "rules" else "s", m.group(1), "",
                         query[min(a0, m.start()):max(a1, m.end())])

    for m in REPORTER.finditer(query):
        iid = cat["case_forms"].get(re.sub(r"\s+", " ", re.sub(r"[^\w]+", " ", m.group().lower())).strip())
        if iid and iid not in out.cases:
            out.cases.append(iid)
            out.mentions.append({"text": m.group(), "case": iid})
    qwords = set(re.findall(r"[a-z]{5,}", re.sub(r"[.'’]", "", query.lower())))
    for w in sorted(qwords & cat["case_words"].keys()):
        if cat["case_words"][w] & set(out.cases):   # a reporter citation already picked the case
            continue
        for iid in sorted(cat["case_words"][w]):
            if iid not in out.cases:
                out.cases.append(iid)
                out.mentions.append({"text": w, "case": iid})
    return out


def _resolve(out: Cited, cat: dict, iid: str, word: str, num: str, sub: str, text: str) -> None:
    if cat["kinds"].get(iid) == "rules":
        word = "r"
    num = num.upper() if re.search(r"\d[a-z]{1,2}$", num) else num
    uid = f"{iid}:{word}{num}"
    if uid in cat["units"]:
        if uid not in out.unit_ids:
            out.unit_ids.append(uid)
        out.mentions.append({"text": text, "unit_id": uid, "sub": sub or None})
    else:
        label = f"{iid} {word}.{num}{sub}"
        if label not in out.unresolved:
            out.unresolved.append(label)
        out.mentions.append({"text": text, "unresolved": label})
