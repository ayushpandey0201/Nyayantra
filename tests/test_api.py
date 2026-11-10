"""Phase 6: store (re-redaction, review queue, erasure, audit chain) and API auth and routes.
Runs against a separate database, nyayantra_test, so the real audit chain is never touched.
The flows are stubbed, so no model is called."""
import os
import re

import psycopg
import pytest

from nyayantra import store
from nyayantra.config import ROOT

TEST_DSN = "host=/tmp dbname=nyayantra_test"
APP_TABLES = ("llm_cache", "request", "review", "audit_log")


@pytest.fixture(scope="module", autouse=True)
def test_db():
    with psycopg.connect("host=/tmp dbname=postgres", autocommit=True) as c:
        if not c.execute("select 1 from pg_database where datname='nyayantra_test'").fetchone():
            c.execute("create database nyayantra_test")
    schema = (ROOT / "db" / "schema.sql").read_text()
    with psycopg.connect(TEST_DSN, autocommit=True) as c:
        c.execute("drop table if exists " + ", ".join(APP_TABLES) + " cascade")
        for t in APP_TABLES:
            c.execute(re.search(rf"create table if not exists {t} \(.*?\n\);", schema, re.S).group(0))
        c.execute((ROOT / "db" / "migrations" / "005_app.sql").read_text())
    old = store.DSN
    store.DSN = TEST_DSN
    yield
    store.DSN = old


def _result(rid="R-test0001", review=True, pii="ravi.k@gmail.com"):
    return {"request_id": rid, "kind": "research", "input_sha256": store.sha256("q"), "decision": "review" if review else "pass",
            "review_required": review, "reason": "flagged",
            "answer": {"text": f"Write to {pii} about it [ICA-1872:s27].", "points": [], "caveats": []},
            "trace": [{"stage": "generate", "llm": [{"provider": "groq", "model": "m", "prompt_version": "v", "cache_key": "k-" + rid,
                                                     "user": "Write to [EMAIL_1]"}]}],
            "_private": {"mapping": {"[EMAIL_1]": pii}, "redacted_input": "Write to [EMAIL_1]", "sensitivity": "confidential", "as_of": None}}


def test_saved_result_is_redacted_but_response_is_not():
    out = store.save(_result("R-red0001"))
    assert "ravi.k@gmail.com" in out["answer"]["text"] and "_private" not in out
    with psycopg.connect(TEST_DSN) as c:
        stored = c.execute("select result::text, redacted_input from request where request_id='R-red0001'").fetchone()
    assert "ravi.k@gmail.com" not in stored[0] and "[EMAIL_1]" in stored[0]


def test_review_once_and_release():
    store.save(_result("R-rev0001"))
    assert store.get("R-rev0001")["released"] is None          # held until an advocate decides
    with pytest.raises(store.ReviewError) as e:
        store.decide("R-rev0001", "Adv. A", "edited")          # edited needs the corrected answer
    assert e.value.code == 422
    store.decide("R-rev0001", "Adv. A", "edited", "fixed the citation", {"text": "Corrected [ICA-1872:s27]."})
    assert store.get("R-rev0001")["released"] == {"text": "Corrected [ICA-1872:s27]."}
    with pytest.raises(store.ReviewError) as e:
        store.decide("R-rev0001", "Adv. B", "approved")
    assert e.value.code == 409


def test_passing_answer_released_without_review():
    store.save(_result("R-pass0001", review=False))
    g = store.get("R-pass0001")
    assert g["review"] is None and "[EMAIL_1]" in g["released"]["text"]


def test_erasure_removes_text_and_cache_and_is_audited():
    store.save(_result("R-era0001"))
    with psycopg.connect(TEST_DSN) as c:
        c.execute("insert into llm_cache (cache_key, provider, model, request, response) values ('k-R-era0001','groq','m','{}','{}')")
    out = store.erase("R-era0001", "test")
    assert out["llm_cache_rows_deleted"] == 1
    g = store.get("R-era0001")
    assert g["result"] == {"erased": True, "kind": "research", "decision": "review"} and g["released"] is None
    assert store.audit_tail(1)[0]["event"] == "erasure"
    assert store.erase("R-era0001", "test")["already_erased"]


def test_audit_chain_verifies_and_detects_tampering():
    assert store.audit_verify()["ok"]
    with psycopg.connect(TEST_DSN) as c:
        with pytest.raises(psycopg.errors.RaiseException):
            c.execute("update audit_log set event='x' where seq = (select min(seq) from audit_log)")
    with psycopg.connect(TEST_DSN) as c:     # a table owner who disables the trigger is still caught by the chain
        c.execute("alter table audit_log disable trigger audit_log_no_change")
        seq = c.execute("select min(seq) from audit_log").fetchone()[0]
        c.execute("update audit_log set body = jsonb_set(body, '{decision}', '\"pass\"') where seq=%s", (seq,))
        c.execute("alter table audit_log enable trigger audit_log_no_change")
    v = store.audit_verify()
    assert not v["ok"] and v["first_bad_seq"] == seq


# ---------- API ----------
@pytest.fixture(scope="module")
def client():
    os.environ.update(NYAYANTRA_API_KEY="test-user", NYAYANTRA_REVIEWER_KEY="test-reviewer", NYAYANTRA_WARM="0")
    from fastapi.testclient import TestClient
    from nyayantra import api
    api.research_flow = lambda q, **kw: _result("R-api" + store.sha256(q)[:6])
    with TestClient(api.app) as c:
        yield c


U, R = {"X-API-Key": "test-user"}, {"X-API-Key": "test-reviewer"}


def test_auth_and_roles(client):
    assert client.post("/v1/redact", json={"text": "x"}).status_code == 401
    assert client.get("/v1/reviews", headers=U).status_code == 403
    assert client.get("/v1/reviews", headers=R).status_code == 200
    assert client.post("/v1/erase", headers=U, json={"request_id": "R-x"}).status_code == 403


def test_redact_route_keeps_citations(client):
    r = client.post("/v1/redact", headers=U, json={"text": "Mail ravi.k@gmail.com re s.27 ICA, (2017) 10 SCC 1"}).json()
    assert "ravi.k" not in r["redacted_text"] and "(2017) 10 SCC 1" in r["redacted_text"] and r["sensitivity"] == "confidential"


def test_research_route_stores_and_queues(client):
    r = client.post("/v1/research", headers=U, json={"question": "Is a non-compete enforceable?"}).json()
    assert r["stored"]["review_queued"]
    ids = [v["request_id"] for v in client.get("/v1/reviews", headers=R).json()["reviews"]]
    assert r["request_id"] in ids
    d = client.post(f"/v1/reviews/{r['request_id']}/decision", headers=R, json={"reviewer": "Adv. C", "status": "rejected"})
    assert d.status_code == 200
    assert client.post(f"/v1/reviews/{r['request_id']}/decision", headers=R, json={"reviewer": "Adv. C", "status": "approved"}).status_code == 409


def test_input_limits(client):
    assert client.post("/v1/research", headers=U, json={"question": "x" * 2001}).status_code == 422
    assert client.post("/v1/reviews/R-none/decision", headers=R, json={"reviewer": "Adv. C", "status": "maybe"}).status_code == 422


def test_compliance_map_covers_every_stage(client):
    m = client.get("/v1/compliance/map").json()
    assert all(any(k.endswith(s) for k in m) for s in ("redact", "screen", "understand", "retrieve", "generate", "guardrail",
                                                       "review", "audit", "erase"))
