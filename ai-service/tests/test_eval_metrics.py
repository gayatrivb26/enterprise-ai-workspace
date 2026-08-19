"""
Eval metric behaviour.

The scoring functions are the harness's only opinion about quality, so they get
tested directly. Groundedness in particular has to punish the specific failure
it exists to catch: a confident answer citing a source that was never retrieved.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from eval.run_eval import (
    citation_score,
    correctness_score,
    groundedness_score,
    retrieval_score,
)


@dataclass
class FakeChunk:
    source_path: str


CHUNKS = [FakeChunk("docs/leave-policy.md"), FakeChunk("docs/handbook.pdf")]


# ── Retrieval ───────────────────────────────────────────────────────────────

def test_retrieval_hits_when_expected_document_is_present():
    assert retrieval_score("leave-policy", CHUNKS) == 1.0


def test_retrieval_misses_when_absent():
    assert retrieval_score("expenses", CHUNKS) == 0.0


def test_retrieval_is_case_insensitive():
    assert retrieval_score("LEAVE-POLICY", CHUNKS) == 1.0


def test_no_expectation_is_not_a_failure():
    assert retrieval_score("", CHUNKS) == 1.0


# ── Correctness ─────────────────────────────────────────────────────────────

def test_correctness_is_a_fraction_of_expected_facts():
    assert correctness_score(["leave", "days"], "Leave is 25 days") == 1.0
    assert correctness_score(["leave", "days"], "Leave is generous") == 0.5
    assert correctness_score(["leave"], "Nothing relevant") == 0.0


def test_no_keywords_means_nothing_to_check():
    assert correctness_score([], "anything at all") == 1.0


# ── Groundedness ────────────────────────────────────────────────────────────

def test_a_real_citation_is_grounded():
    assert groundedness_score("Leave is 25 days [Source 1].", CHUNKS) == 1.0


def test_an_answer_with_no_citation_is_not_grounded():
    assert groundedness_score("Leave is 25 days.", CHUNKS) == 0.0


def test_a_fabricated_citation_scores_zero():
    """
    [Source 7] when only two chunks were retrieved is an invented source —
    the single clearest hallucination signal available, so it fails outright.
    """
    assert groundedness_score("Leave is 25 days [Source 7].", CHUNKS) == 0.0


def test_mixing_a_real_and_an_invented_citation_is_penalised_proportionally():
    score = groundedness_score("See [Source 1] and [Source 9].", CHUNKS)
    assert score == pytest.approx(0.5)


def test_declining_an_unanswerable_question_is_correct():
    assert groundedness_score("The sources do not cover this.", CHUNKS,
                              should_answer=False) == 1.0


def test_citing_anything_on_an_unanswerable_question_is_wrong():
    assert groundedness_score("It is 25 days [Source 1].", CHUNKS,
                              should_answer=False) == 0.0


def test_citing_nothing_when_nothing_was_retrieved_is_correct():
    assert groundedness_score("I could not find that.", []) == 1.0


# ── Citation accuracy ───────────────────────────────────────────────────────

def test_citation_points_at_the_expected_document():
    assert citation_score("leave-policy", "Leave is 25 days [Source 1].", CHUNKS) == 1.0


def test_citing_the_wrong_document_scores_zero():
    assert citation_score("leave-policy", "Leave is 25 days [Source 2].", CHUNKS) == 0.0


def test_partial_citation_accuracy():
    score = citation_score("leave-policy", "See [Source 1] and [Source 2].", CHUNKS)
    assert score == pytest.approx(0.5)


def test_uncited_answer_has_no_citation_accuracy():
    assert citation_score("leave-policy", "Leave is 25 days.", CHUNKS) == 0.0


# ── Dataset ─────────────────────────────────────────────────────────────────

def test_question_set_is_valid_and_covers_unanswerable_cases():
    path = Path(__file__).resolve().parents[1] / "eval" / "questions.json"
    cases = json.loads(path.read_text(encoding="utf-8"))

    assert len(cases) >= 20, "the design doc asks for roughly 20 cases"
    for case in cases:
        assert case.get("question"), "every case needs a question"

    # A corpus-grounded assistant must be measured on what it refuses, not
    # only on what it answers.
    assert any(c.get("answerable") is False for c in cases)
