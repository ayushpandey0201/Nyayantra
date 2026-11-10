"""Requests, the advocate review queue, erasure and the hash-chained audit log.

What is stored is redacted: before a result is saved, every personal value and party name that the
pipeline put back for the user is replaced by its placeholder again (DPDP Act 2023 s.8(5), s.8(7)).
The audit log keeps ids, hashes and decisions only, never client text, and the database refuses
updates or deletes on it (db/migrations/005_app.sql), so the chain can be checked end to end.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from nyayantra.config import DSN

RETENTION_DAYS = int(os.environ.get("NYAYANTRA_RETENTION_DAYS", 30))
GENESIS = "0" * 64
REVIEW_STATUSES = ("approved", "edited", "rejected")
_AUDIT_LOCK = 4242   # pg advisory lock id: one writer extends the chain at a time


def _conn():
    return psycopg.connect(DSN)


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def canonical(body: dict) -> str:
    return json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


# ---------- audit ----------
def audit(event: str, cur=None, **fields: Any) -> dict:
    """Append one entry: hash = sha256(prev_hash + canonical(body)). Fields must be ids, hashes, counts."""
    if cur is None:
        with _conn() as conn, conn.cursor() as c:
            return audit(event, c, **fields)
    cur.execute("select pg_advisory_xact_lock(%s)", (_AUDIT_LOCK,))
    row = cur.execute("select seq, hash from audit_log order by seq desc limit 1").fetchone()
    prev = row[1] if row else GENESIS
    body = {"event": event, "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds"), **fields}
    h = sha256(prev + canonical(body))
    seq = cur.execute("insert into audit_log (event, body, prev_hash, hash) values (%s, %s, %s, %s) returning seq",
                      (event, Jsonb(body), prev, h)).fetchone()[0]
    return {"seq": seq, "hash": h, "prev_hash": prev, "body": body}


def audit_verify() -> dict:
    prev, n = GENESIS, 0
    with _conn() as conn:
        for seq, body, p, h in conn.execute("select seq, body, prev_hash, hash from audit_log order by seq"):
            n += 1
            if p != prev:
                return {"ok": False, "entries": n, "first_bad_seq": seq, "problem": "prev_hash does not match the previous entry"}
            if sha256(prev + canonical(body)) != h:
                return {"ok": False, "entries": n, "first_bad_seq": seq, "problem": "entry was changed after it was written"}
            prev = h
    return {"ok": True, "entries": n, "head": prev, "first_bad_seq": None}


def audit_tail(limit: int = 50) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("select seq, ts, event, body, prev_hash, hash from audit_log order by seq desc limit %s", (limit,)).fetchall()
    return [{"seq": r[0], "ts": r[1].isoformat(), "event": r[2], "body": r[3], "prev_hash": r[4], "hash": r[5]} for r in rows]


# ---------- requests ----------
def _reredact(obj: Any, back: list[tuple[str, str]]) -> Any:
    if isinstance(obj, str):
        for orig, ph in back:
            obj = obj.replace(orig, ph)
        return obj
    if isinstance(obj, list):
        return [_reredact(x, back) for x in obj]
    if isinstance(obj, dict):
        return {k: _reredact(v, back) for k, v in obj.items()}
    return obj


def _calls(result: dict) -> list[dict]:
    return [c for s in result.get("trace", []) for c in s.get("llm", [])]


def save(result: dict) -> dict:
    """Persist a research / contract / citation result (redacted), queue it for review if needed, audit it.
    Returns the public result (with the private redaction data removed)."""
    private = result.pop("_private", None) or {}
    mapping = private.get("mapping") or {}
    back = sorted(((orig, ph) for ph, orig in mapping.items()), key=lambda t: -len(t[0]))
    stored = _reredact(result, back)
    calls = _calls(result)
    keys = sorted({c["cache_key"] for c in calls if c.get("cache_key")})
    retain = dt.date.today() + dt.timedelta(days=RETENTION_DAYS)
    result_hash = sha256(canonical(stored))
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""insert into request (request_id, kind, redacted_input, input_sha256, as_of, result, retain_until,
                                            decision, sensitivity, cache_keys)
                       values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (result["request_id"], result["kind"], private.get("redacted_input"), result["input_sha256"],
                     private.get("as_of"), Jsonb(stored), retain, result.get("decision"), private.get("sensitivity"), keys))
        if result.get("review_required"):
            cur.execute("insert into review (request_id, reason) values (%s, %s)", (result["request_id"], result.get("reason")))
        audit("request", cur, request_id=result["request_id"], kind=result["kind"], input_sha256=result["input_sha256"],
              result_sha256=result_hash, decision=result.get("decision"), sensitivity=private.get("sensitivity"),
              review_queued=bool(result.get("review_required")),
              models=sorted({f"{c['provider']}:{c['model']}" for c in calls}),
              prompt_versions=sorted({c["prompt_version"] for c in calls if c.get("prompt_version")}),
              llm_calls=len(calls), retain_until=str(retain))
    result["stored"] = {"retain_until": str(retain), "result_sha256": result_hash, "review_queued": bool(result.get("review_required"))}
    return result


def get(request_id: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute("""select r.kind, r.created_at, r.decision, r.sensitivity, r.retain_until, r.erased_at, r.result,
                                     v.status, v.reviewer, v.note, v.decided_at, v.edited, v.reason
                              from request r left join review v using (request_id) where r.request_id = %s""",
                           (request_id,)).fetchone()
    if not row:
        return None
    kind, created, decision, sens, retain, erased, result, status, reviewer, note, decided, edited, reason = row
    review = None if status is None else {"status": status, "reviewer": reviewer, "note": note, "reason": reason,
                                          "decided_at": decided.isoformat() if decided else None, "edited": edited}
    return {"request_id": request_id, "kind": kind, "created_at": created.isoformat(), "decision": decision,
            "sensitivity": sens, "retain_until": str(retain) if retain else None,
            "erased_at": erased.isoformat() if erased else None, "review": review,
            "released": _released(result or {}, decision, review), "result": result}


def _released(result: dict, decision: str | None, review: dict | None):
    """What may be shown as the answer: a passing answer, or what an advocate approved or corrected."""
    if result.get("erased"):
        return None
    if review is None:
        return result.get("answer") if decision in ("pass", "insufficient") else None
    if review["status"] == "approved":
        return result.get("answer") or result.get("clauses")
    if review["status"] == "edited":
        return review["edited"]
    return None


def recent(limit: int = 30) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("""select r.request_id, r.kind, r.created_at, r.decision, r.sensitivity, v.status, r.erased_at
                               from request r left join review v using (request_id)
                               order by r.created_at desc limit %s""", (limit,)).fetchall()
    return [{"request_id": r[0], "kind": r[1], "created_at": r[2].isoformat(), "decision": r[3], "sensitivity": r[4],
             "review": r[5], "erased": r[6] is not None} for r in rows]


# ---------- reviews ----------
def reviews(status: str = "pending", limit: int = 50) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("""select v.request_id, r.kind, r.decision, v.reason, v.queued_at, v.status, v.reviewer
                               from review v join request r using (request_id)
                               where (%s = 'all' or v.status = %s) order by v.queued_at limit %s""",
                            (status, status, limit)).fetchall()
    return [{"request_id": r[0], "kind": r[1], "decision": r[2], "reason": r[3], "queued_at": r[4].isoformat(),
             "status": r[5], "reviewer": r[6]} for r in rows]


class ReviewError(ValueError):
    def __init__(self, msg: str, code: int):
        super().__init__(msg)
        self.code = code


def decide(request_id: str, reviewer: str, status: str, note: str = "", edited: dict | None = None) -> dict:
    """An advocate approves, corrects or rejects a queued result (Advocates Act 1961 s.35: the advocate
    is accountable for what is released). One decision per request; it cannot be changed afterwards."""
    if status not in REVIEW_STATUSES:
        raise ReviewError(f"status must be one of {', '.join(REVIEW_STATUSES)}", 422)
    if status == "edited" and not edited:
        raise ReviewError("an edited decision needs the corrected answer", 422)
    if not reviewer.strip():
        raise ReviewError("reviewer is required", 422)
    with _conn() as conn, conn.cursor() as cur:
        row = cur.execute("select status from review where request_id = %s for update", (request_id,)).fetchone()
        if not row:
            raise ReviewError("no review is queued for this request", 404)
        if row[0] != "pending":
            raise ReviewError(f"already decided ({row[0]})", 409)
        cur.execute("update review set status=%s, reviewer=%s, note=%s, edited=%s, decided_at=now() where request_id=%s",
                    (status, reviewer.strip(), note or None, Jsonb(edited) if edited else None, request_id))
        a = audit("review", cur, request_id=request_id, reviewer=reviewer.strip(), status=status,
                  note_sha256=sha256(note) if note else None, edited_sha256=sha256(canonical(edited)) if edited else None)
    return {"request_id": request_id, "status": status, "reviewer": reviewer.strip(), "audit_seq": a["seq"]}


# ---------- erasure and retention ----------
def erase(request_id: str, actor: str, reason: str = "data principal request") -> dict:
    """Erase a request's stored text, its trace and the LLM cache rows made for it (DPDP Act 2023 s.12).
    The audit entries stay: they hold only hashes and ids, which is what proves the erasure happened."""
    with _conn() as conn, conn.cursor() as cur:
        row = cur.execute("select kind, decision, cache_keys, erased_at from request where request_id = %s for update",
                          (request_id,)).fetchone()
        if not row:
            raise ReviewError("no such request", 404)
        kind, decision, keys, erased_at = row
        if erased_at:
            return {"request_id": request_id, "already_erased": True}
        cache_rows = cur.execute("delete from llm_cache where cache_key = any(%s)", (keys or [],)).rowcount
        cur.execute("""update request set redacted_input = null, cache_keys = null, erased_at = now(),
                       result = %s where request_id = %s""",
                    (Jsonb({"erased": True, "kind": kind, "decision": decision}), request_id))
        cur.execute("update review set note = null, edited = null, reason = null where request_id = %s", (request_id,))
        a = audit("erasure", cur, request_id=request_id, actor=actor, reason=reason, llm_cache_rows=cache_rows)
    return {"request_id": request_id, "erased": True, "llm_cache_rows_deleted": cache_rows, "audit_seq": a["seq"]}


def purge_expired(actor: str = "retention") -> dict:
    with _conn() as conn:
        ids = [r[0] for r in conn.execute("select request_id from request where erased_at is null and retain_until < current_date")]
    for rid in ids:
        erase(rid, actor, "retention period ended")
    return {"erased": len(ids), "request_ids": ids}
