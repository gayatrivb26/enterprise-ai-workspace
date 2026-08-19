"""
agents/dev_agent.py — answers questions about a real Git repository.

Graph shape (the design doc's Phase 4):

    START -> plan -> [search_code] -> [git_history] -> synthesize -> END
                        (each conditional on the plan)

`plan` uses structured output rather than free-text routing, so the decision is
a parsed object, and a malformed reply fails open to "run both tools" instead
of silently doing nothing.

On LangGraph: every node here is a pure `(state) -> state` function and the
edges are declared in ROUTES — exactly the shape `StateGraph` wants. If
langgraph is installed it is used; if not, `_run_graph` walks the same node and
edge definitions itself. That keeps the dependency genuinely optional without
the fallback being a *different* agent: same nodes, same order, same state.

Run it standalone:
    python -m agents.dev_agent "who changed authentication recently?"
"""
from __future__ import annotations

import json
import logging
import re
import sys
from typing import Callable, TypedDict

from agents import git_tools
from agents.github_client import GitHubClient, GitHubError
from app.llm_client import strip_json_fences
from app.llm_service import LlmError, complete

log = logging.getLogger(__name__)

MAX_TOOL_CHARS = 6000


class DevAgentState(TypedDict, total=False):
    question: str
    # Set when the user has connected a GitHub account and picked a repo; the
    # agent then answers against that instead of a locally mounted checkout.
    github_token: str | None
    repo: str | None
    needs_code_search: bool
    needs_git_history: bool
    search_terms: str
    since: str
    file_path: str | None
    code_results: str
    git_results: str
    overview: str
    needs_overview: bool
    resolved_repo: str | None
    answer: str
    tools_used: list[str]
    error: str | None


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

def plan_node(state: DevAgentState) -> DevAgentState:
    """Decide which tools the question needs, as structured output."""
    prompt = f"""Classify this question about a software repository.

Respond with ONLY a JSON object, no prose:
{{
  "needs_code_search": true,
  "needs_git_history": true,
  "search_terms": "2-5 keywords to grep for, no punctuation",
  "since": "a git date expression such as 30 days ago",
  "file_path": null
}}

Set needs_code_search when the answer lives in the source. Set
needs_git_history when it lives in commit history (who changed what, when,
recent activity). Set file_path only when the question names a specific file.

Question: {state['question']}"""

    try:
        raw, _ = complete(prompt, max_tokens=250, operation="dev_agent_plan")
        plan = json.loads(strip_json_fences(raw))
        if not isinstance(plan, dict):
            raise ValueError("plan was not an object")
    except (LlmError, json.JSONDecodeError, ValueError) as e:
        # Fail open to running both tools: a routing failure should degrade the
        # answer, never silently return nothing.
        log.warning("Dev agent planning failed (%s); running both tools.", e)
        plan = {}

    return {
        **state,
        "needs_code_search": bool(plan.get("needs_code_search", True)),
        "needs_git_history": bool(plan.get("needs_git_history", True)),
        "search_terms": str(plan.get("search_terms") or state["question"])[:200],
        "since": str(plan.get("since") or "90 days ago")[:60],
        "file_path": plan.get("file_path") or None,
        # Default true: describing the repo is cheap and is the most common
        # thing people ask first.
        "needs_overview": bool(plan.get("needs_overview", True)),
        "tools_used": [],
    }


def _normalise(value: str) -> str:
    """Fold separators and case so repo names compare the way people type them."""
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def resolve_repo_node(state: DevAgentState) -> DevAgentState:
    """
    Work out which repository the question is actually about.

    Users name a repo in the question far more often than they remember to
    select one in settings — and if the named repo is not the selected one,
    searching the selected one is guaranteed to find nothing. So the account's
    repositories are matched against the question text, and a name found there
    wins over the stored selection.
    """
    token = state.get("github_token")
    selected = state.get("repo")

    if not token:
        return {**state, "resolved_repo": selected}

    question = _normalise(state.get("question", ""))
    matched = None

    try:
        for repo in GitHubClient(token).list_repos():
            name = repo.full_name.split("/")[-1]
            # Require a reasonably distinctive name so a repo called "app"
            # does not capture every question containing that word.
            if len(name) >= 4 and _normalise(name) in question:
                matched = repo.full_name
                break
    except GitHubError as e:
        log.info("Could not resolve a repository from the question: %s", e)

    return {**state, "resolved_repo": matched or selected}


