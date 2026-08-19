"""
Hybrid retrieval: salient-term extraction, rank fusion and the relevance band.

These back the fix for the worst failure this system had — a fact sitting
verbatim in an indexed document that the assistant reported as "not mentioned"
because vector similarity alone did not bring it back.
"""
from __future__ import annotations

from rag.retrieval import _fuse, _within_relevance_band, salient_terms


# ── Salient terms ───────────────────────────────────────────────────────────

def test_stopwords_are_dropped():
    terms = salient_terms("What is the notice period for resignation?")
    lowered = {t.lower() for t in terms}
    assert "notice" in lowered
    assert "resignation" in lowered
    assert "what" not in lowered and "the" not in lowered and "for" not in lowered


def test_longest_terms_come_first():
    """The keyword pass has a budget, and a long token is far more likely to be
    the distinguishing one."""
    terms = salient_terms("reimbursement for taxi")
    assert terms[0].lower() == "reimbursement"


def test_identifiers_and_filenames_survive():
    terms = salient_terms("what does policy-2024.v3 say about genpact qn.txt?")
    joined = " ".join(terms).lower()
    assert "policy-2024.v3" in joined
    assert "genpact" in joined


def test_very_short_tokens_are_ignored():
    assert all(len(t) >= 3 for t in salient_terms("is a b cd efg"))


def test_empty_query_yields_nothing():
    assert salient_terms("") == []


def test_duplicates_are_collapsed():
    terms = salient_terms("leave leave leave policy")
    assert len([t for t in terms if t.lower() == "leave"]) == 1


# ── Rank fusion ─────────────────────────────────────────────────────────────

def hit(chunk_id: str, distance=None, **extra):
    return {"id": chunk_id, "text": chunk_id, "metadata": {}, "distance": distance, **extra}


def test_a_chunk_found_by_both_retrievers_ranks_top():
    """RRF's whole point: agreement between two independent signals wins."""
    vector = [hit("a", 0.9), hit("b", 0.4)]
    keyword = [hit("b"), hit("c")]

    fused = _fuse([vector, keyword])
    assert fused[0]["id"] == "b"


def test_fusion_keeps_everything_from_both_lists():
    fused = _fuse([[hit("a", 0.2)], [hit("z")]])
    assert {h["id"] for h in fused} == {"a", "z"}


def test_fusion_prefers_the_copy_carrying_an_embedding():
    """MMR needs the vector, so the richer copy must survive the merge."""
    vector = [hit("a", 0.3, embedding=[1.0, 0.0])]
    keyword = [hit("a")]

    fused = _fuse([vector, keyword])
    assert fused[0].get("embedding") == [1.0, 0.0]


def test_keyword_only_result_can_outrank_a_weak_vector_result():
    vector = [hit("weak1", 1.4), hit("weak2", 1.5), hit("target", 1.6)]
    keyword = [hit("target")]

    fused = _fuse([vector, keyword])
    assert fused[0]["id"] == "target"


# ── Relevance band ──────────────────────────────────────────────────────────

def test_band_is_relative_to_the_best_hit():
    """
    An absolute cutoff is brittle across models and corpora. With a best hit of
    0.2, a 1.0 is far away in relative terms even though it would pass a fixed
    1.15 ceiling.
    """
    kept = _within_relevance_band([hit("best", 0.2), hit("near", 0.3), hit("far", 1.0)])
    ids = {h["id"] for h in kept}
    assert "best" in ids and "near" in ids
    assert "far" not in ids


def test_band_widens_when_everything_is_distant():
    """If the closest hit is itself far away, the band must not collapse to it
    alone — the model still needs context to judge."""
    kept = _within_relevance_band([hit("a", 1.0), hit("b", 1.2)])
    assert len(kept) == 2


def test_keyword_hits_are_never_filtered_out():
    """A literal match is a stronger signal than proximity, and has no
    distance to compare in the first place."""
    kept = _within_relevance_band([hit("vector", 0.2), hit("keyword", None)])
    assert any(h["id"] == "keyword" for h in kept)


def test_something_is_always_returned():
    kept = _within_relevance_band([hit("only", 1.95)])
    assert len(kept) == 1


def test_no_scored_hits_passes_everything_through():
    hits = [hit("k1"), hit("k2")]
    assert _within_relevance_band(hits) == hits
