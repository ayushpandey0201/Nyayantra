"""Load parsed instruments and units into Postgres, and build the citation index.

Reads data/processed/instruments/*.json and data/processed/units/*.jsonl (written by parse_act.py
and parse_scanned.py), adds catalogue metadata from sources.yaml, and upserts everything.
Re-running is safe: an instrument's units and citation forms are replaced as a whole.

Usage: python -m ingest.load_db
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import psycopg
import yaml
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "data" / "processed"
DSN = os.environ.get("NYAYANTRA_DSN", "host=/tmp dbname=nyayantra")


def norm(form: str) -> str:
    """Normalised citation form: lower case, punctuation to spaces, 'section'/'sec' -> 's'."""
    f = form.lower().replace("&", " and ")
    f = re.sub(r"\b(sections?|secs?|ss|s|u/s)\.?\s*(?=\d)", " s ", f)
    f = re.sub(r"\b(rules?|r)\.?\s*(?=\d)", " r ", f)
    f = re.sub(r"[^\w()]+", " ", f)
    return re.sub(r"\s+", " ", f).strip()


def citation_forms(unit: dict, inst: dict, aliases: list[str]) -> set[str]:
    num = unit["number"]
    year = re.search(r"\d{4}", inst["short_title"])
    year = year.group() if year else ""
    title = re.sub(r"^The\s+", "", inst["short_title"]).split(" (")[0]
    names = {title, title.replace(f", {year}", "").strip(), *aliases}
    names |= {f"{n} {year}".strip() for n in list(names) if year and year not in n}
    if unit["unit_type"] == "schedule":
        return {norm(f"{n} schedule {num}") for n in names}
    word = "r" if unit["unit_type"] == "rule" else "s"
    forms = set()
    for n in names:
        forms |= {norm(f"{n} {word} {num}"), norm(f"{word} {num} {n}"), norm(f"{word} {num} of the {n}")}
    return forms


DOMAIN_OF = {"ICA": "contract", "SRA": "contract", "SGA": "contract", "ACA": "arbitration", "ITA": "technology",
             "DPDPA": "privacy", "CA": "ip", "AA": "ethics", "BCIR": "ethics", "CPA": "consumer",
             "IPC": "criminal", "BNS": "criminal", "IEA": "evidence", "BSA": "evidence", "LA": "procedure"}


def load_judgment(cur, inst: dict) -> None:
    iid = inst["instrument_id"]
    domains = sorted({DOMAIN_OF[s.split("-")[0]] for s in inst["statutes"] if s.split("-")[0] in DOMAIN_OF}
                     | ({"privacy"} if "privacy" in inst["topics"] else set()))
    cur.execute(
        """insert into instrument (instrument_id, kind, short_title, long_title, status, domains, source_url,
               retrieved_at, source_sha256, licence_basis, neutral_citation, reporter_citations, court, bench_size,
               judges, decided_on, parties, treatment, treatment_note, dataset, bench_line, statutes_cited)
           values (%s,'judgment',%s,%s,'in_force',%s,%s,now(),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           on conflict (instrument_id) do update set short_title = excluded.short_title,
               long_title = excluded.long_title, domains = excluded.domains, source_url = excluded.source_url,
               source_sha256 = excluded.source_sha256, neutral_citation = excluded.neutral_citation,
               reporter_citations = excluded.reporter_citations, bench_size = excluded.bench_size,
               judges = excluded.judges, decided_on = excluded.decided_on, parties = excluded.parties,
               treatment = excluded.treatment, treatment_note = excluded.treatment_note,
               bench_line = excluded.bench_line, statutes_cited = excluded.statutes_cited""",
        (iid, inst["short_title"], inst["long_title"], domains, inst["source_url"], inst["source_sha256"],
         inst["licence_basis"], inst["neutral_citation"], inst["reporter_citations"], inst["court"],
         inst["bench_size"], inst["judges"], inst["decided_on"], inst["parties"], inst["treatment"],
         inst.get("treatment_note"), inst["dataset"], inst.get("bench_line"), inst["statutes"]))
    # whole-case citation forms: neutral citation, reporter citations, case name
    names = {inst["short_title"], inst["short_title"].split(" v. ")[0], inst["parties"]}
    forms = {norm(inst["neutral_citation"]), *(norm(c) for c in inst["reporter_citations"])}
    forms |= {norm(c).replace("s c r", "scr") for c in inst["reporter_citations"]}
    forms |= {norm(n) for n in names if len(n) > 4}
    forms |= {re.sub(r"\s+", " ", f.replace("(", " ").replace(")", " ")).strip() for f in list(forms)}  # "(2017) 10 SCC 1"
    for f in forms:
        cur.execute("insert into citation_index (form, unit_id, instrument_id) values (%s, null, %s) "
                    "on conflict do nothing", (f, iid))
    units = [json.loads(l) for l in open(PROC / "units" / f"{iid}.jsonl")]
    cite = f"{inst['short_title']}, {inst['neutral_citation']} : {inst['reporter_citations'][0]}"
    for sort_key, u in enumerate(units):
        where = "headnote" if u["number"] == "HN" else (
            f"para {u['number']}" if u["number"].isdigit() else f"segment {u['number'][1:]}")
        cur.execute(
            """insert into unit (unit_id, instrument_id, parent_id, unit_type, number, heading, path, text,
                   canonical_citation, topics, statutes_cited, sort_key, source_page, token_count, content_sha256,
                   opinion_author)
               values (%s,%s,null,'paragraph',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (u["unit_id"], iid, u["number"], u["heading"], u["path"], u["text"],
             f"{cite}, {where} (at p. {u['source_page']})", u.get("topics", []), u.get("statutes_cited", []),
             sort_key, u["source_page"], len(u["text"].split()), u["content_sha256"], u.get("opinion_author")))
        if u["number"].isdigit() and not u.get("part"):
            for n in names:
                cur.execute("insert into citation_index (form, unit_id, instrument_id) values (%s,%s,%s) "
                            "on conflict do nothing", (norm(f"{n} para {u['number']}"), u["unit_id"], iid))
    print(f"{iid:30} {len(units):4} units  bench {inst['bench_size']}  {inst['treatment']}")


def main() -> None:
    src = yaml.safe_load((ROOT / "ingest" / "sources.yaml").read_text())
    catalogue = {e["id"]: e for e in src["acts"] + src["rules"]}
    insts = [json.loads(p.read_text()) for p in sorted((PROC / "instruments").glob("*.json"))]
    # parents and repealing Acts first, so foreign keys resolve
    insts.sort(key=lambda i: (i["status"] == "repealed", i["kind"] == "rules"))

    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        for inst in insts:
            iid = inst["instrument_id"]
            meta = catalogue.get(iid, {})
            cur.execute("delete from citation_index where instrument_id = %s", (iid,))
            cur.execute("delete from unit where instrument_id = %s", (iid,))
            if inst["kind"] == "judgment":
                load_judgment(cur, inst)
                continue
            cur.execute(
                """insert into instrument (instrument_id, kind, short_title, act_number, enacted_on, commenced_on,
                       status, repealed_by, repealed_on, as_amended_upto, domains, source_url, retrieved_at,
                       source_sha256, licence_basis, parent_id, ocr)
                   values (%(id)s, %(kind)s, %(title)s, %(num)s, %(enacted)s, %(commenced)s, %(status)s, %(rep_by)s,
                       %(rep_on)s, %(amended)s, %(domains)s, %(url)s, now(), %(sha)s, %(licence)s, %(parent)s, %(ocr)s)
                   on conflict (instrument_id) do update set kind = excluded.kind, short_title = excluded.short_title,
                       act_number = excluded.act_number, enacted_on = excluded.enacted_on,
                       commenced_on = excluded.commenced_on, status = excluded.status,
                       repealed_by = excluded.repealed_by, repealed_on = excluded.repealed_on,
                       as_amended_upto = excluded.as_amended_upto, domains = excluded.domains,
                       source_url = excluded.source_url, retrieved_at = excluded.retrieved_at,
                       source_sha256 = excluded.source_sha256, licence_basis = excluded.licence_basis,
                       parent_id = excluded.parent_id, ocr = excluded.ocr""",
                {"id": iid, "kind": inst["kind"], "title": inst["short_title"], "num": inst.get("act_number"),
                 "enacted": inst.get("enacted_on"), "commenced": meta.get("commenced"),
                 "status": inst["status"], "rep_by": inst.get("repealed_by"), "rep_on": inst.get("repealed_on"),
                 "amended": inst.get("as_amended_upto"), "domains": inst.get("domains", []),
                 "url": meta.get("where") or "https://indiacode.gov.in (downloaded by hand)",
                 "sha": inst["source_sha256"], "licence": inst["licence_basis"], "parent": inst.get("parent"),
                 "ocr": bool(inst.get("ocr"))})

            title = re.sub(r"^The\s+", "", inst["short_title"])
            valid_from = meta.get("commenced") or inst.get("enacted_on")
            units = [json.loads(l) for l in open(PROC / "units" / f"{iid}.jsonl")]
            n_forms = 0
            for sort_key, u in enumerate(units):
                word = {"rule": "r.", "schedule": "Schedule"}.get(u["unit_type"], "s.")
                canonical = f"{title}, {word} {u['number']}"
                aliases = meta.get("aliases", [])
                cur.execute(
                    """insert into unit (unit_id, instrument_id, unit_type, number, heading, path, text,
                           canonical_citation, aliases, valid_from, valid_to, amended_by, sort_key, source_page,
                           token_count, content_sha256, omitted, ocr, state_amendments)
                       values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (u["unit_id"], iid, u["unit_type"], u["number"], u["heading"], u["path"], u["text"], canonical,
                     [f"{a} {word} {u['number']}" for a in aliases], valid_from, inst.get("repealed_on"),
                     Jsonb(u.get("amended_by", [])), sort_key, u.get("source_page"),
                     len(u["text"].split()), u["content_sha256"], u.get("omitted", False), u.get("ocr", False),
                     Jsonb(u.get("state_amendments", []))))
                for form in citation_forms(u, inst, aliases):
                    cur.execute("insert into citation_index (form, unit_id, instrument_id) values (%s,%s,%s) "
                                "on conflict do nothing", (form, u["unit_id"], iid))
                    n_forms += 1
            print(f"{iid:11} {len(units):4} units  {n_forms:5} citation forms")
        conn.commit()


if __name__ == "__main__":
    main()
