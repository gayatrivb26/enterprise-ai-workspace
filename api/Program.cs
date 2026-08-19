using System.Text;
using EnterpriseAiWorkspace.Api.Auth;
using EnterpriseAiWorkspace.Api.Data;
using EnterpriseAiWorkspace.Api.Services;
using Microsoft.AspNetCore.Authentication;
using Microsoft.AspNetCore.Authentication.JwtBearer;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Http.Features;
using Microsoft.EntityFrameworkCore;
using Microsoft.IdentityModel.Tokens;
using Microsoft.OpenApi.Models;

var builder = WebApplication.CreateBuilder(args);

builder.Services.AddControllers();
builder.Services.AddEndpointsApiExplorer();
builder.Services.AddHttpContextAccessor();

// ── Configuration ───────────────────────────────────────────────────────────
builder.Services.Configure<AuthOptions>(builder.Configuration.GetSection(AuthOptions.SectionName));
var auth = builder.Configuration.GetSection(AuthOptions.SectionName).Get<AuthOptions>() ?? new AuthOptions();

// Fail closed. Without this, forgetting to set Auth:Enabled in production would
// serve every request as the seeded dev user — a total authorization bypass
// that still looks like a working deployment.
if (!auth.Enabled && !builder.Environment.IsDevelopment())
{
    throw new InvalidOperationException(
        "Auth:Enabled is false outside Development. Configure Auth:Authority and " +
        "Auth:Audience for your identity provider (Auth0, Supabase, Entra, ...) " +
        "before deploying.");
}

builder.Services.AddSwaggerGen(options =>
{
    options.SwaggerDoc("v1", new OpenApiInfo
    {
        Title = "Folio — Enterprise AI Workspace API",
        Version = "v1",
        Description = "Chat, documents, agents and memory for the Folio workspace.",
    });

    var scheme = new OpenApiSecurityScheme
    {
        Name = "Authorization",
        Type = SecuritySchemeType.Http,
        Scheme = "bearer",
        BearerFormat = "JWT",
        In = ParameterLocation.Header,
        Description = "Paste the access token issued by your identity provider.",
        Reference = new OpenApiReference { Type = ReferenceType.SecurityScheme, Id = "Bearer" },
    };
    options.AddSecurityDefinition("Bearer", scheme);
    options.AddSecurityRequirement(new OpenApiSecurityRequirement { [scheme] = Array.Empty<string>() });
});

// ── Data ────────────────────────────────────────────────────────────────────
builder.Services.AddDbContext<WorkspaceDbContext>(options =>
    options.UseNpgsql(builder.Configuration.GetConnectionString("Postgres")
        ?? "Host=postgres;Database=workspace;Username=postgres;Password=postgres"));

builder.Services.AddScoped<ICurrentUser, CurrentUser>();
builder.Services.AddSingleton<ITokenProtector, TokenProtector>();

// ── AI service client ───────────────────────────────────────────────────────
builder.Services.AddHttpClient<AiServiceClient>(client =>
{
    var baseUrl = builder.Configuration["AiService:BaseUrl"] ?? "http://ai-service:8001";
    client.BaseAddress = new Uri(baseUrl);
    // Long-lived by design: /chat/stream and /documents/events are SSE streams
    // that stay open, and HttpClient.Timeout covers the whole response.
    client.Timeout = Timeout.InfiniteTimeSpan;

    // Service-to-service credential. The AI service holds the model keys and
    // every user's vectors; being reachable on the network must not be the
    // same thing as being callable.
    if (!string.IsNullOrWhiteSpace(auth.ServiceToken))
        client.DefaultRequestHeaders.Add("X-Service-Token", auth.ServiceToken);
});

// Applies db/migrations on boot so an existing Postgres volume picks up new
// columns without a manual step.
builder.Services.AddHostedService<SchemaMigrator>();

