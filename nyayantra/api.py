"""Nyayantra HTTP API. Each pipeline stage has its own route so it can be tested and audited alone;
the flow routes (/v1/research, /v1/contracts/analyze, /v1/citations/verify) chain them, store the
redacted result, queue it for advocate review when needed and write the audit chain.

    .venv/bin/uvicorn nyayantra.api:app --port 8010      then open http://127.0.0.1:8010/console

Two keys (in .env): NYAYANTRA_API_KEY for research users, NYAYANTRA_REVIEWER_KEY for advocates, who
alone may decide reviews, erase records and see the audit log (separation of duties).
"""
from __future__ import annotations

import datetime as dt
import hmac
import os
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from typing import Literal

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field

from nyayantra import compliance, llm, prompts, store
from nyayantra.config import DSN, ROOT
from nyayantra.contracts import analyze
from nyayantra.generate import generate as gen_stage
from nyayantra.guardrail import guardrail as guard_stage
from nyayantra.pipeline import CONTEXT_K, DISCLAIMER, _hit_view, research as research_flow
from nyayantra.redact import redact as redact_stage
from nyayantra.retrieve import Filters, Options, fetch, retrieve as retrieve_stage
from nyayantra.screen import screen as screen_stage
from nyayantra.understand import understand as understand_stage
from nyayantra.verify import check as verify_flow

