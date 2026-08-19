using System.Security.Cryptography;
using System.Text;
using EnterpriseAiWorkspace.Api.Auth;
using Microsoft.Extensions.Options;

namespace EnterpriseAiWorkspace.Api.Services;

/// <summary>
/// Encrypts third-party access tokens before they touch the database.
///
/// A stored GitHub token is a live credential to somebody's source code, so
/// plaintext at rest would turn any database dump, stray backup or read-only
/// SQL leak into a source-code breach. AES-256-GCM is used rather than plain
/// CBC/CTR because it authenticates the ciphertext: tampering is detected on
/// decrypt instead of silently yielding a corrupted token.
///
/// Wire format is base64( nonce[12] || ciphertext || tag[16] ) — self-contained,
/// so no separate nonce column is needed and rotation only has to consider the
/// key.
/// </summary>
public interface ITokenProtector
{
    string Protect(string plaintext);
    bool TryUnprotect(string payload, out string plaintext);
}

public sealed class TokenProtector : ITokenProtector
{
    private const int NonceSize = 12;   // GCM standard
    private const int TagSize = 16;

    private readonly byte[] _key;

    public TokenProtector(IOptions<AuthOptions> options, IHostEnvironment environment)
    {
        var configured = options.Value.TokenEncryptionKey;

        if (!string.IsNullOrWhiteSpace(configured))
        {
            // Hashed to exactly 32 bytes so any passphrase length works while
            // the key material stays full-entropy for AES-256.
            _key = SHA256.HashData(Encoding.UTF8.GetBytes(configured));
            return;
        }

        if (!environment.IsDevelopment())
        {
            // Refusing to start beats silently protecting tokens with a key
            // that is identical on every deployment.
            throw new InvalidOperationException(
                "Auth:TokenEncryptionKey must be set outside Development — " +
                "third-party tokens cannot be stored safely without it.");
        }

        _key = SHA256.HashData(Encoding.UTF8.GetBytes("folio-development-key"));
    }

    public string Protect(string plaintext)
    {
        ArgumentException.ThrowIfNullOrEmpty(plaintext);

        var nonce = RandomNumberGenerator.GetBytes(NonceSize);
        var source = Encoding.UTF8.GetBytes(plaintext);
        var ciphertext = new byte[source.Length];
        var tag = new byte[TagSize];

        using var aes = new AesGcm(_key, TagSize);
        aes.Encrypt(nonce, source, ciphertext, tag);

        var payload = new byte[NonceSize + ciphertext.Length + TagSize];
        nonce.CopyTo(payload, 0);
        ciphertext.CopyTo(payload, NonceSize);
        tag.CopyTo(payload, NonceSize + ciphertext.Length);

        return Convert.ToBase64String(payload);
    }

    public bool TryUnprotect(string payload, out string plaintext)
    {
        plaintext = string.Empty;
        if (string.IsNullOrWhiteSpace(payload)) return false;

        byte[] bytes;
        try
        {
            bytes = Convert.FromBase64String(payload);
        }
        catch (FormatException)
        {
            return false;
        }

        if (bytes.Length <= NonceSize + TagSize) return false;

        var nonce = bytes.AsSpan(0, NonceSize);
        var tag = bytes.AsSpan(bytes.Length - TagSize, TagSize);
        var ciphertext = bytes.AsSpan(NonceSize, bytes.Length - NonceSize - TagSize);
        var result = new byte[ciphertext.Length];

        try
        {
            using var aes = new AesGcm(_key, TagSize);
            aes.Decrypt(nonce, ciphertext, tag, result);
        }
        catch (CryptographicException)
        {
            // Wrong key or tampered payload. Returning false lets the caller
            // treat it as "not connected" rather than crashing a request.
            return false;
        }

        plaintext = Encoding.UTF8.GetString(result);
        return true;
    }
}
