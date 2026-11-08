"""Stage 4 tests. Need the local database (and, for the retrieve() tests, the local models)."""
import datetime as dt

import psycopg
import pytest

from nyayantra import citations
from nyayantra.config import DSN
from nyayantra.retrieve import UNIT_COLS, UNIT_SQL, Filters, Options, retrieve, status_of


@pytest.mark.parametrize("q, units, unresolved", [
    ("Which section of the Indian Contract Act, 1872 deals with restraint of trade?", [], []),
    ("punishment under s.420 IPC", ["IPC-1860:s420"], []),
    ("IPC 498A cruelty", ["IPC-1860:s498A"], []),
    ("Explain rule 7 of the DPDP Rules", ["DPDPR-2025:r7"], []),
    ("under the Contract Act, s 73 and s.74", ["ICA-1872:s73", "ICA-1872:s74"], []),
    ("A&C Act s. 11", ["ACA-1996:s11"], []),
    ("BNS section 318(4)", ["BNS-2023:s318"], []),
    # invented provisions must not resolve
    ("What does Section 27B of the Indian Contract Act, 1872 provide?", [], ["ICA-1872 s.27B"]),
    ("Section 112 of the Advocates Act, 1961 on AI", [], ["AA-1961 s.112"]),
])
def test_statute_citations(q, units, unresolved):
    c = citations.find(q)
    assert c.unit_ids == units
    assert c.unresolved == unresolved


@pytest.mark.parametrize("q, cases", [
    ("Puttaswamy, (2017) 10 SCC 1", ["SC-2017-PUTTASWAMY"]),
    ("What did Percept D'Mark hold?", ["SC-2006-PERCEPT"]),
    ("Shreya Singhal and section 66A", ["SC-2015-SHREYA-SINGHAL"]),
    ("Mehra Infotech Pvt Ltd v. Union of India, (2022) 8 SCC 614", []),   # invented case: nothing
    ("Which section of the Indian Contract Act applies?", []),
])
def test_case_citations(q, cases):
    assert citations.find(q).cases == cases


def _unit(uid):
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute(UNIT_SQL, ([uid],))
        return dict(zip(UNIT_COLS, cur.fetchone()))


def test_status_by_date():
    ipc = _unit("IPC-1860:s420")
    assert status_of(ipc, dt.date(2020, 1, 1))[0] == "in_force"
    assert status_of(ipc, dt.date(2024, 7, 1))[0] == "repealed"
    assert status_of(_unit("DPDPA-2023:s8"), dt.date(2026, 1, 1))[0] == "not_yet_in_force"
    assert status_of(_unit("DPDPA-2023:s8"), dt.date(2027, 6, 1))[0] == "in_force"
    nn = _unit("SC-2023-NN-GLOBAL:p1")
    assert status_of(nn, dt.date(2023, 6, 1))[0] == "in_force"
    assert status_of(nn, dt.date(2024, 1, 1))[0] == "overruled"


@pytest.fixture(scope="module")
def fast():
    return Options(k=5, rerank=False)


def test_cited_repealed_section_brings_successor(fast):
    hits = retrieve("punishment under s.420 IPC", opts=fast)["hits"]
    assert hits[0]["unit_id"] == "IPC-1860:s420" and hits[0]["status"] == "repealed"
    assert hits[1]["unit_id"] == "BNS-2023:s318" and hits[1]["via"]["replaces"] == "IPC-1860:s420"


def test_uncited_repealed_hits_are_replaced(fast):
    r = retrieve("What is the punishment for cheating under the new criminal law?", opts=fast)
    ids = [h["unit_id"] for h in r["hits"]]
    assert "BNS-2023:s318" in ids
    assert not any(i.startswith("IPC-1860") for i in ids)
    assert r["trace"]["replaced"]


def test_as_of_past_date_hides_later_law(fast):
    r = retrieve("punishment for cheating", Filters(as_of=dt.date(2020, 6, 30)), fast)
    ids = [h["unit_id"] for h in r["hits"]]
    assert not any(i.startswith(("BNS-2023", "BSA-2023", "DPDP")) for i in ids)
    assert any(i.startswith("IPC-1860") and h["status"] == "in_force" for i, h in zip(ids, r["hits"]))


def test_overruled_judgment_dropped(fast):
    r = retrieve("N.N. Global: is an arbitration clause in an unstamped agreement enforceable?", opts=fast)
    assert not any(h["instrument_id"] == "SC-2023-NN-GLOBAL" for h in r["hits"])
    assert any(d["unit_id"].startswith("SC-2023-NN-GLOBAL") for d in r["trace"]["dropped"])


def test_not_yet_in_force_is_flagged(fast):
    hits = retrieve("intimation of personal data breach to the Board", opts=fast)["hits"]
    r7 = [h for h in hits if h["unit_id"] == "DPDPR-2025:r7"]
    assert r7 and r7[0]["status"] == "not_yet_in_force"


def test_filters_restrict_instruments(fast):
    hits = retrieve("consent", Filters(instruments=["ICA-1872"]), fast)["hits"]
    assert hits and all(h["instrument_id"] == "ICA-1872" for h in hits)
