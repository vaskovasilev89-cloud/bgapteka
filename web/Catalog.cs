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

/// <summary>Всички продукти в паметта (~100 000 записа, търсене за няколко милисекунди).</summary>
public sealed partial class Catalog
{
    public static readonly JsonSerializerOptions Json = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        DefaultIgnoreCondition = JsonIgnoreCondition.Never,
        NumberHandling = JsonNumberHandling.AllowReadingFromString,
    };

    [GeneratedRegex("^[a-z0-9_-]{1,32}$")] public static partial Regex SiteKey();

    readonly string _dir;
    readonly ILogger _log;
    volatile Dictionary<string, SiteData> _sites = new();
    readonly object _write = new();

    public Catalog(string dataDir, ILogger<Catalog> log)
    {
        _dir = dataDir;
        _log = log;
        Directory.CreateDirectory(_dir);
        var dict = new Dictionary<string, SiteData>();
        foreach (var f in Directory.GetFiles(_dir, "*.json.gz"))
        {
            try
            {
                using var fs = File.OpenRead(f);
                using var gz = new GZipStream(fs, CompressionMode.Decompress);
                var sd = JsonSerializer.Deserialize<SiteData>(gz, Json);
                if (sd != null && SiteKey().IsMatch(sd.Site)) { Prepare(sd); dict[sd.Site] = sd; }
            }
            catch (Exception e) { _log.LogError(e, "Не може да се зареди {File}", f); }
        }
        _sites = dict;
        _log.LogInformation("Заредени {Count} продукта от {Sites} аптеки", dict.Values.Sum(s => s.Products.Count), dict.Count);
    }

    static void Prepare(SiteData sd)
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

    public IReadOnlyCollection<SiteData> Sites => _sites.Values;

    public async Task<int> ImportAsync(string site, Stream body, CancellationToken ct)
    {
        var sd = await JsonSerializer.DeserializeAsync<SiteData>(body, Json, ct)
                 ?? throw new InvalidDataException("празни данни");
        sd.Site = site;
        Prepare(sd);
        var path = Path.Combine(_dir, site + ".json.gz");
        var tmp = path + ".tmp";
        lock (_write)
        {
            using (var fs = File.Create(tmp))
            using (var gz = new GZipStream(fs, CompressionLevel.Optimal))
                JsonSerializer.Serialize(gz, sd, Json);
            File.Move(tmp, path, overwrite: true);
            var copy = new Dictionary<string, SiteData>(_sites) { [site] = sd };
            _sites = copy;
        }
        return sd.Products.Count;
    }

    public List<Offer> Search(string q, bool inStock, HashSet<string>? sites, int limit = 400)
    {
        var tokens = TextNorm.Norm(q).Split(' ', StringSplitOptions.RemoveEmptyEntries);
        if (tokens.Length == 0) return new();
        var padded = tokens.Select(t => " " + t).ToArray();
        var snapshot = _sites;

        if (tokens.Length == 1 && tokens[0].Length is 8 or 13 && tokens[0].All(char.IsAsciiDigit))
        {
            var byEan = snapshot.Values.SelectMany(s => s.Products).Where(p => p.Ean == tokens[0]).ToList();
            if (byEan.Count > 0) return byEan;
        }

        var hits = new List<(Offer o, double score)>();
        foreach (var sd in snapshot.Values)
        {
            if (sites is { Count: > 0 } && !sites.Contains(sd.Site)) continue;
            foreach (var p in sd.Products)
            {
                if (inStock && p.InStock == 0) continue;
                double score = 0;
                bool all = true;
                for (int i = 0; i < padded.Length; i++)
                {
                    int idx = p.Padded.IndexOf(padded[i], StringComparison.Ordinal);
                    if (idx < 0) { all = false; break; }
                    int end = idx + padded[i].Length;
                    score += (end == p.Padded.Length || p.Padded[end] == ' ') ? 2 : 1;   // цяла дума > начало на дума
                    if (i == 0 && idx == 0) score += 2;                                  // името започва с първата дума
                }
                if (!all) continue;
                score -= p.Norm.Length / 200.0;
                hits.Add((p, score));
            }
        }
        return hits.OrderByDescending(h => h.score).Take(limit).Select(h => h.o).ToList();
    }
}
