"""Builds docs/Nyayantra_AAT_Report.docx on the BMSCE report template.

  .venv/bin/python docs/report/build_report.py                 # 1st pass
  (export the .docx to PDF)                                     # Pages or Word
  .venv/bin/python docs/report/paginate.py report.pdf           # page numbers for the contents tables
  .venv/bin/python docs/report/build_report.py                 # 2nd pass with page numbers

The front pages (cover, certificate, declaration, acknowledgement, contents) come from our v3 report and
are retitled; the body is generated from content.py. Adapted from the course's worked example builder.
"""
import copy
import json
import pathlib
import re
import sys

from docx import Document
from docx.oxml import parse_xml
from docx.oxml.ns import qn
from docx.shared import Inches
from PIL import Image

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import content  # noqa: E402

TPL = ROOT / "docs" / "reference" / "legal_services_v1" / "AAT_Report_LLMs_Legal_Services_BMSCE_v3.docx"
OUT = ROOT / "docs" / "Nyayantra_AAT_Report.docx"
OLD_TITLE = "LLMs for Legal Research and Contract Analysis: Societal, Ethical, Legal and Compliance Evaluation"
TITLE = "LLMs for Legal Research and Contract Analysis: Societal, Ethical, Legal and Compliance Evaluation of Nyayantra, a Working System"
FIG = HERE / "figures"
PAGES = json.loads((HERE / "pages.json").read_text()) if (HERE / "pages.json").exists() else {}

d = Document(str(TPL))
body = d.element.body
kids = list(body)
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
RF = '<w:rFonts w:ascii="Times New Roman" w:cs="Times New Roman" w:eastAsia="Times New Roman" w:hAnsi="Times New Roman"/>'


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def run(t, sz=24, b=False, font=None, it=False):
    f = font or "Times New Roman"
    return (f'<w:r><w:rPr><w:rFonts w:ascii="{f}" w:cs="{f}" w:eastAsia="{f}" w:hAnsi="{f}"/>' + ("<w:b/><w:bCs/>" if b else "")
            + ("<w:i/><w:iCs/>" if it else "") + f'<w:sz w:val="{sz}"/><w:szCs w:val="{sz}"/></w:rPr><w:t xml:space="preserve">{esc(t)}</w:t></w:r>')


def para(ppr, runs):
    return parse_xml(f'<w:p xmlns:w="{W}"><w:pPr>{ppr}</w:pPr>{runs}</w:p>')


