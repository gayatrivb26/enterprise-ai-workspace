using System.Net.Http.Headers;
using System.Text.Json;
using EnterpriseAiWorkspace.Api.Auth;
using EnterpriseAiWorkspace.Api.Data;
using EnterpriseAiWorkspace.Api.Models;
using EnterpriseAiWorkspace.Api.Services;
using Microsoft.AspNetCore.Mvc;
using Microsoft.EntityFrameworkCore;

namespace EnterpriseAiWorkspace.Api.Controllers;

public record ConnectGitHubRequest(string Token);
public record SelectRepoRequest(string? Repo);

public record IntegrationStatus(
    bool Connected,
    string? Login,
    string? Name,
    string? AvatarUrl,
    string? SelectedRepo,
    string? Scopes);

public record RepoSummary(
    string FullName,
    string? Description,
    bool Private,
    string? Language,
    string? UpdatedAt);

/// <summary>
/// Connects a user's GitHub account so the Dev Agent can answer questions
/// about their own repositories — including private ones, because it acts with
/// their token and therefore sees exactly what they can see. Authorisation is
/// GitHub's, which means this service never has to decide who may read what.
/// </summary>
[ApiController]
[Route("api/[controller]")]
public class IntegrationsController(
    WorkspaceDbContext db,
    ICurrentUser currentUser,
    ITokenProtector protector,
    IHttpClientFactory httpClientFactory,
    ILogger<IntegrationsController> logger) : ControllerBase
{
    private const string Provider = "github";

    [HttpGet("github")]
    public async Task<ActionResult<IntegrationStatus>> Status(CancellationToken ct)
    {
        var integration = await FindAsync(ct);
        if (integration is null)
            return Ok(new IntegrationStatus(false, null, null, null, null, null));

        // Note what is never returned: the token itself. Once stored it only
        // ever travels server-side.
        return Ok(new IntegrationStatus(
            true,
            integration.AccountLogin,
            integration.AccountName,
            integration.AvatarUrl,
            integration.SelectedRepo,
            integration.Scopes));
    }

    [HttpPost("github")]
    public async Task<ActionResult<IntegrationStatus>> Connect(
        [FromBody] ConnectGitHubRequest req, CancellationToken ct)
    {
        var token = (req.Token ?? string.Empty).Trim();
        if (token.Length is < 20 or > 500)
            return BadRequest(new { error = "That does not look like a GitHub token." });

        // Verify before storing: a token that cannot even identify itself is
        // not worth persisting, and this is also where we learn the scopes.
        var client = httpClientFactory.CreateClient();
        client.Timeout = TimeSpan.FromSeconds(15);

        using var request = new HttpRequestMessage(HttpMethod.Get, "https://api.github.com/user");
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
        request.Headers.Accept.Add(new MediaTypeWithQualityHeaderValue("application/vnd.github+json"));
        request.Headers.UserAgent.ParseAdd("folio-workspace");

        HttpResponseMessage response;
        try
        {
            response = await client.SendAsync(request, ct);
        }
        catch (Exception ex)
        {
            logger.LogWarning(ex, "Could not reach GitHub while verifying a token.");
            return StatusCode(StatusCodes.Status502BadGateway,
                new { error = "Could not reach GitHub. Try again shortly." });
        }

        using (response)
        {
            if (response.StatusCode == System.Net.HttpStatusCode.Unauthorized)
                return BadRequest(new { error = "GitHub rejected that token." });
            if (!response.IsSuccessStatusCode)
                return BadRequest(new { error = $"GitHub returned {(int)response.StatusCode}." });

            var body = await response.Content.ReadAsStringAsync(ct);
            string? login = null, name = null, avatar = null;
            try
            {
                using var doc = JsonDocument.Parse(body);
                var root = doc.RootElement;
                login = root.TryGetProperty("login", out var l) ? l.GetString() : null;
                name = root.TryGetProperty("name", out var n) ? n.GetString() : null;
                avatar = root.TryGetProperty("avatar_url", out var a) ? a.GetString() : null;
            }
            catch (JsonException) { /* identity is a nicety, not a requirement */ }

            // Classic PATs report granted scopes here; fine-grained tokens do not.
            var scopes = response.Headers.TryGetValues("X-OAuth-Scopes", out var values)
                ? string.Join(", ", values)
                : null;

            var userId = await currentUser.GetIdAsync(ct);
            var integration = await db.Integrations
                .FirstOrDefaultAsync(i => i.UserId == userId && i.Provider == Provider, ct);

            if (integration is null)
            {
                integration = new Integration
                {
                    Id = Guid.NewGuid(),
                    UserId = userId,
                    Provider = Provider,
                    CreatedAt = DateTimeOffset.UtcNow,
                };
                db.Integrations.Add(integration);
            }

            integration.AccessToken = protector.Protect(token);
            integration.AccountLogin = login;
            integration.AccountName = name;
            integration.AvatarUrl = avatar;
            integration.Scopes = scopes;
            integration.UpdatedAt = DateTimeOffset.UtcNow;

            await db.SaveChangesAsync(ct);

            logger.LogInformation("Connected GitHub account for user {UserId}.", userId);
            return Ok(new IntegrationStatus(true, login, name, avatar, integration.SelectedRepo, scopes));
        }
    }

    [HttpDelete("github")]
    public async Task<IActionResult> Disconnect(CancellationToken ct)
    {
        var integration = await FindAsync(ct, tracking: true);
        if (integration is null) return Ok(new { disconnected = true });

        db.Integrations.Remove(integration);
        await db.SaveChangesAsync(ct);
        return Ok(new { disconnected = true });
    }

    [HttpGet("github/repos")]
    public async Task<ActionResult<List<RepoSummary>>> Repos(CancellationToken ct)
    {
        var token = await ResolveTokenAsync(ct);
        if (token is null) return BadRequest(new { error = "GitHub is not connected." });

        var client = httpClientFactory.CreateClient();
        client.Timeout = TimeSpan.FromSeconds(20);

        using var request = new HttpRequestMessage(
            HttpMethod.Get,
            "https://api.github.com/user/repos?per_page=100&sort=pushed&affiliation=owner,collaborator,organization_member");
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
        request.Headers.Accept.Add(new MediaTypeWithQualityHeaderValue("application/vnd.github+json"));
        request.Headers.UserAgent.ParseAdd("folio-workspace");

        using var response = await client.SendAsync(request, ct);
        if (!response.IsSuccessStatusCode)
            return StatusCode(StatusCodes.Status502BadGateway,
                new { error = $"GitHub returned {(int)response.StatusCode}." });

        var body = await response.Content.ReadAsStringAsync(ct);
        var repos = new List<RepoSummary>();
        try
        {
            using var doc = JsonDocument.Parse(body);
            foreach (var item in doc.RootElement.EnumerateArray())
            {
                repos.Add(new RepoSummary(
                    item.GetProperty("full_name").GetString() ?? "",
                    item.TryGetProperty("description", out var d) ? d.GetString() : null,
                    item.TryGetProperty("private", out var p) && p.GetBoolean(),
                    item.TryGetProperty("language", out var l) ? l.GetString() : null,
                    item.TryGetProperty("pushed_at", out var u) ? u.GetString() : null));
            }
        }
        catch (JsonException ex)
        {
            logger.LogWarning(ex, "Could not parse the GitHub repository list.");
            return StatusCode(StatusCodes.Status502BadGateway,
                new { error = "GitHub returned an unexpected response." });
        }

        return Ok(repos);
    }

    [HttpPut("github/repo")]
    public async Task<ActionResult<IntegrationStatus>> SelectRepo(
        [FromBody] SelectRepoRequest req, CancellationToken ct)
    {
        var integration = await FindAsync(ct, tracking: true);
        if (integration is null) return BadRequest(new { error = "GitHub is not connected." });

        var repo = req.Repo?.Trim();
        if (!string.IsNullOrEmpty(repo))
        {
            // "owner/name" only. This value is interpolated into a GitHub API
            // path, so anything else could reshape the request.
            if (!System.Text.RegularExpressions.Regex.IsMatch(
                    repo, @"^[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}$"))
            {
                return BadRequest(new { error = "Repository must be in owner/name form." });
            }
        }

        integration.SelectedRepo = string.IsNullOrEmpty(repo) ? null : repo;
        integration.UpdatedAt = DateTimeOffset.UtcNow;
        await db.SaveChangesAsync(ct);

        return Ok(new IntegrationStatus(
            true, integration.AccountLogin, integration.AccountName,
            integration.AvatarUrl, integration.SelectedRepo, integration.Scopes));
    }

    // ── internals ───────────────────────────────────────────────────────

    private async Task<Integration?> FindAsync(CancellationToken ct, bool tracking = false)
    {
        var userId = await currentUser.GetIdAsync(ct);
        var query = tracking ? db.Integrations : db.Integrations.AsNoTracking();
        return await query.FirstOrDefaultAsync(i => i.UserId == userId && i.Provider == Provider, ct);
    }

    private async Task<string?> ResolveTokenAsync(CancellationToken ct)
    {
        var integration = await FindAsync(ct);
        if (integration is null) return null;
        return protector.TryUnprotect(integration.AccessToken, out var token) ? token : null;
    }
}
