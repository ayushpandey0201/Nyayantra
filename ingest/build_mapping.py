"""Build the repealed -> replacement provision map (IPC -> BNS, Evidence Act -> BSA) from the bare Acts.

No official correspondence table is published with the Acts, so the map is derived from the texts:
every live old section is compared with every new section and sub-section (TF-IDF cosine on the
text, plus the heading), and the closest is chosen. A row is marked `verified` only when either
  * the texts are near-identical (score >= 0.80), or
  * the match agrees with the independent anchor list in mapping_anchors.yaml.
Everything else stays unverified and is written to docs/mapping_review.csv for a person to check.

Usage: python -m ingest.build_mapping
"""
from __future__ import annotations

import csv
import datetime as dt
import re
from pathlib import Path

import psycopg
import yaml
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from ingest.load_db import DSN

ROOT = Path(__file__).resolve().parents[1]
PAIRS = [("IPC-1860", "BNS-2023", "ipc_bns"), ("IEA-1872", "BSA-2023", "iea_bsa")]
SAME, MODIFIED, WEAK = 0.80, 0.45, 0.30


def subsections(text: str) -> list[tuple[str, str]]:
    """Split '(1) ... (2) ...' at line starts; returns [(label, text)]."""
    parts = re.split(r"\n(?=\(\d+[A-Z]?\)\s)", "\n" + text)
    out = []
    for p in parts:
        m = re.match(r"\((\d+[A-Z]?)\)\s", p.strip())
        if m:
            out.append((m.group(1), p.strip()))
    return out


def norm_words(t: str) -> str:
    t = t.lower()
    t = re.sub(r"\b(sanhita|adhiniyam)\b", "code", t)  # the new Acts call themselves Sanhita/Adhiniyam
    t = re.sub(r"\bindian penal code\b|\bindian evidence act\b", "code", t)
    t = re.sub(r"\b(section|sub-section|clause)\s+\d+\w*(\(\w+\))*", " ", t)  # numbers differ by design
    return t


def main() -> None:
    anchors = yaml.safe_load((ROOT / "ingest" / "mapping_anchors.yaml").read_text())
    review_rows = []
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        for old_id, new_id, key in PAIRS:
            cur.execute("select count(*) from unit where instrument_id=%s", (old_id,))
            if not cur.fetchone()[0]:
                print(f"skip {old_id} -> {new_id}: {old_id} not loaded yet")
                continue
            cur.execute("select unit_id, number, heading, text, omitted from unit where instrument_id=%s and unit_type='section' order by sort_key", (old_id,))
            old = cur.fetchall()
            cur.execute("select unit_id, number, heading, text from unit where instrument_id=%s and unit_type='section' order by sort_key", (new_id,))
            new = cur.fetchall()
            # candidates: each new section, and each of its sub-sections
            cands = []
            for uid, num, head, text in new:
                cands.append((uid, num, None, head or "", text))
                for lab, sub in subsections(text):
                    cands.append((uid, num, lab, head or "", sub))
            live = [o for o in old if not o[4]]
            vec = TfidfVectorizer(ngram_range=(1, 2), min_df=1, sublinear_tf=True, preprocessor=norm_words)
            vec.fit([o[3] for o in live] + [c[4] for c in cands])
            sim_text = cosine_similarity(vec.transform([o[3] for o in live]), vec.transform([c[4] for c in cands]))
            hvec = TfidfVectorizer(preprocessor=norm_words).fit([o[2] or "" for o in live] + [c[3] for c in cands])
            sim_head = cosine_similarity(hvec.transform([o[2] or "" for o in live]), hvec.transform([c[3] for c in cands]))
            # short provisions ("Whoever commits theft shall be punished ...") look alike as text,
            # so their heading carries more weight
            import numpy as np
            short = np.array([len(o[3].split()) < 60 for o in live])[:, None]
            score = np.where(short, 0.5 * sim_text + 0.5 * sim_head, 0.8 * sim_text + 0.2 * sim_head)

            cur.execute("delete from mapping where old_unit_id like %s", (old_id + ":%",))
            truth = {str(k): str(v) for k, v in (anchors.get(key) or {}).items()}
            stats = {"verified": 0, "unverified": 0, "anchor_agree": 0, "anchor_total": 0}
            today = dt.date.today()
            for i, (ouid, onum, ohead, otext, _) in enumerate(live):
                j = int(score[i].argmax())
                best = float(score[i, j])
                uid, num, lab, _, _ = cands[j]
                # prefer the whole section if it scores within 0.02 of its best sub-section
                whole = next(k for k, c in enumerate(cands) if c[0] == uid and c[2] is None)
                if lab and float(score[i, whole]) >= best - 0.02:
                    lab, best = None, float(score[i, whole])
                target = num + (f"({lab})" if lab else "")
                relation = "same" if best >= SAME else ("modified" if best >= MODIFIED else None)
                anchor = truth.get(onum)
                agrees = None
                if anchor:
                    stats["anchor_total"] += 1
                    agrees = anchor != "omitted" and anchor.split("(")[0] == num
                    stats["anchor_agree"] += bool(agrees)
                if anchor == "omitted":
                    relation, uid, target = "omitted", None, None
                verified = (relation == "same") or bool(agrees and best >= WEAK) or anchor == "omitted"
                if relation is None:
                    relation = "modified"  # low similarity: substantially rewritten, or a weak guess left for review
                note = (f"BNS/BSA {target}; " if target else "") + f"text similarity {best:.2f}"
                if anchor:
                    note += f"; anchor list says {anchor}" + ("" if agrees or anchor == "omitted" else " (DISAGREES)")
                cur.execute(
                    """insert into mapping (old_unit_id, new_unit_id, relation, note, source, verified, verified_on)
                       values (%s,%s,%s,%s,%s,%s,%s) on conflict do nothing""",
                    (ouid, uid, relation, note,
                     "derived: TF-IDF comparison of both bare Acts (India Code text)" + ("; anchor list" if anchor else ""),
                     verified, today if verified else None))
                stats["verified" if verified else "unverified"] += 1
                if not verified:
                    review_rows.append([old_id, onum, ohead, new_id, target, f"{best:.2f}", anchor or "", otext[:160].replace("\n", " ")])
            # many old sections landing on one new section = merged
            cur.execute("""update mapping m set relation='merged' from (
                               select new_unit_id from mapping where old_unit_id like %s and new_unit_id is not null
                               group by 1 having count(*) > 1) d
                           where m.new_unit_id = d.new_unit_id and m.old_unit_id like %s and m.relation = 'modified'""",
                        (old_id + ":%", old_id + ":%"))
            print(f"{old_id} -> {new_id}: {len(live)} live sections mapped, {stats['verified']} verified, "
                  f"{stats['unverified']} for review; matcher agrees with anchors {stats['anchor_agree']}/{stats['anchor_total']}"
                  f" (excluding 'omitted' anchors)")
        conn.commit()
    out = ROOT / "docs" / "mapping_review.csv"
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["old_act", "old_section", "old_heading", "new_act", "best_match", "similarity", "anchor", "old_text_start"])
        w.writerows(review_rows)
    print(f"{len(review_rows)} rows for manual review -> {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
