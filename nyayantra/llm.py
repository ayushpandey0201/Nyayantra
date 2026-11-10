"""One OpenAI-compatible client for two providers.

  * Groq is primary for every role. Google AI Studio (Gemini) is the fallback, and only for PUBLIC
    requests: its free tier may use prompts to improve Google products and allows human review, so
    anything derived from a client's document (sensitivity="confidential") never goes there
    (DPDP Act s.8(5); BSA s.132; OWASP LLM02). A confidential request with Groq down fails, and the
    pipeline routes it to an advocate instead.
  * Fallback only on 429, 5xx, timeouts and transport errors, behind a small circuit breaker.
  * Every response is cached in Postgres (llm_cache) by a hash of provider, model, messages and
    parameters, so re-runs and the benchmark replay instead of spending the free-tier quota.
  * Every call returns a record for the trace: provider, model, endpoint, exact prompts, raw reply,
    tokens, latency, cache hit, fallback.
API keys come from .env and are never logged or returned.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass

import httpx
import psycopg
from psycopg.types.json import Jsonb

from nyayantra.config import DSN

PROVIDERS = {
    "groq": {"base": "https://api.groq.com/openai/v1", "key": "GROQ_API_KEY"},
    "gemini": {"base": "https://generativelanguage.googleapis.com/v1beta/openai", "key": "GEMINI_API_KEY"},
}
# role -> provider -> model. Gemini models are pinned versions (not *-latest) so results reproduce.
MODELS = {
    "main":      {"groq": "openai/gpt-oss-120b", "gemini": "gemini-3.5-flash"},
    "helper":    {"groq": "openai/gpt-oss-20b", "gemini": "gemini-3.5-flash-lite"},
    "guard":     {"groq": "meta-llama/llama-prompt-guard-2-86m"},
    "safeguard": {"groq": "openai/gpt-oss-safeguard-20b"},
}
TIMEOUT_S = 60
RETRYABLE = {408, 409, 429, 500, 502, 503, 504}


class LLMError(RuntimeError):
    def __init__(self, msg: str, status: int | None = None, retryable: bool = False):
        super().__init__(msg)
        self.status, self.retryable = status, retryable


class LLMUnavailable(LLMError):
    """No permitted provider could answer."""


@dataclass
class _Breaker:
    failures: int = 0
    open_until: float = 0.0

    def ok(self) -> bool:
        return time.time() >= self.open_until

    def record(self, success: bool) -> None:
        if success:
            self.failures = 0
            return
        self.failures += 1
        if self.failures >= 3:
            self.open_until = time.time() + 60


_breakers = {p: _Breaker() for p in PROVIDERS}

# Client-side token budget per model, learned from Groq's x-ratelimit-limit-tokens header: wait before
# a call that would exceed the per-minute limit, instead of being refused with 429/413.
_tpm_limit: dict[str, int] = {}
_spent: dict[str, list[tuple[float, int]]] = {}


def _estimate(body: dict) -> int:
    chars = sum(len(m["content"]) for m in body["messages"])
    return int(chars / 3.2) + int(body.get("max_completion_tokens") or body.get("max_tokens") or 0)


def _throttle(model: str, need: int) -> float:
    limit = _tpm_limit.get(model)
    if not limit:
        return 0.0
    waited = 0.0
    while True:
        now = time.time()
        window = [(t, n) for t, n in _spent.get(model, []) if now - t < 60]
        _spent[model] = window
        used = sum(n for _, n in window)
        if used + need <= limit or not window:
            return waited
        sleep = 60 - (now - window[0][0]) + 0.5
        time.sleep(sleep)
        waited += sleep
_disable_fallback = os.environ.get("NYAYANTRA_NO_FALLBACK") == "1"   # benchmark: one provider per config


def _key(provider: str) -> str:
    k = os.environ.get(PROVIDERS[provider]["key"])
    if not k:
        raise LLMError(f"{PROVIDERS[provider]['key']} is not set", retryable=True)
    return k


def _body(provider: str, model: str, system: str | None, user: str, json_mode: bool, max_tokens: int,
          reasoning: str | None) -> dict:
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": user}]
    body: dict = {"model": model, "messages": msgs, "temperature": 0}
    if provider == "groq":
        body["seed"] = 7
        body["max_completion_tokens"] = max_tokens
        if reasoning and model.startswith("openai/gpt-oss"):
            body["reasoning_effort"] = reasoning
            body["include_reasoning"] = False
    else:
        body["max_tokens"] = max_tokens
        if reasoning:
            body["reasoning_effort"] = reasoning
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    return body


def _cache_key(provider: str, body: dict) -> str:
    return hashlib.sha256(json.dumps({"provider": provider, **body}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _cache_get(key: str) -> dict | None:
    try:
        with psycopg.connect(DSN) as conn:
            row = conn.execute("select response from llm_cache where cache_key=%s", (key,)).fetchone()
        return row[0] if row else None
    except psycopg.Error:
        return None


def _cache_put(key: str, provider: str, model: str, body: dict, resp: dict) -> None:
    try:
        with psycopg.connect(DSN) as conn:
            conn.execute("insert into llm_cache (cache_key, provider, model, request, response) values (%s,%s,%s,%s,%s) "
                         "on conflict (cache_key) do nothing", (key, provider, model, Jsonb(body), Jsonb(resp)))
    except psycopg.Error:
        pass


def _post(provider: str, body: dict) -> tuple[dict, int, float]:
    url = f"{PROVIDERS[provider]['base']}/chat/completions"
    for attempt in range(3):
        t0 = time.perf_counter()
        try:
            r = httpx.post(url, json=body, headers={"Authorization": f"Bearer {_key(provider)}"}, timeout=TIMEOUT_S)
        except httpx.HTTPError as e:
            raise LLMError(f"{provider} transport: {type(e).__name__}", retryable=True) from e
        ms = (time.perf_counter() - t0) * 1000
        lim = r.headers.get("x-ratelimit-limit-tokens")
        if lim and lim.isdigit():
            _tpm_limit[body["model"]] = int(lim)
        if r.status_code == 200:
            return r.json(), r.status_code, ms
        # a short rate-limit wait (Groq's token window resets in seconds) is cheaper than switching provider
        wait = _retry_after(r)
        if r.status_code == 429 and attempt < 2 and wait is not None and wait <= 30:
            time.sleep(wait + 0.5)
            continue
        detail = ""
        try:
            err = r.json()
            err = err[0] if isinstance(err, list) and err else err
            detail = str((err.get("error") or {}).get("message", ""))[:200] if isinstance(err, dict) else ""
        except ValueError:
            pass
        raise LLMError(f"{provider} http {r.status_code}: {detail}", status=r.status_code,
                       retryable=r.status_code in RETRYABLE)
    raise LLMError(f"{provider} rate limited", status=429, retryable=True)


def _retry_after(r: httpx.Response) -> float | None:
    for h in ("retry-after", "x-ratelimit-reset-tokens", "x-ratelimit-reset-requests"):
        v = r.headers.get(h)
        if not v:
            continue
        m = re.fullmatch(r"(?:(\d+)m)?([\d.]+)(ms|s)?", v.strip())
        if m:
            mins, num, unit = m.groups()
            secs = float(num) / (1000 if unit == "ms" else 1) + 60 * int(mins or 0)
            return secs
    return None


def chat(role: str, system: str | None, user: str, *, stage: str, sensitivity: str = "public",
         json_mode: bool = True, max_tokens: int = 1200, reasoning: str | None = "low",
         prompt_version: str | None = None, use_cache: bool = True, provider: str | None = None) -> dict:
    """Call the model for `role`. Returns the call record. Raises LLMUnavailable if no permitted provider answers."""
    if provider:
        # an explicit provider is honoured exactly (never silently swapped), but confidential text never goes to Gemini
        if sensitivity != "public" and provider != "groq":
            raise LLMUnavailable(f"{stage}: a confidential request may not be sent to {provider}")
        if provider not in MODELS[role]:
            raise LLMUnavailable(f"{stage}: no {role} model configured on {provider}")
        order = [provider]
    else:
        order = ["groq"] if sensitivity != "public" or _disable_fallback else ["groq", "gemini"]
        order = [p for p in order if p in MODELS[role]]
    errors, fallback_from = [], None
    for prov in order:
        if not _breakers[prov].ok() and prov != order[-1]:
            errors.append(f"{prov}: circuit open")
            fallback_from = fallback_from or prov
            continue
        model = MODELS[role][prov]
        body = _body(prov, model, system, user, json_mode, max_tokens, reasoning)
        key = _cache_key(prov, body)
        rec = {"stage": stage, "role": role, "provider": prov, "model": model,
               "endpoint": f"{PROVIDERS[prov]['base']}/chat/completions", "prompt_version": prompt_version,
               "sensitivity": sensitivity, "system": system, "user": user, "params": {k: v for k, v in body.items() if k not in ("messages",)},
               "fallback_from": fallback_from, "errors": list(errors), "cache_key": key}
        cached = _cache_get(key) if use_cache else None
        if cached:
            return {**rec, **cached, "cached": True, "latency_ms": 0.0}
        need = _estimate(body)
        if prov == "groq" and _tpm_limit.get(model) and need > _tpm_limit[model]:
            errors.append(f"groq: request of ~{need} tokens exceeds the {_tpm_limit[model]} tokens/minute limit")
            raise LLMUnavailable(f"{stage}: request too large for {model}", 413)
        rec["throttled_s"] = round(_throttle(model, need), 1) if prov == "groq" else 0.0
        try:
            try:
                data, status, ms = _post(prov, body)
            except LLMError as e:
                # Groq refuses a json_object reply that is not valid JSON (HTTP 400). Ask once more without
                # JSON mode and let parse_json() read the reply; the caller's schema checks still apply.
                if not (e.status == 400 and "validate json" in str(e).lower() and "response_format" in body):
                    raise
                body = {k: v for k, v in body.items() if k != "response_format"}
                key = _cache_key(prov, body)
                rec["params"], rec["cache_key"], rec["json_mode_dropped"] = {k: v for k, v in body.items() if k != "messages"}, key, True
                data, status, ms = _post(prov, body)
        except LLMError as e:
            _breakers[prov].record(False)
            errors.append(str(e))
            if not e.retryable:
                raise LLMUnavailable(f"{stage}: {e}", e.status) from e
            fallback_from = fallback_from or prov
            continue
        _breakers[prov].record(True)
        _spent.setdefault(model, []).append((time.time(), (data.get("usage") or {}).get("total_tokens") or need))
        msg = data["choices"][0]["message"]
        usage = data.get("usage", {})
        out = {"raw": msg.get("content") or "", "finish_reason": data["choices"][0].get("finish_reason"),
               "model_reported": data.get("model", model), "http_status": status,
               "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
               "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")}
        if use_cache:
            _cache_put(key, prov, model, body, out)
        return {**rec, **out, "cached": False, "latency_ms": round(ms, 1)}
    raise LLMUnavailable(f"{stage}: no permitted provider answered ({'; '.join(errors)})"
                         + ("" if sensitivity == "public" else " — confidential request, Gemini fallback not allowed"))


def parse_json(text: str | None) -> dict | None:
    """Strict parse, then the first {...} block. None means the output failed validation."""
    if not text:
        return None
    for candidate in (text, (re.search(r"\{.*\}", text, re.S) or [None])[0]):
        if not candidate:
            continue
        try:
            v = json.loads(candidate)
            if isinstance(v, list) and len(v) == 1:      # the object wrapped in a one-item list
                v = v[0]
            return v if isinstance(v, dict) else None
        except json.JSONDecodeError:
            continue
    return None
