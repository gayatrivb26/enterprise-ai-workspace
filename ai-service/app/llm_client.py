"""
app/llm_client.py

Provider adapter. The rest of the codebase (retrieval, agents, eval) never
imports a provider SDK directly, so swapping Gemini for another vendor means
changing this file only. Orchestration — retries, caching, cost accounting —
deliberately lives one layer up in app/llm_service.py, so this stays a thin,
honest translation of "prompt in, text (and usage) out".
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from google import genai
from google.genai import types

from app.config import settings

_client: genai.Client | None = None


def get_client() -> genai.Client:
    """Created lazily so importing this module never fails without a key."""
    global _client
    if _client is None:
        _client = genai.Client(api_key=settings.gemini_api_key)
    return _client


@dataclass
class RawUsage:
    tokens_in: int = 0
    tokens_out: int = 0


def _read_usage(response) -> RawUsage:
    meta = getattr(response, "usage_metadata", None)
    if meta is None:
        return RawUsage()
    return RawUsage(
        tokens_in=getattr(meta, "prompt_token_count", 0) or 0,
        tokens_out=getattr(meta, "candidates_token_count", 0) or 0,
    )


def _config(max_tokens: int, temperature: float, system_instruction: str | None):
    return types.GenerateContentConfig(
        max_output_tokens=max_tokens,
        temperature=temperature,
        system_instruction=system_instruction,
    )


def complete(
    prompt: str,
    max_tokens: int = 500,
    temperature: float = 0.2,
    system_instruction: str | None = None,
) -> tuple[str, RawUsage]:
    """One-shot generation. Returns (text, usage)."""
    response = get_client().models.generate_content(
        model=settings.gemini_model,
        contents=prompt,
        config=_config(max_tokens, temperature, system_instruction),
    )
    return response.text or "", _read_usage(response)


def stream_complete(
    prompt: str,
    max_tokens: int = 1024,
    temperature: float = 0.2,
    system_instruction: str | None = None,
    usage_sink: list[RawUsage] | None = None,
) -> Iterator[str]:
    """
    Streaming generation, yielding text deltas as they arrive.

    Token counts only appear on the final chunk, and a generator cannot return
    a value to a `for` loop, so the caller passes `usage_sink` and reads the
    usage out of it once iteration completes.
    """
    latest = RawUsage()
    for chunk in get_client().models.generate_content_stream(
        model=settings.gemini_model,
        contents=prompt,
        config=_config(max_tokens, temperature, system_instruction),
    ):
        usage = _read_usage(chunk)
        if usage.tokens_in or usage.tokens_out:
            latest = usage
        if chunk.text:
            yield chunk.text

    if usage_sink is not None:
        usage_sink.append(latest)


def strip_json_fences(text: str) -> str:
    """Gemini often wraps JSON responses in ```json ... ``` fences (Claude
    rarely did). Call this before json.loads() on any structured-output
    call, or you'll get intermittent parse failures."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return text.strip()
