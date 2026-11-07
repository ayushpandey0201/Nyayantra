"""Downloads the per-year Supreme Court judgment metadata (parquet) from the AWS Open Data
mirror (CC-BY-4.0, no account needed). Resumable: skips years already downloaded."""
import pathlib, re, time, urllib.request

BUCKET = "https://indian-supreme-court-judgments.s3.ap-south-1.amazonaws.com"
OUT = pathlib.Path("data/raw/judgments/metadata"); OUT.mkdir(parents=True, exist_ok=True)

def list_keys(prefix):
    keys, token = [], None
    while True:
        url = f"{BUCKET}/?list-type=2&prefix={prefix}" + (f"&continuation-token={urllib.parse.quote(token)}" if token else "")
        xml = urllib.request.urlopen(url, timeout=30).read().decode()
        keys += re.findall(r"<Key>([^<]+)</Key>", xml)
        m = re.search(r"<NextContinuationToken>([^<]+)</NextContinuationToken>", xml)
        if not m: return keys
        token = m.group(1)

import urllib.parse
for key in list_keys("metadata/parquet/"):
    year = re.search(r"year=(\d{4})", key).group(1)
    dest = OUT / f"{year}.parquet"
    if dest.exists(): continue
    urllib.request.urlretrieve(f"{BUCKET}/{key}", dest)
    time.sleep(0.2)
print("years:", len(list(OUT.glob("*.parquet"))), "MB:", round(sum(p.stat().st_size for p in OUT.glob("*.parquet")) / 1e6, 1))
