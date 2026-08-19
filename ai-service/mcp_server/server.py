"""
mcp_server/server.py — exposes Folio's capabilities as MCP tools.

Running this lets an external MCP client (Claude Desktop, Claude Code, or any
other) use the same retrieval, repository and meeting capabilities the web app
uses, against the same Postgres, Chroma and Redis.

Install and run:

    pip install "mcp[cli]"
    python -m mcp_server.server

Then register it with Claude Desktop in `claude_desktop_config.json`:

    {
      "mcpServers": {
        "folio": {
          "command": "python",
          "args": ["-m", "mcp_server.server"],
          "cwd": "/absolute/path/to/ai-service",
          "env": {
            "FOLIO_USER_ID": "00000000-0000-0000-0000-000000000001",
            "GEMINI_API_KEY": "...",
            "POSTGRES_DSN": "postgresql://postgres:postgres@localhost:5432/workspace",
            "CHROMA_HOST": "localhost"
          }
        }
      }
    }

**On identity.** stdio MCP has no login: the client launches this process, and
is trusted by virtue of running as that OS user. The workspace it acts on is
therefore fixed at startup by `FOLIO_USER_ID`, and every tool is scoped to that
one user. This process must never be exposed as a shared network service —
there would be no way to tell callers apart.
"""
# NOTE: deliberately no `from __future__ import annotations` here. It turns
# every annotation into a string, and the MCP 1.x tool introspector calls
# issubclass() on them — which raises on a str. Tool signatures in this module
# must therefore use plain, concrete types.

import logging
import os
import sys

# stderr, never stdout: stdout *is* the MCP transport, and a stray log line
# there corrupts the protocol stream.
logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
log = logging.getLogger(__name__)

# The SDK renamed FastMCP to MCPServer in 2.0 while keeping the decorator API
# identical, so both generations are supported rather than pinning to one.
try:
    from mcp.server import MCPServer as _Server            # mcp >= 2.0
except ImportError:  # pragma: no cover - depends on installed version
    try:
        from mcp.server.fastmcp import FastMCP as _Server  # mcp 1.x
    except ImportError:
        print(
            'The `mcp` package is not installed.\n'
            'Install it with:  pip install "mcp[cli]"',
            file=sys.stderr,
        )
        raise SystemExit(1)

from agents.dev_agent import run as run_dev_agent
from agents.meeting_agent import run as run_meeting_agent
from app import db
from rag.retrieval import retrieve

# The single workspace this server acts on. See the note above.
USER_ID = os.getenv("FOLIO_USER_ID", "00000000-0000-0000-0000-000000000001")

# A repository can be supplied here so the Dev Agent tool works from an MCP
# client without the web UI having selected one.
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN") or None
GITHUB_REPO = os.getenv("GITHUB_REPO") or None

mcp = _Server("folio")


@mcp.tool()
def search_docs(query: str, top_k: int = 5) -> str:
    """
    Search the workspace's indexed documents and return the most relevant
    passages, each labelled with the file and page it came from.

    Use this to ground an answer in company documents rather than guessing.
    """
    query = (query or "").strip()
    if not query:
        return "Provide a search query."

    try:
        chunks = retrieve(query, top_k=max(1, min(top_k, 20)))
    except Exception as e:
        log.warning("search_docs failed: %s", e)
        return f"Document search is unavailable: {e}"

    if not chunks:
        return "No relevant passages found in the indexed documents."

    parts = []
    for index, chunk in enumerate(chunks, start=1):
        where = chunk.source_path
        if chunk.page:
            where += f", p.{chunk.page}"
        if chunk.heading:
            where += f", {chunk.heading}"
        parts.append(f"[Source {index}] {where}\n{chunk.text}")
    return "\n\n".join(parts)


@mcp.tool()
def list_documents() -> str:
    """List the documents indexed in this workspace, with their status."""
    try:
        rows = db.list_documents(USER_ID, None, None, None)
    except Exception as e:
        return f"Could not list documents: {e}"

    if not rows:
        return "No documents have been indexed yet."

    return "\n".join(
        f"{row['filename']}  [{row['status']}]  {row.get('chunk_count', 0)} chunks"
        for row in rows
    )


@mcp.tool()
def explain_code(question: str, repo: str = "") -> str:
    """
    Answer a question about a connected Git repository — what the code does,
    where something is implemented, who changed it and when.

    Pass `repo` as "owner/name" to target a specific GitHub repository;
    leave it empty to use the one configured for this server.

    (`str = ""` rather than `str | None = None`: the MCP 1.x introspector
    cannot handle a PEP 604 union in a tool signature.)
    """
    question = (question or "").strip()
    if not question:
        return "Ask a question about the repository."

    outcome = run_dev_agent(question, GITHUB_TOKEN, (repo or "").strip() or GITHUB_REPO)
    answer = outcome.get("answer") or "No answer was produced."

    tools = outcome.get("tools_used") or []
    if tools:
        answer += f"\n\n---\nTools used: {', '.join(tools)}"
    if outcome.get("source"):
        answer += f"\nSource: {outcome['source']}"
    return answer


@mcp.tool()
def summarize_meeting(transcript: str) -> str:
    """
    Summarise a meeting transcript, extract its decisions and action items, and
    store them in this workspace's long-term memory so later questions can refer
    back to them.
    """
    transcript = (transcript or "").strip()
    if not transcript:
        return "Provide a meeting transcript."

    outcome = run_meeting_agent(transcript, USER_ID)
    return outcome.get("markdown") or "Nothing could be extracted from that transcript."


@mcp.tool()
def recall_memory(limit: int = 10) -> str:
    """
    Recall recent decisions and action items remembered for this workspace —
    what was committed to, and by whom.
    """
    try:
        memories = db.get_recent_memory(USER_ID, limit=max(1, min(limit, 50)))
    except Exception as e:
        return f"Could not read memory: {e}"

    if not memories:
        return "Nothing has been remembered yet."
    return "\n".join(f"- {m}" for m in memories)


def main() -> None:
    log.info("Starting the Folio MCP server for user %s", USER_ID)
    # stdio: the client owns this process's lifetime, which is what makes the
    # fixed-identity model above safe.
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
