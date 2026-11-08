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
                       r"|delivered the following|(?-i:^J\s?U\s?D\s?G\s?M\s?E\s?N\s?T\s*:?\s*$|^O\s?R\s?D\s?E\s?R\s*:?\s*$))", re.I)
# "CIVIL APPELLATE JURISDICTION : Civil Appeal No. ..." (OCR: "Crvn. APPELLATE", "OvIL APPELLATE"); upper case only,
# so a sentence beginning "jurisdiction of this Court ..." is not taken for it
JURISDICTION = re.compile(r"^\W*(?:[A-H]\s+)?(?:\S{2,9}\s+)?(?:APPELLATE|ORIGINAL|ADVISORY|EXTRAORDINARY|WRIT)\s+JURISDICTION\b")
DELIVERED_AT_END = re.compile(r"\b(?:was|were|wa\S)\s+delivered(?:\s+by)?\s*:?\s*$", re.I)
INLINE_AUTHOR = re.compile(r"^\W*([A-Z][\w.;:'\s]{2,40}?),?\s*(?:C\.?\s?J\.?\s?I?|J)\.?\s*(?:[-—–]+\s*|\s+(?=[A-Z][a-z]))\S")


def mostly_caps(x: str) -> bool:
    letters = [c for c in x if c.isalpha()]
    return bool(letters) and sum(c.isupper() for c in letters) / len(letters) > 0.6
# an opinion's first line: "R.F. NARIMAN, J.", "PER R. NARIMAN, J.", "R. NARIMAN, .J.", "Krishna Iyer, J. (concurring) :"
AUTHOR_LINE = re.compile(r"^\W*(?:PER\s*)?([A-Z][A-Za-z.'\s]{2,60}?),?\s*(C\.?\s?J\.?\s?I\.?|C\.\s?J\.?|J\.|\.J\.?)\s*[:.-]?\s*(?:\(\w+\)\s*:?)?\s*$")
# "INDU MALHOTRA, J. 1. I have had the advantage ..." (author and first paragraph on one line; names in capitals)
AUTHOR_THEN_PARA = re.compile(r"^\W*(?:PER\s*)?([A-Z][A-Z.'\s]{2,60}?),\s*\.?(?:C\.?\s?J\.?\s?I\.?|J\.)\s+(1\.\s+\S.*)$")
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


BENCH = re.compile(r"[\[\(\{]([A-Z][^\[\]\(\)\{\}]{3,400}?\b(?:J\s?J|C\.?\s?J\.?\s?I?|J)\s?\.?)\s*[\]\)\}t]")
# OCR misreadings in the printed bench lines, checked by eye against the PDFs
OCR_NAME_FIXES = {"8achawat": "Bachawat", "Giiose": "Ghose", "R. K Naiuman": "R.F. Nariman", "Wal'Clloo": "Wanchoo",
                  "Gajendragadxaj't": "Gajendragadkar", "Smha": "Sinha", "B. P;": "B.P.", "M. Ji. Beg": "M.H. Beg",
                  "Ap .. Sen": "A.P. Sen", "Pinaki . Chandra": "Pinaki Chandra", "R. F .. Nariman": "R.F. Nariman",
                  "Sbelat": "Shelat", "S.N. V Aria Va": "S.N. Variava", "V1Vran": "Vivian"}
# one person, one name: the form the S.C.R. prints most often
SAME_JUDGE = {"Rohinton Fali Nariman": "R.F. Nariman", "Dhananjaya Y. Chandrachud": "D.Y. Chandrachud",
              "Rajendra Mal Lodha": "R.M. Lodha", "Uday Umesh Lalit": "U.U. Lalit",
              "Pamidighantam Sri Narasimha": "P.S. Narasimha", "Jagdish Singh Khehar": "J.S. Khehar",
              "Murtaza Fazal Ali": "S. Murtaza Fazal Ali", "Ar. Lakshmanan": "AR. Lakshmanan", "Rohinton Fali Nariman": "R.F. Nariman",
              "Chandrachud": "D.Y. Chandrachud", "Kurian": "Kurian Joseph"}


def canon_judge(raw: str) -> str:
    s = re.sub(r"\s+", " ", raw.replace("*", " ")).strip(" .,;")
    s = re.sub(r"^(?:In The Judgment Of|Per)\s+", "", s, flags=re.I)
    s = re.sub(r"\s*\(For Himself.*$", "", s, flags=re.I)
    s = re.sub(r",?\s*(?:Acting\s+)?(?:C\.\s?J\.?\s?I?\.?|CJI|J\.?\s?J?\.?)\s*$", "", s, flags=re.I).strip(" .,")
    letters = [c for c in s if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) > 0.6:  # "RANJAN GOGOi", "MuDHOLKAR"
        s = s.title()
    s = re.sub(r"^And\s+", "", s)
    s = re.sub(r"([A-Z]\.)(?=[A-Z][a-z])", r"\1 ", s)  # "A.P.Sen" -> "A.P. Sen"
    for bad, good in OCR_NAME_FIXES.items():
        s = re.sub(re.escape(bad), good, s, flags=re.I)
    s = re.sub(r"^Dr\.?\s+", "", s)
    # initials: "A. N. Ray" / "B R Gavai" -> "A.N. Ray" / "B.R. Gavai"
    toks = s.split()
    out, ini = [], ""
    for i, t in enumerate(toks):
        bare = t.rstrip(".")
        if (len(bare) == 1 or t.endswith(".")) and i < len(toks) - 1 and bare.isalpha() and len(bare) <= 2:
            ini += bare.upper() if len(bare) == 1 else bare + "."
            ini += "." if len(bare) == 1 else ""
            continue
        if ini:
            out.append(ini); ini = ""
        out.append(t)
    s = re.sub(r"\.\.", ".", " ".join(out + ([ini] if ini else [])))
    s = re.sub(r"\bAr\.Lakshmanan|\bAr\. Lakshmanan", "AR. Lakshmanan", s)
    return SAME_JUDGE.get(s, s)


