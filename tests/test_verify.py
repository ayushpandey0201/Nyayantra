"""Citation check (/v1/citations/verify): deterministic lookups against the corpus, no model."""
from nyayantra.verify import check


def _by_text(r):
    return {i["text"]: i for i in r["items"]}


def test_repealed_invented_and_real_citations():
    r = check("Under s.420 IPC and Section 27B of the Indian Contract Act; see Percept D'Mark and "
              "Mehra Infotech v. Union of India, (2022) 8 SCC 614.")
    items = _by_text(r)
    assert items["s.420"]["status"] == "repealed" and "BNS-2023:s318" in items["s.420"]["problem"]
    assert not items["Section 27B"]["found"]
    assert items["Percept D'Mark (India) v. Zaheer Khan"]["ok"]
    assert any(not i["found"] and "Mehra" in i["text"] for i in r["items"])
    assert r["decision"] == "review" and r["usage"]["llm_calls"] == 0


def test_shared_party_word_resolves_to_best_matching_case():
    ids = [i.get("instrument_id") for i in check("the Kailash Nath case on penalties")["items"]]
    assert ids == ["SC-2015-KAILASH-NATH"]


def test_no_citations_is_insufficient():
    assert check("Please review this clause for me.")["decision"] == "insufficient"
