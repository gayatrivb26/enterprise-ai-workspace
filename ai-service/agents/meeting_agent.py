"""
agents/meeting_agent.py — turns a meeting transcript into durable memory.

Deliberately hand-rolled rather than a LangGraph state machine: this pipeline
is linear (extract → dedupe → summarise → persist), so a graph would add a
dependency and a layer of indirection without buying any branching. The Dev
Agent, which genuinely branches on a plan, is the one that uses LangGraph.

Two passes over the transcript:

  1. **Extract** decisions and action items as structured output, so the result
     is parsed data rather than prose that has to be re-read later.
  2. **Dedupe against memory**, so re-pasting the same transcript (or a
     follow-up meeting covering the same ground) annotates existing items
     instead of silently accumulating near-duplicates.

Everything extracted is written to `memory_entries`, which is what lets a later
conversation answer "what did I commit to last week?" without the transcript
being in the retrieval corpus at all.

Run standalone:
    python -m agents.meeting_agent ./transcript.txt
"""
from __future__ import annotations

import json
import logging
import sys

from app.db import get_recent_memory, write_memory
from app.llm_client import strip_json_fences
from app.llm_service import LlmError, complete

log = logging.getLogger(__name__)

DEV_USER_ID = "00000000-0000-0000-0000-000000000001"  # seeded dev user

# Long transcripts are truncated rather than refused: a partial summary of a
# two-hour meeting is far more useful than an error.
MAX_TRANSCRIPT_CHARS = 24_000

# Above this Jaccard overlap, two items are treated as the same commitment.
DUPLICATE_THRESHOLD = 0.45


def extract_action_items(transcript: str) -> list[dict]:
    prompt = f"""Extract decisions and action items from this meeting transcript.

Respond with ONLY a JSON array, no prose. Each element:
{{"type": "action_item" or "decision", "text": "...", "owner": "name or null", "due": "date or null"}}

Only include items explicitly stated. Do not infer or invent owners or dates.
Return [] if there are none.

Transcript:
{transcript[:MAX_TRANSCRIPT_CHARS]}"""

    try:
        raw, _ = complete(prompt, max_tokens=900, operation="meeting_extract")
        parsed = json.loads(strip_json_fences(raw))
    except (LlmError, json.JSONDecodeError, ValueError) as e:
        log.warning("Action-item extraction failed: %s", e)
        return []

    if not isinstance(parsed, list):
        return []

    items: list[dict] = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("text") or "").strip()
        if not text:
            continue
        items.append({
            "type": "action_item" if entry.get("type") == "action_item" else "decision",
            "text": text[:500],
            "owner": (str(entry["owner"]).strip() or None) if entry.get("owner") else None,
            "due": (str(entry["due"]).strip() or None) if entry.get("due") else None,
        })
    return items


def _rough_overlap(a: str, b: str) -> float:
    """Cheap Jaccard similarity, used to spot restated commitments."""
    wa, wb = set(a.lower().split()), set(b.lower().split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def dedupe_against_memory(items: list[dict], user_id: str) -> list[dict]:
    """
    Annotate each item with the earlier memory it restates, if any.

    Recency plus lexical overlap rather than embeddings: this runs once per
    transcript over ~30 short strings, so embedding them would buy very little.
    `memory/store.py` is where a vector version would slot in.
    """
    past = get_recent_memory(user_id, limit=30)
    annotated = []
    for item in items:
        match = next(
            (m for m in past if _rough_overlap(item["text"], m) > DUPLICATE_THRESHOLD),
            None,
        )
        annotated.append({**item, "follow_up_of": match})
    return annotated


def generate_summary(transcript: str) -> str:
    prompt = (
        "Summarise this meeting in 3-5 sentences. State only what is explicitly "
        "in the transcript — do not infer, speculate or add context.\n\n"
        f"{transcript[:MAX_TRANSCRIPT_CHARS]}"
    )
    try:
        summary, _ = complete(prompt, max_tokens=400, operation="meeting_summary")
        return summary.strip()
    except LlmError as e:
        log.warning("Meeting summary failed: %s", e)
        return ""


def _render(summary: str, items: list[dict]) -> str:
    """Compose the Markdown the chat surface shows."""
    parts: list[str] = []

    if summary:
        parts.append("## Summary\n\n" + summary)

    decisions = [i for i in items if i["type"] == "decision"]
    actions = [i for i in items if i["type"] == "action_item"]

    if decisions:
        parts.append("## Decisions\n\n" + "\n".join(f"- {d['text']}" for d in decisions))

    if actions:
        lines = []
        for a in actions:
            line = f"- **{a['text']}**"
            if a.get("owner"):
                line += f" — {a['owner']}"
            if a.get("due"):
                line += f" (due {a['due']})"
            if a.get("follow_up_of"):
                line += "  \n  _Restates an earlier commitment._"
            lines.append(line)
        parts.append("## Action items\n\n" + "\n".join(lines))

    if not parts:
        return "I could not find any decisions or action items in that transcript."

    parts.append("_Saved to memory — you can ask about these later._")
    return "\n\n".join(parts)


def run(transcript: str, user_id: str = DEV_USER_ID) -> dict:
    """
    Summarise a transcript and persist what it committed people to.
    Never raises: this is called directly from a request handler.
    """
    transcript = (transcript or "").strip()
    if not transcript:
        return {
            "markdown": "Paste a meeting transcript and I'll summarise it.",
            "summary": "",
            "action_items": [],
            "decisions": [],
        }

    items = dedupe_against_memory(extract_action_items(transcript), user_id)
    summary = generate_summary(transcript)

    for item in items:
        # Only genuinely new commitments are written; re-summarising the same
        # meeting must not multiply the same action item in memory.
        if item.get("follow_up_of"):
            continue
        entry_type = "episodic" if item["type"] == "action_item" else "fact"
        content = item["text"]
        if item.get("owner"):
            content += f" (owner: {item['owner']})"
        if item.get("due"):
            content += f" (due: {item['due']})"
        try:
            write_memory(user_id, entry_type, content)
        except Exception as e:
            log.warning("Could not persist a meeting memory: %s", e)

    return {
        "markdown": _render(summary, items),
        "summary": summary,
        "action_items": [i for i in items if i["type"] == "action_item"],
        "decisions": [i for i in items if i["type"] == "decision"],
    }


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else None
    text = open(path, encoding="utf-8").read() if path else ""
    print(run(text)["markdown"])
