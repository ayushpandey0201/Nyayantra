"""Stage 5: write the answer from the retrieved passages only (main model, gpt-oss-120b)."""
from __future__ import annotations

from nyayantra import llm, prompts


def generate(question: str, as_of: str, hits: list[dict], notes: list[str], sensitivity: str = "public",
             repair: tuple[str, list[str]] | None = None, language: str = "en") -> dict:
    user = prompts.generate_user(question, as_of, hits, notes, language)
    if repair:
        user += "\n\n" + prompts.repair_user(*repair)
    call = llm.chat("main", prompts.GENERATE_SYSTEM, user, stage="generate" if not repair else "generate_repair",
                    sensitivity=sensitivity, max_tokens=1800, reasoning="low", prompt_version=prompts.GENERATE_V)
    parsed = llm.parse_json(call["raw"])
    if parsed is not None:
        parsed.setdefault("points", [])
        parsed.setdefault("caveats", [])
        parsed["insufficient"] = bool(parsed.get("insufficient"))
    return {"parsed": parsed, "llm": [call],
            "controls": ["Advocates Act 1961 ss.29, 33: research aid, not legal advice",
                         "Grounding: answer limited to retrieved passages, cited by unit id"]}
