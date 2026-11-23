using Microsoft.Extensions.Logging;
using System.IO.Compression;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Text.RegularExpressions;

namespace BgApteka;

public sealed class Offer
{
    public string Url { get; set; } = "";
    public string Name { get; set; } = "";
    public string Norm { get; set; } = "";
    public double Price { get; set; }
    public double? OldPrice { get; set; }
    public int? InStock { get; set; }
    public string? Ean { get; set; }
    public string? Brand { get; set; }
    public string? Image { get; set; }
    public double UpdatedAt { get; set; }

    [JsonIgnore] public string Site { get; set; } = "";
    [JsonIgnore] internal string Padded = "";   // " " + Norm, за бързо търсене по начало на дума
}

public sealed class SiteData
{
    public string Site { get; set; } = "";
    public string Title { get; set; } = "";
    public string Base { get; set; } = "";
    public double Generated { get; set; }
    public List<Offer> Products { get; set; } = new();
}

/// <summary>Обобщение за една аптека (за /api/status).</summary>
public sealed record SiteInfo(string Site, string Title, string Base, double Generated, int Products);

/// <summary>Хранилище на продуктите: MSSQL (<see cref="SqlCatalog"/>) или файлове (<see cref="FileCatalog"/>).</summary>
public interface ICatalog
{
    string Kind { get; }
    Task<IReadOnlyList<SiteInfo>> GetSitesAsync(CancellationToken ct = default);
    Task<int> ImportAsync(string site, Stream body, CancellationToken ct);
    Task<List<Offer>> SearchAsync(string q, bool inStock, HashSet<string>? sites, int limit = 400, CancellationToken ct = default);
}

/// <summary>Общи неща за двете хранилища: JSON формат, валидиране и класиране на резултатите.</summary>
public static partial class Catalog
{
    public static readonly JsonSerializerOptions Json = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        DefaultIgnoreCondition = JsonIgnoreCondition.Never,
        NumberHandling = JsonNumberHandling.AllowReadingFromString,
    };

    [GeneratedRegex("^[a-z0-9_-]{1,32}$")] public static partial Regex SiteKey();

    public static void Prepare(SiteData sd)
    {
        sd.Products.RemoveAll(p => string.IsNullOrEmpty(p.Url) || string.IsNullOrEmpty(p.Name) || p.Price <= 0);
        foreach (var p in sd.Products)
        {
            p.Site = sd.Site;
            if (string.IsNullOrEmpty(p.Norm)) p.Norm = TextNorm.Norm(p.Name + " " + p.Brand);
            p.Padded = " " + p.Norm;
            if (!TextNorm.ValidEan(p.Ean)) p.Ean = null;
        }
    }

    /// <summary>Думите от заявката (нормализирани) или празен масив.</summary>
    public static string[] Tokens(string q) => TextNorm.Norm(q).Split(' ', StringSplitOptions.RemoveEmptyEntries);

    /// <summary>Ако заявката е баркод (8 или 13 цифри) – връща го.</summary>
    public static string? AsEan(string[] tokens) =>
        tokens.Length == 1 && tokens[0].Length is 8 or 13 && tokens[0].All(char.IsAsciiDigit) ? tokens[0] : null;

    /// <summary>
    /// Точка за съвпадение или null, ако някоя дума липсва.
    /// Цяла дума > начало на дума; бонус, ако името започва с първата дума; по-кратките имена са по-напред.
    /// </summary>
    public static double? Score(string text, string[] padded)
    {
        var p = " " + text;
        double score = 0;
        for (int i = 0; i < padded.Length; i++)
        {
            int idx = p.IndexOf(padded[i], StringComparison.Ordinal);
            if (idx < 0) return null;
            int end = idx + padded[i].Length;
            score += (end == p.Length || p[end] == ' ') ? 2 : 1;
            if (i == 0 && idx == 0) score += 2;
        }
        return score - text.Length / 200.0;
    }

    /// <summary>Класира произволни записи по нормализирания им текст (продукти от аптеки или артикули от брошури).</summary>
    public static List<T> Rank<T>(IEnumerable<T> candidates, Func<T, string> norm, string[] tokens, int limit)
    {
        var padded = tokens.Select(t => " " + t).ToArray();
        var hits = new List<(T o, double score)>();
        foreach (var p in candidates)
            if (Score(norm(p), padded) is double s) hits.Add((p, s));
        return hits.OrderByDescending(h => h.score).Take(limit).Select(h => h.o).ToList();
    }

    public static List<Offer> Rank(IEnumerable<Offer> candidates, string[] tokens, int limit) =>
        Rank(candidates, o => o.Norm, tokens, limit);
}