# ---------- references: v3's list plus new sources, numbered in order of first use ----------
tp = [p.text for p in d.paragraphs if re.match(r"\[\d+\]\t", p.text)]
old = {int(re.match(r"\[(\d+)\]", t).group(1)): t.split("\t", 1)[1] for t in tp}
R = {"transformer": old[1], "gpt3": old[2], "bert": old[3], "instruct": old[4], "gpt4": old[5], "llama": old[6], "legalbert": old[7],
     "rag": old[8], "lora": old[9], "qlora": old[10], "legalbench": old[11], "cuad": old[12], "dahl": old[13], "magesh": old[14],
     "ji": old[15], "mata": old[16], "puttaswamy": old[17], "ieee7000": old[18], "gdpr": old[19], "euai": old[20], "iso42001": old[21],
     "iso23894": old[22], "nist": old[23], "oecd": old[24], "unesco": old[25], "dpdp": old[26], "owasp": old[27], "statutes": old[28],
     "rrf": 'G. V. Cormack, C. L. A. Clarke, and S. Buettcher, "Reciprocal rank fusion outperforms Condorcet and individual rank learning methods," in Proc. 32nd Int. ACM SIGIR Conf., Boston, MA, USA, 2009, pp. 758-759.',
     "bgem3": 'J. Chen, S. Xiao, P. Zhang, K. Luo, D. Lian, and Z. Liu, "BGE M3-Embedding: Multi-lingual, multi-functionality, multi-granularity text embeddings through self-knowledge distillation," arXiv:2402.03216, 2024.',
     "gptoss": 'OpenAI, "gpt-oss-120b and gpt-oss-20b model card," 2025.',
     "safeguard": 'OpenAI, "gpt-oss-safeguard technical report," 2025.',
     "promptguard": 'Meta, "Llama Prompt Guard 2 model card," 2025.',
     "pgvector": 'pgvector: open-source vector similarity search for PostgreSQL, https://github.com/pgvector/pgvector.',
     "dpdprules": "Government of India, Digital Personal Data Protection Rules, 2025, notified Nov. 2025.",
     "bns": "Government of India, The Bharatiya Nyaya Sanhita, 2023 (Act No. 45 of 2023), in force from Jul. 1, 2024.",
     "bsa": "Government of India, The Bharatiya Sakshya Adhiniyam, 2023 (Act No. 47 of 2023), in force from Jul. 1, 2024.",
     "itact": "Government of India, The Information Technology Act, 2000.",
     "copyright": "Government of India, The Copyright Act, 1957, s. 52(1)(q).",
     "indiacode": "Legislative Department, Ministry of Law and Justice, Government of India, India Code: Digital Repository of All Central and State Acts.",
     "dattam": "Dattam Labs, Indian Supreme Court Judgments dataset, Registry of Open Data on AWS, licensed CC-BY-4.0.",
     "groq": "Groq, GroqCloud API documentation (OpenAI-compatible endpoint), 2025.",
     "qwen": 'Qwen Team, "Qwen3 technical report," arXiv:2505.09388, 2025.',
     "judge": 'L. Zheng et al., "Judging LLM-as-a-judge with MT-Bench and Chatbot Arena," in Advances in Neural Information Processing Systems 36, Datasets and Benchmarks Track, 2023.',
     "verhoeff": 'J. Verhoeff, "Error detecting decimal codes," Mathematical Centre Tracts, vol. 29, Mathematisch Centrum, Amsterdam, 1969.'}
order = []


def cite(t):
    def f(m):
        k = m.group(1)
        if k not in order:
            order.append(k)
        return f"[{order.index(k) + 1}]"
    return re.sub(r"\[\[(\w+)\]\]", f, str(t))


