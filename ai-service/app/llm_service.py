"""
app/llm_service.py — the LLM service layer.

Everything that should happen around *every* model call, in one place:

  * retries with exponential backoff on transient provider errors
  * streaming, with a cached-answer replay path that looks identical to the UI
  * token accounting and USD cost estimation, written to the llm_usage ledger
  * answer caching keyed on the full context that produced the answer

Callers (`rag.retrieval`, the agents, the eval harness) get a single obvious
entry point and never think about any of it.
"""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from app import cache
from app.config import settings
from app.db import record_usage
from app.llm_client import RawUsage, complete as raw_complete, stream_complete

log = logging.getLogger(__name__)

# Provider errors worth retrying: transient transport / capacity problems.
# A prompt that is simply invalid must fail fast rather than burn three tries.
_RETRYABLE_MARKERS = (
    "429", "500", "502", "503", "504",
    "deadline", "timeout", "temporarily", "unavailable",
    "overloaded", "resource has been exhausted", "rate limit",
    "connection", "reset by peer",
)

# A *daily* quota is not a transient failure. Retrying it three times with
# sub-second backoff cannot possibly succeed, wastes the caller's time and
# hammers an endpoint that has already said no.
_DAILY_QUOTA_MARKERS = (
    "perday", "per day", "requestsperday", "generaterequestsperday",
    "free_tier_requests", "free tier",
)


def _classify_error(err: Exception) -> str:
    """One of: quota, rate_limit, transport, fatal."""
    text = f"{type(err).__name__} {err}".lower()

    if "429" in text or "resource_exhausted" in text or "quota" in text:
        return "quota" if any(m in text for m in _DAILY_QUOTA_MARKERS) else "rate_limit"
    if any(marker in text for marker in _RETRYABLE_MARKERS):
        return "transport"
    return "fatal"


def _retry_after(err: Exception) -> float | None:
    """The delay the provider itself asked for, when it names one."""
    match = re.search(r"retry in ([0-9.]+)s", str(err), re.IGNORECASE)
    if not match:
        match = re.search(r"'retryDelay':\s*'([0-9.]+)s'", str(err))
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _is_retryable(err: Exception) -> bool:
    # Everything except a exhausted daily allowance and outright invalid input.
    return _classify_error(err) in ("rate_limit", "transport")


@dataclass
class Usage:
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    cache_hit: bool = False
    latency_ms: int = 0
    attempts: int = 1

    @property
    def total_tokens(self) -> int:
        return self.tokens_in + self.tokens_out


def estimate_cost(tokens_in: int, tokens_out: int) -> float:
    return round(
        tokens_in / 1_000_000 * settings.price_input_per_mtok
        + tokens_out / 1_000_000 * settings.price_output_per_mtok,
        6,
    )


def _finalize(raw: RawUsage, started: float, attempts: int, cache_hit: bool) -> Usage:
    return Usage(
        model=settings.gemini_model,
        tokens_in=raw.tokens_in,
        tokens_out=raw.tokens_out,
        cost_usd=estimate_cost(raw.tokens_in, raw.tokens_out),
        cache_hit=cache_hit,
        latency_ms=int((time.perf_counter() - started) * 1000),
        attempts=attempts,
    )


# ---------------------------------------------------------------------------
# One-shot
# ---------------------------------------------------------------------------

def complete(
    prompt: str,
    *,
    max_tokens: int = 500,
    temperature: float = 0.2,
    system_instruction: str | None = None,
    operation: str = "complete",
    user_id: str | None = None,
) -> tuple[str, Usage]:
    started = time.perf_counter()
    last_error: Exception | None = None

    for attempt in range(1, settings.llm_max_retries + 1):
        try:
            text, raw = raw_complete(prompt, max_tokens, temperature, system_instruction)
            usage = _finalize(raw, started, attempt, cache_hit=False)
            record_usage(
                user_id=user_id, chat_id=None, operation=operation,
                model=usage.model, prompt_version=settings.prompt_version,
                tokens_in=usage.tokens_in, tokens_out=usage.tokens_out,
                cost_usd=usage.cost_usd, cache_hit=False, latency_ms=usage.latency_ms,
            )
            return text, usage
        except Exception as e:
            last_error = e
            kind = _classify_error(e)
            if attempt >= settings.llm_max_retries or kind not in ("rate_limit", "transport"):
                break
            # Prefer the provider's own advice over our backoff curve, but do
            # not sit on a request for longer than a user will wait.
            delay = min(
                _retry_after(e) or settings.llm_retry_base_delay * (2 ** (attempt - 1)),
                settings.llm_max_retry_delay,
            )
            log.warning("LLM call failed (attempt %d), retrying in %.1fs: %s", attempt, delay, e)
            time.sleep(delay)

    raise LlmError(str(last_error), _classify_error(last_error)) from last_error


