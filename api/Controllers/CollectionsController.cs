using EnterpriseAiWorkspace.Api.Auth;
using EnterpriseAiWorkspace.Api.Data;
using EnterpriseAiWorkspace.Api.Services;
using Microsoft.AspNetCore.Mvc;
using Microsoft.EntityFrameworkCore;

namespace EnterpriseAiWorkspace.Api.Controllers;

// No UserId: the owner is the authenticated caller, never a request field.
public record CreateCollectionRequest(string Name, string? Color);

/// <summary>
/// Collections group documents so a conversation can be scoped to a subset of
/// the corpus ("only the HR handbook"). Storage lives in the AI service
/// alongside the documents themselves.
/// </summary>
[ApiController]
[Route("api/[controller]")]
public class CollectionsController(
    WorkspaceDbContext db,
    AiServiceClient aiService,
    ICurrentUser currentUser) : ControllerBase
{
    [HttpGet]
    public async Task<IActionResult> List(CancellationToken ct)
    {
        var userId = await currentUser.GetIdAsync(ct);
        return await Relay(await aiService.ListCollectionsAsync(userId.ToString(), ct), ct);
    }

    [HttpPost]
    public async Task<IActionResult> Create([FromBody] CreateCollectionRequest req, CancellationToken ct)
    {
        if (string.IsNullOrWhiteSpace(req.Name))
            return BadRequest(new { error = "A collection needs a name." });

        var userId = await currentUser.GetIdAsync(ct);
        var name = req.Name.Trim();
        if (name.Length > 80) name = name[..80];

        return await Relay(
            await aiService.CreateCollectionAsync(
                userId.ToString(), name, req.Color ?? "indigo", ct), ct);
    }

    [HttpDelete("{id:guid}")]
    public async Task<IActionResult> Delete(Guid id, CancellationToken ct)
    {
        // Without this, any authenticated user could delete another user's
        // collection just by knowing its id.
        var userId = await currentUser.GetIdAsync(ct);
        var owned = await db.Collections.AsNoTracking()
            .AnyAsync(c => c.Id == id && c.UserId == userId, ct);
        if (!owned) return NotFound();

        return await Relay(await aiService.DeleteCollectionAsync(id, ct), ct);
    }

    private async Task<IActionResult> Relay(HttpResponseMessage response, CancellationToken ct)
    {
        using (response)
        {
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
}