def judges_from(first_pages: str) -> tuple[list[str], str | None]:
    """Bench from the S.C.R. bench line, e.g. "[R.M. LODHA, CJI, KURIAN JOSEPH AND ROHINTON FALI NARIMAN, JJ.]".
    Returns (canonical names in printed order, the line as printed)."""
    t = re.sub(r"\s+", " ", first_pages)
    found = [m.group(1) for m in BENCH.finditer(t)]
    line = next((f for f in found if re.search(r"J\s?J", f)), None) or next(
        (f for f in found if re.search(r"(?:,|\s)J\.?$", f) and "v." not in f), None)
    if not line:
        return [], None
    raw = re.sub(r",?\s*J\s?J\.?\s*$", "", line)
    raw = re.sub(r",?\s*(?:ACTING\s+)?(?:C\.\s?J\.?\s?I?\.?|CJI\.?|CJ\.)", ",", raw, flags=re.I)
    parts = re.split(r",\s*|\s+AND\s*(?=[A-Z])|\s+and\s+|\bAND(?=[A-Z]\.)|\bASD\b", raw)
    out = []
    for p in parts:
        if len(re.sub(r"[^A-Za-z]", "", p)) > 3:
            name = canon_judge(p)
            if name not in out:
                out.append(name)
    return out, line


