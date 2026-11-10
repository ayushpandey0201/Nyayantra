"""Stage 1: redact client data before anything leaves the machine.

Personal identifiers are replaced by numbered placeholders ([PHONE_1], [AADHAAR_1] ...). The map back
to the real values stays in memory with the caller (to show the user their own answer) and is never
stored, logged, cached or sent to an LLM. DPDP Act 2023 s.8(5) (reasonable security safeguards) and
the advocate's duty of confidentiality (BSA 2023 s.132; BCI Rules r.17) are the reasons.

Legal citations are deliberately left alone: '(2017) 10 SCC 1', 's.27', years and paragraph numbers.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Verhoeff tables: Aadhaar numbers carry a Verhoeff check digit (UIDAI), which rules out most
# 12-digit strings that are not Aadhaar numbers.
_D = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5], [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
      [3, 4, 0, 1, 2, 8, 9, 5, 6, 7], [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
      [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3], [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
      [9, 8, 7, 6, 5, 4, 3, 2, 1, 0]]
_P = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4], [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
      [8, 9, 1, 6, 0, 4, 3, 5, 2, 7], [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
      [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8]]


def verhoeff_ok(num: str) -> bool:
    c = 0
    for i, ch in enumerate(reversed(num)):
        c = _D[c][_P[i % 8][int(ch)]]
    return c == 0


# (label, pattern, validator). Order matters: longer / more specific identifiers first.
RULES: list[tuple[str, re.Pattern, object]] = [
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"), None),
    ("GSTIN", re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b"), None),
    ("PAN", re.compile(r"\b[A-Z]{3}[ABCFGHLJPTK][A-Z]\d{4}[A-Z]\b"), None),
    ("AADHAAR", re.compile(r"(?<![\d/])[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}(?![\d/])"),
     lambda m: verhoeff_ok(re.sub(r"\D", "", m))),
    ("IFSC", re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b"), None),
    ("BANK_ACCOUNT", re.compile(r"(?i)\b(?:a/c|account|acct)\.?(?:\s*(?:no\.?|number))?[\s:.#-]{1,3}(?P<v>\d{9,18})\b"), None),
    ("CARD", re.compile(r"(?<!\d)(?:\d{4}[ -]){3}\d{4}(?!\d)"), None),
    ("PHONE", re.compile(r"(?<![\d(])(?:\+91[ -]?|0)?[6-9]\d{4}[ -]?\d{5}(?![\d)])"), None),
    ("PASSPORT", re.compile(r"\b[A-PR-WY][1-9]\d\s?\d{4}[1-9]\b"), None),
    ("VOTER_ID", re.compile(r"\b[A-Z]{3}\d{7}\b"), None),
]


@dataclass
class Redaction:
    text: str                                   # what may leave the machine
    found: list[dict] = field(default_factory=list)   # [{"type", "count"}] for the trace (no values)
    mapping: dict[str, str] = field(default_factory=dict)  # placeholder -> original; caller memory only

    @property
    def sensitive(self) -> bool:
        return bool(self.found)

    def restore(self, s: str) -> str:
        for ph, orig in self.mapping.items():
            s = s.replace(ph, orig)
        return s


def redact(text: str, extra: dict[str, list[str]] | None = None) -> Redaction:
    """`extra` adds exact strings to redact, e.g. {"PARTY": ["Acme Pvt Ltd"]} from a contract's parties."""
    out = text
    counts: dict[str, int] = {}
    mapping: dict[str, str] = {}
    seen: dict[str, str] = {}

    def place(label: str, value: str) -> str:
        key = re.sub(r"\s+", " ", value).lower()      # the same name across a line break is the same party
        if key in seen:
            return seen[key]
        counts[label] = counts.get(label, 0) + 1
        ph = f"[{label}_{counts[label]}]"
        seen[key], mapping[ph] = ph, value
        return ph

    for label, values in (extra or {}).items():
        for v in sorted(set(values), key=len, reverse=True):
            if v.strip():
                pat = r"\s+".join(re.escape(w) for w in v.split())
                out = re.sub(rf"(?<!\w){pat}(?!\w)", lambda m: place(label, m.group()), out, flags=re.I)
    def sub(label, valid):
        def f(m):
            g = "v" if "v" in m.re.groupindex else 0
            v = m.group(g)
            if valid is not None and not valid(v):
                return m.group()
            return m.group()[:m.start(g) - m.start()] + place(label, v) + m.group()[m.end(g) - m.start():]
        return f

    for label, rx, valid in RULES:
        out = rx.sub(sub(label, valid), out)
    return Redaction(out, [{"type": k, "count": v} for k, v in counts.items()], mapping)
