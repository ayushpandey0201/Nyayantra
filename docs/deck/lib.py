"""Slide primitives that reproduce the BMSCE template: same master, fonts, tag pills, cards and tables."""
import copy
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LABEL_POSITION, XL_LEGEND_POSITION
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Inches as I, Pt
from lxml import etree

def rgb(h): return RGBColor.from_string(h)
NAVY, BLUE, GREEN, AMBER, RED, PURPLE, GREY, INK = "1F3A6E", "1F4E99", "1E6B3C", "B9770E", "C0504D", "5B3FA0", "6B7385", "222222"
TINT, LINE, CODE, CODEFG = "F3F6FB", "D0D7E5", "1B2536", "D7E1F2"
GRN_T, AMB_T, RED_T, PUR_T, BLU_T = "E6F2EA", "FDF3DC", "FBE9E8", "EEE9F8", "E3ECF9"
TAGS = {"CONTEXT": BLUE, "DESIGN": BLUE, "IMPLEMENTATION": PURPLE, "LIVE TRACE": AMBER, "TESTING": RED, "COMPLIANCE": GREEN, "CLOSE": NAVY, "ROLLOUT": GREEN, "THE STORY": BLUE, "API ROUTES": PURPLE, "PIPELINE": PURPLE, "PROOF": AMBER, "TESTS": RED, "THE END": NAVY}

class Deck:
    def __init__(self, template, date):
        self.date = date
        self.p = Presentation(template)
        ids = self.p.slides._sldIdLst
        for s in list(ids):
            self.p.part.drop_rel(s.rId); ids.remove(s)
        self.L = {l.name: l for l in self.p.slide_layouts}
        for sh in self.L["CONTENT"].shapes:
            if sh.has_text_frame and "31 October 2026" in sh.text_frame.text:
                for r in sh.text_frame.paragraphs[0].runs:
                    r.text = date
        self.n = 0

    def blank(self, layout="CONTENT", notes=None):
        s = self.p.slides.add_slide(self.L[layout]); self.n += 1
        for ph in list(s.placeholders):
            if ph.placeholder_format.type is not None and "SLIDE_NUMBER" not in str(ph.placeholder_format.type):
                ph._element.getparent().remove(ph._element)
        if notes:
            s.notes_slide.notes_text_frame.text = notes
        return s

    def slide(self, title, tag, notes=None):
        s = self.blank("CONTENT", notes)
        text(s, title, 1.4, .32, 9.5, .8, sz=28 if len(title) > 44 else 30, b=True, color=NAVY, font="Times New Roman", align="c", anchor="m", name="Slide title")
        c = TAGS[tag]
        box(s, 11.05, .5, 1.7, .34, fill=c, line=c, radius=.5, name="Section tag")
        text(s, tag, 11.05, .5, 1.7, .34, sz=9.5 if len(tag) > 10 else 10, b=True, color="FFFFFF", align="c", anchor="m")
        tb = text(s, "", 11.9, 7.0, .83, .3, sz=11, color=GREY, align="r", name="Slide number")
        p = tb.text_frame.paragraphs[0]._p
        for r in p.findall(qn("a:r")): p.remove(r)
        fld = etree.SubElement(p, qn("a:fld")); fld.set("id", "{B6F15528-21DE-4FAA-801E-634DDDAF4B2B}"); fld.set("type", "slidenum")
        rp = etree.SubElement(fld, qn("a:rPr")); rp.set("lang", "en-US"); rp.set("sz", "1100")
        sf = etree.SubElement(rp, qn("a:solidFill")); c2 = etree.SubElement(sf, qn("a:srgbClr")); c2.set("val", GREY)
        tt = etree.SubElement(fld, qn("a:t")); tt.text = str(self.n)
        return s

def box(s, x, y, w, h, fill=None, line=LINE, radius=.06, lw=1, name=None, shape=None):
    sh = s.shapes.add_shape(shape or MSO_SHAPE.ROUNDED_RECTANGLE, I(x), I(y), I(w), I(h))
    if shape is None:
        sh.adjustments[0] = radius if radius <= .5 else .5
    if fill: sh.fill.solid(); sh.fill.fore_color.rgb = rgb(fill)
    else: sh.fill.background()
    if line: sh.line.color.rgb = rgb(line); sh.line.width = Pt(lw)
    else: sh.line.fill.background()
    sh.shadow.inherit = False
    if name: sh.name = name
    return sh

def _run(p, t, sz, b, color, font, i=False):
    r = p.add_run(); r.text = t; f = r.font; f.size = Pt(sz); f.bold = b; f.italic = i
    if color: f.color.rgb = rgb(color)
    if font: f.name = font
    return r

