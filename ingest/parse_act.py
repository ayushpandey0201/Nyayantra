"""Split an India Code PDF (Act or Rules) into units: one per section, rule or schedule.

The parser uses layout signals from the PDF rather than guessing from plain text:
  * section and rule headings are bold and start with a number ("27. Agreement in restraint ...—");
  * amendment footnotes sit below a short horizontal rule in a smaller font;
  * amendment markers in the text are tiny (7 pt) digits just before "[" or "*";
  * "IndiaCode" is a 30 pt watermark on every page.

Output: data/processed/units/<ID>.jsonl (one unit per line) and data/processed/instruments/<ID>.json.

Usage:  python -m ingest.parse_act            # every act/rules entry in sources.yaml that has a file
        python -m ingest.parse_act ICA-1872   # one instrument
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf
import yaml

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "acts"
OUT = ROOT / "data" / "processed"

DASHES = re.compile(r"\s*(?:––|—|―|–|-{2})\s*")
SEC_START = re.compile(r"^(\d{1,3}[A-Z]{0,3})(?:\.|\s+(?=\"))\s*(.*)$")
CHAPTER = re.compile(r"^(PART|CHAPTER)\s+([IVXLC]+[A-Z]?|\d+[A-Z]?)\b\.?\s*(.*)$", re.I)
SCHEDULE = re.compile(r"^(?:THE\s+)?((?:FIRST|SECOND|THIRD|FOURTH|FIFTH|SIXTH|SEVENTH|EIGHTH|NINTH|TENTH)\s+)?SCHEDULE\b", re.I)
FOOTNOTE = re.compile(r"^(\d+)\.\s+(.*)$")
WEF = re.compile(r"w\.?\s*e\.?\s*f\.?\s*(\d{1,2})-(\d{1,2})-(\d{4})")
ENACTED = re.compile(r"^\s*(\d{1,2})(?:st|nd|rd|th)?\s+([A-Z][a-z]+),?\s+(\d{4})\s*\.?\s*$")
MONTHS = {m: i for i, m in enumerate(
    "January February March April May June July August September October November December".split(), 1)}
ORDINAL = {w: i for i, w in enumerate("FIRST SECOND THIRD FOURTH FIFTH SIXTH SEVENTH EIGHTH NINTH TENTH".split(), 1)}


@dataclass
class Line:
    page: int
    x: float
    y: float
    text: str
    bold: bool
    italic: bool
    size: float
    markers: list[str] = field(default_factory=list)


def clean(text: str) -> str:
    text = text.replace(" ", " ").replace("’", "'").replace("‘", "'")
    text = text.replace("“", '"').replace("”", '"')
    text = re.sub(r"\*(\s*\*)+\.?", " ", text)  # omitted-text asterisks
    text = text.replace("[", "").replace("]", "")
    return re.sub(r"[ \t]+", " ", text).strip()


def page_lines(page: pymupdf.Page, pno: int) -> tuple[list[Line], list[str]]:
    """Body lines and footnote lines for one page."""
    sep_y = None
    for d in page.get_drawings():
        r = d["rect"]
        if r.height < 2 and 40 < r.width < 260 and r.y0 > page.rect.height * 0.35:
            sep_y = r.y0 if sep_y is None else min(sep_y, r.y0)
    body, notes = [], []
    for block in page.get_text("dict")["blocks"]:
        for ln in block.get("lines", []):
            spans = [s for s in ln["spans"] if s["size"] < 20]  # drop the watermark
            if not "".join(s["text"] for s in spans).strip():
                continue
            y = ln["bbox"][1]
            if sep_y is not None and y > sep_y:
                notes.append(clean("".join(s["text"] for s in spans)))
                continue
            text, markers = "", []
            for s in spans:
                if s["size"] < 8 and re.fullmatch(r"\s*\d+\s*\**\s*", s["text"]):
                    markers.append(re.sub(r"\D", "", s["text"]))
                    continue
                text += s["text"]
            text = clean(text)
            if not text:
                continue
            if re.fullmatch(r"\d{1,3}", text) and (y < 70 or y > page.rect.height - 70):
                continue  # page number
            if y < 80 and re.search(r"GAZETTE OF INDIA|PART II\W+SEC|भारत का|भाग II", text):
                continue  # Gazette running header
            real = [s for s in spans if s["text"].strip() and not (s["size"] < 8 and re.fullmatch(r"\s*\d+\s*\**\s*", s["text"]))]
            first = next((s for s in real if re.search(r"\w", s["text"])), real[0])
            # bold if most of the first 25 visible characters are bold ("1[16. Heading" has a plain "[")
            chars = [(c, bool(s["flags"] & 16)) for s in real for c in s["text"] if not c.isspace()][:25]
            bold = bool(first["flags"] & 16) or sum(b for _, b in chars) >= 0.6 * max(1, len(chars))
            body.append(Line(pno, ln["bbox"][0], y, text, bold,
                             bool(first["flags"] & 2) or "Italic" in first["font"] or "-It" in first["font"],
                             first["size"], markers))
    body.sort(key=lambda l: (round(l.y), l.x))
    merged: list[Line] = []
    for l in body:  # PyMuPDF sometimes splits one printed line into several
        if (merged and abs(merged[-1].y - l.y) < 2.5 and merged[-1].page == l.page
                and not (l.bold and SEC_START.match(l.text))):
            merged[-1].text = clean(merged[-1].text + " " + l.text)
            merged[-1].markers += l.markers
        else:
            merged.append(l)
    return merged, notes


def parse_footnotes(lines: list[str]) -> dict[str, str]:
    out, cur = {}, None
    for t in lines:
        m = FOOTNOTE.match(t)
        if m:
            cur = m.group(1)
            out[cur] = m.group(2)
        elif cur:
            out[cur] += " " + t
    return out


def split_heading(text: str) -> tuple[str, str]:
    m = DASHES.search(text)
    if m and m.start() < 400:
        return text[: m.start()].rstrip(" .:"), text[m.end():].strip()
    m = re.search(r"\.\s", text)
    if m and m.start() < 200:
        return text[: m.start()], text[m.end():].strip()
    return text.rstrip(". "), ""


def to_date(d, m, y) -> str | None:
    try:
        return dt.date(int(y), int(m) if str(m).isdigit() else MONTHS[m], int(d)).isoformat()
    except (KeyError, ValueError):
        return None


def parse(entry: dict, kind: str) -> tuple[dict, list[dict]]:
    path = RAW / entry["file"]
    pdf_bytes = path.read_bytes()
    doc = pymupdf.open(path)
    inst_id = entry["id"]
    prefix = "r" if kind == "rules" else "s"
    unit_word = "rule" if kind == "rules" else "section"

    lines, notes = [], {}
    for pno, page in enumerate(doc, 1):
        b, n = page_lines(page, pno)
        lines += b
        for k, v in parse_footnotes(n).items():
            notes[(pno, k)] = v

    # The body starts at the first bold "1." heading; everything before is the table of contents or Hindi text.
    start = next(i for i, l in enumerate(lines) if l.bold and re.match(r"^1\.\s*\S", l.text))
    preamble = lines[max(0, start - 25):start]
    enacted = None
    for l in preamble:
        m = ENACTED.search(l.text)
        if m:
            enacted = to_date(*m.groups())
    lines = lines[start:]
    left = min(l.x for l in lines if not l.bold) if lines else 72

    units: list[dict] = []
    chapter = part = group = None
    schedule_no = 0
    in_schedule = False
    cur: dict | None = None

    def flush():
        nonlocal cur
        if cur:
            cur["text"] = "\n".join(p.strip() for p in cur.pop("_paras") if p.strip())
            units.append(cur)
        cur = None

    def new_unit(utype, number, heading, first_text, line):
        nonlocal cur
        flush()
        crumbs = [c for c in (part, chapter, group) if c]
        if utype == "schedule":
            uid = f"{inst_id}:sch{number}"
            label = heading
        else:
            uid = f"{inst_id}:{prefix}{number}"
            label = f"{'r' if prefix == 'r' else 's'}.{number}"
        cur = {
            "unit_id": uid, "instrument_id": inst_id, "unit_type": utype, "number": number,
            "heading": heading, "path": " > ".join(crumbs + [label]),
            "_paras": [first_text] if first_text else [], "source_page": line.page,
            "markers": [(line.page, m) for m in line.markers],
        }

    i = 0
    while i < len(lines):
        l = lines[i]
        t = l.text
        nxt = lines[i + 1] if i + 1 < len(lines) else None

        sch = SCHEDULE.match(t)
        if sch and (l.x > left + 60 or t.isupper()) and len(t) < 80 and (not cur or cur["unit_type"] != "schedule" or t.isupper()):
            schedule_no += 1
            in_schedule = True
            word = (sch.group(1) or "").strip().upper()
            num = ORDINAL.get(word, schedule_no)
            title = t.title()
            if nxt and nxt.text.startswith("("):  # "(See section 2)"
                i += 1
            if i + 1 < len(lines) and lines[i + 1].text.isupper() and len(lines[i + 1].text) < 120:
                title += " - " + lines[i + 1].text.title()
                i += 1
            new_unit("schedule", str(num), title, "", l)
            i += 1
            continue

        ch = CHAPTER.match(t)
        if ch and not in_schedule and (l.bold or t.isupper()) and len(t) < 60:
            title = ch.group(3).strip()
            j = i + 1
            while not title and j < len(lines) and lines[j].text.isupper() and not SEC_START.match(lines[j].text):
                title = lines[j].text
                j += 1
            label = f"{ch.group(1).title()} {ch.group(2).upper()}" + (f" {title.title()}" if title else "")
            if ch.group(1).upper() == "PART":
                part, chapter = label, None
            else:
                chapter = label
            group = None
            i = j
            continue

        sm = SEC_START.match(t)
        if sm and l.bold and not in_schedule:
            # bold heading text may wrap onto the next bold line(s)
            head = t
            while (DASHES.search(head) is None and nxt and nxt.bold and not SEC_START.match(nxt.text)
                   and len(head) < 400):
                head += " " + nxt.text
                l.markers += nxt.markers
                i += 1
                nxt = lines[i + 1] if i + 1 < len(lines) else None
            num = sm.group(1)
            heading, body = split_heading(SEC_START.match(head).group(2))
            new_unit(unit_word, num, heading, body, l)
            i += 1
            continue

        # a short centred line just before a section heading is a group heading ("Void agreements")
        if (not in_schedule and nxt and nxt.bold and SEC_START.match(nxt.text) and l.x > left + 60
                and len(t) < 90 and not t.endswith((".", ";", ":", ","))):
            group = t
            i += 1
            continue

        if cur is not None:
            indented = l.x > left + 8
            starts_para = indented or re.match(r"^(\(\w{1,4}\)|Explanation|Illustration|Provided|Exception|Note)", t)
            if starts_para or not cur["_paras"]:
                cur["_paras"].append(t)
            else:
                prev = cur["_paras"][-1]
                cur["_paras"][-1] = (prev[:-1] + t) if prev.endswith("-") and not prev.endswith(" -") else prev + " " + t
            cur["markers"] += [(l.page, m) for m in l.markers]
        i += 1
    flush()

    # Attach amendment footnotes, de-duplicate unit ids (e.g. two "Explanation" headings parsed as sections)
    seen: dict[str, int] = {}
    max_wef = None
    for u in units:
        amendments = []
        for page, m in u.pop("markers"):
            note = notes.get((page, m))
            if note:
                w = WEF.search(note)
                on = to_date(*w.groups()) if w else None
                amendments.append({"note": note, "on": on})
                if on and (max_wef is None or on > max_wef):
                    max_wef = on
        u["amended_by"] = amendments
        if u["unit_id"] in seen:
            seen[u["unit_id"]] += 1
            u["unit_id"] += f"~{seen[u['unit_id']]}"
        else:
            seen[u["unit_id"]] = 1
        head_and_text = (u["heading"] or "") + " " + u["text"][:80]
        omitted = len(u["text"]) < 120 and re.search(r"\b(Omitted|Repealed|Rep\.)", head_and_text, re.I)
        u["omitted"] = bool(omitted)
        u["content_sha256"] = hashlib.sha256(u["text"].encode()).hexdigest()

    instrument = {
        "instrument_id": inst_id, "kind": "rules" if kind == "rules" else "act",
        "short_title": entry["title"], "act_number": entry.get("number"),
        "enacted_on": enacted, "status": entry.get("status", "in_force"),
        "repealed_by": entry.get("repealed_by"), "repealed_on": str(entry["repealed_on"]) if entry.get("repealed_on") else None,
        "as_amended_upto": max_wef, "domains": entry.get("domains", []), "parent": entry.get("parent"),
        "source_file": entry["file"], "source_sha256": hashlib.sha256(pdf_bytes).hexdigest(),
        "pages": len(doc), "units": len(units),
        "licence_basis": "Copyright Act 1957 s.52(1)(q): reproduction of Acts and Rules published by Government",
    }
    return instrument, units


def main(only: list[str]) -> None:
    src = yaml.safe_load((ROOT / "ingest" / "sources.yaml").read_text())
    entries = [(e, "act") for e in src["acts"]] + [(e, "rules") for e in src["rules"]]
    (OUT / "units").mkdir(parents=True, exist_ok=True)
    (OUT / "instruments").mkdir(parents=True, exist_ok=True)
    for entry, kind in entries:
        if only and entry["id"] not in only:
            continue
        if entry.get("parser") == "scanned":
            continue  # handled by ingest/parse_scanned.py
        if not (RAW / entry["file"]).exists():
            print(f"skip {entry['id']:11} (no file yet)")
            continue
        inst, units = parse(entry, kind)
        (OUT / "instruments" / f"{inst['instrument_id']}.json").write_text(json.dumps(inst, indent=2, ensure_ascii=False))
        with open(OUT / "units" / f"{inst['instrument_id']}.jsonl", "w") as f:
            for u in units:
                f.write(json.dumps(u, ensure_ascii=False) + "\n")
        kinds = {}
        for u in units:
            kinds[u["unit_type"]] = kinds.get(u["unit_type"], 0) + 1
        print(f"{inst['instrument_id']:11} pages={inst['pages']:4} units={len(units):4} {kinds} enacted={inst['enacted_on']} amended_upto={inst['as_amended_upto']}")


if __name__ == "__main__":
    main(sys.argv[1:])
