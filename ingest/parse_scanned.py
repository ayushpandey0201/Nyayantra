"""Parse the scanned Bar Council of India Rules (Part VI, Chapter II) into rule units.

The only freely available copy is a scan (Bar Council of Gujarat) with an OCR text layer, so the
India Code parser cannot be used: there is no bold, and rule numbers sit in a separate column.
Lines are rebuilt from word positions, and a new rule starts only at the next expected rule number
in the left margin, which keeps stray OCR digits from creating false rules. Every unit is marked
`ocr: true` so the guardrail can show it as lower-confidence text.

Usage: python -m ingest.parse_scanned
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data" / "raw" / "acts" / "BCIR-PVI.pdf"
OUT = ROOT / "data" / "processed"
INST = "BCIR-PVI"

# Section headings of Part VI, Chapter II (Standards of Professional Conduct and Etiquette).
SECTIONS = [
    (1, "Section I - Duty to the Court"),
    (11, "Section II - Duty to the Client"),
    (34, "Section III - Duty to Opponent"),
    (36, "Section IV - Duty to Colleagues"),
    (40, "Section IV-A - Advocates' Welfare Fund"),
    (45, "Section V - Duty in Imparting Training"),
    (46, "Section VI - Duty to Render Legal Aid"),
    (47, "Section VII - Restriction on Other Employments"),
]
RULE = re.compile(r"^[\W_]{0,3}(\d{1,2}[A-Z]?)\s*\.\s+(.*)$")
NOISE = re.compile(r"^[\W_\d]{0,6}$")
FIXES = {"EtiquE!tte": "Etiquette", "breaoh": "breach", "whieh": "which", "leqally": "legally",
         "edit1egal": "edit legal", "counselor": "counsel or", "Councilor": "Council or", "decended": "descended",
         "wher:,;;by": "whereby", "~ny": "any"}


def page_rows(page):
    words = sorted(page.get_text("words"), key=lambda w: (w[1] + w[3]) / 2)
    rows = []
    for w in words:
        yc = (w[1] + w[3]) / 2
        if rows and abs(rows[-1][0] - yc) < 5:
            rows[-1][1].append(w)
        else:
            rows.append([yc, [w]])
    out = []
    for _, ws in rows:
        ws.sort(key=lambda w: w[0])
        out.append((min(w[0] for w in ws), " ".join(w[4] for w in ws)))
    return out


def fix(text: str) -> str:
    for a, b in FIXES.items():
        text = text.replace(a, b)
    text = re.sub(r"^[•.\-~_ ]+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def section_for(n: int) -> str:
    label = SECTIONS[0][1]
    for start, name in SECTIONS:
        if n >= start:
            label = name
    return label


def main():
    doc = pymupdf.open(SRC)
    rules, cur, expected = [], None, 1
    preamble = []
    for pno in range(2, len(doc) + 1):
        rows = page_rows(doc[pno - 1])
        stop = False
        for x, raw in rows:
            t = fix(raw)
            if re.search(r"CHAPTER\s*-?\s*III|PART\s*-?\s*VII", t):
                stop = True
                break
            if NOISE.match(t) or t.startswith("Section ") and len(t) < 60:
                continue
            m = RULE.match(t)
            if m and x < 150:  # the in-order check below rejects stray digits
                num = m.group(1)
                n = int(re.match(r"\d+", num).group())
                if n in (expected, expected + 1) or (num[-1].isalpha() and n == expected - 1):
                    if cur:
                        rules.append(cur)
                    cur = {"number": num, "page": pno, "paras": [m.group(2)]}
                    expected = n + 1
                    continue
            if cur is None:
                preamble.append(t)
            elif t.startswith(("(", "Explanation", "Illustration", "Provided", "Nothing", "Law Officer")):
                cur["paras"].append(t)
            else:
                cur["paras"][-1] = (cur["paras"][-1][:-1] + t) if cur["paras"][-1].endswith("-") else cur["paras"][-1] + " " + t
        if stop:
            break
    if cur:
        rules.append(cur)

    units = []
    for r in rules:
        n = int(re.match(r"\d+", r["number"]).group())
        text = "\n".join(p for p in r["paras"] if p)
        heading = re.split(r"(?<=[.;:])\s", text, maxsplit=1)[0][:120]
        units.append({
            "unit_id": f"{INST}:r{r['number']}", "instrument_id": INST, "unit_type": "rule", "number": r["number"],
            "heading": heading, "path": f"Part VI > Chapter II > {section_for(n)} > r.{r['number']}",
            "text": text, "source_page": r["page"], "amended_by": [], "omitted": False, "ocr": True,
            "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
        })
    inst = {
        "instrument_id": INST, "kind": "rules",
        "short_title": "Bar Council of India Rules, Part VI, Chapter II (Standards of Professional Conduct and Etiquette)",
        "act_number": "Rules under s.49(1)(c) of the Advocates Act, 1961", "enacted_on": None, "status": "in_force",
        "repealed_by": None, "repealed_on": None, "as_amended_upto": None, "domains": ["ethics"], "parent": "AA-1961",
        "source_file": SRC.name, "source_sha256": hashlib.sha256(SRC.read_bytes()).hexdigest(),
        "pages": len(doc), "units": len(units), "ocr": True,
        "licence_basis": "Statutory rules made under the Advocates Act, 1961; reproduced for research (Copyright Act 1957 s.52(1)(q))",
    }
    (OUT / "instruments").mkdir(parents=True, exist_ok=True)
    (OUT / "units").mkdir(parents=True, exist_ok=True)
    (OUT / "instruments" / f"{INST}.json").write_text(json.dumps(inst, indent=2, ensure_ascii=False))
    with open(OUT / "units" / f"{INST}.jsonl", "w") as f:
        for u in units:
            f.write(json.dumps(u, ensure_ascii=False) + "\n")
    print(f"{INST}: {len(units)} rules, numbers {[u['number'] for u in units]}")


if __name__ == "__main__":
    main()
