"""
Turning provider failures into something a user can act on.

The trigger for this: a Gemini free-tier daily quota (20 requests/day) surfaced
in the UI as "The AI service reported an error", and was retried three times
with sub-second backoff — retries that could not possibly succeed, against an
endpoint that had already said no for the next 24 hours.
"""
from __future__ import annotations

import pytest

from app.llm_service import LlmError, _classify_error, _is_retryable, _retry_after

DAILY_QUOTA = (
    "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You exceeded your "
    "current quota... Quota exceeded for metric: "
    "generativelanguage.googleapis.com/generate_content_free_tier_requests, "
    "limit: 20, model: gemini-3.7-flash. Please retry in 10.847937787s.', "
    "'quotaId': 'GenerateRequestsPerDayPerProjectPerModel-FreeTier'}}"
)

PER_MINUTE = (
    "429 RESOURCE_EXHAUSTED. Quota exceeded for quota metric 'Requests per minute'. "
    "Please retry in 2.5s."
)


def test_daily_quota_is_recognised():
    assert _classify_error(RuntimeError(DAILY_QUOTA)) == "quota"


def test_daily_quota_is_not_retried():
    """The whole point: three fast retries against a 24-hour limit are waste."""
    assert _is_retryable(RuntimeError(DAILY_QUOTA)) is False


def test_short_rate_limit_is_retried():
    assert _classify_error(RuntimeError(PER_MINUTE)) == "rate_limit"
    assert _is_retryable(RuntimeError(PER_MINUTE)) is True


@pytest.mark.parametrize("message", [
    "503 Service Unavailable",
    "connection reset by peer",
    "deadline exceeded",
    "The model is overloaded",
])
def test_transient_failures_are_retried(message):
    assert _is_retryable(RuntimeError(message)) is True


def test_a_bad_request_is_not_retried():
    assert _classify_error(ValueError("400 INVALID_ARGUMENT: prompt too long")) == "fatal"
    assert _is_retryable(ValueError("400 INVALID_ARGUMENT")) is False


def test_the_providers_own_delay_is_read():
    assert _retry_after(RuntimeError(PER_MINUTE)) == pytest.approx(2.5)
    assert _retry_after(RuntimeError(DAILY_QUOTA)) == pytest.approx(10.847937787)


def test_no_delay_when_none_is_offered():
    assert _retry_after(RuntimeError("503 Service Unavailable")) is None


# ── What the user is told ───────────────────────────────────────────────────

def test_quota_message_explains_the_limit_and_the_fix():
    message = LlmError(DAILY_QUOTA, "quota").user_message
    assert "daily" in message.lower()
    assert "billing" in message.lower() or "tomorrow" in message.lower()
    # It must not leak the raw provider payload at the user.
    assert "RESOURCE_EXHAUSTED" not in message


def test_rate_limit_message_says_to_wait():
    assert "wait" in LlmError(PER_MINUTE, "rate_limit").user_message.lower()


def test_messages_are_distinct_per_kind():
    kinds = ["quota", "rate_limit", "transport", "fatal"]
    messages = {LlmError("x", k).user_message for k in kinds}
    assert len(messages) == len(kinds)


def test_default_kind_is_fatal():
    assert LlmError("something broke").kind == "fatal"
