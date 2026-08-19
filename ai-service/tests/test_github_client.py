"""
GitHub client behaviour, against a mocked transport.

No network is touched. What is being checked is the part that would be
expensive to get wrong in production: that a caller's text cannot widen the
search beyond the selected repository, and that GitHub's failure modes are
translated into messages a user can act on.
"""
from __future__ import annotations

import httpx
import pytest

from agents.github_client import GitHubClient, GitHubError


def client_with(handler) -> GitHubClient:
    """A GitHubClient whose HTTP calls are served by `handler`."""
    transport = httpx.MockTransport(handler)
    github = GitHubClient("ghp_testtoken_0123456789")

    original = httpx.Client

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    github._patched_client = patched  # type: ignore[attr-defined]
    return github


@pytest.fixture()
def patch_httpx(monkeypatch):
    """Route every httpx.Client through a caller-supplied handler."""
    def apply(handler):
        transport = httpx.MockTransport(handler)
        original = httpx.Client

        def factory(*args, **kwargs):
            kwargs.pop("transport", None)
            return original(*args, transport=transport, **kwargs)

        monkeypatch.setattr(httpx, "Client", factory)
    return apply


def json_response(payload, status=200, headers=None):
    return httpx.Response(status, json=payload, headers=headers or {})


# ── Identity ────────────────────────────────────────────────────────────────

def test_verify_reports_the_account(patch_httpx):
    patch_httpx(lambda request: json_response(
        {"login": "octocat", "name": "The Octocat", "avatar_url": "https://x/y.png"}))

    who = GitHubClient("ghp_x" * 6).verify()
    assert who["login"] == "octocat"
    assert who["name"] == "The Octocat"


def test_empty_token_is_refused():
    with pytest.raises(GitHubError):
        GitHubClient("   ")


# ── Scope confinement ───────────────────────────────────────────────────────

def test_search_is_confined_to_the_selected_repository(patch_httpx):
    """
    The `repo:` qualifier is appended by us, never taken from the query, so a
    crafted question cannot reach repositories the user did not select.
    """
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["q"] = request.url.params.get("q", "")
        return json_response({"items": []})

    patch_httpx(handler)
    GitHubClient("ghp_x" * 6).search_code("acme/private", "authenticate")

    assert "repo:acme/private" in seen["q"]


def test_query_punctuation_cannot_inject_extra_qualifiers(patch_httpx):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["q"] = request.url.params.get("q", "")
        return json_response({"items": []})

    patch_httpx(handler)
    GitHubClient("ghp_x" * 6).search_code("acme/app", "secret repo:victim/private user:someone")

    # The colons that would have created new qualifiers are stripped, so only
    # our own repo: qualifier survives.
    assert seen["q"].count("repo:") == 1
    assert "victim/private" not in seen["q"]


# ── Result shaping ──────────────────────────────────────────────────────────

def test_search_results_list_paths(patch_httpx):
    patch_httpx(lambda request: json_response({"items": [
        {"path": "src/auth.py", "text_matches": [{"fragment": "def authenticate(user)"}]},
        {"path": "src/app.py", "text_matches": []},
    ]}))

    output = GitHubClient("ghp_x" * 6).search_code("acme/app", "authenticate")
    assert "src/auth.py" in output
    assert "def authenticate(user)" in output


def test_no_results_is_reported_plainly(patch_httpx):
    patch_httpx(lambda request: json_response({"items": []}))
    assert "No code matches" in GitHubClient("ghp_x" * 6).search_code("acme/app", "nothing")


def test_commits_are_formatted_one_per_line(patch_httpx):
    patch_httpx(lambda request: json_response([
        {"sha": "abcdef1234", "commit": {"message": "Add auth\n\nlong body",
                                         "author": {"name": "Priya", "date": "2026-01-05T10:00:00Z"}}},
        {"sha": "1234abcdef", "commit": {"message": "Fix logout",
                                         "author": {"name": "Chen", "date": "2026-01-04T10:00:00Z"}}},
    ]))

    output = GitHubClient("ghp_x" * 6).get_commits("acme/app")
    lines = output.splitlines()
    assert len(lines) == 2
    assert "abcdef1" in lines[0] and "Priya" in lines[0] and "Add auth" in lines[0]
    # Only the subject line, never the whole commit body.
    assert "long body" not in output


# ── Failure translation ─────────────────────────────────────────────────────

def test_invalid_token_says_so(patch_httpx):
    patch_httpx(lambda request: httpx.Response(401, json={}))
    with pytest.raises(GitHubError, match="invalid or has been revoked"):
        GitHubClient("ghp_x" * 6).verify()


def test_rate_limiting_is_distinguished_from_permission(patch_httpx):
    patch_httpx(lambda request: httpx.Response(
        403, json={}, headers={"X-RateLimit-Remaining": "0"}))
    with pytest.raises(GitHubError, match="rate limit"):
        GitHubClient("ghp_x" * 6).verify()


def test_forbidden_without_rate_limit_is_a_permission_problem(patch_httpx):
    patch_httpx(lambda request: httpx.Response(403, json={}, headers={"X-RateLimit-Remaining": "42"}))
    with pytest.raises(GitHubError, match="does not have access"):
        GitHubClient("ghp_x" * 6).verify()


def test_missing_repository_is_reported(patch_httpx):
    patch_httpx(lambda request: httpx.Response(404, json={}))
    with pytest.raises(GitHubError, match="does not exist"):
        GitHubClient("ghp_x" * 6).repo_summary("acme/ghost")


def test_code_search_failure_degrades_instead_of_raising(patch_httpx):
    """
    Code search is GitHub's flakiest endpoint (indexing lag, 422s). Losing it
    should cost one tool's output, not the whole answer.
    """
    patch_httpx(lambda request: httpx.Response(422, json={}))
    output = GitHubClient("ghp_x" * 6).search_code("acme/app", "authenticate")
    assert "unavailable" in output.lower()


def test_network_failure_is_translated(patch_httpx):
    def handler(request):
        raise httpx.ConnectError("no route to host")

    patch_httpx(handler)
    with pytest.raises(GitHubError, match="Could not reach GitHub"):
        GitHubClient("ghp_x" * 6).verify()


# ── Repos ───────────────────────────────────────────────────────────────────

def test_repos_are_normalised(patch_httpx):
    patch_httpx(lambda request: json_response([
        {"full_name": "acme/app", "description": "The app", "private": True,
         "default_branch": "main", "pushed_at": "2026-02-01T00:00:00Z", "language": "Python"},
        {"full_name": "acme/site", "private": False, "default_branch": "trunk"},
    ]))

    repos = GitHubClient("ghp_x" * 6).list_repos()
    assert [r.full_name for r in repos] == ["acme/app", "acme/site"]
    assert repos[0].private is True
    # A repo missing default_branch must not break the listing.
    assert repos[1].default_branch == "trunk"
