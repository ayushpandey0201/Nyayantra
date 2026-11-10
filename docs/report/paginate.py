"""Reads the rendered PDF and records the printed page number of every heading, table and figure (for the contents tables).
  .venv/bin/python docs/report/paginate.py report.pdf"""
import json
import pathlib
import re
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
pdf = sys.argv[1]
n = int(re.search(r"Pages:\s+(\d+)", subprocess.run(["pdfinfo", pdf], capture_output=True, text=True).stdout).group(1))
txt = [re.sub(r"\s+", " ", subprocess.run(["pdftotext", "-f", str(i), "-l", str(i), "-layout", pdf, "-"], capture_output=True, text=True).stdout)
       for i in range(1, n + 1)]
ix = json.loads((HERE / "index.json").read_text())
c1 = next(i for i, t in enumerate(txt) if "Chapter 1" in t and "Introduction" in t and "1.1 Background" in t and "CHAPTER NO" not in t)
P = {}
for a, b, lvl in ix["heads"]:
    key = (f"Chapter {a} " + b) if lvl == 1 else f"{a} {b}"
    hit = next((i for i in range(c1, n) if (re.match(rf"\s*Chapter {a}\b", txt[i]) if lvl == 1 else key[:44] in txt[i])), None)
    if hit is None:
        print("MISS", key)
    else:
        P[a] = hit - c1 + 1
for k, cap in ix["tables"] + ix["figs"]:
    hit = next((i for i in range(c1, n) if k + ":" in txt[i]), None)
    if hit is None:
        print("MISS", k)
    else:
        P[k] = hit - c1 + 1
P["REFERENCES"] = next(i for i in range(n - 1, c1, -1) if "REFERENCES" in txt[i]) - c1 + 1
F = {}
for name, key in (("ABSTRACT", "ABSTRACT"), ("DECLARATION", "DECLARATION"), ("ACKNOWLEDGEMENTS", "ACKNOWLEDGEMENT"),
                  ("LIST OF TABLES", "LIST OF TABLES"), ("LIST OF FIGURES", "LIST OF FIGURES")):
    i = next((i for i in range(2, c1) if key in txt[i] and "PAGE NO" not in txt[i][:300]), None)
    F[name] = (i - 1) if i is not None else ""
P["_front"] = F
(HERE / "pages.json").write_text(json.dumps(P))
print("front pages", c1, "main pages", n - c1, F, "references", P["REFERENCES"])
