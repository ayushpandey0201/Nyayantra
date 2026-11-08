"""Stage 4: hybrid retrieval with legal metadata.

  1. Citation lookup  - sections, rules and cases the query names are looked up exactly and pinned.
  2. Three searches   - BGE-M3 dense (pgvector HNSW), BGE-M3 sparse (lexical weights, in memory) and
                        Postgres full-text; plus a dense search inside any case the query names.
  3. Fusion           - reciprocal rank fusion at the unit level (a unit's best chunk counts).
  4. Legal status     - every unit is checked against the as-of date: repealed provisions are replaced
                        by their verified successor (IPC s.420 -> BNS s.318), overruled judgments are
                        dropped, provisions enacted but not yet in force are kept and flagged.
  5. Rerank           - bge-reranker-v2-m3 cross-encoder over the fused pool; at most
                        MAX_PER_JUDGMENT paragraphs per judgment in the final list.

Every step is recorded in the returned trace.
"""
from __future__ import annotations

import datetime as dt
import re
import time
from dataclasses import asdict, dataclass, field
from functools import lru_cache

import numpy as np
import psycopg

from nyayantra import citations, models
from nyayantra.config import CANDIDATES, DSN, MAX_PER_JUDGMENT, RERANK_POOL, RRF_K

RETRIEVERS = ("dense", "sparse", "fts", "named")


@dataclass
class Filters:
    as_of: dt.date | None = None            # None = today; an explicit date also hides law enacted after it
    instruments: list[str] | None = None
    kinds: list[str] | None = None          # act / rules / judgment
    domains: list[str] | None = None        # contract, criminal, privacy ...
    topics: list[str] | None = None
    include_overruled: bool = False


@dataclass
class Options:
    k: int = 5
    retrievers: tuple[str, ...] = RETRIEVERS
    rerank: bool = True
    legal_status: bool = True               # off = plain search, used as a baseline in eval
    pin_citations: bool = True
    diversity: bool = True
    link_statutes: bool = True              # add the provision the retrieved judgments are about


ELIGIBLE = """
    not u.omitted
    and (%(inst)s::text[] is null or i.instrument_id = any(%(inst)s))
    and (%(kinds)s::text[] is null or i.kind::text = any(%(kinds)s))
    and (%(domains)s::text[] is null or i.domains && %(domains)s)
    and (%(topics)s::text[] is null or u.topics && %(topics)s)
    and (not %(explicit)s or coalesce(i.decided_on, i.enacted_on, i.commenced_on, '1800-01-01') <= %(as_of)s)
"""


def _params(f: Filters) -> dict:
    return {"inst": f.instruments, "kinds": f.kinds, "domains": f.domains, "topics": f.topics,
            "explicit": f.as_of is not None, "as_of": f.as_of or dt.date.today()}


# ---------- sparse index (BGE-M3 lexical weights), held in memory ----------

class SparseIndex:
    def __init__(self):
        with psycopg.connect(DSN) as conn, conn.cursor() as cur:
            cur.execute("select chunk_id, unit_id, sparse_terms from chunk where sparse_terms is not null order by chunk_id")
            rows = cur.fetchall()
        self.chunk_ids = [r[0] for r in rows]
        self.unit_ids = [r[1] for r in rows]
        self.pos = {c: n for n, c in enumerate(self.chunk_ids)}
        post: dict[str, tuple[list[int], list[float]]] = {}
        for n, (_, _, terms) in enumerate(rows):
            for tok, w in terms.items():
                p = post.setdefault(tok, ([], []))
                p[0].append(n)
                p[1].append(w)
        self.post = {t: (np.array(i, dtype=np.int32), np.array(w, dtype=np.float32)) for t, (i, w) in post.items()}

    def search(self, q: dict[str, float], mask: np.ndarray, n: int) -> list[str]:
        acc = np.zeros(len(self.chunk_ids), dtype=np.float32)
        for tok, qw in q.items():
            if tok in self.post:
                idx, w = self.post[tok]
                acc[idx] += qw * w
        acc[~mask] = 0
        top = np.argpartition(-acc, min(n, len(acc) - 1))[:n]
        top = top[np.argsort(-acc[top])]
        return [self.chunk_ids[i] for i in top if acc[i] > 0]


@lru_cache(maxsize=1)
def sparse_index() -> SparseIndex:
    return SparseIndex()


