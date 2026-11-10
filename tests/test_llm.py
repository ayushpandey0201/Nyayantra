"""Provider contract tests: the request bodies Nyayantra sends to Groq and Gemini (OpenAI-compatible
chat completions), how their responses and error bodies are read, retries, the circuit breaker and
the token budget. HTTP is mocked; tests/test_live.py checks the same things against the real APIs."""
import httpx
import pytest

from nyayantra import llm

GROQ_OK = {  # shape of a Groq chat completion for gpt-oss (fields Nyayantra reads, plus extras it must ignore)
    "id": "chatcmpl-x", "object": "chat.completion", "created": 1, "model": "openai/gpt-oss-120b",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": '{"answer": "ok"}'}, "logprobs": None, "finish_reason": "stop"}],
    "usage": {"queue_time": 0.01, "prompt_tokens": 812, "prompt_time": 0.02, "completion_tokens": 140, "completion_time": 0.2,
              "total_tokens": 952, "completion_tokens_details": {"reasoning_tokens": 61}},
    "system_fingerprint": "fp_x", "x_groq": {"id": "req_x"},
}


# ---------- request bodies ----------
def test_groq_body_for_gpt_oss():
    b = llm._body("groq", "openai/gpt-oss-120b", "sys", "user", True, 900, "low")
    assert b["messages"] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "user"}]
    assert b["temperature"] == 0 and b["seed"] == 7
    assert b["max_completion_tokens"] == 900 and "max_tokens" not in b
    assert b["reasoning_effort"] == "low" and b["include_reasoning"] is False
    assert b["response_format"] == {"type": "json_object"}


def test_groq_body_for_prompt_guard_has_no_system_or_reasoning():
    b = llm._body("groq", "meta-llama/llama-prompt-guard-2-86m", None, "text", False, 8, None)
    assert b["messages"] == [{"role": "user", "content": "text"}]
    assert "reasoning_effort" not in b and "response_format" not in b


def test_reasoning_effort_only_sent_to_models_that_take_it():
    assert "reasoning_effort" not in llm._body("groq", "meta-llama/llama-3.3-70b", "s", "u", False, 10, "low")


def test_gemini_body_uses_max_tokens():
    b = llm._body("gemini", "gemini-3.5-flash", "s", "u", True, 500, "low")
    assert b["max_tokens"] == 500 and "max_completion_tokens" not in b and "seed" not in b


def test_every_role_has_a_groq_model():
    assert all("groq" in m for m in llm.MODELS.values())
    assert set(llm.MODELS["guard"]) == {"groq"} and set(llm.MODELS["safeguard"]) == {"groq"}


# ---------- HTTP: responses, errors, retries ----------
class Transport:
    """Stands in for httpx.post; returns queued (status, json, headers) responses and records requests."""
    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []

    def __call__(self, url, json=None, headers=None, timeout=None):
        self.requests.append({"url": url, "json": json, "headers": headers})
        status, body, hdrs = self.responses.pop(0)
        return httpx.Response(status, json=body, headers=hdrs, request=httpx.Request("POST", url))


@pytest.fixture
def transport(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-groq")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini")
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    monkeypatch.setattr(llm, "_tpm_limit", {})

    def make(*responses):
        t = Transport(*responses)
        monkeypatch.setattr(llm.httpx, "post", t)
        return t
    return make