// ── Authentication ──────────────────────────────────────────────────────────
if (auth.Enabled)
{
    builder.Services
        .AddAuthentication(JwtBearerDefaults.AuthenticationScheme)
        .AddJwtBearer(options =>
        {
            options.Authority = auth.Authority;
            options.Audience = auth.Audience;
            options.RequireHttpsMetadata = !builder.Environment.IsDevelopment();

            options.TokenValidationParameters = new TokenValidationParameters
            {
                ValidateIssuer = true,
                ValidateAudience = !string.IsNullOrWhiteSpace(auth.Audience),
                ValidateLifetime = true,
                ValidateIssuerSigningKey = true,
                // Tokens are short-lived; a wide clock skew extends the window
                // in which a revoked token still works.
                ClockSkew = TimeSpan.FromSeconds(30),
                NameClaimType = auth.NameClaim,
            };

            // Supabase signs with a project secret rather than publishing JWKS.
            if (!string.IsNullOrWhiteSpace(auth.SigningKey))
            {
                options.TokenValidationParameters.IssuerSigningKey =
                    new SymmetricSecurityKey(Encoding.UTF8.GetBytes(auth.SigningKey));
            }

            // SSE through fetch() can carry an Authorization header, but a plain
            // EventSource cannot — accept a query token for that one route.
            options.Events = new JwtBearerEvents
            {
                OnMessageReceived = context =>
                {
                    if (string.IsNullOrEmpty(context.Token) &&
                        context.Request.Query.TryGetValue("access_token", out var token) &&
                        context.HttpContext.Request.Path.StartsWithSegments("/api/documents/events"))
                    {
                        context.Token = token;
                    }
                    return Task.CompletedTask;
                },
            };
        });
}
else
{
    builder.Services
        .AddAuthentication(DevAuthenticationHandler.SchemeName)
        .AddScheme<AuthenticationSchemeOptions, DevAuthenticationHandler>(
            DevAuthenticationHandler.SchemeName, _ => { });
}

builder.Services.AddAuthorization(options =>
{
    // Every endpoint requires an authenticated caller unless it opts out with
    // [AllowAnonymous]. Defaulting the other way means one forgotten attribute
    // silently exposes data.
    options.FallbackPolicy = new AuthorizationPolicyBuilder()
        .RequireAuthenticatedUser()
        .Build();
});

// ── CORS ────────────────────────────────────────────────────────────────────
builder.Services.AddCors(options =>
{
    options.AddDefaultPolicy(policy =>
        policy.WithOrigins(builder.Configuration["Frontend:Origin"] ?? "http://localhost:4200")
              .AllowAnyHeader()
              .AllowAnyMethod()
              // Custom response headers are invisible to cross-origin JS unless
              // explicitly exposed. Without this the browser drops X-Chat-Id and
              // every turn would start a brand-new chat.
              .WithExposedHeaders("X-Chat-Id"));
});

// Uploads are capped here as well as in the controller so an oversized body is
// rejected before it is buffered.
builder.Services.Configure<FormOptions>(o =>
{
    o.MultipartBodyLengthLimit = 50L * 1024 * 1024;
});

var app = builder.Build();

// ── Pipeline ────────────────────────────────────────────────────────────────
app.UseExceptionHandler(branch => branch.Run(async context =>
{
    // Never surface stack traces or connection strings to a client.
    context.Response.StatusCode = StatusCodes.Status500InternalServerError;
    context.Response.ContentType = "application/json";
    await context.Response.WriteAsync("{\"error\":\"An unexpected error occurred.\"}");
}));

app.Use(async (context, next) =>
{
    var headers = context.Response.Headers;
    headers["X-Content-Type-Options"] = "nosniff";
    headers["X-Frame-Options"] = "DENY";
    headers["Referrer-Policy"] = "no-referrer";
    headers["Cross-Origin-Resource-Policy"] = "same-site";
    // This API serves JSON and SSE only; nothing it returns should ever execute
    // or embed anything.
    headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'";
    await next();
});

if (app.Environment.IsDevelopment())
{
    app.UseSwagger();
    app.UseSwaggerUI();
}

app.UseCors();
app.UseAuthentication();
app.UseAuthorization();
app.MapControllers();

app.MapGet("/health", () => Results.Ok(new { status = "ok" })).AllowAnonymous();

app.Run();

/// <summary>Exposed so an integration-test host can reference this assembly.</summary>
public partial class Program;
