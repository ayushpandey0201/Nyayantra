"""Shared fixtures.

`fake_llm` replaces only the HTTP call (llm._post), so the real routing, fallback, breaker and trace
code runs. Each request body is classified by the prompt it carries (understand, generate, policy,
classify, analyse, guard) and answered from a script. Every body sent is kept in `fake.posts`, so a
test can check exactly what would have left the machine.

Live tests (tests/test_live.py) call Groq and Gemini and run only with NYAYANTRA_LIVE=1.
"""
from __future__ import annotations

import json
import os

import pytest

from nyayantra import llm, prompts


def pytest_configure(config):
    config.addinivalue_line("markers", "live: calls the real Groq / Gemini APIs (set NYAYANTRA_LIVE=1)")


def pytest_collection_modifyitems(config, items):
    if os.environ.get("NYAYANTRA_LIVE") == "1":
        return
    skip = pytest.mark.skip(reason="live API test; run with NYAYANTRA_LIVE=1")
    for it in items:
        if "live" in it.keywords:
            it.add_marker(skip)


def kind_of(body: dict) -> str:
    sys_msg = next((m["content"] for m in body["messages"] if m["role"] == "system"), "")
    if "prompt-guard" in body["model"]:
        return "guard"
    if sys_msg.startswith(prompts.UNDERSTAND_SYSTEM[:60]):
        return "understand"
    if sys_msg.startswith(prompts.GENERATE_SYSTEM[:60]):
        return "generate"
    if sys_msg.startswith(prompts.POLICY_SYSTEM[:60]):
        return "policy"
    if sys_msg.startswith(prompts.CLAUSES_SYSTEM[:60]):
        return "classify" if "return only its types" in sys_msg else "analyse"
    return "other"


DEFAULTS = {
    "guard": "0.002",
    "understand": json.dumps({"language": "en", "english_query": "", "as_of": None, "intent": "legal_research",
                              "domains": ["contract"], "foreign_law": False, "cited_authorities": []}),
    "generate": json.dumps({"answer": "An agreement in restraint of trade is void to that extent [ICA-1872:s27].",
                            "points": [{"statement": "Restraint of trade is void.", "ids": ["ICA-1872:s27"],
                                        "quote": "is to that extent void"}],
                            "caveats": [], "insufficient": False}),
    "policy": json.dumps({"violation": False, "categories": [], "rationale": "explains the law"}),
    "classify": json.dumps({"clauses": []}),
    "analyse": json.dumps({"clauses": []}),
}


class FakeLLM:
    def __init__(self):
        self.posts: list[tuple[str, dict]] = []
        self.script: dict[str, list] = {}          # kind -> replies, used in order (last one repeats)
        self.down: dict[str, llm.LLMError] = {}     # provider -> error raised

    def reply(self, kind: str, *contents) -> None:
        self.script[kind] = list(contents)

    def __call__(self, provider: str, body: dict):
        self.posts.append((provider, body))
        if provider in self.down:
            raise self.down[provider]
        k = kind_of(body)
        queue = self.script.get(k)
        if queue:
            content = queue.pop(0) if len(queue) > 1 else queue[0]
        else:
            content = DEFAULTS.get(k, "{}")
        if callable(content):
            content = content(body)
        if isinstance(content, dict):
            content = json.dumps(content)
        data = {"id": "fake", "model": body["model"], "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                                                                    "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}
        return data, 200, 5.0

    def kinds(self) -> list[str]:
        return [kind_of(b) for _, b in self.posts]

    def sent_text(self) -> str:
        """Everything that would have been sent to a provider, as one string."""
        return "\n".join(m["content"] for _, b in self.posts for m in b["messages"])

    def providers(self) -> set[str]:
        return {p for p, _ in self.posts}


@pytest.fixture
def fake_llm(monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr(llm, "_post", fake)
    monkeypatch.setattr(llm, "_cache_get", lambda k: None)
    monkeypatch.setattr(llm, "_cache_put", lambda *a: None)
    monkeypatch.setattr(llm, "_disable_fallback", False)
    monkeypatch.setattr(llm, "_tpm_limit", {})
    monkeypatch.setattr(llm, "_breakers", {p: llm._Breaker() for p in llm.PROVIDERS})
    return fake
