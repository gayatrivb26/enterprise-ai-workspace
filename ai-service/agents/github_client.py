"""
agents/github_client.py — read-only access to a user's GitHub account.

This is what lets the Dev Agent answer questions about repositories the user
actually owns, rather than a single directory an operator mounted. It talks to
the REST API with the user's own token, so it sees exactly the repositories
that user can see — private ones included — and nothing else. Authorisation is
therefore GitHub's, not ours, which is the only version of this that is safe:
we never have to decide who may read what.

Deliberately API-only. Cloning would mean unbounded disk, credential-bearing
working copies on our filesystem, and a sync problem; the endpoints below
answer the questions people actually ask without any of that.

Every method is read-only. No write scope is ever requested or used.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

API_ROOT = "https://api.github.com"
TIMEOUT = httpx.Timeout(20.0, connect=10.0)

# Bounds so one question cannot pull a repository into a prompt.
MAX_SEARCH_RESULTS = 25
MAX_COMMITS = 40
MAX_FILE_BYTES = 200_000
MAX_SNIPPET_CHARS = 600


class GitHubError(RuntimeError):
    """A GitHub call failed in a way worth reporting to the user."""


@dataclass
class Repo:
    full_name: str
    description: str | None
    private: bool
    default_branch: str
    updated_at: str
    language: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "full_name": self.full_name,
            "description": self.description,
            "private": self.private,
            "default_branch": self.default_branch,
            "updated_at": self.updated_at,
            "language": self.language,
        }


class GitHubClient:
    """Thin, bounded wrapper over the GitHub REST API."""

    def __init__(self, token: str, *, api_root: str = API_ROOT) -> None:
        if not token or not token.strip():
            raise GitHubError("No GitHub token was provided.")
        self._token = token.strip()
        self._api_root = api_root.rstrip("/")

    # -- plumbing ----------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "folio-workspace",
        }

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = path if path.startswith("http") else f"{self._api_root}{path}"
        try:
            with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
                response = client.get(url, headers=self._headers(), params=params)
        except httpx.HTTPError as e:
            raise GitHubError(f"Could not reach GitHub: {e}") from e

        if response.status_code == 401:
            raise GitHubError("The GitHub token is invalid or has been revoked.")
        if response.status_code == 403:
            # 403 covers both rate limiting and insufficient scope; the header
            # is what distinguishes them, and the difference matters to the user.
            if response.headers.get("X-RateLimit-Remaining") == "0":
                raise GitHubError("GitHub's rate limit has been reached. Try again shortly.")
            raise GitHubError("The GitHub token does not have access to that resource.")
        if response.status_code == 404:
            raise GitHubError("That repository or path does not exist, or the token cannot see it.")
        if response.status_code >= 400:
            raise GitHubError(f"GitHub returned {response.status_code}.")

        try:
            return response.json()
        except ValueError as e:
            raise GitHubError("GitHub returned a response that could not be parsed.") from e

    # -- identity ----------------------------------------------------------

    def verify(self) -> dict[str, Any]:
        """Confirm the token works and report who it belongs to."""
        user = self._get("/user")
        return {
            "login": user.get("login"),
            "name": user.get("name"),
            "avatar_url": user.get("avatar_url"),
        }

    # -- repositories ------------------------------------------------------

    def list_repos(self, limit: int = 100) -> list[Repo]:
        """Repositories the token can see, most recently pushed first."""
        raw = self._get(
            "/user/repos",
            {"per_page": min(limit, 100), "sort": "pushed", "affiliation": "owner,collaborator,organization_member"},
        )
        repos = []
        for item in raw or []:
            repos.append(Repo(
                full_name=item.get("full_name", ""),
                description=item.get("description"),
                private=bool(item.get("private")),
                default_branch=item.get("default_branch") or "main",
                updated_at=item.get("pushed_at") or item.get("updated_at") or "",
                language=item.get("language"),
            ))
        return repos

    # -- the four agent tools ---------------------------------------------

    def search_code(self, repo: str, query: str, limit: int = MAX_SEARCH_RESULTS) -> str:
        """
        Search code within one repository.

        `repo:` is appended by us rather than taken from the caller's query, so
        a crafted question cannot widen the search to repositories the user
        never selected.
        """
        query = (query or "").strip()
        if len(query) < 2:
            return "Search query is too short."

        # GitHub's code search rejects some punctuation outright; keeping words
        # and a few code-ish characters avoids 422s on natural-language input.
        cleaned = " ".join(
            "".join(ch for ch in term if ch.isalnum() or ch in "_-.")
            for term in query.split()
        ).strip()
        if not cleaned:
            return "Search query contained nothing searchable."

        try:
            data = self._get(
                "/search/code",
                {"q": f"{cleaned} repo:{repo}", "per_page": min(limit, 50)},
            )
        except GitHubError as e:
            # Code search is the flakiest GitHub endpoint (indexing lag, 422s).
            # Degrading to "no results" beats failing the whole answer.
            log.info("GitHub code search failed for %s: %s", repo, e)
            return f"Code search unavailable: {e}"

        items = data.get("items") or []
        if not items:
            return "No code matches found in this repository."

        lines = []
        for item in items[:limit]:
            path = item.get("path", "?")
            fragments = []
            for match in (item.get("text_matches") or [])[:1]:
                snippet = (match.get("fragment") or "").strip().replace("\n", " ⏎ ")
                if snippet:
                    fragments.append(snippet[:MAX_SNIPPET_CHARS])
            lines.append(f"{path}" + (f" — {fragments[0]}" if fragments else ""))
        return "\n".join(lines)

    def get_commits(self, repo: str, since: str | None = None, path: str | None = None,
                    limit: int = MAX_COMMITS) -> str:
        """Recent commits, optionally for one path (which is `get_file_history`)."""
        params: dict[str, Any] = {"per_page": min(limit, 100)}
        if since:
            params["since"] = since
        if path:
            params["path"] = path

        data = self._get(f"/repos/{repo}/commits", params)
        if not data:
            return "No commits found for that query."

        lines = []
        for item in data[:limit]:
            sha = (item.get("sha") or "")[:7]
            commit = item.get("commit") or {}
            author = (commit.get("author") or {})
            date = (author.get("date") or "")[:10]
            name = author.get("name") or "unknown"
            message = (commit.get("message") or "").splitlines()[0][:160]
            lines.append(f"{sha}  {date}  {name}  {message}")
        return "\n".join(lines)

    def get_file(self, repo: str, path: str, ref: str | None = None) -> str:
        """Read one file's contents at a ref."""
        import base64

        params = {"ref": ref} if ref else None
        data = self._get(f"/repos/{repo}/contents/{path.lstrip('/')}", params)

        if isinstance(data, list):
            names = [d.get("name", "?") for d in data[:60]]
            return "Directory listing:\n" + "\n".join(names)

        if data.get("encoding") != "base64":
            return "That file is not text."
        if int(data.get("size") or 0) > MAX_FILE_BYTES:
            return "That file is too large to read."

        try:
            raw = base64.b64decode(data.get("content") or "")
        except Exception:
            return "That file could not be decoded."
        return raw.decode("utf-8", errors="replace")[:MAX_FILE_BYTES]

    def get_readme(self, repo: str, max_chars: int = 6000) -> str:
        """
        The repository's README, which is what actually answers "what is this
        repo?". Code search cannot: a project's purpose is described in prose,
        not mentioned in its own source.
        """
        import base64

        try:
            data = self._get(f"/repos/{repo}/readme")
        except GitHubError as e:
            return f"(no README available: {e})"

        if data.get("encoding") != "base64":
            return "(README is not text)"
        try:
            raw = base64.b64decode(data.get("content") or "")
        except Exception:
            return "(README could not be decoded)"
        return raw.decode("utf-8", errors="replace")[:max_chars]

    def repo_summary(self, repo: str) -> str:
        data = self._get(f"/repos/{repo}")
        return (
            f"Repository: {data.get('full_name')}\n"
            f"Description: {data.get('description') or '(none)'}\n"
            f"Default branch: {data.get('default_branch')}\n"
            f"Language: {data.get('language') or 'unknown'}\n"
            f"Private: {bool(data.get('private'))}"
        )
