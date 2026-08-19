namespace EnterpriseAiWorkspace.Api.Models;

public class User
{
    public Guid Id { get; set; }
    public string Email { get; set; } = "";
    public string Name { get; set; } = "";

    /// <summary>
    /// Stable identifier from the identity provider (the token's `sub`).
    /// This — not the email — is the join key: emails get reassigned, and
    /// trusting one would hand a new employee the previous holder's data.
    /// </summary>
    public string? AuthSubject { get; set; }

    public DateTimeOffset CreatedAt { get; set; }
}

public class Chat
{
    public Guid Id { get; set; }
    public Guid UserId { get; set; }
    public string Title { get; set; } = "New chat";
    public DateTimeOffset CreatedAt { get; set; }
    public DateTimeOffset UpdatedAt { get; set; }
    public bool Archived { get; set; }

    /// <summary>Documents this conversation is scoped to. Empty = search everything.</summary>
    public Guid[] DocumentIds { get; set; } = [];

    public List<Message> Messages { get; set; } = new();
}

public class Message
{
    public Guid Id { get; set; }
    public Guid ChatId { get; set; }
    public string Role { get; set; } = "user"; // "user" | "assistant"
    public string Content { get; set; } = "";
    public DateTimeOffset CreatedAt { get; set; }

    /// <summary>
    /// Citations as raw JSON. They previously existed only inside the SSE
    /// stream, so reloading a conversation lost every source it was based on.
    /// </summary>
    public string Sources { get; set; } = "[]";

    public int TokensIn { get; set; }
    public int TokensOut { get; set; }
    public bool Cached { get; set; }
    public int LatencyMs { get; set; }
}

public class Document
{
    public Guid Id { get; set; }
    public Guid UserId { get; set; }
    public string Filename { get; set; } = "";
    public string Type { get; set; } = "";      // "pdf" | "markdown" | "text"
    public string Status { get; set; } = "queued";
    public int Progress { get; set; }
    public string? Error { get; set; }
    public long SizeBytes { get; set; }
    public int? PageCount { get; set; }
    public int ChunkCount { get; set; }
    public int TokenCount { get; set; }
    public Guid? CollectionId { get; set; }
    public DateTimeOffset UploadedAt { get; set; }
    public DateTimeOffset UpdatedAt { get; set; }
}

public class Collection
{
    public Guid Id { get; set; }
    public Guid UserId { get; set; }
    public string Name { get; set; } = "";
    public string Color { get; set; } = "indigo";
    public DateTimeOffset CreatedAt { get; set; }
}

/// <summary>A user's connection to an external provider, e.g. GitHub.</summary>
public class Integration
{
    public Guid Id { get; set; }
    public Guid UserId { get; set; }
    public string Provider { get; set; } = "github";

    /// <summary>AES-256-GCM ciphertext — never the raw token.</summary>
    public string AccessToken { get; set; } = "";

    public string? AccountLogin { get; set; }
    public string? AccountName { get; set; }
    public string? AvatarUrl { get; set; }
    public string? Scopes { get; set; }

    /// <summary>Repository the Dev Agent answers against, e.g. "octocat/hello".</summary>
    public string? SelectedRepo { get; set; }

    public DateTimeOffset CreatedAt { get; set; }
    public DateTimeOffset UpdatedAt { get; set; }
}