class LlmError(RuntimeError):
    """
    Raised when the provider fails after all retries are exhausted.

    `kind` lets callers turn this into something a user can act on. "The AI
    service reported an error" tells nobody anything; "your daily free-tier
    quota is used up" tells them exactly what to do next.
    """

    def __init__(self, message: str, kind: str = "fatal") -> None:
        super().__init__(message)
        self.kind = kind

    @property
    def user_message(self) -> str:
        if self.kind == "quota":
            return (
                "The daily quota for this model has been used up. Google's free "
                "tier allows a limited number of requests per day and resets "
                "every 24 hours — add billing to the API key, or try again "
                "tomorrow."
            )
        if self.kind == "rate_limit":
            return "Too many requests just now. Wait a few seconds and try again."
        if self.kind == "transport":
            return "The AI provider is unreachable right now. Please try again."
        return "The AI provider could not complete this request."


# ---------------------------------------------------------------------------
# Streaming (with cache)
# ---------------------------------------------------------------------------

@dataclass
class StreamResult:
    """Mutable handle the caller reads after the generator is exhausted."""
    text: str = ""
    usage: Usage = field(default_factory=Usage)
    sources: list[dict[str, Any]] = field(default_factory=list)


def stream_answer(
    prompt: str,
    *,
    cache_key: str | None = None,
    cache_document_ids: Sequence[str] = (),
    sources: Sequence[dict[str, Any]] = (),
    max_tokens: int | None = None,
    temperature: float = 0.2,
    system_instruction: str | None = None,
    operation: str = "chat",
    user_id: str | None = None,
    chat_id: str | None = None,
    result: StreamResult | None = None,
    on_cache_hit: Callable[[], None] | None = None,
) -> Iterator[str]:
    """
    Yields answer deltas, serving from cache when the exact same context has
    been answered before.

    A cache hit is replayed in small pieces rather than as one blob so the UI
    animates identically either way — the user should not be able to tell a
    cached answer from a fresh one except by how fast it arrives.

    Retries only happen *before* the first delta is emitted. Once bytes are on
    the wire a retry would duplicate text, so at that point the error is
    surfaced instead.
    """
    started = time.perf_counter()
    result = result if result is not None else StreamResult()
    result.sources = list(sources)
    max_tokens = max_tokens or settings.llm_max_output_tokens

    # --- Cache lookup ---------------------------------------------------
    if cache_key:
        hit = cache.get_answer(cache_key)
        cache.record_hit(bool(hit))
        if hit and hit.get("text"):
            if on_cache_hit:
                on_cache_hit()
            text = hit["text"]
            result.text = text
            result.sources = hit.get("sources", list(sources))
            result.usage = Usage(
                model=hit.get("model", settings.gemini_model),
                tokens_in=hit.get("tokens_in", 0),
                tokens_out=hit.get("tokens_out", 0),
                cost_usd=0.0,          # a replay costs nothing
                cache_hit=True,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
            record_usage(
                user_id=user_id, chat_id=chat_id, operation=operation,
                model=result.usage.model, prompt_version=settings.prompt_version,
                tokens_in=0, tokens_out=0, cost_usd=0.0,
                cache_hit=True, latency_ms=result.usage.latency_ms,
            )
            yield from _replay(text)
            return

    # --- Live generation ------------------------------------------------
    last_error: Exception | None = None
    for attempt in range(1, settings.llm_max_retries + 1):
        usage_sink: list[RawUsage] = []
        emitted = False
        buffer: list[str] = []
        try:
            for delta in stream_complete(
                prompt, max_tokens, temperature, system_instruction, usage_sink
            ):
                emitted = True
                buffer.append(delta)
                yield delta

            text = "".join(buffer)
            raw = usage_sink[0] if usage_sink else RawUsage()
            result.text = text
            result.usage = _finalize(raw, started, attempt, cache_hit=False)

            record_usage(
                user_id=user_id, chat_id=chat_id, operation=operation,
                model=result.usage.model, prompt_version=settings.prompt_version,
                tokens_in=result.usage.tokens_in, tokens_out=result.usage.tokens_out,
                cost_usd=result.usage.cost_usd, cache_hit=False,
                latency_ms=result.usage.latency_ms,
            )

            if cache_key and text.strip():
                cache.set_answer(
                    cache_key,
                    {
                        "text": text,
                        "sources": list(result.sources),
                        "model": result.usage.model,
                        "tokens_in": result.usage.tokens_in,
                        "tokens_out": result.usage.tokens_out,
                        "prompt_version": settings.prompt_version,
                    },
                    cache_document_ids,
                )
            return

        except Exception as e:
            last_error = e
            kind = _classify_error(e)
            # Mid-stream failures cannot be retried without duplicating text.
            if emitted or attempt >= settings.llm_max_retries or kind not in ("rate_limit", "transport"):
                break
            delay = min(
                _retry_after(e) or settings.llm_retry_base_delay * (2 ** (attempt - 1)),
                settings.llm_max_retry_delay,
            )
            log.warning("Stream failed before first token (attempt %d), retrying in %.1fs: %s",
                        attempt, delay, e)
            time.sleep(delay)

    raise LlmError(str(last_error), _classify_error(last_error)) from last_error


def _replay(text: str, piece: int = 24) -> Iterator[str]:
    """Re-emit a cached answer in word-aligned pieces so it still 'types'."""
    i = 0
    n = len(text)
    while i < n:
        end = min(i + piece, n)
        if end < n:
            space = text.rfind(" ", i + 1, end + 1)
            if space > i:
                end = space + 1
        yield text[i:end]
        i = end
