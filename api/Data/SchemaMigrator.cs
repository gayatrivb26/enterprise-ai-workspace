using Npgsql;

namespace EnterpriseAiWorkspace.Api.Data;

/// <summary>
/// Applies the idempotent SQL migrations in db/migrations at startup.
///
/// db/init.sql only ever runs on a *fresh* Postgres volume, so anyone with an
/// existing database would otherwise be missing every new column. Running the
/// additive migrations on boot keeps both cases on the same schema without
/// requiring a manual step or a destructive volume reset.
/// </summary>
public sealed class SchemaMigrator(
    IConfiguration configuration,
    IHostEnvironment environment,
    ILogger<SchemaMigrator> logger) : IHostedService
{
    public async Task StartAsync(CancellationToken cancellationToken)
    {
        var connectionString = configuration.GetConnectionString("Postgres");
        if (string.IsNullOrWhiteSpace(connectionString))
        {
            logger.LogWarning("No Postgres connection string configured; skipping migrations.");
            return;
        }

        var directory = ResolveMigrationsDirectory();
        if (directory is null)
        {
            logger.LogWarning("Could not locate db/migrations; skipping migrations.");
            return;
        }

        var files = Directory.GetFiles(directory, "*.sql").OrderBy(f => f, StringComparer.Ordinal).ToList();
        if (files.Count == 0) return;

        // The API often starts before Postgres finishes accepting connections.
        await using var connection = await OpenWithRetryAsync(connectionString, cancellationToken);
        if (connection is null)
        {
            logger.LogError("Could not reach Postgres to apply migrations.");
            return;
        }

        foreach (var file in files)
        {
            var sql = await File.ReadAllTextAsync(file, cancellationToken);
            if (string.IsNullOrWhiteSpace(sql)) continue;

            try
            {
                await using var command = new NpgsqlCommand(sql, connection);
                command.CommandTimeout = 120;
                await command.ExecuteNonQueryAsync(cancellationToken);
                logger.LogInformation("Applied migration {Migration}.", Path.GetFileName(file));
            }
            catch (Exception ex)
            {
                // A failed migration must not take the API down — it would
                // turn a schema drift into a total outage.
                logger.LogError(ex, "Migration {Migration} failed.", Path.GetFileName(file));
            }
        }
    }

    public Task StopAsync(CancellationToken cancellationToken) => Task.CompletedTask;

    private async Task<NpgsqlConnection?> OpenWithRetryAsync(string connectionString, CancellationToken ct)
    {
        for (var attempt = 1; attempt <= 10; attempt++)
        {
            try
            {
                var connection = new NpgsqlConnection(connectionString);
                await connection.OpenAsync(ct);
                return connection;
            }
            catch (Exception ex) when (attempt < 10 && !ct.IsCancellationRequested)
            {
                logger.LogDebug(ex, "Postgres not ready (attempt {Attempt}); retrying.", attempt);
                await Task.Delay(TimeSpan.FromSeconds(Math.Min(attempt, 5)), ct);
            }
            catch
            {
                return null;
            }
        }
        return null;
    }

    /// <summary>
    /// Walks up from the content root looking for db/migrations, so this works
    /// from bin/Debug during `dotnet run` and from /src inside the container.
    /// </summary>
    private string? ResolveMigrationsDirectory()
    {
        var configured = configuration["Database:MigrationsPath"];
        if (!string.IsNullOrWhiteSpace(configured) && Directory.Exists(configured))
            return configured;

        var current = new DirectoryInfo(environment.ContentRootPath);
        for (var depth = 0; current is not null && depth < 6; depth++, current = current.Parent)
        {
            var candidate = Path.Combine(current.FullName, "db", "migrations");
            if (Directory.Exists(candidate)) return candidate;
        }
        return null;
    }
}