def test_post_reads_groq_response_and_learns_token_limit(transport):
    t = transport((200, GROQ_OK, {"x-ratelimit-limit-tokens": "8000"}))
    data, status, ms = llm._post("groq", {"model": "openai/gpt-oss-120b", "messages": []})
    assert status == 200 and data["choices"][0]["message"]["content"] == '{"answer": "ok"}'
    assert t.requests[0]["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert t.requests[0]["headers"]["Authorization"] == "Bearer test-groq"
    assert llm._tpm_limit["openai/gpt-oss-120b"] == 8000


def test_chat_record_fields(monkeypatch, transport):
    transport((200, GROQ_OK, {}))
    monkeypatch.setattr(llm, "_cache_get", lambda k: None)
    monkeypatch.setattr(llm, "_cache_put", lambda *a: None)
    monkeypatch.setattr(llm, "_breakers", {p: llm._Breaker() for p in llm.PROVIDERS})
    r = llm.chat("main", "sys", "user", stage="t", prompt_version="v1")
    assert r["raw"] == '{"answer": "ok"}' and r["finish_reason"] == "stop"
    assert (r["prompt_tokens"], r["completion_tokens"], r["reasoning_tokens"]) == (812, 140, 61)
    assert r["provider"] == "groq" and r["model"] == "openai/gpt-oss-120b" and r["prompt_version"] == "v1"
    assert r["system"] == "sys" and r["user"] == "user" and not r["cached"] and r["cache_key"]
    assert "test-groq" not in str(r)       # the key is never in the trace


def test_short_rate_limit_is_retried_on_same_provider(transport):
    t = transport((429, {"error": {"message": "rate limit"}}, {"retry-after": "2"}), (200, GROQ_OK, {}))
    data, status, _ = llm._post("groq", {"model": "m", "messages": []})
    assert status == 200 and len(t.requests) == 2


def test_long_rate_limit_is_raised_as_retryable(transport):
    transport((429, {"error": {"message": "tokens per day"}}, {"retry-after": "3600"}))
    with pytest.raises(llm.LLMError) as e:
        llm._post("groq", {"model": "m", "messages": []})
    assert e.value.status == 429 and e.value.retryable and "tokens per day" in str(e.value)


def test_groq_400_error_body_not_retryable(transport):
    transport((400, {"error": {"message": "response_format json_object requires the word json", "type": "invalid_request_error"}}, {}))
    with pytest.raises(llm.LLMError) as e:
        llm._post("groq", {"model": "m", "messages": []})
    assert not e.value.retryable and "requires the word json" in str(e.value)


def test_gemini_error_body_is_a_list(transport):
    transport((503, [{"error": {"code": 503, "message": "The model is overloaded.", "status": "UNAVAILABLE"}}], {}))
    with pytest.raises(llm.LLMError) as e:
        llm._post("gemini", {"model": "gemini-3.5-flash", "messages": []})
    assert e.value.retryable and "overloaded" in str(e.value)


def test_transport_error_is_retryable(monkeypatch, transport):
    def boom(*a, **kw):
        raise httpx.ConnectTimeout("timeout")
    monkeypatch.setattr(llm.httpx, "post", boom)
    with pytest.raises(llm.LLMError) as e:
        llm._post("groq", {"model": "m", "messages": []})
    assert e.value.retryable


def test_missing_key_is_an_error_not_a_crash(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(llm.LLMError):
        llm._key("groq")


@pytest.mark.parametrize("value, seconds", [("2", 2.0), ("7.66s", 7.66), ("350ms", 0.35), ("1m2.5s", 62.5)])
def test_retry_after_formats(value, seconds):
    r = httpx.Response(429, headers={"x-ratelimit-reset-tokens": value})
    assert llm._retry_after(r) == pytest.approx(seconds)


# ---------- routing, breaker, budget, cache ----------
def test_breaker_opens_after_three_failures(fake_llm):
    fake_llm.down["groq"] = llm.LLMError("groq http 503", status=503, retryable=True)
    for _ in range(3):
        llm.chat("main", "s", "u", stage="t", sensitivity="public")      # each falls back to gemini
    assert not llm._breakers["groq"].ok()
    n = len(fake_llm.posts)
    r = llm.chat("main", "s", "u", stage="t", sensitivity="public")
    assert r["provider"] == "gemini" and len(fake_llm.posts) == n + 1   # groq skipped while open
    assert "circuit open" in r["errors"][0]


def test_confidential_request_waits_for_groq_even_with_open_breaker(fake_llm):
    llm._breakers["groq"].open_until = 1e12
    llm.chat("main", "s", "u", stage="t", sensitivity="confidential")
    assert fake_llm.providers() == {"groq"}


def test_oversized_request_refused_before_sending(fake_llm):
    llm._tpm_limit["openai/gpt-oss-120b"] = 1000
    with pytest.raises(llm.LLMUnavailable) as e:
        llm.chat("main", "s", "x" * 10_000, stage="t", sensitivity="confidential")
    assert e.value.status == 413 and fake_llm.posts == []


def test_throttle_waits_when_minute_budget_is_spent(monkeypatch):
    slept = []
    monkeypatch.setattr(llm.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(llm, "_tpm_limit", {"m": 1000})
    now = llm.time.time()
    monkeypatch.setattr(llm, "_spent", {"m": [(now - 50, 900)]})
    clock = iter([now, now + 11])
    monkeypatch.setattr(llm.time, "time", lambda: next(clock, now + 11))
    assert llm._throttle("m", 200) > 0 and slept and slept[0] == pytest.approx(10.5, abs=0.1)


def test_cache_hit_sends_nothing(monkeypatch, fake_llm):
    monkeypatch.setattr(llm, "_cache_get", lambda k: {"raw": "{}", "prompt_tokens": 1, "completion_tokens": 1})
    r = llm.chat("main", "s", "u", stage="t")
    assert r["cached"] and fake_llm.posts == []


@pytest.mark.parametrize("text, ok", [
    ('{"a": 1}', True), ('Here you go:\n```json\n{"a": 1}\n```', True), ("[1, 2]", False), ("not json", False),
    ("", False), (None, False), ('{"a": 1', False),
])
def test_parse_json(text, ok):
    assert (llm.parse_json(text) is not None) is ok