def text(s, content, x, y, w, h, sz=14, b=False, color=INK, font=None, align="l", anchor="t", name=None, sp=0, ls=None):
    """content: str | list of paragraphs. A paragraph is str or list of (text, opts) runs."""
    tb = s.shapes.add_textbox(I(x), I(y), I(w), I(h)); tf = tb.text_frame; tf.word_wrap = True
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    tf.vertical_anchor = {"t": MSO_ANCHOR.TOP, "m": MSO_ANCHOR.MIDDLE, "b": MSO_ANCHOR.BOTTOM}[anchor]
    paras = content if isinstance(content, list) else [content]
    for n, para in enumerate(paras):
        p = tf.paragraphs[0] if n == 0 else tf.add_paragraph()
        p.alignment = {"l": PP_ALIGN.LEFT, "c": PP_ALIGN.CENTER, "r": PP_ALIGN.RIGHT}[align]
        if sp: p.space_after = Pt(sp)
        if ls: p.line_spacing = ls
        for run in ([(para, {})] if isinstance(para, str) else para):
            t, o = run
            _run(p, t, o.get("sz", sz), o.get("b", b), o.get("color", color), o.get("font", font), o.get("i", False))
    if name: tb.name = name
    return tb

def bullets(s, items, x, y, w, h, sz=14, color=INK, sp=5):
    tb = s.shapes.add_textbox(I(x), I(y), I(w), I(h)); tf = tb.text_frame; tf.word_wrap = True
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    for n, it in enumerate(items):
        p = tf.paragraphs[0] if n == 0 else tf.add_paragraph()
        p.space_after = Pt(sp)
        pPr = p._p.get_or_add_pPr(); pPr.set("marL", "171450"); pPr.set("indent", "-171450")
        bu = etree.SubElement(pPr, qn("a:buChar")); bu.set("char", "•")
        for t, o in ([(it, {})] if isinstance(it, str) else it):
            _run(p, t, o.get("sz", sz), o.get("b", False), o.get("color", color), None)
    return tb

def card(s, x, y, w, h, title, body, color=BLUE, fill=None, tsz=15, bsz=13):
    box(s, x, y, w, h, fill=fill, line=color, lw=1)
    text(s, title, x + .16, y + .1, w - .32, .32, sz=tsz, b=True, color=color)
    if isinstance(body, list):
        bullets(s, body, x + .16, y + .45, w - .3, h - .5, sz=bsz, sp=3)
    else:
        text(s, body, x + .16, y + .45, w - .32, h - .5, sz=bsz)

def code(s, x, y, w, h, lines, sz=11.5, title=None):
    """lines: str or (str, color)."""
    box(s, x, y, w, h, fill=CODE, line=CODE, radius=.03, name="Code card")
    top = y + .13
    if title:
        text(s, title, x + .18, top, w - .36, .25, sz=10.5, b=True, color="F2C14E"); top += .3
    paras = []
    for l in lines:
        t, c = (l, CODEFG) if isinstance(l, str) else l
        paras.append([(t if t else " ", {"color": c})])
    text(s, paras, x + .18, top, w - .36, h - (top - y) - .08, sz=sz, font="Courier New")

def table(s, x, y, w, rows, widths, sz=11, hsz=None, rowh=.3, hfill=BLUE, cellfill=None, bold_first=True, name="Table"):
    nr, nc = len(rows), len(rows[0])
    gs = s.shapes.add_table(nr, nc, I(x), I(y), I(w), I(rowh * nr)); gs.name = name; t = gs.table
    tblPr = t._tbl.tblPr
    for k in ("firstRow", "bandRow"): tblPr.set(k, "0")
    st = tblPr.find(qn("a:tableStyleId"))
    if st is not None: tblPr.remove(st)
    tot = sum(widths)
    for j, wd in enumerate(widths): t.columns[j].width = I(w * wd / tot)
    for i in range(nr):
        t.rows[i].height = I(rowh)
        for j in range(nc):
            c = t.cell(i, j); v = rows[i][j]; c.margin_left = c.margin_right = I(.07); c.margin_top = c.margin_bottom = I(.025)
            c.vertical_anchor = MSO_ANCHOR.MIDDLE
            tf = c.text_frame; tf.word_wrap = True; p = tf.paragraphs[0]
            fill, fg, bold = None, INK, False
            if i == 0: fill, fg, bold = hfill, "FFFFFF", True
            else:
                fill = TINT if i % 2 == 0 else "FFFFFF"
                if j == 0 and bold_first: bold = True
                if cellfill:
                    o = cellfill(i, j, v)
                    if o: fill, fg = o[0], o[1]; bold = bold or (len(o) > 2 and o[2])
            _run(p, str(v), (hsz or sz) if i == 0 else sz, bold, fg, None)
            c.fill.solid(); c.fill.fore_color.rgb = rgb(fill)
            tcPr = c._tc.get_or_add_tcPr()
            for tag in ("a:lnL", "a:lnR", "a:lnT", "a:lnB"):
                ln = etree.SubElement(tcPr, qn(tag)); ln.set("w", "9525")
                sf = etree.SubElement(ln, qn("a:solidFill")); cl = etree.SubElement(sf, qn("a:srgbClr")); cl.set("val", LINE)
            # schema order: borders must precede the fill
            fills = [e for e in tcPr if e.tag == qn("a:solidFill")]
            for e in fills: tcPr.remove(e); tcPr.append(e)
    return gs