/// <summary>Всички продукти в паметта, записани в App_Data/sites/*.json.gz (без база данни).</summary>
public sealed class FileCatalog : ICatalog
{
    readonly string _dir;
    readonly ILogger _log;
    volatile Dictionary<string, SiteData> _sites = new();
    readonly object _write = new();

    public string Kind => "files";

    public FileCatalog(string dataDir, ILogger<FileCatalog> log)
    {
        _dir = dataDir;
        _log = log;
        Directory.CreateDirectory(_dir);
        var dict = new Dictionary<string, SiteData>();
        foreach (var sd in LoadAll(_dir, _log)) dict[sd.Site] = sd;
        _sites = dict;
        _log.LogInformation("Заредени {Count} продукта от {Sites} аптеки", dict.Values.Sum(s => s.Products.Count), dict.Count);
    }

    /// <summary>Чете всички *.json.gz от папката (ползва се и за пренасяне към MSSQL).</summary>
    public static IEnumerable<SiteData> LoadAll(string dir, ILogger log)
    {
        if (!Directory.Exists(dir)) yield break;
        foreach (var f in Directory.GetFiles(dir, "*.json.gz"))
        {
            SiteData? sd = null;
            try
            {
                using var fs = File.OpenRead(f);
                using var gz = new GZipStream(fs, CompressionMode.Decompress);
                sd = JsonSerializer.Deserialize<SiteData>(gz, Catalog.Json);
            }
            catch (Exception e) { log.LogError(e, "Не може да се зареди {File}", f); }
            if (sd != null && Catalog.SiteKey().IsMatch(sd.Site)) { Catalog.Prepare(sd); yield return sd; }
        }
    }

    public Task<IReadOnlyList<SiteInfo>> GetSitesAsync(CancellationToken ct = default) =>
        Task.FromResult<IReadOnlyList<SiteInfo>>(_sites.Values
            .Select(s => new SiteInfo(s.Site, s.Title, s.Base, s.Generated, s.Products.Count)).ToList());

    public async Task<int> ImportAsync(string site, Stream body, CancellationToken ct)
    {
        var sd = await JsonSerializer.DeserializeAsync<SiteData>(body, Catalog.Json, ct)
                 ?? throw new InvalidDataException("празни данни");
        sd.Site = site;
        Catalog.Prepare(sd);
        var path = Path.Combine(_dir, site + ".json.gz");
        var tmp = path + ".tmp";
        lock (_write)
        {
            using (var fs = File.Create(tmp))
            using (var gz = new GZipStream(fs, CompressionLevel.Optimal))
                JsonSerializer.Serialize(gz, sd, Catalog.Json);
            File.Move(tmp, path, overwrite: true);
            var copy = new Dictionary<string, SiteData>(_sites) { [site] = sd };
            _sites = copy;
        }
        return sd.Products.Count;
    }

    public Task<List<Offer>> SearchAsync(string q, bool inStock, HashSet<string>? sites, int limit = 400, CancellationToken ct = default)
    {
        var tokens = Catalog.Tokens(q);
        if (tokens.Length == 0) return Task.FromResult(new List<Offer>());
        var snapshot = _sites;

        if (Catalog.AsEan(tokens) is string ean)
        {
            var byEan = snapshot.Values.SelectMany(s => s.Products).Where(p => p.Ean == ean).ToList();
            if (byEan.Count > 0) return Task.FromResult(byEan);
        }

        var candidates = snapshot.Values
            .Where(sd => sites is not { Count: > 0 } || sites.Contains(sd.Site))
            .SelectMany(sd => sd.Products)
            .Where(p => !(inStock && p.InStock == 0));
        return Task.FromResult(Catalog.Rank(candidates, tokens, limit));
    }
}
