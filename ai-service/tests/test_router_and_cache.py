"""
Intent routing and answer-cache key derivation.

Both are pure functions over their inputs, which is deliberate: routing decides
what work happens, and the cache key decides whether a *previous* answer may be
reused. Getting either wrong is expensive or unsafe, and neither should need a
database or a model to test.
"""
from __future__ import annotations

import pytest

from app import cache
from app.router import classify


# ── Routing ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("message", ["hi", "Hello!", "hey there", "thanks", "thank you", "ok"])
def test_greetings_never_trigger_retrieval(message):
    assert classify(message).intent == "chat"


@pytest.mark.parametrize("message", ["what can you do?", "who are you", "how do you work"])
def test_capability_questions_are_chat(message):
    assert classify(message).intent == "chat"


@pytest.mark.parametrize("message", [
    "Who changed authentication recently?",
    "where is the login endpoint implemented?",
    "show me recent commits",
    "what changed in the codebase last week?",
])
def test_repository_questions_route_to_code_when_a_repo_exists(message):
    assert classify(message, has_repository=True).intent == "code"


def test_repository_vocabulary_does_not_route_to_code_without_a_repo():
    # Routing to a capability that cannot answer is worse than not routing:
    # the user gets a confident "no repository connected" for a document
    # question that retrieval could have answered.
    routing = classify("who changed the authentication policy?",
                       has_repository=False, allow_llm=False)
    assert routing.intent == "docs"


@pytest.mark.parametrize("message", [
    "summarize this meeting transcript",
    "what are the action items?",
    "minutes of the meeting please",
])
def test_meeting_vocabulary_routes_to_meeting(message):
    assert classify(message).intent == "meeting"


def test_a_pasted_transcript_is_detected_by_shape():
    transcript = "\n".join([
        "Alice: Let's start with the roadmap for next quarter and the hiring plan.",
        "Bob: I think we should prioritise the billing migration before anything else.",
        "Carol: Agreed, but we need design sign-off first from the platform team.",
        "Dave: I'll own the migration plan and circulate it by Friday afternoon.",
        "Alice: Great, let's reconvene next week to review progress on all of this.",
    ]) * 2
    assert classify(transcript).intent == "meeting"


def test_short_speaker_text_is_not_mistaken_for_a_transcript():
    assert classify("Alice: hi\nBob: hey", allow_llm=False).intent != "meeting"


def test_empty_message_is_chat():
    assert classify("").intent == "chat"


def test_no_documents_means_general_chat():
    assert classify("what is the capital of France?", has_documents=False).intent == "chat"


def test_classifier_failure_falls_back_to_grounded_retrieval(monkeypatch):
    # A wrong "docs" answer still cites its sources and can be checked; a wrong
    # "chat" answer cannot. Grounded is the safe default.
    def boom(*args, **kwargs):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr("app.router.complete", boom)
    assert classify("something genuinely ambiguous here").intent == "docs"


# ── Cache keys ──────────────────────────────────────────────────────────────

BASE = dict(
    user_id="u1",
    question="What is our leave policy?",
    document_ids=["d1"],
    chunk_ids=["c1", "c2"],
    model="gemini-flash",
    prompt_version="v2",
)


def test_identical_context_produces_the_same_key():
    assert cache.answer_key(**BASE) == cache.answer_key(**BASE)


def test_question_normalisation_folds_harmless_differences():
    a = cache.answer_key(**BASE)
    b = cache.answer_key(**{**BASE, "question": "  what is our LEAVE policy  "})
    assert a == b


@pytest.mark.parametrize("field,value", [
    ("user_id", "u2"),
    ("document_ids", ["d2"]),
    ("model", "gemini-pro"),
    ("prompt_version", "v3"),
    ("memory_fingerprint", "abc"),
])
def test_any_change_of_context_changes_the_key(field, value):
    assert cache.answer_key(**BASE) != cache.answer_key(**{**BASE, field: value})


def test_changed_retrieval_changes_the_key():
    """
    The load-bearing property. If re-ingestion changes which chunks come back,
    the key must change too — otherwise a stale answer could be served even
    though the underlying corpus moved.
    """
    assert cache.answer_key(**BASE) != cache.answer_key(**{**BASE, "chunk_ids": ["c1", "c3"]})


def test_chunk_and_document_order_does_not_matter():
    a = cache.answer_key(**{**BASE, "chunk_ids": ["c1", "c2"], "document_ids": ["d1", "d2"]})
    b = cache.answer_key(**{**BASE, "chunk_ids": ["c2", "c1"], "document_ids": ["d2", "d1"]})
    assert a == b


def test_keys_are_namespaced_and_opaque():
    key = cache.answer_key(**BASE)
    assert key.startswith("eaw:ans:")
    # The question must not be recoverable from the key itself.
    assert "leave" not in key