def node(s, x, y, w, h, title, sub=None, color=BLUE, fill=None, tsz=11.5, ssz=9.5):
    box(s, x, y, w, h, fill=fill or "FFFFFF", line=color, lw=1.25, radius=.1)
    paras = [[(title, {"b": True, "color": color, "sz": tsz})]]
    if sub: paras.append([(sub, {"color": "444B5A", "sz": ssz})])
    text(s, paras, x + .05, y + .03, w - .1, h - .06, align="c", anchor="m")

def arrow(s, x1, y1, x2, y2, color=NAVY, lw=1.5, dash=False):
    c = s.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, I(x1), I(y1), I(x2), I(y2))
    c.line.color.rgb = rgb(color); c.line.width = Pt(lw)
    ln = c.line._get_or_add_ln()
    if dash:
        d = etree.SubElement(ln, qn("a:prstDash")); d.set("val", "dash")
    t = etree.SubElement(ln, qn("a:tailEnd")); t.set("type", "triangle"); t.set("w", "med"); t.set("len", "med")
    return c

def banner(s, msg, y=6.2, h=.55, x=.6, w=12.13, sz=15, fill=NAVY):
    box(s, x, y, w, h, fill=fill, line=fill, radius=.12, name="Key message")
    text(s, msg, x + .25, y, w - .5, h, sz=sz, b=True, color="FFFFFF", anchor="m")

def stat(s, x, y, w, h, big, small, color=BLUE, bsz=26):
    box(s, x, y, w, h, line=color, lw=1)
    text(s, big, x + .16, y + .1, w - .3, .55, sz=bsz, b=True, color=color, font="Times New Roman")
    text(s, small, x + .16, y + .1 + .55, w - .3, h - .7, sz=12.5)

def bar(s, x, y, w, h, title, cats, series, colors, pct=True, horizontal=False, legend=None, maxv=None, sz=11, fmt=None, gap=60):
    cd = CategoryChartData(); cd.categories = cats
    for n, vals in series: cd.add_series(n, vals)
    tp = XL_CHART_TYPE.BAR_CLUSTERED if horizontal else XL_CHART_TYPE.COLUMN_CLUSTERED
    ch = s.shapes.add_chart(tp, I(x), I(y), I(w), I(h), cd).chart
    ch.has_title = True; ch.chart_title.text_frame.text = title
    r = ch.chart_title.text_frame.paragraphs[0].runs[0]; r.font.size = Pt(13); r.font.bold = True; r.font.color.rgb = rgb(NAVY)
    ch.font.size = Pt(sz); ch.font.color.rgb = rgb("444B5A")
    ch.has_legend = bool(legend if legend is not None else len(series) > 1)
    if ch.has_legend:
        ch.legend.position = XL_LEGEND_POSITION.BOTTOM; ch.legend.include_in_layout = False; ch.legend.font.size = Pt(sz)
    va, ca = ch.value_axis, ch.category_axis
    va.has_major_gridlines = True; va.major_gridlines.format.line.color.rgb = rgb("E4E8F0"); va.format.line.fill.background()
    va.minimum_scale = 0
    if maxv: va.maximum_scale = maxv
    va.tick_labels.font.size = Pt(sz); ca.tick_labels.font.size = Pt(sz); ca.format.line.color.rgb = rgb(LINE)
    if horizontal: ca.reverse_order = True
    pl = ch.plots[0]; pl.gap_width = gap; pl.overlap = -5 if len(series) > 1 else 0
    pl.has_data_labels = True; dl = pl.data_labels; dl.show_value = True; dl.position = XL_LABEL_POSITION.OUTSIDE_END
    dl.font.size = Pt(sz); dl.font.bold = True; dl.number_format = fmt or ('0.0"%"' if pct else "0"); dl.number_format_is_linked = False
    for i, ser in enumerate(pl.series):
        c = colors[i] if len(series) > 1 else colors[0]
        ser.format.fill.solid(); ser.format.fill.fore_color.rgb = rgb(c)
        if len(series) == 1 and len(colors) > 1:
            for j in range(len(cats)):
                pt = ser.points[j]; pt.format.fill.solid(); pt.format.fill.fore_color.rgb = rgb(colors[j])
    return ch