def bench_member(name: str, bench: list[str]) -> str | None:
    """Match an opinion's author line to a bench member (by surname, tolerant of OCR)."""
    import difflib
    c = canon_judge(name)
    if c in bench:
        return c
    initials = lambda x: re.sub(r"[^A-Z]", "", " ".join(w for w in x.split()[:-1] if w.endswith(".")))
    # a different judge with the same surname ("S.C. Agrawal" quoted in a B.N. Agrawal bench) is not a member
    ic = initials(c)
    bench = [b for b in bench if not (ic and initials(b) and not (ic in initials(b) or initials(b) in ic))]
    c = re.sub(r"([a-z])J\.?$", r"\1", c)  # "S.C.AGRAWALJ." OCR
    # whole-name similarity first (OCR: "Fazal Au" -> "S. Murtaza Fazal Ali", "MuDIIOLll:AR" -> "J.R. Mudholkar")
    letters = lambda x: re.sub(r"[^a-z]", "", x.lower())
    scored = sorted(((difflib.SequenceMatcher(None, letters(c), letters(b.split()[-1])).ratio(), b) for b in bench), reverse=True)
    if scored and scored[0][0] >= 0.7 and (len(scored) == 1 or scored[1][0] < scored[0][0] - 0.15):
        return scored[0][1]
    tail = [(difflib.SequenceMatcher(None, letters(c), letters(" ".join(b.split()[-2:]))).ratio(), b) for b in bench]
    tail.sort(reverse=True)
    if tail and tail[0][0] >= 0.7:
        return tail[0][1]
    sur = c.split()[-1].lower().replace(" ", "")
    best = max(bench, key=lambda b: difflib.SequenceMatcher(None, sur, b.split()[-1].lower()).ratio(), default=None)
    if best and difflib.SequenceMatcher(None, sur, best.split()[-1].lower()).ratio() >= 0.75:
        same = [b for b in bench if b.split()[-1] == best.split()[-1]]
        if len(same) == 1:
            return best
        return max(same, key=lambda b: difflib.SequenceMatcher(None, c.lower(), b.lower()).ratio())
    return None


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
    judges, bench_line = judges_from("\n".join(page.get_text() for page in doc[:2]))
    if meta.get("bench"):  # hand-read where the printed bench line is not machine-readable
        judges, bench_line = [canon_judge(j) for j in meta["bench"]], "entered by hand from the PDF (see judgments.yaml)"

    # Headnote: from the top to the jurisdiction line (or the first "delivered by" line).
    early = int(len(lines) * 0.5)
    jur = next((i for i, (_, t) in enumerate(lines[:early]) if JURISDICTION.search(t)), None)
    dlv = next((i for i, (_, t) in enumerate(lines) if i > (jur or 0) and
                (DELIVERED.search(t) or DELIVERED_AT_END.search(t))), None)
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
                "heading": ("Headnote (S.C.R. editorial)" if number == "HN" else
                            f"Opinion of {author}, J." if author else "Judgment text"),
                "opinion_author": author if number != "HN" else None,
                "path": " > ".join(x for x in [f"Opinion of {author}, J." if author else None, label] if x),
                "text": text, "source_page": part[0][0], "amended_by": [], "omitted": False,
                "statutes_cited": meta.get("statutes", []), "topics": meta.get("topics", []),
                "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
            })

    if head_end:
        add("HN", None, lines[:head_end], limit=400, target=300)

    # Numbered paragraphs, accepted in sequence (gaps of up to 6 tolerated for OCR misses);
    # restart at 1 only when a new opinion starts; quoted paragraphs ("57. ...) never count.
    author = None
    m = re.search(r"delivered by\s+([A-Z][A-Za-z.\s]+?),?\s*(C\.?\s?J\.?\s?I?\.?|J\.)", " ".join(t for _, t in lines[max(0, (dlv or 0) - 1):(dlv or 0) + 2]), re.I)
    if m:
        author = bench_member(m.group(1), judges) or None
    if not author and dlv is not None and dlv + 1 < len(lines):
        im = INLINE_AUTHOR.match(lines[dlv + 1][1])  # "MuKRERJEA J.-The facts ..." on the next line
        if im:
            author = bench_member(im.group(1), judges)
    # numbered only if paragraph numbers really run on (1, 2, ... 10+); a quoted list "1. ... 7." is not enough
    run, best = 0, 0
    for _, t in body:
        pm = PARA.match(re.sub(r"^[^\w\"'(\u201c\u2018]+", "", t))
        if pm:
            n = int(pm.group(1))
            run = n if (n == 1 or run < n <= run + 3) else run
            best = max(best, run)
    numbered = best if best >= 10 else 0
    cur_num, buf, last = None, [], 0
    new_opinion = True
    prev_last = -10  # numbering before the last restart, so a false restart (a quoted list) can be undone
    if numbered:
        for page, t in body:
            ap = AUTHOR_THEN_PARA.match(t)
            if ap and bench_member(ap.group(1), judges):
                if buf:
                    add(cur_num or "s0", author, buf)
                    buf, cur_num = [], None
                author, last, new_opinion = bench_member(ap.group(1), judges), 0, True
                t = ap.group(2)
            am = AUTHOR_LINE.match(t)
            if am and len(t) < 60 and not bench_member(am.group(1), judges):
                buf.append((page, t))  # a quoted judge (e.g. from a cited case), not a new opinion
                continue
            if am and len(t) < 60:
                if buf:
                    add(cur_num or "s0", author, buf)
                    buf, cur_num = [], None
                author, last = bench_member(am.group(1), judges), 0
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
        # unnumbered (older) judgments: a new opinion starts with "SHAH, J.-..." naming a bench member
        buf = []
        for page, t in body:
            im = INLINE_AUTHOR.match(t)
            dash = bool(im and re.search(r"J\.?\s*[-—–]", t[:im.end()]))
            who = bench_member(im.group(1), judges) if im and (dash or mostly_caps(im.group(1))) else None
            if who and buf and who != author:
                add("s1", author, buf, limit=300, target=220)
                buf = []
            if who:
                author = who
            buf.append((page, t))
        if buf:
            add("s1", author, buf, limit=300, target=220)
        # renumber segments s1, s2, ... in order
        k = 0
        for u in units:
            if u["number"] != "HN":
                k += 1
                u["number"], u["part"] = f"s{k}", None
                u["path"] = re.sub(r"segment \d+( \(part \d+\))?$", f"segment {k}", u["path"])

    pdf = (RAW / "pdf" / f"{jid}.pdf").read_bytes()
    inst = {
        "instrument_id": jid, "kind": "judgment", "short_title": meta["short"], "long_title": meta["title"],
        "neutral_citation": meta["neutral_citation"],
        "reporter_citations": [meta["scr_citation"]] + meta.get("reporter_citations", []),
        "court": "Supreme Court of India", "judges": judges, "bench_line": bench_line,
        "bench_size": len(judges) or None, "decided_on": meta["decided_on"],
        "parties": f"{meta['petitioner']} v. {meta['respondent']}", "treatment": meta.get("treatment"),
        "treatment_note": meta.get("note"), "topics": meta.get("topics", []), "statutes": meta.get("statutes", []),
        "status": "in_force", "dataset": "AWS Open Data: indian-supreme-court-judgments (S.C.R. text)",
        "source_url": meta["source_url"], "source_sha256": hashlib.sha256(pdf).hexdigest(),
        "licence_basis": "CC-BY-4.0 (dataset); judgments of courts: Copyright Act 1957 s.52(1)(q)(iv)",
        "pages": len(doc), "units": len(units), "numbered_paragraphs": bool(numbered),
    }
    return inst, units


def main(only: list[str]) -> None:
    selected = json.loads((RAW / "selected.json").read_text())
    import yaml  # curated fields (bench corrections, treatment) may change after the fetch
    curated = {j["id"]: j for j in yaml.safe_load((ROOT / "ingest" / "judgments.yaml").read_text())["judgments"]}
    selected = [{**m, **curated.get(m["id"], {})} for m in selected]
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