def overview_node(state: DevAgentState) -> DevAgentState:
    """
    Describe the repository itself.

    "What is this repo?" cannot be answered by code search: a project's purpose
    lives in its README and metadata, not in a grep of its own source. Without
    this the agent searched for the repo's own name, found nothing, and
    reported that it had no information about a repository it was looking at.
    """
    if not state.get("needs_overview"):
        return state

    repo = state.get("resolved_repo")
    client = _github(state)
    if client is None or not repo:
        return state

    used = [*state.get("tools_used", []), "repo_summary"]
    parts = []
    try:
        parts.append(client.repo_summary(repo))
    except GitHubError as e:
        parts.append(f"(summary unavailable: {e})")

    readme = client.get_readme(repo)
    if readme and not readme.startswith("(no README"):
        used.append("get_readme")
        parts.append("README:\n" + readme)

    return {**state, "overview": "\n\n".join(parts)[:MAX_TOOL_CHARS], "tools_used": used}


def code_search_node(state: DevAgentState) -> DevAgentState:
    if not state.get("needs_code_search"):
        return state

    terms = state.get("search_terms", "")
    used = [*state.get("tools_used", []), "search_code"]

    client = _github(state)
    if client is not None:
        try:
            output = client.search_code(state["resolved_repo"] or state["repo"], terms)
        except GitHubError as e:
            output = f"GitHub search failed: {e}"
    else:
        output = str(git_tools.search_code(terms))

    return {**state, "code_results": output[:MAX_TOOL_CHARS], "tools_used": used}


def git_history_node(state: DevAgentState) -> DevAgentState:
    if not state.get("needs_git_history"):
        return state

    used = list(state.get("tools_used", []))
    path = state.get("file_path")
    used.append("get_file_history" if path else "get_git_log")

    client = _github(state)
    if client is not None:
        try:
            output = client.get_commits(
                state["resolved_repo"] or state["repo"],
                since=_iso_since(state.get("since")),
                path=path,
            )
        except GitHubError as e:
            output = f"GitHub history failed: {e}"
    elif path:
        output = str(git_tools.get_file_history(path))
    else:
        output = str(git_tools.get_git_log(state.get("since", "90 days ago")))

    return {**state, "git_results": output[:MAX_TOOL_CHARS], "tools_used": used}


def _github(state: DevAgentState) -> GitHubClient | None:
    """A client only when a token and some repository are available."""
    token = state.get("github_token")
    repo = state.get("resolved_repo") or state.get("repo")
    if not token or not repo:
        return None
    try:
        return GitHubClient(token)
    except GitHubError:
        return None


def _iso_since(expression: str | None) -> str | None:
    """
    Translate the planner's git-style date ("30 days ago") into the ISO 8601
    instant the GitHub API expects. Anything unrecognised returns None, which
    simply means "no lower bound" rather than an error.
    """
    if not expression:
        return None
    import re
    from datetime import datetime, timedelta, timezone

    match = re.search(r"(\d+)\s*(day|week|month|year)", expression.lower())
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2)
    days = {"day": 1, "week": 7, "month": 30, "year": 365}[unit] * amount
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def synthesize_node(state: DevAgentState) -> DevAgentState:
    code = (state.get("code_results") or "").strip()
    history = (state.get("git_results") or "").strip()
    overview = (state.get("overview") or "").strip()

    if not code and not history and not overview:
        return {
            **state,
            "answer": "I could not find anything in the repository for that question.",
        }

    prompt = f"""You are a senior engineer answering a question about a codebase.
Use ONLY the tool output below. Cite concrete file paths and commit hashes.
If the output does not answer the question, say so plainly rather than guessing.

QUESTION
{state['question']}

REPOSITORY
{overview or '(not fetched)'}

CODE SEARCH (path:line:match)
{code or '(not run)'}

GIT HISTORY (hash date author subject)
{history or '(not run)'}

Answer in Markdown. Be specific and brief."""

    try:
        answer, _ = complete(prompt, max_tokens=900, operation="dev_agent_answer")
    except LlmError as e:
        # The tools already succeeded; only synthesis failed. Say which, so a
        # quota problem is not mistaken for "the repository has nothing".
        return {**state, "error": str(e), "answer": e.user_message}

    return {**state, "answer": answer.strip()}


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------