class Doc:
    def __init__(s):
        s.out, s.ch, s.t, s.f, s.tables, s.figs, s.heads = [], 0, 0, 0, [], [], []

    def add(s, e):
        s.out.append(e)

    def H1(s, a, b):
        s.ch = int(a.split()[1]); s.t = s.f = 0; s.heads.append((a.split()[1], b, 1))
        s.add(parse_xml(f'<w:p xmlns:w="{W}"><w:pPr><w:pStyle w:val="Heading1"/><w:pageBreakBefore/><w:spacing w:after="360"/><w:jc w:val="center"/></w:pPr>'
                        + run(a, 36) + f'<w:r><w:rPr>{RF}<w:sz w:val="44"/><w:szCs w:val="44"/></w:rPr><w:br/><w:t xml:space="preserve">{esc(b)}</w:t></w:r></w:p>'))

    def H2(s, t):
        s.heads.append((t.split()[0], t.split(" ", 1)[1], 2))
        s.add(para('<w:pStyle w:val="Heading2"/><w:keepNext/><w:spacing w:after="120" w:before="240"/>', run(t, 32, True)))

    def H3(s, t):
        m = re.match(r"(\d+\.\d+\.\d+) (.*)", t)
        if m:
            s.heads.append((m.group(1), m.group(2), 3))
        s.add(para('<w:pStyle w:val="Heading3"/><w:keepNext/><w:spacing w:after="120" w:before="240"/>', run(t, 28, True)))

    def P(s, t):
        s.add(para('<w:spacing w:after="100" w:line="360"/><w:jc w:val="both"/>', run(cite(t))))

    def L(s, a, b):
        s.add(para('<w:spacing w:after="100" w:line="360"/><w:jc w:val="both"/>', run(cite(a) + " ", 24, True) + run(cite(b))))

    def B(s, items):
        for a, b in items:
            r = (run(cite(a), 24, True) + run(" " + cite(b))) if a else run(cite(b))
            s.add(para('<w:pStyle w:val="ListParagraph"/><w:numPr><w:ilvl w:val="0"/><w:numId w:val="3"/></w:numPr><w:spacing w:after="40" w:line="360"/><w:jc w:val="both"/>', r))

    def CODE(s, text):
        lines = []
        for raw in text.split("\n"):
            while len(raw) > 86:
                c = raw.rfind(" ", 0, 86); c = c if c > 0 else 86
                lines.append(raw[:c]); raw = raw[c:].lstrip()
            lines.append(raw)
        for n, l in enumerate(lines):
            s.add(para('<w:keepLines/><w:shd w:val="clear" w:color="auto" w:fill="F2F2F2"/><w:spacing w:before="0" w:after="%d" w:line="228"/>'
                       % (160 if n == len(lines) - 1 else 0), run(l, 18, font="Courier New")))

    def T(s, cap, rows, widths, sz=22):
        s.t += 1; n = f"{s.ch}.{s.t}"; s.tables.append((f"Table {n}", cap))
        s.add(para('<w:keepNext/><w:spacing w:after="80" w:before="160"/><w:jc w:val="center"/>', run(f"Table {n}:", 24, True) + run(" " + cap)))
        bd = "".join(f'<w:{e} w:val="single" w:color="000000" w:sz="4"/>' for e in ("top", "left", "bottom", "right"))
        mar = '<w:tcMar><w:top w:type="dxa" w:w="40"/><w:left w:type="dxa" w:w="90"/><w:bottom w:type="dxa" w:w="40"/><w:right w:type="dxa" w:w="90"/></w:tcMar>'
        x = (f'<w:tbl xmlns:w="{W}"><w:tblPr><w:tblW w:type="dxa" w:w="{sum(widths)}"/><w:tblBorders>'
             + "".join(f'<w:{e} w:val="single" w:color="auto" w:sz="4"/>' for e in ("top", "left", "bottom", "right", "insideH", "insideV"))
             + '</w:tblBorders><w:tblLayout w:type="fixed"/></w:tblPr><w:tblGrid>' + "".join(f'<w:gridCol w:w="{w}"/>' for w in widths) + "</w:tblGrid>")
        for i, row in enumerate(rows):
            x += "<w:tr><w:trPr><w:cantSplit/>" + ("<w:tblHeader/>" if i == 0 else "") + "</w:trPr>"
            for w, c in zip(widths, row):
                sh = '<w:shd w:fill="D9E2F3" w:color="auto" w:val="clear"/>' if i == 0 else ""
                keep = "<w:keepNext/>" if i < len(rows) - 1 and len(rows) < 12 else ""
                x += (f'<w:tc><w:tcPr><w:tcW w:type="dxa" w:w="{w}"/><w:tcBorders>{bd}</w:tcBorders>{sh}{mar}<w:vAlign w:val="top"/></w:tcPr>'
                      f'<w:p><w:pPr>{keep}<w:spacing w:after="0" w:line="252"/><w:jc w:val="left"/></w:pPr>{run(cite(c), sz, i == 0)}</w:p></w:tc>')
            x += "</w:tr>"
        s.add(parse_xml(x + "</w:tbl>"))
        s.add(para('<w:spacing w:after="120"/>', ""))

    def F(s, name, cap, width):
        s.f += 1; n = f"{s.ch}.{s.f}"; s.figs.append((f"Figure {n}", cap))
        p = d.add_paragraph(); r = p.add_run(); w, h = Image.open(FIG / name).size
        width = min(width, 8.2 * w / h)
        r.add_picture(str(FIG / name), width=Inches(width))
        for dp in p._p.iter("{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}docPr"):
            dp.set("descr", cap); dp.set("title", cap)
        e = p._p; e.getparent().remove(e)
        pPr = e.get_or_add_pPr()
        for x in parse_xml(f'<w:pPr xmlns:w="{W}"><w:keepNext/><w:spacing w:after="60" w:before="120"/><w:jc w:val="center"/></w:pPr>'):
            pPr.append(x)
        s.add(e)
        s.add(para('<w:spacing w:after="200"/><w:jc w:val="center"/>', run(f"Figure {n}:", 24, True) + run(" " + cap)))