@lru_cache(maxsize=64)
def _mask(params_items: tuple) -> np.ndarray:
    """Chunks the filters allow, as a boolean mask over the sparse index (cached per filter set)."""
    idx = sparse_index()
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute(f"select c.chunk_id from chunk c join unit u using (unit_id) join instrument i using (instrument_id) "
                    f"where {ELIGIBLE}", {k: list(v) if isinstance(v, tuple) else v for k, v in params_items})
        ok = {r[0] for r in cur.fetchall()}
    return np.array([c in ok for c in idx.chunk_ids])


# ---------- the searches ----------

def _dense(cur, vec: list[float], p: dict, n: int, only: str | None = None) -> list[str]:
    cur.execute("set local hnsw.ef_search = 200")
    cur.execute("set local hnsw.iterative_scan = relaxed_order")
    extra = "and i.instrument_id = %(only)s" if only else ""
    cur.execute(f"""select chunk_id from (
                      select c.chunk_id, c.dense <=> %(vec)s::halfvec as d
                      from chunk c join unit u using (unit_id) join instrument i using (instrument_id)
                      where {ELIGIBLE} {extra}
                      order by c.dense <=> %(vec)s::halfvec limit %(n)s) t order by d""",
                {**p, "vec": "[" + ",".join(f"{x:.6f}" for x in vec) + "]", "n": n, "only": only})
    return [r[0] for r in cur.fetchall()]


# words every legal question uses; as full-text terms they match half the corpus
FTS_STOP = {"section", "sections", "act", "acts", "indian", "india", "law", "laws", "legal", "provision", "provisions",
            "rule", "rules", "court", "courts", "say", "says", "deal", "deals", "mean", "means", "govern", "governs",
            "provide", "provides", "valid", "happen", "happens", "new", "still", "now", "case", "held", "hold",
            "supreme", "judgment", "question", "clause"}


def _fts(cur, text: str, p: dict, n: int) -> list[str]:
    """Lexemes of the query (minus generic legal words), all required; if that finds too little, any of them,
    ranked by how many distinct query terms a chunk covers, then by ts_rank."""
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z'-]+|\d+[A-Z]{0,2}", text) if w.lower() not in FTS_STOP
             and not re.fullmatch(r"(18|19|20)\d\d", w)]
    if not words:
        return []
    cur.execute("select array(select distinct unnest(tsvector_to_array(to_tsvector('english', %s))))", (" ".join(words),))
    lex = [x for x in cur.fetchone()[0] if len(x) > 1]
    if not lex:
        return []
    q_and = " & ".join(f"'{x}'" for x in lex)
    q_or = " | ".join(f"'{x}'" for x in lex)
    found: list[str] = []
    for q in (q_and, q_or):
        cur.execute(f"""select c.chunk_id from chunk c join unit u using (unit_id) join instrument i using (instrument_id)
                        where c.tsv @@ %(q)s::tsquery and {ELIGIBLE} and not (c.chunk_id = any(%(found)s))
                        order by (select count(*) from unnest(%(lex)s::text[]) x where c.tsv @@ x::tsquery) desc,
                                 ts_rank(c.tsv, %(q)s::tsquery, 1) desc limit %(n)s""",
                    {**p, "q": q, "n": n - len(found), "found": found, "lex": lex})
        found += [r[0] for r in cur.fetchall()]
        if len(found) >= n // 2:
            break
    return found


def _unit_of(chunk_id: str) -> str:
    return chunk_id.rsplit("#", 1)[0]


def _fuse(lists: dict[str, list[str]]) -> tuple[list[str], dict[str, dict], dict[str, str]]:
    """RRF over units. Returns units in fused order, per-unit ranks, and each unit's best chunk."""
    score: dict[str, float] = {}
    ranks: dict[str, dict] = {}
    best: dict[str, tuple[int, str]] = {}
    for name, chunks in lists.items():
        seen = []
        for c in chunks:
            u = _unit_of(c)
            if u in seen:
                continue
            seen.append(u)
            r = len(seen)
            ranks.setdefault(u, {})[name] = r
            score[u] = score.get(u, 0.0) + 1.0 / (RRF_K + r)
            if u not in best or r < best[u][0]:
                best[u] = (r, c)
    order = sorted(score, key=lambda u: -score[u])
    for u in order:
        ranks[u]["rrf"] = round(score[u], 5)
    return order, ranks, {u: c for u, (_, c) in best.items()}


# ---------- legal status ----------