NODES: dict[str, Callable[[DevAgentState], DevAgentState]] = {
    "plan": plan_node,
    "resolve_repo": resolve_repo_node,
    "overview": overview_node,
    "search_code": code_search_node,
    "git_history": git_history_node,
    "synthesize": synthesize_node,
}

# (from, to) edges in execution order — what add_edge() would declare.
ROUTES: list[tuple[str, str]] = [
    ("plan", "resolve_repo"),
    ("resolve_repo", "overview"),
    ("overview", "search_code"),
    ("search_code", "git_history"),
    ("git_history", "synthesize"),
]

ENTRY = "plan"
FINISH = "synthesize"


def _build_langgraph():
    """Compile the same nodes/edges with LangGraph when it is installed."""
    from langgraph.graph import END, StateGraph  # lazy: optional dependency

    graph = StateGraph(DevAgentState)
    for name, fn in NODES.items():
        graph.add_node(name, fn)
    graph.set_entry_point(ENTRY)
    for source, target in ROUTES:
        graph.add_edge(source, target)
    graph.add_edge(FINISH, END)
    return graph.compile()


def _run_graph(state: DevAgentState) -> DevAgentState:
    """Walk NODES/ROUTES directly. Identical semantics, no dependency."""
    current = ENTRY
    while True:
        state = NODES[current](state)
        following = [target for source, target in ROUTES if source == current]
        if not following:
            return state
        current = following[0]


def run(
    question: str,
    github_token: str | None = None,
    repo: str | None = None,
) -> dict:
    """
    Answer one repository question. Never raises: the caller is an HTTP handler,
    and a missing repository is an expected condition, not a server error.

    Two sources, in order of preference:
      1. the user's own GitHub account (token + selected repo), which sees
         private repositories exactly as that user does; otherwise
      2. a repository mounted locally at DEV_AGENT_REPO.
    """
    question = (question or "").strip()
    if not question:
        return {"answer": "Ask a question about the repository.", "tools_used": [], "ok": False}

    using_github = bool(github_token)
    if not using_github and not git_tools.repo_available():
        return {
            "answer": (
                "I'm not connected to any code yet. Connect your GitHub account in "
                "**Settings → Integrations** and pick a repository, and I'll be able "
                "to answer questions about its code and history."
            ),
            "tools_used": [],
            "ok": False,
        }

    initial: DevAgentState = {
        "question": question,
        "github_token": github_token,
        "repo": repo,
    }

    try:
        compiled = _build_langgraph()
    except ImportError:
        log.debug("langgraph not installed; using the built-in graph runner.")
        final = _run_graph(initial)
    except Exception as e:  # pragma: no cover - defensive
        log.warning("LangGraph build failed (%s); using the built-in runner.", e)
        final = _run_graph(initial)
    else:
        final = compiled.invoke(initial)

    return {
        "answer": final.get("answer", ""),
        "tools_used": final.get("tools_used", []),
        "source": final.get("resolved_repo") or (repo if using_github else str(git_tools.REPO_ROOT)),
        "ok": not final.get("error"),
    }


if __name__ == "__main__":
    query = " ".join(sys.argv[1:]) or "What changed recently?"
    outcome = run(query)
    print(outcome["answer"])
    if outcome["tools_used"]:
        print("\n-- tools:", ", ".join(outcome["tools_used"]))
