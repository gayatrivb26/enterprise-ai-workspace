using System.Text;
using System.Text.Json;
using EnterpriseAiWorkspace.Api.Auth;
using EnterpriseAiWorkspace.Api.Data;
using EnterpriseAiWorkspace.Api.Models;
using EnterpriseAiWorkspace.Api.Services;
using Microsoft.AspNetCore.Mvc;
using Microsoft.EntityFrameworkCore;

namespace EnterpriseAiWorkspace.Api.Controllers;

/// <summary>
/// Note what is absent from these DTOs: a user id. The caller's identity comes
/// from the validated token only. Accepting a userId from the request body
/// would let anyone read or write anyone's data by editing one field.
/// </summary>
public record SendMessageRequest(Guid? ChatId, string Question, Guid[]? DocumentIds = null);

public record UpdateChatRequest(string? Title, Guid[]? DocumentIds, bool? Archived);

public record ChatSummary(
    Guid Id,
    string Title,
    DateTimeOffset CreatedAt,
    DateTimeOffset UpdatedAt,
    Guid[] DocumentIds,
    int MessageCount,
    string? Preview);

[ApiController]
[Route("api/[controller]")]
public class ChatController(
    WorkspaceDbContext db,
    AiServiceClient aiService,
    ICurrentUser currentUser,
    ITokenProtector protector,
    ILogger<ChatController> logger) : ControllerBase
{
    /// <summary>How many prior turns are replayed to the model for context.</summary>
    private const int HistoryTurns = 6;

    private const int MaxQuestionLength = 8000;

    // ── Conversations ───────────────────────────────────────────────────

    [HttpGet]
    public async Task<ActionResult<List<ChatSummary>>> GetChats(
        [FromQuery] string? search, CancellationToken ct)
    {
        var userId = await currentUser.GetIdAsync(ct);

        var query = db.Chats.Where(c => c.UserId == userId && !c.Archived);

        if (!string.IsNullOrWhiteSpace(search))
        {
            // EF.Functions.ILike parameterises the term, so the wildcards below
            // are the only pattern characters that reach Postgres.
            var term = $"%{EscapeLike(search.Trim())}%";
            query = query.Where(c =>
                EF.Functions.ILike(c.Title, term) ||
                db.Messages.Any(m => m.ChatId == c.Id && EF.Functions.ILike(m.Content, term)));
        }

        var chats = await query
            .OrderByDescending(c => c.UpdatedAt)
            .Select(c => new ChatSummary(
                c.Id,
                c.Title,
                c.CreatedAt,
                c.UpdatedAt,
                c.DocumentIds,
                db.Messages.Count(m => m.ChatId == c.Id),
                db.Messages.Where(m => m.ChatId == c.Id)
                    .OrderByDescending(m => m.CreatedAt)
                    .Select(m => m.Content)
                    .FirstOrDefault()))
            .Take(200)
            .ToListAsync(ct);

        return Ok(chats);
    }

    [HttpGet("{chatId:guid}/history")]
    public async Task<ActionResult<List<Message>>> GetHistory(Guid chatId, CancellationToken ct)
    {
        // Ownership first: without this, any authenticated user could read any
        // conversation by guessing or leaking its id.
        if (await FindOwnedChatAsync(chatId, ct) is null) return NotFound();

        var messages = await db.Messages
            .Where(m => m.ChatId == chatId)
            .OrderBy(m => m.CreatedAt)
            .ToListAsync(ct);
        return Ok(messages);
    }

    [HttpPatch("{chatId:guid}")]
    public async Task<IActionResult> UpdateChat(
        Guid chatId, [FromBody] UpdateChatRequest req, CancellationToken ct)
    {
        var chat = await FindOwnedChatAsync(chatId, ct, tracking: true);
        if (chat is null) return NotFound();

        if (!string.IsNullOrWhiteSpace(req.Title))
        {
            var title = req.Title.Trim();
            chat.Title = title.Length > 120 ? title[..120] : title;
        }
        if (req.DocumentIds is not null)
            chat.DocumentIds = await FilterOwnedDocumentsAsync(req.DocumentIds, ct);
        if (req.Archived is not null) chat.Archived = req.Archived.Value;
        chat.UpdatedAt = DateTimeOffset.UtcNow;

        await db.SaveChangesAsync(ct);
        return Ok(new { chat.Id, chat.Title, chat.DocumentIds, chat.Archived });
    }

    [HttpDelete("{chatId:guid}")]
    public async Task<IActionResult> DeleteChat(Guid chatId, CancellationToken ct)
    {
        var chat = await FindOwnedChatAsync(chatId, ct, tracking: true);
        if (chat is null) return NotFound();

        db.Chats.Remove(chat); // messages cascade at the database level
        await db.SaveChangesAsync(ct);
        return Ok(new { deleted = chatId });
    }

    // ── Streaming ───────────────────────────────────────────────────────

    /// <summary>
    /// Streams the assistant's answer back as Server-Sent Events while it
    /// arrives from the Python AI service, then persists both the question and
    /// the final answer once the stream completes.
    /// </summary>
    /// <remarks>
    /// Two contract guarantees this endpoint owes the browser:
    /// <list type="bullet">
    /// <item>a terminal <c>done</c> or <c>error</c> event is always emitted, on
    /// every path, so the UI can never be left waiting forever; and</item>
    /// <item>SSE framing is preserved exactly — a delta containing newlines is
    /// re-emitted as multiple <c>data:</c> lines, which the client rejoins with
    /// "\n" per the SSE spec.</item>
    /// </list>
    /// </remarks>
    [HttpPost("stream")]
    public async Task StreamMessage([FromBody] SendMessageRequest req, CancellationToken ct)
    {
        var userId = await currentUser.GetIdAsync(ct);

        var question = (req.Question ?? string.Empty).Trim();
        if (question.Length == 0 || question.Length > MaxQuestionLength)
        {
            Response.StatusCode = StatusCodes.Status400BadRequest;
            await Response.WriteAsync("{\"error\":\"Question must be between 1 and 8000 characters.\"}", ct);
            return;
        }

        var now = DateTimeOffset.UtcNow;

        // An existing chat is only continued if the caller owns it; otherwise a
        // supplied chatId would append the attacker's turn to someone else's
        // conversation and stream that conversation's context back.
        var chat = req.ChatId is null ? null : await FindOwnedChatAsync(req.ChatId.Value, ct, tracking: true);
        if (req.ChatId is not null && chat is null)
        {
            Response.StatusCode = StatusCodes.Status404NotFound;
            await Response.WriteAsync("{\"error\":\"Conversation not found.\"}", ct);
            return;
        }

        // Document scope is likewise filtered to what the caller owns.
        var requestedScope = await FilterOwnedDocumentsAsync(req.DocumentIds ?? [], ct);

        var chatId = chat?.Id ?? Guid.NewGuid();
        if (chat is null)
        {
            chat = new Chat
            {
                Id = chatId,
                UserId = userId,
                Title = BuildTitle(question),
                CreatedAt = now,
                UpdatedAt = now,
                DocumentIds = requestedScope,
            };
            db.Chats.Add(chat);
        }
        else
        {
            chat.UpdatedAt = now;
            if (req.DocumentIds is not null) chat.DocumentIds = requestedScope;
        }

        // Prior turns give the model the context to resolve follow-ups. Read
        // before the new question is added so it is not duplicated.
        var history = await db.Messages
            .Where(m => m.ChatId == chatId)
            .OrderByDescending(m => m.CreatedAt)
            .Take(HistoryTurns)
            .Select(m => new { m.Role, m.Content })
            .ToListAsync(ct);
        history.Reverse();

        db.Messages.Add(new Message
        {
            Id = Guid.NewGuid(),
            ChatId = chatId,
            Role = "user",
            Content = question,
            CreatedAt = now,
        });
        await db.SaveChangesAsync(ct);

        // Assign, don't Append: appending to Content-Type risks emitting a
        // second, malformed value alongside whatever the framework sets.
        Response.ContentType = "text/event-stream";
        Response.Headers.CacheControl = "no-cache, no-transform";
        Response.Headers["X-Chat-Id"] = chatId.ToString();
        // Tells nginx and friends not to buffer the stream into oblivion.
        Response.Headers["X-Accel-Buffering"] = "no";

        var answer = new StringBuilder();
        var sourcesJson = "[]";
        var meta = new StreamMeta();
        string? failure = null;
        var startedAt = System.Diagnostics.Stopwatch.StartNew();

        var (githubToken, githubRepo) = await ResolveGitHubAsync(userId, ct);

        try
        {
            using var upstream = await aiService.StreamChatAsync(
                userId.ToString(), question, useMemory: true, ct,
                chatId.ToString(),
                chat.DocumentIds,
                history.Select(h => new { role = h.Role, content = h.Content }),
                githubToken,
                githubRepo);

            if (!upstream.IsSuccessStatusCode)
            {
                var detail = await SafeReadAsync(upstream, ct);
                logger.LogError("AI service returned {Status}: {Detail}", (int)upstream.StatusCode, detail);
                failure = "The AI service is unavailable right now.";
            }
            else
            {
                await using var stream = await upstream.Content.ReadAsStreamAsync(ct);
                using var reader = new StreamReader(stream, Encoding.UTF8);

                var currentEvent = "message";
                var data = new List<string>();

                // NOTE: `while (!reader.EndOfStream)` would do a *synchronous,
                // blocking* read to answer the question, tying up a thread-pool
                // thread for the whole generation. ReadLineAsync returning null
                // is the correct end-of-stream signal.
                string? line;
                while ((line = await reader.ReadLineAsync(ct)) is not null)
                {
                    if (line.Length == 0)
                    {
                        await DispatchAsync(currentEvent, data, answer, ct,
                            onSources: json => sourcesJson = json,
                            onMeta: json => meta = ParseMeta(json, meta),
                            onError: message => failure = string.IsNullOrWhiteSpace(message)
                                ? "The AI service reported an error."
                                : message);
                        currentEvent = "message";
                        data.Clear();
                        continue;
                    }

                    if (line[0] == ':')
                    {
                        // Keep-alive comment: forward it so intermediaries keep
                        // the connection warm, but do not treat it as data.
                        await Response.WriteAsync(line + "\n\n", ct);
                        await Response.Body.FlushAsync(ct);
                        continue;
                    }

                    var colon = line.IndexOf(':');
                    var field = colon < 0 ? line : line[..colon];
                    var value = colon < 0 ? string.Empty : line[(colon + 1)..];
                    if (value.StartsWith(' ')) value = value[1..]; // exactly one

                    if (field == "event") currentEvent = value;
                    else if (field == "data") data.Add(value);
                }

                // A final frame that arrived without its trailing blank line.
                await DispatchAsync(currentEvent, data, answer, ct,
                    onSources: json => sourcesJson = json,
                    onMeta: json => meta = ParseMeta(json, meta),
                    onError: message => failure = string.IsNullOrWhiteSpace(message)
                                ? "The AI service reported an error."
                                : message);
            }
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            // Browser navigated away or hit Stop. Nothing to send; the partial
            // answer is still persisted below.
            logger.LogInformation("Chat stream {ChatId} cancelled by the client.", chatId);
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "Chat stream {ChatId} failed.", chatId);
            failure = "The AI service became unreachable while answering.";
        }

        // Always terminate the stream properly, whatever happened above.
        if (!ct.IsCancellationRequested)
        {
            try
            {
                if (failure is not null) await WriteEventAsync("error", failure, ct);
                await WriteEventAsync("done", string.Empty, ct);
            }
            catch (Exception ex)
            {
                logger.LogDebug(ex, "Could not write terminal SSE event (client likely gone).");
            }
        }

        await PersistAnswerAsync(chatId, answer.ToString(), sourcesJson, meta,
            (int)startedAt.ElapsedMilliseconds);
    }

    // ── Ownership helpers ───────────────────────────────────────────────

    private async Task<Chat?> FindOwnedChatAsync(Guid chatId, CancellationToken ct, bool tracking = false)
    {
        var userId = await currentUser.GetIdAsync(ct);
        var query = tracking ? db.Chats : db.Chats.AsNoTracking();
        // Returning null (-> 404) rather than 403 avoids confirming that an id
        // exists for some other user.
        return await query.FirstOrDefaultAsync(c => c.Id == chatId && c.UserId == userId, ct);
    }

    /// <summary>Reduces a requested document scope to the ids the caller owns.</summary>
    private async Task<Guid[]> FilterOwnedDocumentsAsync(Guid[] requested, CancellationToken ct)
    {
        if (requested.Length == 0) return [];
        var userId = await currentUser.GetIdAsync(ct);
        return await db.Documents
            .Where(d => d.UserId == userId && requested.Contains(d.Id))
            .Select(d => d.Id)
            .ToArrayAsync(ct);
    }

    /// <summary>
    /// Decrypts the caller's GitHub token, if they have connected one and
    /// chosen a repository. Returns nulls otherwise, which the AI service
    /// reads as "fall back to the locally mounted repo, if any".
    /// </summary>
    private async Task<(string? Token, string? Repo)> ResolveGitHubAsync(
        Guid userId, CancellationToken ct)
    {
        var integration = await db.Integrations.AsNoTracking()
            .FirstOrDefaultAsync(i => i.UserId == userId && i.Provider == "github", ct);

        if (integration is null || string.IsNullOrEmpty(integration.SelectedRepo))
            return (null, null);

        if (!protector.TryUnprotect(integration.AccessToken, out var token))
        {
            // Usually means the encryption key changed. Treat it as not
            // connected rather than failing the whole chat request.
            logger.LogWarning("Could not decrypt the GitHub token for user {UserId}.", userId);
            return (null, null);
        }

        return (token, integration.SelectedRepo);
    }

    private static string EscapeLike(string value) =>
        value.Replace("\\", "\\\\").Replace("%", "\\%").Replace("_", "\\_");

    // ── SSE plumbing ────────────────────────────────────────────────────

    private sealed record StreamMeta(int TokensIn = 0, int TokensOut = 0, bool Cached = false);

    private static StreamMeta ParseMeta(string json, StreamMeta fallback)
    {
        try
        {
            using var doc = JsonDocument.Parse(json);
            var root = doc.RootElement;
            return new StreamMeta(
                root.TryGetProperty("tokens_in", out var i) ? i.GetInt32() : 0,
                root.TryGetProperty("tokens_out", out var o) ? o.GetInt32() : 0,
                root.TryGetProperty("cached", out var c) && c.GetBoolean());
        }
        catch
        {
            return fallback;
        }
    }

    /// <summary>Handles one complete SSE frame from the upstream service.</summary>
    private async Task DispatchAsync(
        string eventName,
        List<string> data,
        StringBuilder answer,
        CancellationToken ct,
        Action<string> onSources,
        Action<string> onMeta,
        Action<string> onError)
    {
        if (data.Count == 0 && eventName == "message") return;

        // Multiple data: lines in one frame represent embedded newlines.
        var payload = string.Join("\n", data);

        switch (eventName)
        {
            case "delta":
                answer.Append(payload);
                await WriteEventAsync("delta", payload, ct);
                break;

            case "sources":
                onSources(payload);
                await WriteEventAsync("sources", payload, ct);
                break;

            case "meta":
                onMeta(payload);
                await WriteEventAsync("meta", payload, ct);
                break;

            case "error":
                // Recorded, then emitted once by the terminal block so the
                // client never sees two different error events.
                onError(payload);
                break;

            case "done":
                // Swallowed: this method emits the single terminal `done`
                // itself, so a well-behaved upstream cannot cause two.
                break;

            default:
                await WriteEventAsync(eventName, payload, ct);
                break;
        }
    }

    private async Task WriteEventAsync(string eventName, string data, CancellationToken ct)
    {
        var frame = new StringBuilder();
        frame.Append("event: ").Append(eventName).Append('\n');
        // Re-split on newlines so the frame stays spec-compliant.
        foreach (var chunk in data.Split('\n'))
            frame.Append("data: ").Append(chunk).Append('\n');
        frame.Append('\n');

        await Response.WriteAsync(frame.ToString(), ct);
        await Response.Body.FlushAsync(ct);
    }

    // ── Persistence ─────────────────────────────────────────────────────

    /// <summary>
    /// Persists whatever the assistant produced, including a partial answer
    /// after a cancel or mid-stream failure. Deliberately uses
    /// CancellationToken.None: the request token is already tripped on the
    /// paths that need this most.
    /// </summary>
    private async Task PersistAnswerAsync(
        Guid chatId, string content, string sourcesJson, StreamMeta meta, int latencyMs)
    {
        if (string.IsNullOrWhiteSpace(content)) return;

        try
        {
            db.Messages.Add(new Message
            {
                Id = Guid.NewGuid(),
                ChatId = chatId,
                Role = "assistant",
                Content = content,
                CreatedAt = DateTimeOffset.UtcNow,
                Sources = string.IsNullOrWhiteSpace(sourcesJson) ? "[]" : sourcesJson,
                TokensIn = meta.TokensIn,
                TokensOut = meta.TokensOut,
                Cached = meta.Cached,
                LatencyMs = latencyMs,
            });
            await db.SaveChangesAsync(CancellationToken.None);
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "Failed to persist the assistant answer for chat {ChatId}.", chatId);
        }
    }

    private static string BuildTitle(string question)
    {
        var trimmed = question.Trim().ReplaceLineEndings(" ");
        return trimmed.Length > 60 ? trimmed[..60] + "…" : trimmed;
    }

    private static async Task<string> SafeReadAsync(HttpResponseMessage response, CancellationToken ct)
    {
        try
        {
            var body = await response.Content.ReadAsStringAsync(ct);
            return body.Length > 500 ? body[..500] : body;
        }
        catch
        {
            return "<unreadable>";
        }
    }
}
