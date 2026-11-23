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
// MSSQL, ако има connection string (env BGAPTEKA_SQL, App_Data/sql.txt или ConnectionStrings:BgApteka);
// иначе – файлове в App_Data/sites.
string? SqlConnectionString()
{
    var cs = Environment.GetEnvironmentVariable("BGAPTEKA_SQL");
    var f = Path.Combine(dataDir, "sql.txt");
    if (string.IsNullOrWhiteSpace(cs) && File.Exists(f)) cs = File.ReadAllText(f);
    if (string.IsNullOrWhiteSpace(cs)) cs = builder.Configuration.GetConnectionString("BgApteka");
    return string.IsNullOrWhiteSpace(cs) ? null : cs.Trim();
}
var sitesDir = Path.Combine(dataDir, "sites");
var sqlCs = SqlConnectionString();
if (sqlCs != null)
    builder.Services.AddSingleton<ICatalog>(sp => new SqlCatalog(sqlCs, sitesDir, sp.GetRequiredService<ILogger<SqlCatalog>>()));
else
    builder.Services.AddSingleton<ICatalog>(sp => new FileCatalog(sitesDir, sp.GetRequiredService<ILogger<FileCatalog>>()));
// брошурите на супермаркетите се пазят само в MSSQL
if (sqlCs != null)
    builder.Services.AddSingleton(sp => new FlyerStore(sqlCs, sp.GetRequiredService<ILogger<FlyerStore>>()));

var app = builder.Build();
app.UseResponseCompression();
app.UseRequestDecompression();
app.UseDefaultFiles();
app.UseStaticFiles();

var catalog = app.Services.GetRequiredService<ICatalog>();
var note = app.Configuration["BgApteka:Note"] ?? "";

string? ApiKey()
{
    var k = Environment.GetEnvironmentVariable("BGAPTEKA_APIKEY");
    var f = Path.Combine(dataDir, "apikey.txt");
    if (string.IsNullOrWhiteSpace(k) && File.Exists(f)) k = File.ReadAllText(f);
    k = k?.Trim();
    return string.IsNullOrEmpty(k) || k.Length < 16 ? null : k;
}

app.MapGet("/api/search", async (string? q, bool? in_stock, string? sites, CancellationToken ct) =>
{
    q ??= "";
    if (q.Length > 200) q = q[..200];
    var filter = string.IsNullOrWhiteSpace(sites) ? null : sites.Split(',').ToHashSet();
    var titles = (await catalog.GetSitesAsync(ct)).ToDictionary(s => s.Site, s => s.Title);
    var rows = await catalog.SearchAsync(q, in_stock == true, filter, ct: ct);
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

app.MapGet("/api/status", async (CancellationToken ct) => Results.Json(new
{
    running = false,
    canRefresh = false,
    remote = "",
    note,
    storage = catalog.Kind,
    sites = (await catalog.GetSitesAsync(ct)).OrderBy(s => s.Title).Select(s => new
    {
        site = s.Site, title = s.Title, @base = s.Base, products = s.Products,
        last_update = s.Generated, last_run = (object?)null, live = (object?)null,
    }),
}, new System.Text.Json.JsonSerializerOptions { PropertyNamingPolicy = null }));

// null = ключът е верен; иначе – отговорът за грешка
IResult? CheckApiKey(HttpRequest req)
{
    var key = ApiKey();
    if (key == null) return Results.Problem("На сървъра не е зададен API ключ (App_Data/apikey.txt)", statusCode: 503);
    var given = req.Headers["X-Api-Key"].ToString();
    return CryptographicOperations.FixedTimeEquals(Encoding.UTF8.GetBytes(given), Encoding.UTF8.GetBytes(key))
        ? null : Results.Unauthorized();
}

app.MapPost("/api/import/{site}", async (string site, HttpRequest req, CancellationToken ct) =>
{
    if (CheckApiKey(req) is IResult denied) return denied;
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

// ---------- брошури на супермаркети ----------
var flyers = app.Services.GetService<FlyerStore>();
IResult NoFlyers() => Results.Problem("Брошурите изискват MSSQL база (App_Data/sql.txt)", statusCode: 503);

app.MapGet("/api/flyers", async (CancellationToken ct) =>
    flyers == null ? NoFlyers() : Results.Json(new { today = FlyerStore.Today(), flyers = await flyers.CurrentAsync(ct) }));

app.MapGet("/api/flyers/search", async (string? q, string? stores, CancellationToken ct) =>
{
    if (flyers == null) return NoFlyers();
    q ??= "";
    if (q.Length > 200) q = q[..200];
    var filter = string.IsNullOrWhiteSpace(stores) ? null : stores.Split(',').ToHashSet();
    var items = await flyers.SearchAsync(q, filter, 300, ct);
    return Results.Json(new { query = q, count = items.Count, items });
});

app.MapPost("/api/flyers/import", async (HttpRequest req, CancellationToken ct) =>
{
    if (CheckApiKey(req) is IResult denied) return denied;
    if (flyers == null) return NoFlyers();
    try
    {
        var n = await flyers.ImportAsync(req.Body, ct);
        return Results.Ok(new { items = n });
    }
    catch (Exception e) when (e is System.Text.Json.JsonException or InvalidDataException)
    {
        return Results.BadRequest("невалиден JSON: " + e.Message);
    }
}).WithMetadata(new Microsoft.AspNetCore.Mvc.RequestSizeLimitAttribute(MaxImport));

app.Run();

public partial class Program { }
