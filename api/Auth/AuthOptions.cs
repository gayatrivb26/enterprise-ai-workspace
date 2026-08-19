namespace EnterpriseAiWorkspace.Api.Auth;

/// <summary>
/// Authentication configuration, provider-agnostic by design.
///
/// Auth0 and Supabase both issue standard OIDC JWTs, so validating them needs
/// nothing more than an authority and an audience — which is why this is
/// config rather than a provider-specific integration. Point <see cref="Authority"/>
/// at either one (or Entra, Keycloak, Cognito) and nothing else changes.
/// </summary>
public sealed class AuthOptions
{
    public const string SectionName = "Auth";

    /// <summary>
    /// When false, the API runs with a single seeded development identity.
    /// This is refused outside the Development environment — see Program.cs —
    /// so a misconfigured production deployment fails closed rather than
    /// silently serving every request as the dev user.
    /// </summary>
    public bool Enabled { get; set; }

    /// <summary>e.g. https://your-tenant.eu.auth0.com/ or https://xxx.supabase.co/auth/v1</summary>
    public string? Authority { get; set; }

    /// <summary>The API identifier the token must be issued for.</summary>
    public string? Audience { get; set; }

    /// <summary>Supabase signs with a shared secret rather than JWKS.</summary>
    public string? SigningKey { get; set; }

    /// <summary>Claim holding the stable provider user id. `sub` for both Auth0 and Supabase.</summary>
    public string SubjectClaim { get; set; } = "sub";

    public string EmailClaim { get; set; } = "email";
    public string NameClaim { get; set; } = "name";

    /// <summary>
    /// Shared secret the API presents to the Python AI service. The AI service
    /// holds the model keys, the vector store and every user's documents; it
    /// must not be callable directly just because it is reachable on the
    /// network.
    /// </summary>
    public string? ServiceToken { get; set; }

    /// <summary>
    /// Key used to encrypt stored third-party access tokens (see
    /// <c>TokenProtector</c>). Required outside Development.
    /// </summary>
    public string? TokenEncryptionKey { get; set; }
}
