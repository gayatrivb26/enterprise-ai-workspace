using System.Security.Claims;
using System.Text.Encodings.Web;
using Microsoft.AspNetCore.Authentication;
using Microsoft.Extensions.Options;

namespace EnterpriseAiWorkspace.Api.Auth;

/// <summary>
/// Authenticates every request as the seeded development user.
///
/// This exists so the stack stays runnable without provisioning an external
/// identity provider. It is registered only when Auth:Enabled is false AND the
/// environment is Development (Program.cs throws otherwise), so it cannot be
/// the reason a production deployment accepts unauthenticated traffic.
/// </summary>
public sealed class DevAuthenticationHandler(
    IOptionsMonitor<AuthenticationSchemeOptions> options,
    ILoggerFactory logger,
    UrlEncoder encoder) : AuthenticationHandler<AuthenticationSchemeOptions>(options, logger, encoder)
{
    public const string SchemeName = "DevAuth";

    /// <summary>Matches the user seeded by db/init.sql.</summary>
    public static readonly Guid DevUserId = Guid.Parse("00000000-0000-0000-0000-000000000001");

    protected override Task<AuthenticateResult> HandleAuthenticateAsync()
    {
        var claims = new[]
        {
            new Claim(ClaimTypes.NameIdentifier, $"dev|{DevUserId}"),
            new Claim("sub", $"dev|{DevUserId}"),
            new Claim("email", "dev@local.test"),
            new Claim("name", "Dev User"),
        };

        var identity = new ClaimsIdentity(claims, SchemeName);
        var ticket = new AuthenticationTicket(new ClaimsPrincipal(identity), SchemeName);
        return Task.FromResult(AuthenticateResult.Success(ticket));
    }
}
