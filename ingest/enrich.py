"""Derive the finer legal metadata from the loaded text. Run after load_db (and before embed).

Every field is derived from the official text or from a small curated YAML file, and every derived
field records how it was derived in instrument.metadata_notes. Nothing is guessed silently: a
reference that cannot be resolved to a unit in the corpus is kept in unit.external_refs.

Acts and Rules
  * long_title           "An Act to ..." (or the Preamble) read from the PDF
  * as_amended_upto      latest amendment date in the footnotes; else the enactment date (text as enacted)
  * amended_by           de-duplicated and parsed: action (Subs./Ins./Omitted), amending Act, date; "ibid." resolved
  * valid_from           commencement, inserted-section date, or the phase in commencement.yaml
  * text_valid_from      the current wording is in force since (latest amendment of that unit)
  * status               partly_in_force when some provisions are not yet in force
  * cross_refs           "section 7", "sub-section (2) of section 34", "section 2 of the Act" -> unit_ids
  * external_refs        references to law outside the corpus ("section 9 of the Code of Civil Procedure, 1908")
  * defined_terms        '"data fiduciary" means ...' -> unit.defined_terms and the defined_term table
  * topics               heading / chapter rules in topics.yaml
  * clause_types         curated clause_map.yaml
  * path                 roman numerals restored ("Chapter Ii" -> "Chapter II")
Judgments
  * valid_from = decided_on; valid_to = date overruled (only when wholly overruled)
  * statutes_cited per paragraph (sections named in that paragraph), case-level list on the instrument
  * cites_judgments: other corpus judgments cited, by S.C.R./SCC citation or case name; cited_by_judgments
  * aliases: the paragraph's citation forms

Usage: python -m ingest.enrich
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import re
from pathlib import Path

import psycopg
import pymupdf
import yaml
from psycopg.types.json import Jsonb

from ingest.load_db import DOMAIN_OF, DSN

ROOT = Path(__file__).resolve().parents[1]
ACTS_DIR = ROOT / "data" / "raw" / "acts"
TODAY = dt.date.today()

QUOTE = "\"'“”‘’"
NUM = r"\d{1,3}[A-Z]{0,3}"
SUB = r"(?:\s*\(\s*[0-9A-Za-z]{1,5}\s*\))*"


# ---------------------------------------------------------------- helpers
def load_yaml(name: str):
    return yaml.safe_load((ROOT / "ingest" / name).read_text())


def roman_fix(path: str | None) -> str | None:
    if not path:
        return path
    return re.sub(r"\b(Chapter|Part)\s+([IVXLC]+)([A-Z]?)\b",
                  lambda m: f"{m.group(1)} {m.group(2).upper()}{m.group(3).upper()}", path, flags=re.I)


def title_index(cur) -> dict[str, list[tuple[str, int | None]]]:
    """normalised Act name -> [(instrument_id, year)]; includes repealed and not-yet-loaded Acts from sources.yaml."""
    src = load_yaml("sources.yaml")
    idx = collections.defaultdict(list)
    for e in src["acts"] + src["rules"]:
        year = re.search(r"\d{4}", e["title"])
        year = int(year.group()) if year else None
        base = re.sub(r"^the\s+", "", e["title"].lower())
        for name in {base, re.sub(r",?\s*\d{4}$", "", base), *(a.lower() for a in e.get("aliases", []))}:
            idx[name.strip()].append((e["id"], year))
    return idx


def resolve_act(name: str, year: str | None, idx, before: dt.date | None = None) -> str | None:
    n = re.sub(r"^the\s+", "", name.lower()).strip(" ,")
    n = re.sub(r",?\s*\d{4}$", "", n).strip()
    for cand in (n, n.replace("indian ", "")):
        for iid, y in idx.get(cand, []):
            if year and y and int(year) != y:
                continue
            # an unnamed-year reference in a judgment older than the Act means an earlier Act of that name
            if not year and before and y and before.year < y:
                continue
            return iid
    return None


def expand_numbers(spec: str) -> list[str]:
    """'10, 11 and 12' / '10 to 12' / '29A' -> ['10', '11', '12']."""
    out = []
    for part in re.split(r"\s*(?:,|\band\b|\bor\b)\s*", spec):
        m = re.match(rf"({NUM})\s*(?:to|-|–)\s*({NUM})$", part.strip())
        if m and m.group(1).isdigit() and m.group(2).isdigit() and int(m.group(2)) - int(m.group(1)) < 40:
            out += [str(i) for i in range(int(m.group(1)), int(m.group(2)) + 1)]
        else:
            m = re.match(rf"({NUM})", part.strip())
            if m:
                out.append(m.group(1))
    return out


REF = re.compile(
    rf"\b(?:(?i:sub-sections?|clauses?|provisos?\s+to)\s*(?:\([^)]{{1,6}}\)\s*(?:,|and|or)?\s*)+(?:of\s+)?)?"
    rf"(?P<kind>(?i:sections?|ss?\.|secs?\.|rules?|r\.))\s*(?P<nums>{NUM}{SUB}(?:\s*(?:,|and|or|to)\s*{NUM}{SUB})*)"
    rf"(?P<tail>\s+of\s+(?:the\s+)?(?P<act>(?:this|the|said)\s+(?:Act|Code|Rules|Sanhita|Adhiniyam)\b|"
    rf"[A-Z][A-Za-z&()'\-\s]{{2,80}}?(?:Act|Code|Rules|Regulations|Sanhita|Adhiniyam|Procedure))(?:,?\s*(?:\(\s*\d+\s+of\s+)?(?P<year>\d{{4}}))?)?",
)


def extract_refs(text: str, here: str, parent: str | None, units: set[str], idx, kind: str,
                 default_act: str | None = None, before: dt.date | None = None, case_statutes=()):
    """Returns (resolved unit_ids, external references as text)."""
    refs, ext = [], []
    matches = list(REF.finditer(text))
    for i, m in enumerate(matches):
        word = m.group("kind").lower()
        is_rule = word.startswith("r")
        nums = expand_numbers(re.sub(r"\([^)]*\)", "", m.group("nums")))
        act, year = m.group("act"), m.group("year")
        # "section 14A and section 16 of the TRAI Act": a bare reference shares the next one's Act
        j = i + 1
        while not act and j < len(matches) and re.fullmatch(r"[\s,;]*(?:and|or)?[\s,]*(?:clause \(\w+\) of )?(?:sub-rule \(\w+\) of )?",
                                                            text[matches[j - 1].end():matches[j].start()]):
            if matches[j].group("act"):
                act, year = matches[j].group("act"), matches[j].group("year")
            j += 1
        target = None
        if act:
            a = act.lower()
            if re.match(r"(this|said)\s", a):
                target = here
            elif a in ("the act",) and parent:
                target = parent  # Rules: "section 2 of the Act" = the parent Act
            elif a in ("the act", "the code", "the rules", "the sanhita", "the adhiniyam"):
                target = here if kind != "judgment" else default_act
            else:
                target = resolve_act(act, year, idx, before)
                if not target:
                    act_name = re.sub(r"\s+", " ", act).strip()
                    for n in nums:
                        ext.append(f"{act_name}{', ' + year if year else ''} {'r' if is_rule else 's'}.{n}")
                    continue
        else:
            target = here if kind != "judgment" else default_act
            if kind == "judgment" and not target:
                # bare "Section 65B" in a case about several Acts: the Act whose section the case is about
                hits = {s.split(":")[0] for s in case_statutes for n in nums if s.endswith(f":s{n}")}
                target = hits.pop() if len(hits) == 1 else None
            if kind == "rules" and not is_rule and parent:
                target = parent  # a bare "section" in Rules points at the parent Act
        if not target:
            continue
        prefix = "r" if (is_rule and target.startswith(("DPDPR", "BCIR"))) else "s"
        loaded = {u.split(":")[0] for u in units}
        for n in nums:
            uid = f"{target}:{prefix}{n}"
            if uid in units:
                refs.append(uid)
            elif target not in loaded:
                ext.append(f"{target} {prefix}.{n}")              # Act known but not loaded (e.g. IEA-1872)
            else:
                ext.append(f"{target} {prefix}.{n} (not found)")  # surfaced by validate.py
    return sorted(set(refs)), sorted(set(ext))


DEF = re.compile(
    rf"[{QUOTE}]([^{QUOTE}\n]{{2,70}})[{QUOTE}]\s*(?:,\s*[^,]{{0,60}},\s*)?"
    r"(means|includes|denotes|shall mean|shall include|has the (?:same )?meaning|shall have the meaning|"
    r"is said to|are said to|shall be deemed to include)\b")
CALLED = re.compile(rf"\b(?:is|are)\s+(?:called|said to be|said to make|termed)\s+(?:an?\s+|the\s+)?[{QUOTE}]?([a-z][a-z\- ]{{2,40}}?)[{QUOTE}]?(?=[;:.,]|\s+and\b)")


ENDS_AS = re.compile(r"\b(?:is|are|becomes)\s+(?:an?\s+)?([a-z]+(?: [a-z]+){0,2})\s*[;.]?\s*$")


def extract_definitions(text: str, definition_section: bool = False) -> list[tuple[str, str, str, str]]:
    """[(term, clause, verb, definition text)]"""
    out = []
    # split into clauses "(a) ..." / "(12) ..." at line starts so each definition keeps its own text
    pieces = re.split(r"\n(?=\(\s*[0-9a-z]{1,5}\s*\)\s)", "\n" + text)
    for piece in pieces:
        piece = piece.strip()
        cm = re.match(r"\(\s*([0-9a-z]{1,5})\s*\)\s", piece)
        clause = f"({cm.group(1)})" if cm else ""
        body = re.sub(r"\s+", " ", piece)
        found = False
        for m in DEF.finditer(body):
            term = m.group(1).strip(" ,")
            if 1 <= len(term.split()) <= 8 and not re.search(r"\d{3}", term):
                out.append((term, clause, m.group(2), body[:1200]))
                found = True
        if not found:
            for m in CALLED.finditer(body):
                term = re.sub(r"\s+(when|if|where|unless)$", "", m.group(1).strip())
                if 1 <= len(term.split()) <= 5:
                    out.append((term, clause, "is said to / is called", body[:1200]))
                    found = True
        if not found and definition_section and clause:  # ICA s.2(e): "... is an agreement;"
            m = ENDS_AS.search(body)
            if m:
                out.append((m.group(1), clause, "is", body[:1200]))
    seen, uniq = set(), []
    for t in out:
        k = t[0].lower()
        if k not in seen:
            seen.add(k)
            uniq.append(t)
    return uniq


AMEND = re.compile(r"^(Subs\.|Ins\.|Omitted|Added|Renumbered|Rep\.|Inserted|Substituted)", re.I)
BY_ACT = re.compile(r"\b(?:Act|Regulation|Ordinance)\s+(?:No\.\s*)?(\d+)\s+of\s+(\d{4})", re.I)
BY_SEC = re.compile(r"\b(?:s|ss|section|sec)\.?\s*(\d+[A-Z]?)", re.I)


def parse_amendments(raw: list[dict], last_act: str | None) -> tuple[list[dict], str | None]:
    out, seen = [], set()
    for a in raw:
        note = a["note"].strip()
        if note in seen:
            continue  # the same footnote marker printed at several places in one section
        seen.add(note)
        act = BY_ACT.search(note)
        by = f"Act {act.group(1)} of {act.group(2)}" if act else (last_act if re.search(r"\bibid\b", note, re.I) else None)
        if act:
            last_act = by
        action = AMEND.match(note)
        sec = BY_SEC.search(note.split(" for ")[0])
        out.append({"on": a.get("on"), "action": action.group(1).rstrip(".").capitalize() if action else None,
                    "by": by, "by_provision": f"s. {sec.group(1)}" if sec else None, "note": note})
    return out, last_act


# ---------------------------------------------------------------- PDFs
def long_title_from_pdf(iid: str, kind: str) -> tuple[str | None, str]:
    pdf = ACTS_DIR / f"{iid}.pdf"
    if not pdf.exists():
        return None, "no PDF"
    doc = pymupdf.open(pdf)
    text = re.sub(r"\s+", " ", " ".join(doc[p].get_text() for p in range(len(doc))))
    text = re.sub(r"(\w)-\s(\w)", r"\1\2", text)
    m = re.search(r"\]\s*(An Act (?:to|further to|for)\b.{10,2000}?\.)\s*(?:Preamble|WHEREAS|BE it|Be it|BE IT)", text)
    if m:
        return re.sub(r"\d+\[|\]", "", m.group(1)).strip(), "long title printed above the enacting formula"
    m = re.search(r"Preamble\.?\s*[—–-]+\s*(WHEREAS it is expedient to .{10,400}?);", text)
    if m:
        return re.sub(r"\d+\[|\]", "", m.group(1)).strip(), "no long title in this Act; the Preamble is used"
    if kind == "rules":
        m = re.search(r"((?:in exercise of (?:the )?powers conferred by) .{20,600}?namely)\s*:?", text, re.I)
        if m:
            return m.group(1).strip(), "rule-making statement printed before the Rules"
    return None, "not printed"


# ---------------------------------------------------------------- main
def main() -> None:
    idx = title_index(None)
    topics_cfg = load_yaml("topics.yaml")
    clause_cfg = load_yaml("clause_map.yaml")
    comm_cfg = load_yaml("commencement.yaml")
    jcfg = {j["id"]: j for j in load_yaml("judgments.yaml")["judgments"]}
    src = {e["id"]: e for e in (lambda s: s["acts"] + s["rules"])(load_yaml("sources.yaml"))}
    stats = collections.Counter()

    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("select unit_id from unit")
        all_units = {r[0] for r in cur.fetchall()}
        cur.execute("select instrument_id, kind, short_title, enacted_on, commenced_on, parent_id, decided_on, "
                    "statutes_cited, status, act_number from instrument")
        insts = {r[0]: dict(zip(["id", "kind", "title", "enacted", "commenced", "parent", "decided", "statutes",
                                 "status", "act_number"], r)) for r in cur.fetchall()}
        cur.execute("delete from defined_term")

        # ---------------- Acts and Rules
        for iid, inst in sorted(insts.items()):
            if inst["kind"] == "judgment":
                continue
            notes = {}
            lt, how = long_title_from_pdf(iid, inst["kind"])
            if not lt and inst["kind"] == "rules" and inst["act_number"]:
                lt, how = inst["act_number"], "rule-making power as recorded in sources.yaml (no title page in the scan)"
            notes["long_title"] = how
            enacted = inst["enacted"]
            if not enacted and src.get(iid, {}).get("notified"):
                enacted = src[iid]["notified"]
                notes["enacted_on"] = "date of Gazette notification (sources.yaml: notified)"

            cur.execute("select unit_id, number, heading, path, text, amended_by, valid_from, unit_type, omitted "
                        "from unit where instrument_id=%s order by sort_key", (iid,))
            rows = cur.fetchall()
            comm = comm_cfg.get(iid, {})
            phase_of = {}
            for ph in comm.get("phases", []):
                for u in ph["units"]:
                    phase_of[f"{iid}:{u}"] = ph
            last_act, max_on, not_in_force = None, None, 0
            for uid, number, heading, path, text, amended, valid_from, utype, omitted in rows:
                amendments, last_act = parse_amendments(amended or [], last_act)
                D = lambda x: dt.date.fromisoformat(x) if isinstance(x, str) else x
                dates = [D(a["on"]) for a in amendments if a["on"]]
                text_from = max(dates) if dates else None
                if text_from and (max_on is None or text_from > max_on):
                    max_on = text_from
                # provision inserted after commencement: valid from its insertion
                vf, cnote = valid_from, None
                ins = [D(a["on"]) for a in amendments if a["on"] and a["action"] in ("Ins", "Inserted", "Added")]
                if re.search(r"\d[A-Z]+$", number or "") and ins:
                    vf, cnote = min(ins), "provision inserted by amendment"
                if uid in phase_of:
                    vf, cnote = phase_of[uid]["from"], phase_of[uid].get("note")
                elif comm.get("default") and utype in ("section", "rule"):
                    vf, cnote = comm["default"], "commences with the remaining provisions (commencement.yaml default)"
                elif comm.get("default") and utype == "schedule":
                    vf, cnote = None, "in force with the provision that applies this Schedule"
                if (comm.get("default") or uid in phase_of) and utype != "schedule":
                    if vf is None or vf > TODAY:
                        not_in_force += 1
                if text_from and vf and text_from < vf:
                    text_from = None
                refs, ext = extract_refs(text, iid, inst["parent"], all_units, idx, inst["kind"])
                refs = [r for r in refs if r != uid]
                is_def = bool(re.search(r"definition|interpretation", heading or "", re.I))
                defs = extract_definitions(text, is_def) if not omitted else []
                for term, clause, verb, body in defs:
                    cur.execute("""insert into defined_term (term, term_norm, instrument_id, unit_id, clause, verb, definition)
                                   values (%s,%s,%s,%s,%s,%s,%s) on conflict do nothing""",
                                (term, re.sub(r"\s+", " ", term.lower()), iid, uid, clause, verb, body))
                tps = []
                for topic, rule in topics_cfg.items():
                    if rule.get("only") and iid not in rule["only"]:
                        continue
                    hit = False
                    if rule.get("heading") and heading and re.search(rule["heading"], heading, re.I):
                        hit = True
                    if rule.get("path") and path and re.search(rule["path"], path, re.I):
                        hit = True
                    if hit:
                        tps.append(topic)
                if defs and "definitions" not in tps and len(defs) >= 3:
                    tps.append("definitions")
                cts = sorted(ct for ct, us in clause_cfg.items() if uid in us)
                cur.execute("""update unit set amended_by=%s, text_valid_from=%s, valid_from=%s, commencement_note=%s,
                                   cross_refs=%s, external_refs=%s, defined_terms=%s, topics=%s, clause_types=%s, path=%s
                               where unit_id=%s""",
                            (Jsonb(amendments), text_from, vf, cnote, refs, ext,
                             sorted({d[0] for d in defs}), sorted(set(tps)), cts, roman_fix(path), uid))
                stats["refs"] += len(refs)
                stats["ext_refs"] += len(ext)
                stats["defs"] += len(defs)
                stats["units_with_topics"] += bool(tps)
                stats["act_units"] += 1
            amended_upto = max_on or enacted
            notes["as_amended_upto"] = ("latest w.e.f. date in the amendment footnotes" if max_on
                                        else "no amendment footnotes in the source text: text as enacted")
            status = inst["status"]
            if status != "repealed" and not_in_force:
                status = "partly_in_force"
                notes["status"] = f"{not_in_force} provisions not yet in force as of {TODAY} (commencement.yaml)"
            if comm:
                notes["commencement"] = comm["source"] + ("; VERIFY against the Gazette" if comm.get("verify") else "")
            cur.execute("""update instrument set long_title=%s, enacted_on=%s, as_amended_upto=%s, status=%s,
                               commencement_source=%s, metadata_notes=%s where instrument_id=%s""",
                        (lt, enacted, amended_upto, status, comm.get("source"), Jsonb(notes), iid))
            print(f"{iid:11} long_title={'yes' if lt else 'NO '}  status={status:15} amended_upto={amended_upto}")

        # every clause_map unit must exist
        missing = sorted({u for us in clause_cfg.values() for u in us} - all_units)
        if missing:
            raise SystemExit(f"clause_map.yaml names units that are not in the database: {missing}")

        # ---------------- Judgments
        cite_patterns = []  # (regex, instrument_id) for case-to-case citations
        name_patterns = []
        for iid, inst in insts.items():
            if inst["kind"] != "judgment":
                continue
            cur.execute("select reporter_citations, neutral_citation, short_title from instrument where instrument_id=%s", (iid,))
            reps, neutral, short = cur.fetchone()
            for c in reps + [neutral]:
                m = re.match(r"\[(\d{4})\]\s*(\d+)\s*S\.\s?C\.\s?R\.?\s*(\d+)", c)
                if m:
                    cite_patterns.append((re.compile(rf"\[?\(?{m.group(1)}\]?\)?\s*{m.group(2)}\s*S\.?\s?C\.?\s?R\.?\s*{m.group(3)}\b"), iid))
                m = re.match(r"\((\d{4})\)\s*(\d+)\s*SCC\s*(\d+)", c)
                if m:
                    cite_patterns.append((re.compile(rf"\({m.group(1)}\)\s*{m.group(2)}\s*SCC\s*{m.group(3)}\b"), iid))
            # by name only as "A v. B": both parties must appear, so a publisher called "Eastern Book Company"
            # or another case against "Life Insurance Corporation" is not taken for this judgment
            parts = re.split(r"\s+v\.?\s+", re.sub(r"\s*\([^)]*\)\s*$", "", short), maxsplit=1)
            if len(parts) == 2:
                generic = r"^(union|state|india|bar|council|commissioner|government|the|and|of|co|ltd|corporation|company|others?)$"
                kw = lambda x: [w for w in re.findall(r"[A-Za-z']{4,}", x) if not re.match(generic, w, re.I)]
                a, b = kw(re.sub(r"^(Justice|Dr\.)\s*", "", parts[0])), kw(parts[1])
                # the whole first party as printed ("O.P. Sharma", not just "Sharma"), spacing/dots tolerated
                first = re.sub(r"^(Justice|Dr\.)\s*", "", parts[0]).strip()
                first_rx = r"[\s.]*".join(re.escape(w) for w in re.split(r"[\s.]+", first) if w)
                if a and b:
                    pat = (rf"\b{first_rx}\b[^;]{{0,40}}?\bv(?:s|ersus)?\.?\s"
                           rf"[^;]{{0,80}}?\b{re.escape(b[0])}")
                    name_patterns.append((pat, iid))

        # a name shared by two corpus judgments (the two Puttaswamy cases) is left to the citations
        counts = collections.Counter(p for p, _ in name_patterns)
        cite_patterns += [(re.compile(p, re.I), j) for p, j in name_patterns if counts[p] == 1]
        cited_by = collections.defaultdict(set)
        for iid, inst in sorted(insts.items()):
            if inst["kind"] != "judgment":
                continue
            meta = jcfg.get(iid, {})
            overruled_by = meta.get("overruled_by")
            overruled_on = insts[overruled_by]["decided"] if overruled_by in insts else None
            acts_of_case = {s.split(":")[0] for s in inst["statutes"]}
            default_act = next(iter(acts_of_case)) if len(acts_of_case) == 1 else None
            cur.execute("select unit_id, number, text from unit where instrument_id=%s order by sort_key", (iid,))
            case_cites = set()
            for uid, number, text in cur.fetchall():
                refs, ext = extract_refs(text, iid, None, all_units, idx, "judgment", default_act, inst["decided"],
                                         inst["statutes"])
                # a judgment can only cite one decided on or before its own date
                cites = sorted({j for rx, j in cite_patterns if j != iid and insts[j]["decided"] <= inst["decided"]
                                and rx.search(text)})
                case_cites |= set(cites)
                cur.execute("select form from citation_index where unit_id=%s order by form", (uid,))
                aliases = [r[0] for r in cur.fetchall()]
                cur.execute("""update unit set valid_from=%s, valid_to=%s, statutes_cited=%s, external_refs=%s,
                                   cites_judgments=%s, aliases=%s where unit_id=%s""",
                            (inst["decided"], overruled_on, refs, ext[:40], cites, aliases, uid))
                stats["judgment_statute_refs"] += len(refs)
                stats["judgment_units"] += 1
            for c in case_cites:
                cited_by[c].add(iid)
            notes = {"judges": "S.C.R. bench line on page 1 (dataset metadata lists only one judge)",
                     "statutes_cited": "case level: curated (judgments.yaml); paragraph level: sections named in the paragraph",
                     "cites_judgments": "S.C.R./SCC citation or distinctive party name of another corpus judgment"}
            if default_act:
                notes["statutes_cited"] += f"; a bare 'section N' is read as {default_act}"
            cur.execute("""update instrument set cites_judgments=%s, overruled_by=%s, overruled_on=%s, metadata_notes=%s,
                               domains = case when domains = '{}' then %s else domains end where instrument_id=%s""",
                        (sorted(case_cites), overruled_by, overruled_on, Jsonb(notes),
                         sorted({DOMAIN_OF.get(a.split("-")[0], "general") for a in acts_of_case}) or ["constitutional"], iid))
        for iid in insts:
            if insts[iid]["kind"] == "judgment":
                cur.execute("update instrument set cited_by_judgments=%s where instrument_id=%s", (sorted(cited_by[iid]), iid))
        conn.commit()

        cur.execute("select count(*), count(distinct term_norm) from defined_term")
        n_def, n_terms = cur.fetchone()
        cur.execute("select count(*) from instrument where cardinality(cites_judgments) > 0")
        n_citing = cur.fetchone()[0]
    print(f"acts/rules: {stats['act_units']} units, {stats['refs']} cross-references resolved, "
          f"{stats['ext_refs']} external, {n_def} definitions ({n_terms} terms), "
          f"{stats['units_with_topics']} units tagged with topics")
    print(f"judgments: {stats['judgment_units']} paragraphs, {stats['judgment_statute_refs']} statute references, "
          f"{n_citing} judgments cite another corpus judgment")


if __name__ == "__main__":
    main()
