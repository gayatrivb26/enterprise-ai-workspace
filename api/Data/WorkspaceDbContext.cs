using EnterpriseAiWorkspace.Api.Models;
using Microsoft.EntityFrameworkCore;

namespace EnterpriseAiWorkspace.Api.Data;

public class WorkspaceDbContext(DbContextOptions<WorkspaceDbContext> options) : DbContext(options)
{
    public DbSet<User> Users => Set<User>();
    public DbSet<Chat> Chats => Set<Chat>();
    public DbSet<Message> Messages => Set<Message>();
    public DbSet<Document> Documents => Set<Document>();
    public DbSet<Collection> Collections => Set<Collection>();
    public DbSet<Integration> Integrations => Set<Integration>();

    protected override void OnModelCreating(ModelBuilder modelBuilder)
    {
        // Tables are created by db/init.sql and evolved by db/migrations
        // (applied at startup by SchemaMigrator). These mappings just tell
        // EF Core the snake_case column names.
        modelBuilder.Entity<User>(e =>
        {
            e.ToTable("users");
            e.Property(p => p.Id).HasColumnName("id");
            e.Property(p => p.Email).HasColumnName("email");
            e.Property(p => p.Name).HasColumnName("name");
            e.Property(p => p.AuthSubject).HasColumnName("auth_sub");
            e.Property(p => p.CreatedAt).HasColumnName("created_at");
            e.HasIndex(p => p.AuthSubject).IsUnique();
        });

        modelBuilder.Entity<Chat>(e =>
        {
            e.ToTable("chats");
            e.Property(p => p.Id).HasColumnName("id");
            e.Property(p => p.UserId).HasColumnName("user_id");
            e.Property(p => p.Title).HasColumnName("title");
            e.Property(p => p.CreatedAt).HasColumnName("created_at");
            e.Property(p => p.UpdatedAt).HasColumnName("updated_at");
            e.Property(p => p.Archived).HasColumnName("archived");
            e.Property(p => p.DocumentIds).HasColumnName("document_ids");
        });

        modelBuilder.Entity<Message>(e =>
        {
            e.ToTable("messages");
            e.Property(p => p.Id).HasColumnName("id");
            e.Property(p => p.ChatId).HasColumnName("chat_id");
            e.Property(p => p.Role).HasColumnName("role");
            e.Property(p => p.Content).HasColumnName("content");
            e.Property(p => p.CreatedAt).HasColumnName("created_at");
            e.Property(p => p.Sources).HasColumnName("sources").HasColumnType("jsonb");
            e.Property(p => p.TokensIn).HasColumnName("tokens_in");
            e.Property(p => p.TokensOut).HasColumnName("tokens_out");
            e.Property(p => p.Cached).HasColumnName("cached");
            e.Property(p => p.LatencyMs).HasColumnName("latency_ms");
        });

        modelBuilder.Entity<Integration>(e =>
        {
            e.ToTable("integrations");
            e.Property(p => p.Id).HasColumnName("id");
            e.Property(p => p.UserId).HasColumnName("user_id");
            e.Property(p => p.Provider).HasColumnName("provider");
            e.Property(p => p.AccessToken).HasColumnName("access_token");
            e.Property(p => p.AccountLogin).HasColumnName("account_login");
            e.Property(p => p.AccountName).HasColumnName("account_name");
            e.Property(p => p.AvatarUrl).HasColumnName("avatar_url");
            e.Property(p => p.Scopes).HasColumnName("scopes");
            e.Property(p => p.SelectedRepo).HasColumnName("selected_repo");
            e.Property(p => p.CreatedAt).HasColumnName("created_at");
            e.Property(p => p.UpdatedAt).HasColumnName("updated_at");
            e.HasIndex(p => new { p.UserId, p.Provider }).IsUnique();
        });

        modelBuilder.Entity<Collection>(e =>
        {
            e.ToTable("collections");
            e.Property(p => p.Id).HasColumnName("id");
            e.Property(p => p.UserId).HasColumnName("user_id");
            e.Property(p => p.Name).HasColumnName("name");
            e.Property(p => p.Color).HasColumnName("color");
            e.Property(p => p.CreatedAt).HasColumnName("created_at");
        });

        modelBuilder.Entity<Document>(e =>
        {
            e.ToTable("documents");
            e.Property(p => p.Id).HasColumnName("id");
            e.Property(p => p.UserId).HasColumnName("user_id");
            e.Property(p => p.Filename).HasColumnName("filename");
            e.Property(p => p.Type).HasColumnName("type");
            e.Property(p => p.Status).HasColumnName("status");
            e.Property(p => p.Progress).HasColumnName("progress");
            e.Property(p => p.Error).HasColumnName("error");
            e.Property(p => p.SizeBytes).HasColumnName("size_bytes");
            e.Property(p => p.PageCount).HasColumnName("page_count");
            e.Property(p => p.ChunkCount).HasColumnName("chunk_count");
            e.Property(p => p.TokenCount).HasColumnName("token_count");
            e.Property(p => p.CollectionId).HasColumnName("collection_id");
            e.Property(p => p.UploadedAt).HasColumnName("uploaded_at");
            e.Property(p => p.UpdatedAt).HasColumnName("updated_at");
        });
    }
}
