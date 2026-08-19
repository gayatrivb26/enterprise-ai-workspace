"""
eval/run_eval.py — regression harness for retrieval and answer quality.

Measures the four things the design doc asks for, each deliberately cheap and
deterministic so this can run on every change without a model-graded bill:

  retrieval     did the expected source document appear in the top-k?
  correctness   do the expected facts appear in the answer?
  groundedness  did the answer cite at least one retrieved source, and no
                source that was never retrieved? A fabricated [Source 7] is
                the clearest hallucination signal there is.
  citation      do the cited sources point at the document we expected?

Groundedness and citation accuracy are the two that catch the failure people
actually care about: an answer that reads well and cites nothing real.

Run:
    python -m eval.run_eval                     # uses eval/questions.json
    python -m eval.run_eval path/to/cases.json
    python -m eval.run_eval --no-persist        # skip writing to eval_runs
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from app.db import get_conn
from app.llm_service import LlmError, complete
from rag.retrieval import build_prompt, retrieve

CITATION_RE = re.compile(r"\[Source\s+(\d+)\]", re.IGNORECASE)

DEFAULT_CASES = Path(__file__).with_name("questions.json")

# Below this, a case is reported as weak and the run exits non-zero.
PASS_THRESHOLD = 0.75


@dataclass
class CaseResult:
    question: str
    retrieval: float
    correctness: float
    groundedness: float
    citation: float
    answer: str
    sources: list[str]

    @property
    def overall(self) -> float:
        return (self.retrieval + self.correctness + self.groundedness + self.citation) / 4


# ── Metrics ─────────────────────────────────────────────────────────────────

def retrieval_score(expected_source: str, retrieved: list) -> float:
    """1.0 if the expected document appears anywhere in top-k."""
    if not expected_source:
        return 1.0
    return 1.0 if any(expected_source.lower() in c.source_path.lower() for c in retrieved) else 0.0


def correctness_score(expected_keywords: list[str], answer: str) -> float:
    """Fraction of expected facts present — a cheap stand-in for an LLM judge."""
    if not expected_keywords:
        return 1.0
    lowered = answer.lower()
    return sum(1 for kw in expected_keywords if kw.lower() in lowered) / len(expected_keywords)


def groundedness_score(answer: str, retrieved: list, should_answer: bool = True) -> float:
    """
    Is every citation real, and did the answer cite at all?

    A citation pointing past the end of the retrieved set is a fabrication and
    scores 0 outright — this is precisely the failure the harness exists for.
    """
    cited = {int(n) for n in CITATION_RE.findall(answer)}

    if not should_answer:
        # For a question the corpus cannot answer, citing nothing is correct.
        return 1.0 if not cited else 0.0

    if not retrieved:
        return 1.0 if not cited else 0.0
    if not cited:
        return 0.0

    valid = {i for i in cited if 1 <= i <= len(retrieved)}
    if not valid:
        return 0.0
    # Proportional penalty when an invented index sits alongside real ones.
    return len(valid) / len(cited)


def citation_score(expected_source: str, answer: str, retrieved: list) -> float:
    """Do the sources the answer actually cited point at the expected document?"""
    if not expected_source:
        return 1.0

    cited = [i for i in {int(n) for n in CITATION_RE.findall(answer)}
             if 1 <= i <= len(retrieved)]
    if not cited:
        return 0.0

    matches = sum(
        1 for i in cited
        if expected_source.lower() in retrieved[i - 1].source_path.lower()
    )
    return matches / len(cited)


# ── Runner ──────────────────────────────────────────────────────────────────

def run_case(case: dict) -> CaseResult:
    question = case["question"]
    expected_source = case.get("expected_source", "")
    should_answer = case.get("answerable", True)

    chunks = retrieve(question)
    prompt = build_prompt(question, chunks)

    try:
        answer, _ = complete(prompt, max_tokens=700, operation="eval")
    except LlmError as e:
        answer = f"(model unavailable: {e})"

    return CaseResult(
        question=question,
        retrieval=retrieval_score(expected_source, chunks),
        correctness=correctness_score(case.get("expected_keywords", []), answer),
        groundedness=groundedness_score(answer, chunks, should_answer),
        citation=citation_score(expected_source, answer, chunks),
        answer=answer,
        sources=[c.source_path for c in chunks],
    )


def persist(case: dict, result: CaseResult) -> None:
    """Best effort: a telemetry failure must not fail the eval run."""
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO eval_runs
                    (question, expected_answer, retrieved_chunks, actual_answer,
                     retrieval_score, answer_score)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    result.question,
                    case.get("expected_answer", ""),
                    json.dumps(result.sources),
                    result.answer,
                    result.retrieval,
                    # One column exists for answer quality, so store the blend
                    # of the three answer metrics; a single number still tracks
                    # regressions over time.
                    round((result.correctness + result.groundedness + result.citation) / 3, 3),
                ),
            )
    except Exception as e:
        print(f"  (could not persist: {e})", file=sys.stderr)


def run_eval(question_file: str | Path, persist_results: bool = True) -> int:
    path = Path(question_file)
    if not path.exists():
        print(f"No such file: {path}", file=sys.stderr)
        return 2

    cases = json.loads(path.read_text(encoding="utf-8"))
    if not cases:
        print("No eval cases defined.", file=sys.stderr)
        return 2

    results: list[CaseResult] = []
    print(f"Running {len(cases)} eval cases\n")

    for case in cases:
        result = run_case(case)
        results.append(result)
        if persist_results:
            persist(case, result)

        flag = "ok  " if result.overall >= PASS_THRESHOLD else "WEAK"
        print(f"  [{flag}] {result.question[:56]:<56} "
              f"r={result.retrieval:.2f} c={result.correctness:.2f} "
              f"g={result.groundedness:.2f} x={result.citation:.2f}")

    def mean(attr: str) -> float:
        return sum(getattr(r, attr) for r in results) / len(results)

    print("\n" + "-" * 74)
    print(f"  retrieval     {mean('retrieval'):.2f}   expected document in top-k")
    print(f"  correctness   {mean('correctness'):.2f}   expected facts present")
    print(f"  groundedness  {mean('groundedness'):.2f}   citations exist and are real")
    print(f"  citation      {mean('citation'):.2f}   citations point at the right document")
    print(f"  OVERALL       {sum(r.overall for r in results) / len(results):.2f}")
    print("-" * 74)

    weak = [r for r in results if r.overall < PASS_THRESHOLD]
    if weak:
        print(f"\n{len(weak)} case(s) below {PASS_THRESHOLD}:")
        for r in weak:
            print(f"  - {r.question}")

    # Non-zero exit so CI can gate on this.
    return 1 if weak else 0


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    persist_results = "--no-persist" not in sys.argv
    return run_eval(args[0] if args else DEFAULT_CASES, persist_results)


if __name__ == "__main__":
    raise SystemExit(main())
