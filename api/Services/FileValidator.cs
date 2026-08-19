namespace EnterpriseAiWorkspace.Api.Services;

/// <summary>
/// Validates uploads by what they actually contain, not by what they claim.
///
/// Extension and Content-Type are both attacker-controlled, so on their own
/// they gate nothing. Checking the leading bytes means a payload renamed to
/// .pdf is rejected before it ever reaches the parsers — which are native
/// libraries (PyMuPDF, python-docx, Pillow) where malformed input is exactly
/// where memory-safety bugs live.
/// </summary>
public static class FileValidator
{
    public sealed record Result(bool Ok, string? Error, string? Kind);

    private const long MaxBytes = 50L * 1024 * 1024;

    /// <summary>extension -> logical kind.</summary>
    private static readonly Dictionary<string, string> Allowed = new(StringComparer.OrdinalIgnoreCase)
    {
        [".pdf"] = "pdf",
        [".md"] = "markdown",
        [".markdown"] = "markdown",
        [".txt"] = "text",
        [".text"] = "text",
        [".log"] = "text",
        [".csv"] = "text",
        [".docx"] = "word",
        [".xlsx"] = "excel",
        [".pptx"] = "powerpoint",
        [".png"] = "image",
        [".jpg"] = "image",
        [".jpeg"] = "image",
        [".webp"] = "image",
    };

    public static IReadOnlyCollection<string> AllowedExtensions => Allowed.Keys;

    public static async Task<Result> ValidateAsync(IFormFile? file, CancellationToken ct)
    {
        if (file is null || file.Length == 0)
            return new Result(false, "The file is empty.", null);

        if (file.Length > MaxBytes)
            return new Result(false, "Files must be 50 MB or smaller.", null);

        var name = Path.GetFileName(file.FileName ?? string.Empty);
        if (string.IsNullOrWhiteSpace(name))
            return new Result(false, "The file has no name.", null);

        // Reject separators outright rather than trying to sanitise them: the
        // filename is only ever a label here, never a path.
        if (name.Contains('/') || name.Contains('\\') || name.Contains("..", StringComparison.Ordinal))
            return new Result(false, "The file name is not valid.", null);

        var extension = Path.GetExtension(name);
        if (!Allowed.TryGetValue(extension, out var kind))
            return new Result(false, "Unsupported file type.", null);

        await using var stream = file.OpenReadStream();
        var header = new byte[16];
        var read = await stream.ReadAsync(header.AsMemory(0, header.Length), ct);

        if (!HeaderMatches(kind, header.AsSpan(0, read)))
            return new Result(false, $"The file contents do not look like a valid {kind} file.", null);

        return new Result(true, null, kind);
    }

    private static bool HeaderMatches(string kind, ReadOnlySpan<byte> header) => kind switch
    {
        // %PDF
        "pdf" => header.Length >= 4 && header[0] == 0x25 && header[1] == 0x50
                 && header[2] == 0x44 && header[3] == 0x46,

        // OOXML files are ZIP containers: "PK\x03\x04" (or an empty/spanned archive).
        "word" or "excel" or "powerpoint" =>
            header.Length >= 4 && header[0] == 0x50 && header[1] == 0x4B
            && (header[2] == 0x03 || header[2] == 0x05 || header[2] == 0x07),

        "image" => IsPng(header) || IsJpeg(header) || IsWebp(header),

        // Text has no signature. Reject only an embedded NUL, which no genuine
        // UTF-8 text file contains and which usually means a binary in disguise.
        "text" or "markdown" => header.IndexOf((byte)0) < 0,

        _ => false,
    };

    private static bool IsPng(ReadOnlySpan<byte> h) =>
        h.Length >= 8 && h[0] == 0x89 && h[1] == 0x50 && h[2] == 0x4E && h[3] == 0x47
        && h[4] == 0x0D && h[5] == 0x0A && h[6] == 0x1A && h[7] == 0x0A;

    private static bool IsJpeg(ReadOnlySpan<byte> h) =>
        h.Length >= 3 && h[0] == 0xFF && h[1] == 0xD8 && h[2] == 0xFF;

    private static bool IsWebp(ReadOnlySpan<byte> h) =>
        h.Length >= 12 && h[0] == 0x52 && h[1] == 0x49 && h[2] == 0x46 && h[3] == 0x46
        && h[8] == 0x57 && h[9] == 0x45 && h[10] == 0x42 && h[11] == 0x50;
}