VERSION = "0.6.0"
RATE_PER_MIN = int(os.environ.get("NYAYANTRA_RATE_PER_MIN", 30))
MAX_QUESTION = 2000
MAX_CONTRACT = 60_000
MAX_TEXT = 100_000

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load the local models and the sparse index in the background so the first query is not slow."""
    app.state.warm = False
    if os.environ.get("NYAYANTRA_WARM", "1") == "1":
        def go():
            from nyayantra import models
            from nyayantra.retrieve import sparse_index
            models.embedder(), sparse_index(), models.reranker()
            models.rerank("warm up", ["warm up"])
            app.state.warm = True
        threading.Thread(target=go, daemon=True).start()
    yield


app = FastAPI(title="Nyayantra", lifespan=lifespan, version=VERSION,
              description="Indian legal research and contract review with RAG on Groq. Every response carries a trace: "
                          "each stage's output, latency, the exact prompts, raw model replies and the compliance controls applied. "
                          "A research aid for advocates, not legal advice (Advocates Act 1961, ss. 29 and 33).")

_calls: dict[str, deque] = defaultdict(deque)
_rate_lock = threading.Lock()


# ---------- auth ----------
def _role(key: str | None) -> str:
    user, reviewer = os.environ.get("NYAYANTRA_API_KEY"), os.environ.get("NYAYANTRA_REVIEWER_KEY")
    if not user or not reviewer:
        raise HTTPException(503, "service keys are not configured (NYAYANTRA_API_KEY, NYAYANTRA_REVIEWER_KEY in .env)")
    if key and hmac.compare_digest(key, reviewer):
        return "reviewer"
    if key and hmac.compare_digest(key, user):
        return "user"
    raise HTTPException(401, "invalid or missing X-API-Key")


def _limit(role: str) -> None:
    now = time.time()
    with _rate_lock:
        q = _calls[role]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= RATE_PER_MIN:
            raise HTTPException(429, f"rate limit: {RATE_PER_MIN} requests per minute", headers={"Retry-After": "60"})
        q.append(now)


def user(x_api_key: str | None = Header(default=None)) -> str:
    role = _role(x_api_key)
    _limit(role)
    return role


def reviewer(x_api_key: str | None = Header(default=None)) -> str:
    role = _role(x_api_key)
    if role != "reviewer":
        raise HTTPException(403, "this action needs the reviewer (advocate) key")
    _limit(role)
    return role


def _llm_guard(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except llm.LLMUnavailable as e:
        raise HTTPException(503, f"no permitted model available: {e}")


def _audit_stage(route: str, text: str, **extra) -> None:
    store.audit("stage", route=route, input_sha256=store.sha256(text), **extra)


# ---------- bodies ----------
Sensitivity = Literal["public", "confidential"]


class TextIn(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TEXT)
    party_names: list[str] = Field(default_factory=list, description="exact strings to redact as PARTY, e.g. contract parties")


class ScreenIn(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_CONTRACT)
    use_model: bool = True


class QuestionIn(BaseModel):
    question: str = Field(min_length=3, max_length=MAX_QUESTION)
    today: dt.date | None = None


class RetrieveIn(BaseModel):
    query: str = Field(min_length=2, max_length=MAX_QUESTION)
    as_of: dt.date | None = None
    mode: Literal["accurate", "fast"] = "accurate"
    k: int = Field(CONTEXT_K, ge=1, le=20)
    instruments: list[str] | None = None
    kinds: list[Literal["act", "rules", "regulation", "judgment"]] | None = None
    domains: list[str] | None = None
    topics: list[str] | None = None
    include_overruled: bool = False


class GenerateIn(BaseModel):
    question: str = Field(min_length=3, max_length=MAX_QUESTION)
    unit_ids: list[str] = Field(min_length=1, max_length=10, description="passages to answer from (from /v1/retrieve)")
    as_of: dt.date | None = None
    notes: list[str] = Field(default_factory=list, max_length=10)
    language: Literal["en", "hi"] = "en"


class GuardrailIn(BaseModel):
    draft: dict | None = Field(description="the generator's JSON: {answer, points[{statement, ids, quote}], caveats, insufficient}")
    unit_ids: list[str] = Field(max_length=10, description="the passages the draft was given")
    question: str = Field(min_length=1, max_length=MAX_QUESTION)
    as_of: dt.date | None = None
    unverified: list[str] = Field(default_factory=list, description="authorities named in the question that are not in the corpus")
    sensitivity: Sensitivity = "public"
    use_policy_model: bool = True


class ResearchIn(BaseModel):
    question: str = Field(min_length=3, max_length=MAX_QUESTION)
    as_of: dt.date | None = None
    mode: Literal["accurate", "fast"] = "accurate"
    repair: bool = True
    use_policy_model: bool = True


class ContractIn(BaseModel):
    text: str = Field(min_length=20, max_length=MAX_CONTRACT)
    use_policy_model: bool = True


class VerifyIn(BaseModel):
    text: str = Field(min_length=3, max_length=MAX_TEXT)
    as_of: dt.date | None = None


class DecisionIn(BaseModel):
    reviewer: str = Field(min_length=2, max_length=120, description="advocate's name and enrolment number")
    status: Literal["approved", "edited", "rejected"]
    note: str = Field("", max_length=4000)
    edited: dict | None = Field(None, description="the corrected answer, required when status is 'edited'")


class EraseIn(BaseModel):
    request_id: str = Field(min_length=3, max_length=40)
    reason: str = Field("data principal request", max_length=200)


# ---------- ops ----------
@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/console")


@app.get("/console", include_in_schema=False)
def console():
    return FileResponse(ROOT / "console" / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/health", tags=["ops"])
def health():
    db = {}
    try:
        with psycopg.connect(DSN, connect_timeout=3) as conn:
            for t in ("instrument", "unit", "chunk", "request", "audit_log", "llm_cache"):
                db[t] = conn.execute(f"select count(*) from {t}").fetchone()[0]
            db["pending_reviews"] = conn.execute("select count(*) from review where status='pending'").fetchone()[0]
        db_ok = True
    except psycopg.Error as e:
        db_ok, db = False, {"error": str(e).splitlines()[0]}
    return {"status": "ok" if db_ok else "degraded", "version": VERSION, "db": db,
            "models_warm": getattr(app.state, "warm", None),
            "providers": {p: {"configured": bool(os.environ.get(c["key"])), **{r: m[p] for r, m in llm.MODELS.items() if p in m}}
                          for p, c in llm.PROVIDERS.items()},
            "fallback_enabled": not llm._disable_fallback,
            "prompt_versions": {"understand": prompts.UNDERSTAND_V, "generate": prompts.GENERATE_V,
                                "policy": prompts.POLICY_V, "clauses": prompts.CLAUSES_V},
            "retention_days": store.RETENTION_DAYS, "rate_limit_per_min": RATE_PER_MIN}


@app.get("/v1/compliance/map", tags=["ops"])
def compliance_map():
    return compliance.CONTROLS


@app.get("/v1/sources", tags=["ops"], dependencies=[Depends(user)])
def sources(kind: str | None = None):
    with psycopg.connect(DSN) as conn:
        rows = conn.execute("""select i.instrument_id, i.kind::text, i.short_title, i.status::text, i.enacted_on, i.commenced_on,
                                      i.repealed_by, i.decided_on, i.reporter_citations, i.treatment, i.source_url, i.licence_basis,
                                      count(u.unit_id)
                               from instrument i left join unit u using (instrument_id)
                               where %s::text is null or i.kind::text = %s
                               group by i.instrument_id order by i.kind, i.instrument_id""", (kind, kind)).fetchall()
    cols = ["instrument_id", "kind", "title", "status", "enacted_on", "commenced_on", "repealed_by", "decided_on",
            "reporter_citations", "treatment", "source_url", "licence_basis", "units"]
    return {"instruments": [{c: (str(v) if isinstance(v, dt.date) else v) for c, v in zip(cols, r)} for r in rows]}


@app.get("/v1/units/{unit_id}", tags=["ops"], dependencies=[Depends(user)])
def unit(unit_id: str, as_of: dt.date | None = None):
    hits = fetch([unit_id], as_of)
    if not hits:
        raise HTTPException(404, "no such unit (or overruled on that date)")
    return {"unit": hits[0], "successors": hits[1:]}


# ---------- stages ----------
@app.post("/v1/redact", tags=["stages"], dependencies=[Depends(user)])
def redact(b: TextIn):
    """Stage 1. Returns the redacted text and what was found (types and counts only; the values never leave memory)."""
    r = redact_stage(b.text, {"PARTY": b.party_names} if b.party_names else None)
    return {"redacted_text": r.text, "found": r.found, "sensitivity": "confidential" if r.sensitive else "public",
            "controls": compliance.CONTROLS["1_redact"]["refs"]}


@app.post("/v1/screen", tags=["stages"], dependencies=[Depends(user)])
def screen(b: ScreenIn):
    """Stage 2. Injection screen: rules + Prompt Guard 2. The text is redacted first."""
    r = redact_stage(b.text)
    out = screen_stage(r.text, "confidential" if r.sensitive else "public", use_model=b.use_model)
    _audit_stage("screen", b.text, flagged=out["flagged"])
    return {**out, "redaction": {"found": r.found}}


@app.post("/v1/understand", tags=["stages"], dependencies=[Depends(user)])
def understand(b: QuestionIn):
    """Stage 3. Language, as-of date, domains, and every cited authority checked against the database."""
    r = redact_stage(b.question)
    out = _llm_guard(understand_stage, r.text, b.today, "confidential" if r.sensitive else "public")
    _audit_stage("understand", b.question)
    return {**out, "redaction": {"redacted_text": r.text, "found": r.found}}


@app.post("/v1/retrieve", tags=["stages"], dependencies=[Depends(user)])
def retrieve(b: RetrieveIn):
    """Stage 4. Hybrid search. mode=fast skips the cross-encoder reranker (about 1 s instead of 4-6 s on this laptop)."""
    r = retrieve_stage(redact_stage(b.query).text,
                       Filters(as_of=b.as_of, instruments=b.instruments, kinds=b.kinds, domains=b.domains, topics=b.topics,
                               include_overruled=b.include_overruled),
                       Options(k=b.k, rerank=b.mode == "accurate"))
    return {"hits": [_hit_view(h) | {"text": h["text"]} for h in r["hits"]], "trace": r["trace"],
            "controls": compliance.CONTROLS["4_retrieve"]["refs"]}


@app.post("/v1/generate", tags=["stages"], dependencies=[Depends(user)])
def generate(b: GenerateIn):
    """Stage 5. Answer from the given passages only. Passages are fetched from the database by id,
    never taken from the request, so a caller cannot slip invented law into the context."""
    r = redact_stage(b.question)
    hits = fetch(b.unit_ids, b.as_of)
    missing = [u for u in b.unit_ids if u not in {h["unit_id"] for h in hits}]
    if missing:
        raise HTTPException(404, f"unknown or excluded unit ids: {', '.join(missing)}")
    out = _llm_guard(gen_stage, r.text, str(b.as_of or dt.date.today()), hits, b.notes,
                     "confidential" if r.sensitive else "public", language=b.language)
    _audit_stage("generate", b.question, unit_ids=b.unit_ids)
    return {**out, "unit_ids": [h["unit_id"] for h in hits], "redaction": {"found": r.found}}


@app.post("/v1/guardrail", tags=["stages"], dependencies=[Depends(user)])
def guardrail(b: GuardrailIn):
    """Stage 6. Fail-closed output checks on a draft. Returns withhold / review / pass and every check."""
    hits = fetch(b.unit_ids, b.as_of) if b.unit_ids else []
    und = {"unverified": [{"text": t, "resolved": False} for t in b.unverified]}
    draft = b.draft if b.draft is None else dict(b.draft)
    out = guard_stage(draft, hits, redact_stage(b.question).text, und, b.sensitivity, b.use_policy_model)
    return {**out, "draft_normalised": draft}


# ---------- flows ----------
@app.post("/v1/research", tags=["flows"], dependencies=[Depends(user)])
def research(b: ResearchIn):
    """The whole research flow. Stored redacted; queued for an advocate when withheld or flagged."""
    r = research_flow(b.question, as_of=b.as_of, mode=b.mode, repair=b.repair, use_policy_model=b.use_policy_model)
    return store.save(r)


@app.post("/v1/contracts/analyze", tags=["flows"], dependencies=[Depends(user)])
def contracts_analyze(b: ContractIn):
    """Contract review. Confidential: Groq only. Always queued for an advocate. A 25-clause contract takes
    about 5-6 minutes on the free Groq tier because calls are paced to its tokens-per-minute limit."""
    return store.save(analyze(b.text, use_policy_model=b.use_policy_model))


@app.post("/v1/citations/verify", tags=["flows"], dependencies=[Depends(user)])
def citations_verify(b: VerifyIn):
    """Check every citation in a draft against the database and its status on the as-of date. No LLM."""
    return store.save(verify_flow(b.text, b.as_of))


# ---------- requests, review, audit, erasure ----------
@app.get("/v1/requests", tags=["records"], dependencies=[Depends(user)])
def requests_recent(limit: int = Query(30, ge=1, le=200)):
    return {"requests": store.recent(limit)}


@app.get("/v1/requests/{request_id}", tags=["records"], dependencies=[Depends(user)])
def request_get(request_id: str):
    """A stored result (redacted), its review state, and what may be released as the answer."""
    r = store.get(request_id)
    if not r:
        raise HTTPException(404, "no such request")
    return r


@app.get("/v1/reviews", tags=["records"], dependencies=[Depends(reviewer)])
def reviews(status: Literal["pending", "approved", "edited", "rejected", "all"] = "pending", limit: int = Query(50, ge=1, le=200)):
    return {"reviews": store.reviews(status, limit)}


@app.post("/v1/reviews/{request_id}/decision", tags=["records"], dependencies=[Depends(reviewer)])
def review_decision(request_id: str, b: DecisionIn):
    """An advocate approves, corrects or rejects a queued result. One decision per request, audited."""
    try:
        return store.decide(request_id, b.reviewer, b.status, b.note, b.edited)
    except store.ReviewError as e:
        raise HTTPException(e.code, str(e))


@app.get("/v1/audit", tags=["records"], dependencies=[Depends(reviewer)])
def audit_tail(limit: int = Query(50, ge=1, le=500)):
    return {"entries": store.audit_tail(limit)}


@app.get("/v1/audit/verify", tags=["records"], dependencies=[Depends(reviewer)])
def audit_verify():
    """Recompute the SHA-256 chain from the first entry. ok=false names the first entry that was changed."""
    return store.audit_verify()


@app.post("/v1/erase", tags=["records"], dependencies=[Depends(reviewer)])
def erase(b: EraseIn):
    """Erase a request's stored text, trace and LLM cache rows (DPDP Act 2023 s.12). The audit chain keeps a record of the erasure."""
    try:
        return store.erase(b.request_id, "reviewer", b.reason)
    except store.ReviewError as e:
        raise HTTPException(e.code, str(e))


@app.post("/v1/retention/purge", tags=["records"], dependencies=[Depends(reviewer)])
def retention_purge():
    """Erase every request past its retention date (NYAYANTRA_RETENTION_DAYS, default 30)."""
    return store.purge_expired()


@app.get("/v1/disclaimer", tags=["ops"])
def disclaimer():
    return {"disclaimer": DISCLAIMER}
