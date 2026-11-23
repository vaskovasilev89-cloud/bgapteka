using System.Security.Cryptography;
using System.Text;
using BgApteka;
using Microsoft.AspNetCore.ResponseCompression;

var builder = WebApplication.CreateBuilder(args);

const long MaxImport = 400L * 1024 * 1024;   // разархивиран JSON на една аптека
builder.WebHost.ConfigureKestrel(o => o.Limits.MaxRequestBodySize = MaxImport);
builder.Services.Configure<IISServerOptions>(o => o.MaxRequestBodySize = MaxImport);
builder.Services.AddRequestDecompression();
builder.Services.AddResponseCompression(o => { o.EnableForHttps = true; o.Providers.Add<GzipCompressionProvider>(); });
builder.Services.ConfigureHttpJsonOptions(o =>
{
    o.SerializerOptions.PropertyNamingPolicy = Catalog.Json.PropertyNamingPolicy;
});

var dataDir = Environment.GetEnvironmentVariable("BGAPTEKA_DATA")
              ?? Path.Combine(builder.Environment.ContentRootPath, "App_Data");
builder.Services.AddSingleton(sp => new Catalog(Path.Combine(dataDir, "sites"), sp.GetRequiredService<ILogger<Catalog>>()));

var app = builder.Build();
app.UseResponseCompression();
app.UseRequestDecompression();
app.UseDefaultFiles();
app.UseStaticFiles();

var catalog = app.Services.GetRequiredService<Catalog>();
var note = app.Configuration["BgApteka:Note"] ?? "";

string? ApiKey()
{
    var k = Environment.GetEnvironmentVariable("BGAPTEKA_APIKEY");
    var f = Path.Combine(dataDir, "apikey.txt");
    if (string.IsNullOrWhiteSpace(k) && File.Exists(f)) k = File.ReadAllText(f);
    k = k?.Trim();
    return string.IsNullOrEmpty(k) || k.Length < 16 ? null : k;
}

app.MapGet("/api/search", (string? q, bool? in_stock, string? sites) =>
{
    q ??= "";
    if (q.Length > 200) q = q[..200];
    var filter = string.IsNullOrWhiteSpace(sites) ? null : sites.Split(',').ToHashSet();
    var titles = catalog.Sites.ToDictionary(s => s.Site, s => s.Title);
    var rows = catalog.Search(q, in_stock == true, filter);
    var groups = Grouping.Build(rows).Select(g =>
    {
        var avail = g.Offers.Where(o => o.InStock != 0).Select(o => o.Price).ToList();
        if (avail.Count == 0) avail = g.Offers.Select(o => o.Price).ToList();
        return new
        {
            name = g.Name,
            image = g.Image,
            eans = g.Eans.Order().ToList(),
            min_price = avail.Min(),
            max_price = avail.Max(),
            sites = g.Offers.Select(o => o.Site).Distinct().Count(),
            offers = g.Offers.Select(o => new
            {
                site = o.Site, name = o.Name, price = o.Price, old_price = o.OldPrice, currency = "EUR",
                url = o.Url, in_stock = o.InStock, ean = o.Ean, brand = o.Brand, image = o.Image,
                updated_at = o.UpdatedAt, site_title = titles.GetValueOrDefault(o.Site, o.Site),
            }),
        };
    }).ToList();
    return Results.Json(new { query = q, offers = rows.Count, groups });
});

app.MapGet("/api/status", () => Results.Json(new
{
    running = false,
    canRefresh = false,
    remote = "",
    note,
    sites = catalog.Sites.OrderBy(s => s.Title).Select(s => new
    {
        site = s.Site, title = s.Title, @base = s.Base, products = s.Products.Count,
        last_update = s.Generated, last_run = (object?)null, live = (object?)null,
    }),
}, new System.Text.Json.JsonSerializerOptions { PropertyNamingPolicy = null }));

app.MapPost("/api/import/{site}", async (string site, HttpRequest req, CancellationToken ct) =>
{
    var key = ApiKey();
    if (key == null) return Results.Problem("На сървъра не е зададен API ключ (App_Data/apikey.txt)", statusCode: 503);
    var given = req.Headers["X-Api-Key"].ToString();
    if (!CryptographicOperations.FixedTimeEquals(Encoding.UTF8.GetBytes(given), Encoding.UTF8.GetBytes(key)))
        return Results.Unauthorized();
    if (!Catalog.SiteKey().IsMatch(site)) return Results.BadRequest("невалиден ключ на аптека");
    try
    {
        var n = await catalog.ImportAsync(site, req.Body, ct);
        return Results.Ok(new { site, products = n });
    }
    catch (Exception e) when (e is System.Text.Json.JsonException or InvalidDataException)
    {
        return Results.BadRequest("невалиден JSON: " + e.Message);
    }
}).WithMetadata(new Microsoft.AspNetCore.Mvc.RequestSizeLimitAttribute(MaxImport));

app.Run();

public partial class Program { }