UNIT_SQL = """select u.unit_id, u.instrument_id, i.kind::text, i.short_title, u.canonical_citation, u.heading, u.path,
                     u.text, u.unit_type::text, u.valid_from, u.valid_to, u.commencement_note, u.opinion_author,
                     i.status::text, i.repealed_by, i.repealed_on, i.commenced_on, i.decided_on, i.overruled_by,
                     i.overruled_on, i.reporter_citations, u.statutes_cited, u.topics, i.statutes_cited
              from unit u join instrument i using (instrument_id) where u.unit_id = any(%s)"""
UNIT_COLS = ["unit_id", "instrument_id", "kind", "title", "citation", "heading", "path", "text", "unit_type",
             "valid_from", "valid_to", "commencement_note", "opinion_author", "instrument_status", "repealed_by",
             "repealed_on", "commenced_on", "decided_on", "overruled_by", "overruled_on", "reporter_citations",
             "statutes_cited", "topics", "case_statutes"]


def _units(cur, ids: list[str]) -> dict[str, dict]:
    if not ids:
        return {}
    cur.execute(UNIT_SQL, (list(ids),))
    return {r[0]: dict(zip(UNIT_COLS, r)) for r in cur.fetchall()}


def _successors(cur, ids: list[str]) -> dict[str, list[dict]]:
    if not ids:
        return {}
    cur.execute("""select old_unit_id, new_unit_id, relation::text, verified, note from mapping
                   where old_unit_id = any(%s) order by old_unit_id, new_unit_id""", (list(ids),))
    out: dict[str, list[dict]] = {}
    for old, new, rel, ver, note in cur.fetchall():
        out.setdefault(old, []).append({"new": new, "relation": rel, "verified": ver, "note": note})
    return out


def status_of(u: dict, d: dt.date) -> tuple[str, str | None]:
    """in_force / not_yet_in_force / repealed / overruled, with a short reason."""
    if u["kind"] == "judgment":
        if u["overruled_on"] and u["overruled_on"] <= d:
            return "overruled", f"overruled by {u['overruled_by']} on {u['overruled_on']}"
        return "in_force", None
    if (u["repealed_on"] and u["repealed_on"] <= d) or (u["valid_to"] and u["valid_to"] <= d):
        return "repealed", f"repealed by {u['repealed_by']} w.e.f. {u['repealed_on'] or u['valid_to']}"
    start = u["valid_from"] or u["commenced_on"]
    if start and start > d:
        return "not_yet_in_force", f"in force from {start}" + (f"; {u['commencement_note']}" if u["commencement_note"] else "")
    return "in_force", u["commencement_note"]


# ---------- the stage ----------

