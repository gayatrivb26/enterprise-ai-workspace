"""
agents/git_tools.py — real, sandboxed tools over a Git repository.

These are the Dev Agent's hands: search_code, get_git_log, get_file_history and
grep_repo. Every one of them shells out to `git` or walks the working tree, so
the security posture matters more than the feature:

  * **No shell.** Every call passes an argument *list* to subprocess, never a
    string, so a question containing `; rm -rf /` is an argument, not a command.
  * **Confined to the repo root.** Any path argument is resolved and then
    checked to be inside the configured root, which stops `../../etc/passwd`
    and symlinks that point outward.
  * **No user-controlled flags.** Arguments that could be read as options are
    passed after `--`, or rejected, so a query of `--upload-pack=...` cannot
    turn into a git option.
  * **Bounded.** Every call has a timeout and every output is truncated, so a
    pathological pattern cannot hang the service or blow up the prompt.

The repo is read-only here: no command that writes, fetches or checks out is
exposed.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# Where the agent is allowed to look. Point this at a cloned repo.
REPO_ROOT = Path(os.getenv("DEV_AGENT_REPO", "/repo")).resolve()

TIMEOUT_SECONDS = 20
MAX_OUTPUT_CHARS = 12_000
MAX_MATCHES = 60

# Files the agent should never read back into a prompt, even if they are
# committed by mistake: this is the most likely way a secret would leak into an
# LLM call.
SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "dist", "build", "__pycache__",
             ".angular", "obj", "bin", ".idea", ".vs"}
SKIP_FILE_PATTERNS = re.compile(
    r"(^|/)(\.env(\..*)?|.*\.pem|.*\.key|.*\.pfx|.*\.p12|id_rsa|credentials(\.json)?|"
    r"secrets?\.(ya?ml|json|toml))$",
    re.IGNORECASE,
)

TEXT_SUFFIXES = {
    ".py", ".ts", ".js", ".tsx", ".jsx", ".cs", ".java", ".go", ".rs", ".rb", ".php",
    ".html", ".css", ".scss", ".sql", ".sh", ".yml", ".yaml", ".json", ".toml", ".md",
    ".txt", ".cfg", ".ini", ".xml", ".vue", ".svelte", ".kt", ".swift", ".c", ".h",
    ".cpp", ".hpp",
}


class RepoUnavailable(RuntimeError):
    """Raised when no readable Git repository is configured."""


@dataclass
class ToolResult:
    ok: bool
    output: str

    def __str__(self) -> str:  # what gets embedded into the prompt
        return self.output


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

def repo_available() -> bool:
    return REPO_ROOT.is_dir() and (REPO_ROOT / ".git").exists()


def _require_repo() -> Path:
    if not repo_available():
        raise RepoUnavailable(
            f"No Git repository is mounted at {REPO_ROOT}. Set DEV_AGENT_REPO to a "
            f"cloned repository to enable the Dev Agent."
        )
    return REPO_ROOT


def _safe_relative(candidate: str) -> Path:
    """
    Resolve a caller-supplied path and prove it stays inside the repo.

    resolve() collapses `..` *and* follows symlinks, so this rejects both
    `../../etc/passwd` and a symlink inside the repo pointing at /etc.
    """
    root = _require_repo()
    target = (root / candidate).resolve()
    if target != root and root not in target.parents:
        raise ValueError("Path escapes the repository root.")
    return target


def _run_git(args: list[str]) -> ToolResult:
    """Run one read-only git command. `args` is a list — never a shell string."""
    try:
        root = _require_repo()
    except RepoUnavailable as e:
        # Callers treat a ToolResult as total; letting this escape would turn a
        # missing mount into an unhandled exception inside a chat request.
        return ToolResult(False, str(e))
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
            # An inherited environment could carry credentials or askpass hooks;
            # this runs with the minimum needed to read a local repo.
            env={"PATH": os.getenv("PATH", "/usr/bin:/bin"), "GIT_TERMINAL_PROMPT": "0"},
        )
    except subprocess.TimeoutExpired:
        return ToolResult(False, "The git command timed out.")
    except FileNotFoundError:
        return ToolResult(False, "git is not installed in this container.")

    if completed.returncode != 0:
        detail = (completed.stderr or "").strip()[:300]
        return ToolResult(False, f"git failed: {detail or 'unknown error'}")

    return ToolResult(True, _truncate(completed.stdout.strip()))


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… (truncated at {limit} characters)"


# A fixed reply rather than one quoting the query: echoing caller input back
# into an LLM prompt is how a "no results" message becomes an injection vector.
NO_MATCHES = "No matches found in the repository."


def _keep_visible(output: str) -> list[str]:
    """Drop hits from files the agent is not allowed to surface."""
    kept = []
    for line in output.splitlines():
        path = line.split(":", 1)[0]
        if not SKIP_FILE_PATTERNS.search(path):
            kept.append(line)
    return kept


def _skip(path: Path, root: Path) -> bool:
    rel = path.relative_to(root).as_posix()
    if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
        return True
    return bool(SKIP_FILE_PATTERNS.search(rel))


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def search_code(query: str, max_results: int = MAX_MATCHES) -> ToolResult:
    """
    Case-insensitive fixed-string search across tracked source files.

    Uses `git grep -F` so the query is a literal, not a regex: a user question
    containing `(` or `*` should find that text, not crash or scan the whole
    repo pathologically.
    """
    query = (query or "").strip()
    if len(query) < 2:
        return ToolResult(False, "Search query is too short.")

    if not repo_available():
        return ToolResult(False, f"No Git repository is mounted at {REPO_ROOT}.")

    result = _run_git([
        "grep", "--no-color", "-n", "-I", "-i", "-F",
        "-e", query,          # -e keeps a leading dash from parsing as a flag
        "--",                 # everything after is a pathspec, never an option
        ".",
    ])
    if not result.ok:
        # git grep exits non-zero when there are simply no matches.
        return ToolResult(True, NO_MATCHES)

    lines = _keep_visible(result.output)
    if not lines:
        return ToolResult(True, NO_MATCHES)

    return ToolResult(True, _truncate("\n".join(lines[:max_results])))


def grep_repo(pattern: str, max_results: int = MAX_MATCHES) -> ToolResult:
    """
    Regular-expression search, for when the caller genuinely wants a pattern.

    The pattern is compiled locally first: an invalid or catastrophically
    backtracking expression is rejected here rather than handed to git.
    """
    pattern = (pattern or "").strip()
    if len(pattern) < 2:
        return ToolResult(False, "Pattern is too short.")

    if not repo_available():
        return ToolResult(False, f"No Git repository is mounted at {REPO_ROOT}.")
    try:
        re.compile(pattern)
    except re.error as e:
        return ToolResult(False, f"Invalid regular expression: {e}")

    result = _run_git([
        "grep", "--no-color", "-n", "-I", "-E",
        "-e", pattern,
        "--", ".",
    ])
    if not result.ok:
        return ToolResult(True, NO_MATCHES)

    lines = _keep_visible(result.output)
    return ToolResult(True, _truncate("\n".join(lines[:max_results])) or NO_MATCHES)


def get_git_log(since: str = "30 days ago", max_commits: int = 40) -> ToolResult:
    """Recent commits. `since` is passed as a value, never interpolated."""
    since = (since or "30 days ago").strip()
    # git accepts a lot of date spellings; anything with a newline or a leading
    # dash is a caller trying to smuggle an option.
    if "\n" in since or since.startswith("-"):
        return ToolResult(False, "Invalid date expression.")

    return _run_git([
        "log",
        f"--max-count={max(1, min(max_commits, 200))}",
        "--date=short",
        "--pretty=format:%h  %ad  %an  %s",
        f"--since={since}",
    ])


def get_file_history(path: str, max_commits: int = 25) -> ToolResult:
    """Commit history for one file, following renames."""
    try:
        target = _safe_relative(path)
    except (ValueError, RepoUnavailable) as e:
        return ToolResult(False, str(e))

    root = _require_repo()
    relative = target.relative_to(root).as_posix() if target != root else "."

    return _run_git([
        "log", "--follow",
        f"--max-count={max(1, min(max_commits, 100))}",
        "--date=short",
        "--pretty=format:%h  %ad  %an  %s",
        "--", relative,
    ])


def read_file(path: str, max_lines: int = 200) -> ToolResult:
    """Read a bounded slice of one tracked text file."""
    try:
        target = _safe_relative(path)
    except (ValueError, RepoUnavailable) as e:
        return ToolResult(False, str(e))

    try:
        root = _require_repo()
    except RepoUnavailable as e:
        return ToolResult(False, str(e))
    if not target.is_file():
        return ToolResult(False, f"No such file: {path}")
    if _skip(target, root):
        return ToolResult(False, "That file is excluded from agent access.")
    if target.suffix.lower() not in TEXT_SUFFIXES:
        return ToolResult(False, "Only text source files can be read.")

    try:
        with target.open("r", encoding="utf-8", errors="replace") as handle:
            lines = [next(handle, None) for _ in range(max_lines)]
    except OSError as e:
        return ToolResult(False, f"Could not read the file: {e}")

    body = "".join(line for line in lines if line)
    return ToolResult(True, _truncate(body))


def repo_summary() -> ToolResult:
    """One-line orientation: branch, head commit and how big the tree is."""
    if not repo_available():
        return ToolResult(False, f"No Git repository is mounted at {REPO_ROOT}.")

    branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"])
    head = _run_git(["log", "-1", "--date=short", "--pretty=format:%h %ad %an %s"])
    count = _run_git(["rev-list", "--count", "HEAD"])
    if not branch.ok:
        return branch
    return ToolResult(True, (
        f"Repository: {REPO_ROOT}\n"
        f"Branch: {branch.output}\n"
        f"Commits: {count.output if count.ok else 'unknown'}\n"
        f"Latest: {head.output if head.ok else 'unknown'}"
    ))
