"""Stage 2: prompt-injection screening (OWASP LLM01).

Two independent signals on the already-redacted text:
  * rules  - regular expressions for instruction-override phrasing, in English and Hindi;
  * model  - Meta Llama Prompt Guard 2 (86M) on Groq, which returns the probability that the text is an
             attack. Long text is scored in windows; the highest window counts.
A request is flagged if either fires. The pipeline then does not send that text to the main model as
instructions: a flagged question is stopped, and flagged contract text is analysed only as quoted data
and routed to an advocate.
"""
from __future__ import annotations

import re

from nyayantra import llm

THRESHOLD = 0.5
WINDOW_CHARS = 1800   # Prompt Guard 2 reads up to 512 tokens

RULES = [
    r"ignore (all |any |the )?(previous|prior|above|earlier|preceding) (instructions|rules|prompts?|directions)",
    r"disregard (all |any |the |your )?(previous|prior|above|earlier)?\s*(instructions|rules|guidelines|system prompt)",
    r"\b(system|developer) (prompt|message|override|instruction)s?\b",
    r"\byou are now\b", r"\bnew instructions?\s*:", r"\bfrom now on,? (you|the assistant)\b",
    r"\bact as\b.{0,40}\b(admin|system|developer|unrestricted|jailbroken)\b", r"\bjailbreak\b|\bDAN mode\b",
    r"reveal (your|the) (system )?(prompt|instructions|rules)",
    r"(mark|treat|classify|rate|report) (this|every|each|all)( \w+){0,3} (clause|clauses|contract|term)s? as "
    r"(compliant|valid|safe|enforceable|low[- ]risk|acceptable)",
    r"(ai|assistant|model|reviewer|llm)s?,? (must|should|shall|will) (approve|ignore|not flag|skip|mark)",
    r"do not (flag|report|mention) (this|the following|any)",
    r"पिछले (सभी |सारे )?निर्देश", r"निर्देशों को (अनदेखा|नज़रअंदाज़)", r"pichhle (saare |sabhi )?(nirdesh|instructions)",
]
RULES_RX = [re.compile(p, re.I) for p in RULES]


def screen(text: str, sensitivity: str = "public", use_model: bool = True) -> dict:
    rules = [rx.pattern for rx in RULES_RX if rx.search(text)]
    calls, scores, error = [], [], None
    if use_model and text.strip():
        windows = [text[i:i + WINDOW_CHARS] for i in range(0, len(text), WINDOW_CHARS - 200)][:20]
        try:
            for w in windows:
                c = llm.chat("guard", None, w, stage="screen", sensitivity=sensitivity, json_mode=False,
                             reasoning=None, max_tokens=8, prompt_version="prompt-guard-2")
                calls.append(c)
                try:
                    scores.append(float(c["raw"].strip()))
                except ValueError:
                    scores.append(1.0)   # unparseable classifier output: fail closed
        except llm.LLMError as e:
            error = str(e)
    model_score = max(scores) if scores else None
    flagged = bool(rules) or (model_score is not None and model_score >= THRESHOLD)
    return {"flagged": flagged, "rules": rules, "model_score": model_score, "threshold": THRESHOLD,
            "windows": len(scores), "model_error": error, "llm": calls,
            "controls": ["OWASP LLM01 prompt injection: rules + Prompt Guard 2"]}
