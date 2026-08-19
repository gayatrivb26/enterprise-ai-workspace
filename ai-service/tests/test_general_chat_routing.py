"""
Questions that must never reach document retrieval.

This is the behaviour that produced the worst answers in practice. Asking "who
built you?" with a novel sitting in the retrieval context made a *correctly
grounded* model reply that the novel did not say who built it — technically
true, completely useless. The fix is not a better prompt: it is not retrieving
at all for questions the corpus cannot answer.
"""
from __future__ import annotations

import pytest

from app.persona import ASSISTANT_IDENTITY
from app.router import classify


# ── Identity ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("question", [
    "who built you",
    "who build you and how?",
    "who created you",
    "who made you?",
    "how were you built",
    "how do you work",
    "what are you",
    "who are you?",
    "what model are you",
    "which model do you use",
    "what is your purpose",
    "what is Folio",
    "what can you do",
])
def test_questions_about_the_assistant_never_reach_retrieval(question):
    assert classify(question, allow_llm=False).intent == "chat"


# ── Greetings and pleasantries ──────────────────────────────────────────────

@pytest.mark.parametrize("message", [
    "hi",
    "Hi. How are you",
    "hey there",
    "hello!",
    "hi, how are you?",
    "hey, what's up",
    "good morning",
    "thanks",
    "thank you so much",
    "ok",
    "bye",
])
def test_small_talk_never_reaches_retrieval(message):
    assert classify(message, allow_llm=False).intent == "chat"


# ── Still routes real work correctly ────────────────────────────────────────

def test_document_questions_still_route_to_retrieval():
    assert classify("What is the leave policy?", allow_llm=False).intent == "docs"


def test_repository_questions_still_route_to_the_agent():
    routing = classify("who changed authentication recently?", has_repository=True)
    assert routing.intent == "code"


def test_a_greeting_followed_by_a_real_question_is_not_swallowed():
    """
    "hi" is small talk; "hi, what does the handbook say about leave?" is not.
    The greeting pattern must anchor to the whole message.
    """
    routing = classify("hi, what does the handbook say about leave?", allow_llm=False)
    assert routing.intent == "docs"


# ── Persona ─────────────────────────────────────────────────────────────────

def test_persona_states_who_built_it():
    assert "Gayatri Bhosale" in ASSISTANT_IDENTITY


def test_persona_forbids_talking_about_sources_on_the_chat_path():
    """
    Nothing was retrieved on this path, so any mention of sources is a
    fabrication. The instruction has to say so explicitly.
    """
    lowered = ASSISTANT_IDENTITY.lower()
    assert "do not mention sources" in lowered
    assert "provided sources do not contain" in lowered


def test_persona_describes_the_real_capabilities():
    lowered = ASSISTANT_IDENTITY.lower()
    for capability in ["document", "github", "citation" if "citation" in lowered else "cite"]:
        assert capability in lowered
