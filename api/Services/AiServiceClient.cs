using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;

namespace EnterpriseAiWorkspace.Api.Services;

/// <summary>
/// Thin HTTP client wrapper around the Python AI service. ASP.NET Core stays
/// the system of record (persists chats and messages) while delegating
/// anything LLM- or vector-specific to FastAPI, per the split described in
/// docs/design.md.
/// </summary>
public class AiServiceClient(HttpClient httpClient)
{
    private static readonly JsonSerializerOptions Json = new(JsonSerializerDefaults.Web);

    // ── Chat ────────────────────────────────────────────────────────────

    public async Task<HttpResponseMessage> StreamChatAsync(
        string userId,
        string question,
        bool useMemory,
        CancellationToken ct,
        string? chatId = null,
        IEnumerable<Guid>? documentIds = null,
        IEnumerable<object>? history = null,
        string? githubToken = null,
        string? githubRepo = null)
    {
        var payload = new
        {
            user_id = userId,
            question,
            use_memory = useMemory,
            chat_id = chatId,
            document_ids = documentIds?.Select(d => d.ToString()).ToArray() ?? [],
            history = history ?? Array.Empty<object>(),
            // Sent only when the user has connected GitHub. The API is the sole
            // custodian of the encryption key, so the token is decrypted here
            // and handed over the already-authenticated service channel rather
            // than the key being shared with a second service.
            github_token = githubToken,
            github_repo = githubRepo,
        };

        var request = new HttpRequestMessage(HttpMethod.Post, "/chat/stream")
        {
            Content = new StringContent(
                JsonSerializer.Serialize(payload, Json), Encoding.UTF8, "application/json"),
        };

        // ResponseHeadersRead lets us start forwarding SSE bytes as they arrive
        // instead of buffering the whole streamed response.
        return await httpClient.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, ct);
    }

    // ── Documents ───────────────────────────────────────────────────────

    public async Task<HttpResponseMessage> UploadDocumentAsync(
        string userId, IFormFile file, Guid? collectionId, CancellationToken ct)
    {
        using var content = new MultipartFormDataContent();
        content.Add(new StringContent(userId), "user_id");
        if (collectionId is not null)
            content.Add(new StringContent(collectionId.Value.ToString()), "collection_id");

        var streamContent = new StreamContent(file.OpenReadStream());
        streamContent.Headers.ContentType =
            new MediaTypeHeaderValue(file.ContentType ?? "application/octet-stream");
        content.Add(streamContent, "file", file.FileName);

        return await httpClient.PostAsync("/documents", content, ct);
    }

    public Task<HttpResponseMessage> ListDocumentsAsync(
        string userId, string? search, string? status, Guid? collectionId, CancellationToken ct)
    {
        var query = new List<string> { $"user_id={Uri.EscapeDataString(userId)}" };
        if (!string.IsNullOrWhiteSpace(search)) query.Add($"search={Uri.EscapeDataString(search)}");
        if (!string.IsNullOrWhiteSpace(status)) query.Add($"status={Uri.EscapeDataString(status)}");
        if (collectionId is not null) query.Add($"collection_id={collectionId}");
        return httpClient.GetAsync($"/documents?{string.Join('&', query)}", ct);
    }

    public Task<HttpResponseMessage> GetDocumentAsync(Guid id, CancellationToken ct) =>
        httpClient.GetAsync($"/documents/{id}", ct);

    public Task<HttpResponseMessage> DeleteDocumentAsync(Guid id, CancellationToken ct) =>
        httpClient.DeleteAsync($"/documents/{id}", ct);

    public Task<HttpResponseMessage> ReingestDocumentAsync(Guid id, CancellationToken ct) =>
        httpClient.PostAsync($"/documents/{id}/reingest", null, ct);

    /// <summary>Long-lived SSE stream of document pipeline progress.</summary>
    public Task<HttpResponseMessage> StreamDocumentEventsAsync(string userId, CancellationToken ct)
    {
        var request = new HttpRequestMessage(
            HttpMethod.Get, $"/documents/events?user_id={Uri.EscapeDataString(userId)}");
        return httpClient.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, ct);
    }

    // ── Collections ─────────────────────────────────────────────────────

    public Task<HttpResponseMessage> ListCollectionsAsync(string userId, CancellationToken ct) =>
        httpClient.GetAsync($"/collections?user_id={Uri.EscapeDataString(userId)}", ct);

    public Task<HttpResponseMessage> CreateCollectionAsync(
        string userId, string name, string color, CancellationToken ct)
    {
        var payload = JsonSerializer.Serialize(new { user_id = userId, name, color }, Json);
        return httpClient.PostAsync(
            "/collections", new StringContent(payload, Encoding.UTF8, "application/json"), ct);
    }

    public Task<HttpResponseMessage> DeleteCollectionAsync(Guid id, CancellationToken ct) =>
        httpClient.DeleteAsync($"/collections/{id}", ct);

    // ── Telemetry ───────────────────────────────────────────────────────

    public Task<HttpResponseMessage> GetStatsAsync(string userId, CancellationToken ct) =>
        httpClient.GetAsync($"/stats?user_id={Uri.EscapeDataString(userId)}", ct);
}
