"""Split Supreme Court judgments (official S.C.R. PDFs) into paragraph units.

Layout handled:
  * running header on every page: "628 SUPREME COURT REPORTS [2015] 1 S.C.R." or
    "CASE v. CASE 635 / [AUTHOR, J.]"; its number is the S.C.R. page, kept for pinpoint citations;
  * margin line-letters A-H printed down the side, often OCR'd as stray one-letter fragments;
  * the official headnote (editorial summary, "HELD: ...") before the judgment;
  * numbered paragraphs, restarting at 1 for each separate opinion (e.g. Puttaswamy has six).
Older judgments without paragraph numbers are cut into ~220-word segments at sentence ends.

Output: data/processed/units/<ID>.jsonl and data/processed/instruments/<ID>.json
Usage:  python -m ingest.parse_judgment [ID ...]
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "judgments"
OUT = ROOT / "data" / "processed"

MARGIN = re.compile(r"^[A-HcIl0o•.,'\"`~\-_]{1,3}$")
HEADER_HINT = re.compile(r"S\.\s?C\.\s?R|SUPREME COURT REPORTS|\[.*J[J]?\.?\]?\s*$|\(.*J\.?\)\s*$|\bv\.\s|\bversus\b", re.I)
DELIVERED = re.compile(r"(judgment of the court was delivered by|judgments?(?: and orders?)? of the court (?:were|was) delivered"
                       r"|delivered the following|^J\s?U\s?D\s?G\s?M\s?E\s?N\s?T\b|^O\s?R\s?D\s?E\s?R\b)", re.I)
JURISDICTION = re.compile(r"^(?:[A-H]\s+)?(?:(?:CIVIL|CRIMINAL)\s+)?(?:APPELLATE|ORIGINAL|ADVISORY|EXTRAORDINARY)?\s*JURISDICTION\s*:?", re.I)
AUTHOR_LINE = re.compile(r"^([A-Z][A-Za-z.\s]{2,60}?),\s*(C\.\s?J\.?\s?I?\.?|J\.)\s*[:.-]?\s*$")
PARA = re.compile(r"^(\d{1,3})\.\s+(\S.*)$")
TRAIL_LETTER = re.compile(r"\s+[A-H0]\s*$")


def page_lines(page: pymupdf.Page) -> tuple[list[str], int | None]:
    rows = []
    for b in page.get_text("dict")["blocks"]:
        for ln in b.get("lines", []):
            spans = [s for s in ln["spans"] if s["text"].strip() and not MARGIN.match(s["text"].strip())]
            if not spans:
                continue
            rows.append((ln["bbox"][1], ln["bbox"][0], " ".join(s["text"].strip() for s in spans)))
    rows.sort()
    merged = []
    for y, x, t in rows:  # rejoin pieces printed on the same baseline
        if merged and abs(merged[-1][0] - y) < 3:
            merged[-1] = (merged[-1][0], merged[-1][1], merged[-1][2] + " " + t)
        else:
            merged.append((y, x, t))
    page_no, body = None, []
    for i, (y, x, t) in enumerate(merged):
        if i < 3 and y < 85 and (HEADER_HINT.search(t) or re.fullmatch(r"\d{1,4}", t.strip())):
            m = re.search(r"(?<![\d.\[])(\d{1,4})(?![\d\]])(?=\s|$)", re.sub(r"\[\d{4}\]|\d+\s*S\.\s?C\.\s?R", "", t))
            if m and page_no is None:
                page_no = int(m.group(1))
            continue
        t = TRAIL_LETTER.sub("", t) if len(t) > 25 else t
        body.append(t)
    return body, page_no


def judges_from(first_page: str) -> list[str]:
    m = re.search(r"\[([^\[\]]{3,400}?\bJ\s?J?\s?\.?)\s*\]", first_page.replace("\n", " "))
    if not m:
        return []
    raw = re.sub(r",?\s*(C\.\s?J\.?\s?I?\.?|J\.?\s?J?\.?)\s*$", "", m.group(1))
    parts = re.split(r",\s*|\s+AND\s*(?=[A-Z])|\s+and\s+", raw)
    out = []
    for p in parts:
        p = re.sub(r"\s*\.\.\s*", ". ", p.replace("C.J.", "").replace("CJI", "")).replace("*", "")
        p = re.sub(r"\s+", " ", p).strip(" .,")
        if len(re.sub(r"[^A-Za-z]", "", p)) > 3:
            out.append(p.title())
    return out


def sentences(text: str, target: int = 220) -> list[str]:
    parts, cur, n = [], [], 0
    for s in re.split(r"(?<=[.?!])\s+(?=[A-Z\"'(])", text):
        cur.append(s)
        n += len(s.split())
        if n >= target:
            parts.append(" ".join(cur))
            cur, n = [], 0
    if cur:
        parts.append(" ".join(cur))
    return parts


def parse(meta: dict) -> tuple[dict, list[dict]]:
    jid = meta["id"]
    doc = pymupdf.open(RAW / "pdf" / f"{jid}.pdf")
    first_page = int(re.search(r"S\.\s?C\.\s?R\.\s*(\d+)", meta["scr_citation"]).group(1))
    lines: list[tuple[int, str]] = []
    last_page = first_page - 1
    for i, page in enumerate(doc):
        body, num = page_lines(page)
        page_no = first_page if i == 0 else (num if num and last_page < num <= last_page + 3 else last_page + 1)
        last_page = page_no
        lines += [(page_no, t) for t in body]
    judges = judges_from("\n".join(page.get_text() for page in doc[:2]))

    # Headnote: from the top to the jurisdiction line (or the first "delivered by" line).
    jur = next((i for i, (_, t) in enumerate(lines) if JURISDICTION.search(t)), None)
    dlv = next((i for i, (_, t) in enumerate(lines) if DELIVERED.search(t) and i > (jur or 0)), None)
    head_end = jur if jur is not None else (dlv or 0)
    headnote = " ".join(t for _, t in lines[:head_end])
    body = lines[(dlv + 1) if dlv is not None else head_end:]

    units: list[dict] = []
    seq = 0

    def add(number: str, author: str | None, pieces: list[tuple[int, str]], limit: int = 600, target: int = 350):
        """One unit per paragraph; a paragraph over `limit` words is split into parts at line
        boundaries, each part keeping the S.C.R. page it starts on (the reliable pinpoint)."""
        nonlocal seq
        parts, cur, n = [], [], 0
        total = sum(len(t.split()) for _, t in pieces)
        for page, t in pieces:
            cur.append((page, t))
            n += len(t.split())
            if total > limit and n >= target and re.search(r"[.;:]\s*$", t):
                parts.append(cur)
                cur, n = [], 0
        if cur:
            parts.append(cur)
        for k, part in enumerate(parts, 1):
            text = " ".join(t for _, t in part)
            text = re.sub(r"(\w)-\s+(\w)", r"\1\2", re.sub(r"\s+", " ", text)).strip()
            if len(text) < 40:
                continue
            seq += 1
            base = "Headnote" if number == "HN" else (f"para {number}" if number.isdigit() else f"segment {number[1:]}")
            label = base + (f" (part {k})" if len(parts) > 1 else "")
            units.append({
                "unit_id": f"{jid}:p{seq}", "instrument_id": jid, "unit_type": "paragraph", "number": number,
                "part": k if len(parts) > 1 else None,
                "heading": author or ("Headnote (S.C.R. editorial)" if number == "HN" else None),
                "path": " > ".join(x for x in [f"Opinion of {author}" if author else None, label] if x),
                "text": text, "source_page": part[0][0], "amended_by": [], "omitted": False,
                "statutes_cited": meta.get("statutes", []), "topics": meta.get("topics", []),
                "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
            })

    if head_end:
        add("HN", None, lines[:head_end], limit=400, target=300)

    # Numbered paragraphs, accepted in sequence (gaps of up to 6 tolerated for OCR misses);
    # restart at 1 only when a new opinion starts; quoted paragraphs ("57. ...) never count.
    author = None
    m = re.search(r"delivered by\s+(.+?),\s*(C\.\s?J\.?|J\.)", " ".join(t for _, t in lines[max(0, (dlv or 0) - 1):(dlv or 0) + 2]), re.I)
    if m:
        author = m.group(1).strip().title() + ", J."
    numbered = sum(1 for _, t in body if PARA.match(t))
    cur_num, buf, last = None, [], 0
    new_opinion = True
    prev_last = -10  # numbering before the last restart, so a false restart (a quoted list) can be undone
    if numbered >= 5:
        for page, t in body:
            am = AUTHOR_LINE.match(t)
            if am and len(t) < 60:
                if buf:
                    add(cur_num or "s0", author, buf)
                    buf, cur_num = [], None
                author, last = am.group(1).strip().title() + (", C.J." if "C" in am.group(2) else ", J."), 0
                new_opinion = True
                continue
            if DELIVERED.search(t) and last > 5:
                new_opinion = True
            pm = PARA.match(re.sub(r"^[^\w\"'(\u201c\u2018]+", "", t))
            n = int(pm.group(1)) if pm else -1
            resume = prev_last < n <= prev_last + 6 and n > last + 6
            if pm and ((last < n <= last + 6) or (n == 1 and new_opinion) or resume):
                if n == 1 and new_opinion and last > 0:
                    prev_last = last
                if n == 1:
                    new_opinion = False
                if resume:
                    prev_last = -10
                if buf:
                    add(cur_num or "s0", author, buf)
                cur_num, buf, last = pm.group(1), [(page, pm.group(2))], n
            else:
                buf.append((page, t))
        if buf:
            add(cur_num or "s0", author, buf)
    else:
        add("s1", author, body, limit=300, target=220)
        # renumber segments s1, s2, ... in order
        k = 0
        for u in units:
            if u["number"] == "s1" and u.get("part"):
                k += 1
                u["number"], u["part"] = f"s{k}", None
                u["path"] = u["path"].rsplit("segment", 1)[0] + f"segment {k}"

    pdf = (RAW / "pdf" / f"{jid}.pdf").read_bytes()
    inst = {
        "instrument_id": jid, "kind": "judgment", "short_title": meta["short"], "long_title": meta["title"],
        "neutral_citation": meta["neutral_citation"],
        "reporter_citations": [meta["scr_citation"]] + meta.get("reporter_citations", []),
        "court": "Supreme Court of India", "judges": judges or meta.get("judges", []),
        "bench_size": len(judges) or meta.get("bench_size"), "decided_on": meta["decided_on"],
        "parties": f"{meta['petitioner']} v. {meta['respondent']}", "treatment": meta.get("treatment"),
        "treatment_note": meta.get("note"), "topics": meta.get("topics", []), "statutes": meta.get("statutes", []),
        "status": "in_force", "dataset": "AWS Open Data: indian-supreme-court-judgments (S.C.R. text)",
        "source_url": meta["source_url"], "source_sha256": hashlib.sha256(pdf).hexdigest(),
        "licence_basis": "CC-BY-4.0 (dataset); judgments of courts: Copyright Act 1957 s.52(1)(q)(iv)",
        "pages": len(doc), "units": len(units), "numbered_paragraphs": numbered >= 5,
    }
    return inst, units


def main(only: list[str]) -> None:
    selected = json.loads((RAW / "selected.json").read_text())
    (OUT / "units").mkdir(parents=True, exist_ok=True)
    (OUT / "instruments").mkdir(parents=True, exist_ok=True)
    for meta in selected:
        if only and meta["id"] not in only:
            continue
        inst, units = parse(meta)
        (OUT / "instruments" / f"{inst['instrument_id']}.json").write_text(json.dumps(inst, indent=2, ensure_ascii=False))
        with open(OUT / "units" / f"{inst['instrument_id']}.jsonl", "w") as f:
            for u in units:
                f.write(json.dumps(u, ensure_ascii=False) + "\n")
        paras = [u for u in units if u["number"].isdigit()]
        print(f"{inst['instrument_id']:30} pages={inst['pages']:4} units={len(units):4} "
              f"{'numbered' if inst['numbered_paragraphs'] else 'segments'} bench={inst['bench_size']} {inst['judges'][:3]}")


if __name__ == "__main__":
    main(sys.argv[1:])
