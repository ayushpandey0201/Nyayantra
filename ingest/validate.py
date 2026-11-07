"""Metadata quality checks, run after every ingest. Exit code 1 if any check fails.

Checks: section numbering has no gaps; no empty units; repealed instruments name their
replacement; every unit's text is found verbatim in its source PDF (30-unit random sample,
or all units with --all); hashes match the stored text; every citation form resolves.

Usage: python -m ingest.validate [--all]
"""
from __future__ import annotations

import hashlib
import random
import re
import sys
from pathlib import Path

import psycopg
import pymupdf

from ingest.load_db import DSN

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "acts"


def squash(t: str) -> str:
    t = t.replace("’", "'").replace("“", '"').replace("”", '"').replace("[", "").replace("]", "")
    return re.sub(r"[^a-z0-9]", "", t.lower())


def main(check_all: bool) -> int:
    failures, warnings = [], []
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("select instrument_id, kind, status, repealed_by, ocr from instrument where kind <> 'judgment' order by 1")
        insts = cur.fetchall()
        for iid, kind, status, repealed_by, _ in insts:
            if status == "repealed" and not repealed_by:
                failures.append(f"{iid}: repealed without repealed_by")
            cur.execute("select number from unit where instrument_id=%s and unit_type in ('section','rule')", (iid,))
            nums = {int(re.match(r"\d+", n).group()) for (n,) in cur.fetchall()}
            gaps = sorted(set(range(1, max(nums) + 1)) - nums)
            if gaps:
                failures.append(f"{iid}: numbering gaps {gaps}")

        cur.execute("select unit_id from unit where length(text) < 10 and not omitted")
        failures += [f"{u}: empty text" for (u,) in cur.fetchall()]
        cur.execute("select unit_id, text, content_sha256 from unit")
        failures += [f"{u}: hash mismatch" for u, t, h in cur.fetchall() if hashlib.sha256(t.encode()).hexdigest() != h]
        cur.execute("select count(*) from citation_index c left join unit u using (unit_id) where c.unit_id is not null and u.unit_id is null")
        if cur.fetchone()[0]:
            failures.append("citation_index has forms pointing at missing units")

        # Text fidelity: a sentence from each sampled unit must appear in the source PDF.
        cur.execute("""select u.unit_id, u.text, i.ocr or u.unit_type = 'schedule', i.instrument_id from unit u join instrument i using (instrument_id)
                       where not u.omitted and length(u.text) > 80 and i.kind <> 'judgment'""")
        rows = cur.fetchall()
        sample = rows if check_all else random.Random(7).sample(rows, 30)
        pdf_text: dict[str, str] = {}
        missing = 0
        for uid, text, ocr, iid in sample:
            if iid not in pdf_text:
                cur.execute("select source_sha256 from instrument where instrument_id=%s", (iid,))
                pdf = next(p for p in RAW.glob("*.pdf") if p.stem == iid)
                pdf_text[iid] = squash(" ".join(
                    s["text"] for pg in pymupdf.open(pdf) for b in pg.get_text("dict")["blocks"]
                    for ln in b.get("lines", []) for s in ln["spans"]
                    if not (s["size"] < 8 and re.fullmatch(r"\s*\d+\s*\**\s*", s["text"]))))  # drop amendment markers
            words = text.split()
            # two short probes, so a section that runs across a page break (footnotes in between) still matches
            probes = [squash(" ".join(words[3:10])), squash(" ".join(words[-9:-2]))]
            if not any(p in pdf_text[iid] for p in probes):
                missing += 1
                msg = f"{uid}: text not found in source PDF: {' '.join(words[3:10])!r}"
                (warnings if ocr else failures).append(msg)  # OCR typo fixes and table schedules are not verbatim
        print(f"text fidelity: {len(sample) - missing}/{len(sample)} sampled units found verbatim in their PDF")

        # Judgments: the official headnote cites "[Para 15] [643-B-D]"; our para 15 must start on that page
        # (or the one before). In multi-opinion judgments para numbers repeat per opinion, so the reference
        # is ambiguous: those are reported, and only single-opinion judgments gate the run.
        cur.execute("select instrument_id from instrument where kind = 'judgment' order by 1")
        tally = {"single": [0, 0, 0], "multi": [0, 0, 0]}
        for (jid,) in cur.fetchall():
            cur.execute("select number, source_page, path from unit where instrument_id=%s order by sort_key", (jid,))
            rows = cur.fetchall()
            pages_of: dict[str, set[int]] = {}
            for num, page, _ in rows:
                pages_of.setdefault(num, set()).add(page)
            opinions = sum(1 for num, _, path in rows if num == "1" and "(part" not in path.replace("(part 1)", ""))
            cur.execute("select string_agg(text, ' ' order by sort_key) from unit where instrument_id=%s and number='HN'", (jid,))
            hn = cur.fetchone()[0] or ""
            refs = re.findall(r"\[\s*Paras?\s+(\d+)[^\]]{0,40}\]\s*\[\s*(\d{1,4})\s*-", hn)
            t = tally["multi" if opinions > 1 else "single"]
            for para, page in refs:
                starts = pages_of.get(para, ())
                t[0] += 1
                t[1] += any(int(page) - 1 <= p <= int(page) for p in starts)
                t[2] += any(abs(int(page) - p) <= 2 for p in starts)
        for kind, (n, exact, near) in tally.items():
            if n:
                print(f"judgment paragraphs ({kind}-opinion): headnote [Para N] [page] references match the parsed "
                      f"paragraph's page exactly in {exact}/{n} ({exact / n:.0%}), within 2 pages in {near}/{n} ({near / n:.0%})")
        n, _, near = tally["single"]
        if n and near / n < 0.9:
            failures.append(f"single-opinion paragraph alignment {near}/{n} below 90%")
        n, _, near = tally["multi"]
        if n and near / n < 0.9:
            warnings.append(f"multi-opinion paragraph alignment {near}/{n}: paragraph numbers repeat across opinions")

        # Repealed Acts: every live section needs a mapping row; report how many are verified.
        cur.execute("""select i.instrument_id, count(u.*) filter (where not u.omitted),
                              count(m.*), count(m.*) filter (where m.verified)
                       from instrument i join unit u using (instrument_id)
                       left join mapping m on m.old_unit_id = u.unit_id
                       where i.status = 'repealed' group by 1""")
        for iid, live, mapped, verified in cur.fetchall():
            print(f"mapping {iid}: {mapped}/{live} live sections mapped, {verified} verified, {mapped - verified} awaiting review")
            if mapped < live:
                failures.append(f"{iid}: {live - mapped} live sections have no mapping row")
            if verified < mapped:
                warnings.append(f"{iid}: {mapped - verified} mapping rows unverified (see docs/mapping_review.csv)")

    for w in warnings:
        print("WARN", w)
    for f in failures:
        print("FAIL", f)
    print("all checks passed" if not failures else f"{len(failures)} problems")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main("--all" in sys.argv))
