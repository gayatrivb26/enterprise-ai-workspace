using EnterpriseAiWorkspace.Api.Auth;
using EnterpriseAiWorkspace.Api.Services;
using Microsoft.AspNetCore.Mvc;

namespace EnterpriseAiWorkspace.Api.Controllers;

/// <summary>
/// Cache and token/cost telemetry, surfaced in the app's settings screen so
/// the numbers shown there are measured rather than decorative.
/// </summary>
[ApiController]
[Route("api/[controller]")]
public class StatsController(AiServiceClient aiService, ICurrentUser currentUser)
    : ControllerBase
{
    [HttpGet]
    public async Task<IActionResult> Get(CancellationToken ct)
    {
        var userId = await currentUser.GetIdAsync(ct);
        using var response = await aiService.GetStatsAsync(userId.ToString(), ct);
        var body = await response.Content.ReadAsStringAsync(ct);

        if (string.IsNullOrWhiteSpace(body)) return StatusCode((int)response.StatusCode);

        return new ContentResult
        {
            Content = body,
            ContentType = "application/json",
            StatusCode = (int)response.StatusCode,
        };
    }
}
