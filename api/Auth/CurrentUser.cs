using System.Security.Claims;
using EnterpriseAiWorkspace.Api.Data;
using EnterpriseAiWorkspace.Api.Models;
using Microsoft.EntityFrameworkCore;
using Microsoft.Extensions.Options;

namespace EnterpriseAiWorkspace.Api.Auth;

/// <summary>
/// Resolves the caller's local user record from their token.
///
/// The whole authorization model rests on this: controllers ask *this* who the
/// caller is, and never read a user id from the request. A client-supplied
/// userId is an access-control decision made by the attacker.
/// </summary>
public interface ICurrentUser
{
    /// <summary>Local users.id for the caller, provisioning on first sign-in.</summary>
    Task<Guid> GetIdAsync(CancellationToken ct = default);
    string? Email { get; }
    string? Name { get; }
}

public sealed class CurrentUser(
    IHttpContextAccessor accessor,
    WorkspaceDbContext db,
    IOptions<AuthOptions> options,
    ILogger<CurrentUser> logger) : ICurrentUser
{
    private readonly AuthOptions _options = options.Value;
    private Guid? _cached;

    private ClaimsPrincipal? Principal => accessor.HttpContext?.User;

    public string? Email => Principal?.FindFirst(_options.EmailClaim)?.Value;
    public string? Name => Principal?.FindFirst(_options.NameClaim)?.Value;

    private string? Subject =>
        Principal?.FindFirst(_options.SubjectClaim)?.Value
        ?? Principal?.FindFirst(ClaimTypes.NameIdentifier)?.Value;

    public async Task<Guid> GetIdAsync(CancellationToken ct = default)
    {
        if (_cached is not null) return _cached.Value;

        var subject = Subject;
        if (string.IsNullOrWhiteSpace(subject))
            throw new UnauthorizedAccessException("The access token carries no subject claim.");

        // The dev identity maps onto the seeded row so local data survives
        // toggling auth on and off.
        if (subject.StartsWith("dev|", StringComparison.Ordinal))
        {
            _cached = DevAuthenticationHandler.DevUserId;
            return _cached.Value;
        }

        var existing = await db.Users
            .AsNoTracking()
            .FirstOrDefaultAsync(u => u.AuthSubject == subject, ct);

        if (existing is not null)
        {
            _cached = existing.Id;
            return existing.Id;
        }

        // First sign-in: provision a local record keyed to the provider subject.
        var user = new User
        {
            Id = Guid.NewGuid(),
            AuthSubject = subject,
            Email = Email ?? $"{subject}@unknown.invalid",
            Name = Name ?? "New user",
            CreatedAt = DateTimeOffset.UtcNow,
        };

        db.Users.Add(user);
        try
        {
            await db.SaveChangesAsync(ct);
        }
        catch (DbUpdateException)
        {
            // Two concurrent first requests can race; the unique index on
            // auth_sub decides, and the loser re-reads the winner's row.
            db.Entry(user).State = EntityState.Detached;
            var raced = await db.Users.AsNoTracking()
                .FirstAsync(u => u.AuthSubject == subject, ct);
            _cached = raced.Id;
            return raced.Id;
        }

        logger.LogInformation("Provisioned local user {UserId} for subject.", user.Id);
        _cached = user.Id;
        return user.Id;
    }
}
