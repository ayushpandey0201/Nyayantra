"""Download the judgments listed in ingest/judgments.yaml from the AWS Open Data mirror.

Bucket: s3://indian-supreme-court-judgments (ap-south-1), CC-BY-4.0, public, no account needed.
One request at a time; files already on disk are skipped. Citation details (neutral citation,
S.C.R. citation, judges, decision date, disposal) come from the dataset's own metadata parquet.

Output: data/raw/judgments/pdf/<ID>.pdf and data/raw/judgments/selected.json
Usage:  python -m ingest.fetch_judgments
"""
from __future__ import annotations

import datetime as dt
import glob
import hashlib
import json
import time
from pathlib import Path

import httpx
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://indian-supreme-court-judgments.s3.ap-south-1.amazonaws.com"
OUT = ROOT / "data" / "raw" / "judgments"


def main() -> None:
    picks = yaml.safe_load((ROOT / "ingest" / "judgments.yaml").read_text())["judgments"]
    meta = pd.concat(pd.read_parquet(p) for p in glob.glob(str(OUT / "metadata" / "*.parquet")))
    meta = meta.drop_duplicates("path").set_index("path")
    (OUT / "pdf").mkdir(parents=True, exist_ok=True)
    selected = []
    with httpx.Client(timeout=120, headers={"User-Agent": "Nyayantra-research/0.1 (academic project)"}) as client:
        for p in picks:
            row = meta.loc[p["path"]]
            year = str(row["year"])
            url = f"{BASE}/data/pdf/year={year}/english/{p['path']}_EN.pdf"
            dest = OUT / "pdf" / f"{p['id']}.pdf"
            if not dest.exists():
                r = client.get(url)
                r.raise_for_status()
                dest.write_bytes(r.content)
                time.sleep(0.5)
            data = dest.read_bytes()
            judges = [j.strip().title() for j in str(row["judge"] or "").split(",") if j.strip()]
            selected.append({
                **p,
                "title": row["title"], "petitioner": row["petitioner"], "respondent": row["respondent"],
                "neutral_citation": row["case_id"], "scr_citation": row["citation"],
                "decided_on": dt.datetime.strptime(row["decision_date"], "%d-%m-%Y").date().isoformat(),
                "judges": judges, "bench_size": len(judges), "disposal": row["disposal_nature"],
                "author_judge": row["author_judge"], "source_url": url,
                "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
            })
            print(f"{p['id']:32} {row['case_id']:15} {len(data) / 1e6:5.1f} MB  {len(judges)} judges")
    (OUT / "selected.json").write_text(json.dumps(selected, indent=2, ensure_ascii=False, default=str))
    print(f"{len(selected)} judgments, {sum(s['bytes'] for s in selected) / 1e6:.0f} MB")


if __name__ == "__main__":
    main()