D = Doc()
content.chapters(D)

# ---------- front matter ----------
def sub_runs(p_el, fn):
    for t in p_el.iter(qn("w:t")):
        if t.text:
            t.text = fn(t.text)


for e in kids[:35]:
    sub_runs(e, lambda s: s.replace(OLD_TITLE, TITLE))
ti = [e for e in kids[:35] if e.tag == qn("w:tbl")]
for r in ti[0].iter(qn("w:r")):     # the cover title is longer, so reduce it to keep the cover on one page
    tx = "".join(t.text or "" for t in r.findall(qn("w:t")))
    if TITLE in tx:
        for z in r.iter(qn("w:sz"), qn("w:szCs")):
            z.set(qn("w:val"), "30")
ps = [e for e in kids[:35] if e.tag == qn("w:p")]


def settext(start, new):
    for e in ps:
        tx = "".join(t.text or "" for t in e.iter(qn("w:t")))
        if tx.startswith(start):
            ts = list(e.iter(qn("w:t"))); ts[0].text = new
            for t in ts[1:]:
                t.text = ""
            return
    raise SystemExit("missing " + start)


for start, new in content.ABSTRACT:
    settext(start, new)


def clone_rows(tbl, rows, hdr=1):
    tr = tbl.findall(qn("w:tr")); proto = tr[hdr]
    for x in tr[hdr:]:
        tbl.remove(x)
    for vals in rows:
        n = copy.deepcopy(proto); cells = n.findall(qn("w:tc"))
        for c, v in zip(cells, vals):
            ts = list(c.iter(qn("w:t")))
            if ts:
                ts[0].text = str(v)
                for t in ts[1:]:
                    t.text = ""
        tbl.append(n)


pg = lambda k: PAGES.get(k, "")  # noqa: E731
FP = PAGES.get("_front", {})
clone_rows(ti[3], [[n, FP.get(n, "")] for n in ("ABSTRACT", "DECLARATION", "ACKNOWLEDGEMENTS", "LIST OF TABLES", "LIST OF FIGURES")])
clone_rows(ti[4], [[a, b, pg(a)] for a, b, l in D.heads] + [["", "References", pg("REFERENCES")]])
clone_rows(ti[5], [[a, b, pg(a)] for a, b in D.tables])
clone_rows(ti[6], [[a, b, pg(a)] for a, b in D.figs])

# ---------- body ----------
for e in kids[35:]:
    if e.tag != qn("w:sectPr"):
        body.remove(e)
final = body.find(qn("w:sectPr"))
for e in D.out:
    final.addprevious(e)
final.addprevious(parse_xml(f'<w:p xmlns:w="{W}"><w:pPr><w:pStyle w:val="Heading1"/><w:pageBreakBefore/><w:spacing w:after="360"/><w:jc w:val="center"/></w:pPr>'
                            + run("REFERENCES", 44) + "</w:p>"))
for i, k in enumerate(order, 1):
    final.addprevious(para('<w:tabs><w:tab w:val="left" w:pos="640"/></w:tabs><w:spacing w:after="120" w:line="276"/><w:ind w:left="640" w:hanging="640"/>',
                           run(f"[{i}]\t" + R[k]).replace("\t", '</w:t><w:tab/><w:t xml:space="preserve">')))
d.core_properties.title = "Nyayantra: LLMs for Legal Research and Contract Analysis"
d.core_properties.author = "Ayush Pandey, Sharanya Sandur"
d.core_properties.subject = "AAT Report 26AI7PELLM"
d.save(str(OUT))
(HERE / "index.json").write_text(json.dumps({"heads": D.heads, "tables": D.tables, "figs": D.figs}, ensure_ascii=False))
print(OUT.name, "tables", len(D.tables), "figs", len(D.figs), "refs", len(order), "unused", [k for k in R if k not in order])
