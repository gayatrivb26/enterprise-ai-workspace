using EnterpriseAiWorkspace.Api.Auth;
using EnterpriseAiWorkspace.Api.Data;
using EnterpriseAiWorkspace.Api.Services;
using Microsoft.AspNetCore.Mvc;
using Microsoft.EntityFrameworkCore;

namespace EnterpriseAiWorkspace.Api.Controllers;

/// <summary>
/// Document management. Ingestion, the vector store and the pipeline state
/// machine live in the AI service, so these routes proxy it — but ownership is
/// decided here, against the caller's token, before anything is forwarded.
/// </summary>
[ApiController]
[Route("api/[controller]")]
public class DocumentsController(
    WorkspaceDbContext db,
    AiServiceClient aiService,
    ICurrentUser currentUser,
    ILogger<DocumentsController> logger) : ControllerBase
{
    [HttpPost]
    [RequestSizeLimit(50_000_000)]
    public async Task<IActionResult> Upload(
        [FromForm] IFormFile file,
        [FromForm] Guid? collectionId,
        CancellationToken ct)
    {
        var userId = await currentUser.GetIdAsync(ct);

        var validation = await FileValidator.ValidateAsync(file, ct);
        if (!validation.Ok) return BadRequest(new { error = validation.Error });

        // A collection the caller does not own is silently dropped rather than
        // letting an upload be filed into someone else's collection.
        if (collectionId is not null && !await OwnsCollectionAsync(collectionId.Value, userId, ct))
            collectionId = null;

        var response = await aiService.UploadDocumentAsync(userId.ToString(), file, collectionId, ct);
        return await Relay(response, ct);
    }

    [HttpGet]
    public async Task<IActionResult> List(
        [FromQuery] string? search,
        [FromQuery] string? status,
        [FromQuery] Guid? collectionId,
        CancellationToken ct)
    {
        var userId = await currentUser.GetIdAsync(ct);
        return await Relay(
            await aiService.ListDocumentsAsync(userId.ToString(), search, status, collectionId, ct), ct);
    }

    [HttpGet("{id:guid}")]
    public async Task<IActionResult> Get(Guid id, CancellationToken ct)
    {
        if (!await OwnsDocumentAsync(id, ct)) return NotFound();
        return await Relay(await aiService.GetDocumentAsync(id, ct), ct);
    }

    [HttpDelete("{id:guid}")]
    public async Task<IActionResult> Delete(Guid id, CancellationToken ct)
    {
        if (!await OwnsDocumentAsync(id, ct)) return NotFound();
        return await Relay(await aiService.DeleteDocumentAsync(id, ct), ct);
    }

    [HttpPost("{id:guid}/reingest")]
    public async Task<IActionResult> Reingest(Guid id, CancellationToken ct)
    {
        if (!await OwnsDocumentAsync(id, ct)) return NotFound();
        return await Relay(await aiService.ReingestDocumentAsync(id, ct), ct);
    }

    /// <summary>
    /// Server-Sent Events carrying live ingestion progress, proxied from the AI
    /// service so the browser only ever talks to one origin. The stream is
    /// scoped to the caller, so it can only ever report their own documents.
    /// </summary>
    [HttpGet("events")]
    public async Task Events(CancellationToken ct)
    {
        var userId = await currentUser.GetIdAsync(ct);

        Response.ContentType = "text/event-stream";
        Response.Headers.CacheControl = "no-cache, no-transform";
        Response.Headers["X-Accel-Buffering"] = "no";

        try
        {
            using var upstream = await aiService.StreamDocumentEventsAsync(userId.ToString(), ct);
            if (!upstream.IsSuccessStatusCode)
            {
                await Response.WriteAsync(
                    "event: error\ndata: The document service is unavailable.\n\n", ct);
                await Response.Body.FlushAsync(ct);
                return;
            }

            await using var stream = await upstream.Content.ReadAsStreamAsync(ct);
            using var reader = new StreamReader(stream);

            string? line;
            while ((line = await reader.ReadLineAsync(ct)) is not null)
            {
                await Response.WriteAsync(line + "\n", ct);
                // A blank line closes an SSE frame — flush on frame boundaries
                // so events arrive whole rather than dribbling out.
                if (line.Length == 0) await Response.Body.FlushAsync(ct);
            }
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            // Client closed the tab; nothing to do.
        }
        catch (Exception ex)
        {
            logger.LogWarning(ex, "Document event stream ended unexpectedly.");
        }
    }

    // ── Ownership ───────────────────────────────────────────────────────

    private async Task<bool> OwnsDocumentAsync(Guid documentId, CancellationToken ct)
    {
        var userId = await currentUser.GetIdAsync(ct);
        return await db.Documents.AsNoTracking()
            .AnyAsync(d => d.Id == documentId && d.UserId == userId, ct);
    }

    private Task<bool> OwnsCollectionAsync(Guid collectionId, Guid userId, CancellationToken ct) =>
        db.Collections.AsNoTracking()
            .AnyAsync(c => c.Id == collectionId && c.UserId == userId, ct);

    private async Task<IActionResult> Relay(HttpResponseMessage response, CancellationToken ct)
    {
        using (response)
        {
            var body = await response.Content.ReadAsStringAsync(ct);
            if (string.IsNullOrWhiteSpace(body))
                return StatusCode((int)response.StatusCode);

            return new ContentResult
            {
                Content = body,
                ContentType = "application/json",
                StatusCode = (int)response.StatusCode,
            };
        }
    }
}
