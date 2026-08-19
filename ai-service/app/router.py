"""
app/router.py — intent routing.

Decides what a message actually is before any work is done:

    chat      small talk, or general knowledge unrelated to the corpus
    docs      a question about the user's uploaded documents (RAG)
    code      a question about the connected Git repository (Dev Agent)
    meeting   a transcript to summarise into decisions and action items

Cheap deterministic rules run first and settle the easy majority — greetings,
filenames, obvious repo vocabulary — because sending "hi" to an LLM twice
(once to classify, once to answer) doubles both latency and cost for the most
common message in the product. Only genuinely ambiguous input reaches the
model, and the model is asked for structured output so the decision is parsed,
not interpreted.

Every failure path falls back to `docs`: the grounded, citation-bearing route
is the safe default for an internal knowledge tool.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Literal

from app.llm_client import strip_json_fences
from app.llm_service import LlmError, complete

log = logging.getLogger(__name__)

Intent = Literal["chat", "docs", "code", "meeting"]


@dataclass
class Routing:
    intent: Intent
    confidence: float
    reason: str
    # True when a rule decided it, so callers can log/measure LLM spend.
    deterministic: bool = True


# ── Deterministic signals ──────────────────────────────────────────────────

_GREETING_RE = re.compile(
    r"^\s*(hi|hey|hello|yo|good\s+(morning|afternoon|evening)|thanks?|thank\s+you|"
    r"ok(ay)?|cool|nice|bye|goodbye|got\s+it)"
    # A short trailing salutation: "hey there", "hi folks", "thanks!"
    r"(\s+(there|folks|team|all|again|so\s+much|a\s+lot))?"
    # …optionally followed by pleasantries: "Hi. How are you?", "hey, what's up"
    r"([\s!.,]*(how\s+(are|r)\s+(you|u)( doing| going)?|how'?s\s+it\s+going|"
    r"what'?s\s+up|hope\s+you'?re\s+well))?"
    r"\b[\s!?.,]*$",
    re.IGNORECASE,
)

# Questions about the assistant itself. These must never reach retrieval: the
# corpus cannot answer them, and a grounded model asked "who built you?" with a
# novel in context will earnestly report that the novel does not say.
_CAPABILITY_RE = re.compile(
    r"\b("
    r"what\s+can\s+you\s+do|who\s+are\s+you|what\s+are\s+you|"
    r"how\s+do\s+you\s+work|how\s+(were|was)\s+you\s+(built|made|created|trained)|"
    r"who\s+(built|build|made|created|developed|designed|wrote)\s+you|"
    r"what\s+(model|llm|ai)\s+(are|do)\s+you|which\s+model|"
    r"your\s+(name|purpose|capabilities|creator|author)|"
    r"help\s+me\s+get\s+started|what\s+is\s+folio"
    r")\b",
    re.IGNORECASE,
)

# Vocabulary that only makes sense against a repository.
_CODE_RE = re.compile(
    r"\b(commit(s|ted)?|git|repo(sitory)?|branch|merge|pull\s?request|pr\b|diff|"
    r"refactor(ed|ing)?|codebase|source\s+code|function|class\b|endpoint|"
    r"implemented|implementation|regression|stack\s?trace|changelog|"
    r"who\s+(wrote|changed|authored)|last\s+changed)\b",
    re.IGNORECASE,
)

_MEETING_RE = re.compile(
    r"\b(meeting\s+(notes?|transcript)|transcript|stand-?up\s+notes|action\s+items?|"
    r"minutes\s+of\s+the\s+meeting|summar(ise|ize)\s+(this\s+)?(meeting|call))\b",
    re.IGNORECASE,
)

# A transcript pasted in tends to be long and speaker-prefixed.
_SPEAKER_LINE_RE = re.compile(r"^\s*[A-Z][\w .'-]{1,30}\s*:\s+\S", re.MULTILINE)


def _looks_like_transcript(message: str) -> bool:
    if len(message) < 400:
        return False
    speakers = len(_SPEAKER_LINE_RE.findall(message))
    return speakers >= 4


def classify(
    message: str,
    *,
    has_documents: bool = True,
    has_repository: bool = False,
    allow_llm: bool = True,
) -> Routing:
    """
    Route one message. `has_documents` / `has_repository` describe what is
    actually wired up, so the router never sends a question to a capability
    that cannot answer it.
    """
    text = (message or "").strip()
    if not text:
        return Routing("chat", 1.0, "Empty message.")

    # 1. Pasted transcript — decided by shape, which is far more reliable than
    #    asking a model whether something "is" a transcript.
    if _looks_like_transcript(text):
        return Routing("meeting", 0.9, "Long, speaker-prefixed text.")

    # 2. Greetings and capability questions never need retrieval.
    if _GREETING_RE.match(text) or _CAPABILITY_RE.search(text):
        return Routing("chat", 0.95, "Greeting or capability question.")

    if _MEETING_RE.search(text):
        return Routing("meeting", 0.8, "Explicit meeting vocabulary.")

    # 3. Repository vocabulary, but only when a repo is actually connected —
    #    otherwise "who wrote this policy?" would be routed somewhere that has
    #    nothing to answer with.
    if has_repository and _CODE_RE.search(text):
        return Routing("code", 0.8, "Repository vocabulary.")

    # 4. Short factual questions with no corpus to search are general chat.
    if not has_documents:
        return Routing("chat", 0.7, "No indexed documents to search.")

    if not allow_llm:
        return Routing("docs", 0.5, "Default route without LLM assistance.")

    return _classify_with_llm(text, has_repository=has_repository)


def _classify_with_llm(message: str, *, has_repository: bool) -> Routing:
    options = ["chat", "docs"] + (["code"] if has_repository else [])

    prompt = f"""Classify the user's message into exactly one route.

Routes:
- "docs": a question whose answer would be in the company's uploaded documents
  (policies, handbooks, reports, specifications).
- "chat": small talk, or general knowledge that has nothing to do with company
  documents (definitions, concepts, arithmetic, world facts).
{'- "code": a question about the source code or commit history of the connected repository.' if has_repository else ''}

Respond with ONLY this JSON, no prose:
{{"intent": "{'|'.join(options)}", "confidence": 0.0, "reason": "a few words"}}

Message: {message[:1500]}"""

    try:
        raw, _ = complete(prompt, max_tokens=120, temperature=0.0, operation="intent_router")
        parsed = json.loads(strip_json_fences(raw))
        intent = str(parsed.get("intent", "")).lower().strip()
        if intent not in options:
            raise ValueError(f"unexpected intent {intent!r}")
        confidence = float(parsed.get("confidence", 0.6))
        reason = str(parsed.get("reason", ""))[:120]
        return Routing(intent, confidence, reason, deterministic=False)  # type: ignore[arg-type]
    except Exception as e:  # noqa: BLE001 - routing must never break a chat
        # Grounded retrieval is the safe default for an internal knowledge tool:
        # a wrong "docs" answer still cites its sources and can be checked.
        log.warning("Intent classification failed (%s); defaulting to docs.", e)
        return Routing("docs", 0.3, "Classifier unavailable.", deterministic=False)