def retrieve(query: str, filters: Filters | None = None, opts: Options | None = None) -> dict:
    f, o = filters or Filters(), opts or Options()
    d = f.as_of or dt.date.today()
    p = _params(f)
    t: dict[str, float] = {}
    trace: dict = {"query": query, "as_of": str(d), "as_of_explicit": f.as_of is not None,
                   "filters": {k: v for k, v in asdict(f).items() if v not in (None, False, [])},
                   "options": asdict(o)}

    t0 = time.perf_counter()
    cited = citations.find(query) if o.pin_citations or "named" in o.retrievers else citations.Cited()
    trace["citations"] = {"units": cited.unit_ids, "cases": cited.cases, "acts": cited.acts,
                          "unresolved": cited.unresolved, "mentions": cited.mentions}
    t["citations"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    vec, lex = models.encode_query(query)
    t["encode"] = time.perf_counter() - t0

    lists: dict[str, list[str]] = {}
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        t0 = time.perf_counter()
        if "dense" in o.retrievers:
            lists["dense"] = _dense(cur, vec, p, CANDIDATES)
        if "sparse" in o.retrievers:
            lists["sparse"] = sparse_index().search(lex, _mask(tuple(sorted((k, tuple(v) if isinstance(v, list) else v) for k, v in p.items()))), CANDIDATES)
        if "fts" in o.retrievers:
            lists["fts"] = _fts(cur, query, p, CANDIDATES)
        if "named" in o.retrievers and cited.cases:
            named = []
            for case in cited.cases:
                named += _dense(cur, vec, p, 3, only=case)
            lists["named"] = named
        t["search"] = time.perf_counter() - t0
        trace["retrievers"] = {k: [_unit_of(c) for c in v[:10]] for k, v in lists.items()}

        order, ranks, best = _fuse(lists)
        pinned = [u for u in cited.unit_ids] if o.pin_citations else []

        # legal status: replace repealed provisions, drop overruled judgments, flag future commencement
        t0 = time.perf_counter()
        pool_ids = list(dict.fromkeys(pinned + order[:max(RERANK_POOL * 2, o.k * 4)]))
        info = _units(cur, pool_ids)
        succ = _successors(cur, [u for u, x in info.items() if x["kind"] != "judgment"]) if o.legal_status else {}
        new_ids = {s["new"] for v in succ.values() for s in v if s["new"]} - info.keys()
        info.update(_units(cur, list(new_ids)))
        dropped, items, seen = [], [], set()

        def add(uid: str, source: str, via: dict | None = None):
            if uid in seen or uid not in info:
                return
            u = info[uid]
            status, note = status_of(u, d) if o.legal_status else ("unchecked", None)
            if status == "overruled" and not f.include_overruled:
                dropped.append({"unit_id": uid, "reason": note})
                return
            seen.add(uid)
            items.append({"unit_id": uid, "source": source, "status": status, "status_note": note,
                          "via": via, "ranks": ranks.get(uid, {}), "best_chunk": best.get(uid, f"{uid}#0")})
            if status == "repealed":
                maps = succ.get(uid, [])
                for m in maps:
                    if m["new"]:
                        add(m["new"], source if source == "cited" else "successor",
                            {"replaces": uid, "relation": m["relation"], "verified": m["verified"]})
                if not maps:
                    items[-1]["status_note"] += "; no successor in the corpus"
                elif all(m["new"] is None for m in maps):
                    items[-1]["status_note"] += "; omitted, no successor provision"

        for uid in pinned:
            add(uid, "cited")
        n_pinned = len(items)
        for uid in order:
            if uid in info:
                add(uid, "search")
        # an uncited repealed provision is replaced, not shown: keep it only in the trace
        if o.legal_status:
            replaced = [x for x in items[n_pinned:] if x["status"] == "repealed" and x["source"] != "cited"]
            trace["replaced"] = [{"unit_id": x["unit_id"], "note": x["status_note"]} for x in replaced]
            items = items[:n_pinned] + [x for x in items[n_pinned:] if x not in replaced]
        trace["dropped"] = dropped
        t["legal_status"] = time.perf_counter() - t0

        # linked authority: the provision the retrieved judgments are about (curated case-level statutes),
        # when two of them cite it, or the best-ranked one does, and search has not found it already
        linked = []
        if o.link_statutes:
            votes: dict[str, set[str]] = {}
            top_cases = []
            for x in items[n_pinned:n_pinned + 10]:
                u = info[x["unit_id"]]
                if u["kind"] == "judgment" and u["instrument_id"] not in top_cases:
                    top_cases.append(u["instrument_id"])
                    for st in u["case_statutes"] or []:
                        votes.setdefault(st, set()).add(u["instrument_id"])
            have = {x["unit_id"] for x in items[:n_pinned + RERANK_POOL]} | {(x.get("via") or {}).get("replaces") for x in items}
            cand = [st for st, by in sorted(votes.items(), key=lambda kv: -len(kv[1]))
                    if st not in have and (len(by) >= 2 or (top_cases and top_cases[0] in by))][:2]
            if cand:
                info.update(_units(cur, [c for c in cand if c not in info]))
                if o.legal_status:
                    succ.update(_successors(cur, cand))
                    info.update(_units(cur, [m["new"] for c in cand for m in succ.get(c, []) if m["new"] and m["new"] not in info]))
                before = len(items)
                for c in cand:
                    add(c, "linked", {"cited_by": sorted(votes[c])})
                new = items[before:]
                if o.legal_status:  # a linked repealed provision is shown through its successor only
                    new = [x for x in new if x["status"] != "repealed"]
                items = items[:before]
                linked = new
            trace["linked"] = [{"unit_id": x["unit_id"], "cited_by": sorted(votes.get(x["unit_id"], set()))} for x in linked]

        pinned_items, rest = items[:n_pinned], items[n_pinned:]
        if linked:  # give linked provisions a place in the rerank pool (or, without rerank, after the top 3)
            at = RERANK_POOL - len(linked) if o.rerank else min(3, len(rest))
            rest = rest[:at] + linked + rest[at:]
        pool = rest[:RERANK_POOL]
        if o.rerank and pool:
            t0 = time.perf_counter()
            # a successor (BNS s.318) is also scored through the text of the provision it replaces
            # (IPC s.417 "Punishment for cheating"): the old heading often matches the question better
            proxies = {x["via"]["replaces"]: best.get(x["via"]["replaces"], f'{x["via"]["replaces"]}#0')
                       for x in pool[:8] if x.get("via") and x["source"] == "successor"}
            proxies = dict(list(proxies.items())[:4])
            want = [x["best_chunk"] for x in pool] + list(proxies.values())
            cur.execute("select chunk_id, embed_text from chunk where chunk_id = any(%s)", (want,))
            texts = dict(cur.fetchall())
            passages = [texts.get(x["best_chunk"], info[x["unit_id"]]["text"])[:1500] for x in pool]
            proxy_ids = [u for u, c in proxies.items() if c in texts]
            scores = models.rerank(query, passages + [texts[proxies[u]][:1500] for u in proxy_ids])
            proxy_score = dict(zip(proxy_ids, scores[len(pool):]))
            for x, s in zip(pool, scores[:len(pool)]):
                inherited = proxy_score.get((x.get("via") or {}).get("replaces"), 0.0) if x["source"] == "successor" else 0.0
                x["ranks"] = {**x["ranks"], "rerank": round(max(s, inherited), 4)}
                if inherited > s:
                    x["ranks"]["rerank_via_replaced"] = True
            pool.sort(key=lambda x: -x["ranks"]["rerank"])
            t["rerank"] = time.perf_counter() - t0
        rest = pool + rest[RERANK_POOL:]

        final, per_case = list(pinned_items), {}
        held = []
        for x in rest:
            if len(final) >= max(o.k, n_pinned):
                break
            iid = info[x["unit_id"]]["instrument_id"]
            if o.diversity and info[x["unit_id"]]["kind"] == "judgment" and per_case.get(iid, 0) >= MAX_PER_JUDGMENT:
                held.append(x)
                continue
            per_case[iid] = per_case.get(iid, 0) + 1
            final.append(x)
        final += held[:max(0, o.k - len(final))]

    hits = []
    for n, x in enumerate(final, 1):
        u = info[x["unit_id"]]
        hits.append({
            "rank": n, "unit_id": x["unit_id"], "instrument_id": u["instrument_id"], "kind": u["kind"],
            "title": u["title"], "citation": u["citation"], "heading": u["heading"], "path": u["path"],
            "text": u["text"], "status": x["status"], "status_note": x["status_note"], "via": x["via"],
            "source": x["source"], "opinion_author": u["opinion_author"],
            "decided_on": str(u["decided_on"]) if u["decided_on"] else None,
            "reporter_citations": u["reporter_citations"], "statutes_cited": u["statutes_cited"],
            "topics": u["topics"], "scores": x["ranks"]})
    trace["timings_ms"] = {k: round(v * 1000, 1) for k, v in t.items()}
    return {"hits": hits, "trace": trace}


def fetch(unit_ids: list[str], as_of: dt.date | None = None) -> list[dict]:
    """Units by id, in the same shape and with the same legal-status rules as retrieve() hits.
    A repealed unit is returned with its mapped successor(s) right after it."""
    d = as_of or dt.date.today()
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        info = _units(cur, unit_ids)
        succ = _successors(cur, list(info))
        info.update(_units(cur, [m["new"] for v in succ.values() for m in v if m["new"] and m["new"] not in info]))
    out = []
    for uid in unit_ids:
        if uid not in info:
            continue
        status, note = status_of(info[uid], d)
        if status == "overruled":
            continue
        u = info[uid]
        out.append({"rank": len(out) + 1, "unit_id": uid, "instrument_id": u["instrument_id"], "kind": u["kind"],
                    "title": u["title"], "citation": u["citation"], "heading": u["heading"], "path": u["path"],
                    "text": u["text"], "status": status, "status_note": note, "via": None, "source": "curated",
                    "opinion_author": u["opinion_author"], "decided_on": str(u["decided_on"]) if u["decided_on"] else None,
                    "reporter_citations": u["reporter_citations"], "statutes_cited": u["statutes_cited"],
                    "topics": u["topics"], "scores": {}})
        if status == "repealed":
            out[-1]["replaced_by"] = [m["new"] for m in succ.get(uid, []) if m["new"]]
    return out
