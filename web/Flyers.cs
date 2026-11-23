using System.Data;
using System.Text.Json;
using System.Text.Json.Serialization;
using Microsoft.Data.SqlClient;
using Microsoft.Extensions.Logging;

namespace BgApteka;

/// <summary>Една брошура (напр. Kaufland 23.11–29.11.2026), както я качва scraper/flyers.py.</summary>
public sealed class FlyerData
{
    public string FlyerId { get; set; } = "";
    public string Store { get; set; } = "";
    public string StoreTitle { get; set; } = "";
    public string Title { get; set; } = "";
    public string Url { get; set; } = "";
    public DateOnly ValidFrom { get; set; }
    public DateOnly ValidTo { get; set; }
    public int Pages { get; set; }
    public List<FlyerItem> Items { get; set; } = new();
}

/// <summary>Артикул от страница на брошура. Box е в проценти от страницата (за изрязване на снимката).</summary>
public sealed class FlyerItem
{
    public int Page { get; set; }
    public string Name { get; set; } = "";
    public string? Brand { get; set; }
    public string? Quantity { get; set; }
    public double? PromoPrice { get; set; }
    public double? RegularPrice { get; set; }
    public int? DiscountPct { get; set; }
    public string? Note { get; set; }
    public string PageImage { get; set; } = "";
    /// <summary>Височина / ширина на страницата (за изрязване на снимката в браузъра).</summary>
    public double PageRatio { get; set; } = 1.6;
    public double BoxLeft { get; set; }
    public double BoxTop { get; set; }
    public double BoxWidth { get; set; }
    public double BoxHeight { get; set; }

    // попълват се при четене от базата
    public string Store { get; set; } = "";
    public string StoreTitle { get; set; } = "";
    public string FlyerId { get; set; } = "";
    public string FlyerUrl { get; set; } = "";
    public DateOnly ValidFrom { get; set; }
    public DateOnly ValidTo { get; set; }
    [JsonIgnore] public string Norm { get; set; } = "";
}

public sealed record FlyerInfo(string FlyerId, string Store, string StoreTitle, string Title, string Url,
                               DateOnly ValidFrom, DateOnly ValidTo, int Pages, int Items);

/// <summary>Брошури на супермаркети в MSSQL (таблици Flyers и FlyerItems).</summary>
public sealed class FlyerStore
{
    const int MaxCandidates = 3000;
    readonly string _cs;
    readonly ILogger _log;
    readonly SemaphoreSlim _init = new(1, 1);
    volatile bool _ready;

    public FlyerStore(string connectionString, ILogger<FlyerStore> log)
    {
        _cs = connectionString;
        _log = log;
    }

    const string Schema = """
        IF OBJECT_ID(N'dbo.Flyers', N'U') IS NULL
        CREATE TABLE dbo.Flyers (
            FlyerId     varchar(100)   NOT NULL CONSTRAINT PK_Flyers PRIMARY KEY,
            Store       varchar(32)    NOT NULL,
            StoreTitle  nvarchar(100)  NOT NULL,
            Title       nvarchar(200)  NOT NULL,
            Url         nvarchar(1000) NOT NULL,
            ValidFrom   date           NOT NULL,
            ValidTo     date           NOT NULL,
            Pages       int            NOT NULL,
            Items       int            NOT NULL,
            ImportedAt  datetime2(0)   NOT NULL CONSTRAINT DF_Flyers_ImportedAt DEFAULT SYSUTCDATETIME()
        );

        IF OBJECT_ID(N'dbo.FlyerItems', N'U') IS NULL
        BEGIN
            CREATE TABLE dbo.FlyerItems (
                Id            int IDENTITY(1,1) NOT NULL CONSTRAINT PK_FlyerItems PRIMARY KEY,
                FlyerId       varchar(100)   NOT NULL,
                Store         varchar(32)    NOT NULL,
                ValidFrom     date           NOT NULL,
                ValidTo       date           NOT NULL,
                Page          int            NOT NULL,
                Name          nvarchar(500)  NOT NULL,
                Norm          nvarchar(1000) COLLATE Latin1_General_100_BIN2 NOT NULL,
                Brand         nvarchar(200)  NULL,
                Quantity      nvarchar(200)  NULL,
                PromoPrice    float          NULL,
                RegularPrice  float          NULL,
                DiscountPct   int            NULL,
                Note          nvarchar(300)  NULL,
                PageImage     nvarchar(1000) NOT NULL,
                PageRatio     float          NOT NULL,
                BoxLeft       float          NOT NULL,
                BoxTop        float          NOT NULL,
                BoxWidth      float          NOT NULL,
                BoxHeight     float          NOT NULL
            );
            CREATE INDEX IX_FlyerItems_Flyer ON dbo.FlyerItems (FlyerId);
            CREATE INDEX IX_FlyerItems_Valid ON dbo.FlyerItems (ValidTo, ValidFrom);
        END
        """;

    async Task<SqlConnection> OpenAsync(CancellationToken ct)
    {
        var con = new SqlConnection(_cs);
        await con.OpenAsync(ct);
        return con;
    }

    async Task EnsureAsync(CancellationToken ct)
    {
        if (_ready) return;
        await _init.WaitAsync(ct);
        try
        {
            if (_ready) return;
            await using var con = await OpenAsync(ct);
            await using var cmd = new SqlCommand(Schema, con);
            await cmd.ExecuteNonQueryAsync(ct);
            _ready = true;
        }
        finally { _init.Release(); }
    }

    static string? Cut(string? s, int max) => s == null || s.Length <= max ? s : s[..max];
    static double Clamp(double v) => double.IsFinite(v) ? Math.Clamp(v, 0, 100) : 0;

    /// <summary>Днешната дата в България – за да се показват само валидните брошури.</summary>
    public static DateOnly Today()
    {
        TimeZoneInfo tz;
        try { tz = TimeZoneInfo.FindSystemTimeZoneById("FLE Standard Time"); }
        catch (TimeZoneNotFoundException) { tz = TimeZoneInfo.FindSystemTimeZoneById("Europe/Sofia"); }
        return DateOnly.FromDateTime(TimeZoneInfo.ConvertTimeFromUtc(DateTime.UtcNow, tz));
    }

    /// <summary>Заменя всички артикули на брошурата в една транзакция.</summary>
    public async Task<int> ImportAsync(Stream body, CancellationToken ct)
    {
        var f = await JsonSerializer.DeserializeAsync<FlyerData>(body, Catalog.Json, ct)
                ?? throw new InvalidDataException("празни данни");
        if (string.IsNullOrWhiteSpace(f.FlyerId) || f.FlyerId.Length > 100) throw new InvalidDataException("невалиден flyer_id");
        if (!Catalog.SiteKey().IsMatch(f.Store)) throw new InvalidDataException("невалиден store");
        if (f.ValidTo < f.ValidFrom) throw new InvalidDataException("valid_to е преди valid_from");
        var items = f.Items.Where(i => !string.IsNullOrWhiteSpace(i.Name) && !string.IsNullOrWhiteSpace(i.PageImage)).ToList();

        await EnsureAsync(ct);
        var table = new DataTable();
        foreach (var (n, t) in new (string, Type)[] {
            ("FlyerId", typeof(string)), ("Store", typeof(string)), ("ValidFrom", typeof(DateTime)), ("ValidTo", typeof(DateTime)),
            ("Page", typeof(int)), ("Name", typeof(string)), ("Norm", typeof(string)), ("Brand", typeof(string)),
            ("Quantity", typeof(string)), ("PromoPrice", typeof(double)), ("RegularPrice", typeof(double)),
            ("DiscountPct", typeof(int)), ("Note", typeof(string)), ("PageImage", typeof(string)), ("PageRatio", typeof(double)),
            ("BoxLeft", typeof(double)), ("BoxTop", typeof(double)), ("BoxWidth", typeof(double)), ("BoxHeight", typeof(double)) })
            table.Columns.Add(n, t);
        foreach (var i in items)
            table.Rows.Add(f.FlyerId, f.Store, f.ValidFrom.ToDateTime(TimeOnly.MinValue), f.ValidTo.ToDateTime(TimeOnly.MinValue),
                i.Page, Cut(i.Name.Trim(), 500), Cut(TextNorm.Norm(i.Name + " " + i.Brand), 1000),
                (object?)Cut(i.Brand, 200) ?? DBNull.Value, (object?)Cut(i.Quantity, 200) ?? DBNull.Value,
                (object?)i.PromoPrice ?? DBNull.Value, (object?)i.RegularPrice ?? DBNull.Value,
                (object?)i.DiscountPct ?? DBNull.Value, (object?)Cut(i.Note, 300) ?? DBNull.Value,
                Cut(i.PageImage, 1000), i.PageRatio is > 0.2 and < 5 ? i.PageRatio : 1.6, Clamp(i.BoxLeft), Clamp(i.BoxTop), Clamp(i.BoxWidth), Clamp(i.BoxHeight));

        await using var con = await OpenAsync(ct);
        await using var tx = (SqlTransaction)await con.BeginTransactionAsync(ct);
        await using (var del = new SqlCommand("DELETE FROM dbo.FlyerItems WHERE FlyerId = @f; DELETE FROM dbo.Flyers WHERE FlyerId = @f;", con, tx))
        {
            del.Parameters.Add("@f", SqlDbType.VarChar, 100).Value = f.FlyerId;
            await del.ExecuteNonQueryAsync(ct);
        }
        await using (var ins = new SqlCommand("""
            INSERT dbo.Flyers (FlyerId, Store, StoreTitle, Title, Url, ValidFrom, ValidTo, Pages, Items)
            VALUES (@f, @s, @st, @t, @u, @vf, @vt, @p, @n)
            """, con, tx))
        {
            ins.Parameters.Add("@f", SqlDbType.VarChar, 100).Value = f.FlyerId;
            ins.Parameters.Add("@s", SqlDbType.VarChar, 32).Value = f.Store;
            ins.Parameters.Add("@st", SqlDbType.NVarChar, 100).Value = Cut(f.StoreTitle, 100) ?? f.Store;
            ins.Parameters.Add("@t", SqlDbType.NVarChar, 200).Value = Cut(f.Title, 200) ?? "";
            ins.Parameters.Add("@u", SqlDbType.NVarChar, 1000).Value = Cut(f.Url, 1000) ?? "";
            ins.Parameters.Add("@vf", SqlDbType.Date).Value = f.ValidFrom.ToDateTime(TimeOnly.MinValue);
            ins.Parameters.Add("@vt", SqlDbType.Date).Value = f.ValidTo.ToDateTime(TimeOnly.MinValue);
            ins.Parameters.Add("@p", SqlDbType.Int).Value = f.Pages;
            ins.Parameters.Add("@n", SqlDbType.Int).Value = items.Count;
            await ins.ExecuteNonQueryAsync(ct);
        }
        using (var bulk = new SqlBulkCopy(con, SqlBulkCopyOptions.Default, tx) { DestinationTableName = "dbo.FlyerItems", BulkCopyTimeout = 300 })
        {
            foreach (DataColumn c in table.Columns) bulk.ColumnMappings.Add(c.ColumnName, c.ColumnName);
            await bulk.WriteToServerAsync(table, ct);
        }
        await tx.CommitAsync(ct);
        return items.Count;
    }

    /// <summary>Брошурите, които още не са изтекли.</summary>
    public async Task<List<FlyerInfo>> CurrentAsync(CancellationToken ct)
    {
        await EnsureAsync(ct);
        await using var con = await OpenAsync(ct);
        await using var cmd = new SqlCommand("""
            SELECT FlyerId, Store, StoreTitle, Title, Url, ValidFrom, ValidTo, Pages, Items
            FROM dbo.Flyers WHERE ValidTo >= @d ORDER BY StoreTitle, ValidFrom
            """, con);
        cmd.Parameters.Add("@d", SqlDbType.Date).Value = Today().ToDateTime(TimeOnly.MinValue);
        await using var r = await cmd.ExecuteReaderAsync(ct);
        var list = new List<FlyerInfo>();
        while (await r.ReadAsync(ct))
            list.Add(new FlyerInfo(r.GetString(0), r.GetString(1), r.GetString(2), r.GetString(3), r.GetString(4),
                DateOnly.FromDateTime(r.GetDateTime(5)), DateOnly.FromDateTime(r.GetDateTime(6)), r.GetInt32(7), r.GetInt32(8)));
        return list;
    }

    static string LikeEscape(string s) => s.Replace(@"\", @"\\").Replace("%", @"\%").Replace("_", @"\_").Replace("[", @"\[");

    /// <summary>
    /// Търси в артикулите от валидните брошури. Празна заявка → най-големите отстъпки.
    /// </summary>
    public async Task<List<FlyerItem>> SearchAsync(string q, HashSet<string>? stores, int limit, CancellationToken ct)
    {
        await EnsureAsync(ct);
        var tokens = Catalog.Tokens(q);
        await using var con = await OpenAsync(ct);
        await using var cmd = new SqlCommand { Connection = con, CommandTimeout = 30 };
        var where = new List<string> { "i.ValidTo >= @d" };
        cmd.Parameters.Add("@d", SqlDbType.Date).Value = Today().ToDateTime(TimeOnly.MinValue);
        for (int k = 0; k < tokens.Length; k++)
        {
            where.Add($"(N' ' + i.Norm) LIKE @t{k} ESCAPE '\\'");
            cmd.Parameters.Add($"@t{k}", SqlDbType.NVarChar, 1100).Value = "% " + LikeEscape(tokens[k]) + "%";
        }
        if (stores is { Count: > 0 })
        {
            var names = new List<string>();
            int j = 0;
            foreach (var s in stores.Where(s => Catalog.SiteKey().IsMatch(s)))
            {
                names.Add($"@s{j}");
                cmd.Parameters.Add($"@s{j++}", SqlDbType.VarChar, 32).Value = s;
            }
            if (names.Count == 0) return new();
            where.Add($"i.Store IN ({string.Join(",", names)})");
        }
        var order = tokens.Length == 0 ? "ORDER BY ISNULL(i.DiscountPct, 0) DESC, i.PromoPrice" : "";
        var top = tokens.Length == 0 ? limit : MaxCandidates;
        cmd.CommandText = $"""
            SELECT TOP ({top}) i.FlyerId, i.Store, f.StoreTitle, f.Url, i.ValidFrom, i.ValidTo, i.Page, i.Name, i.Norm, i.Brand,
                   i.Quantity, i.PromoPrice, i.RegularPrice, i.DiscountPct, i.Note, i.PageImage, i.BoxLeft, i.BoxTop, i.BoxWidth, i.BoxHeight, i.PageRatio
            FROM dbo.FlyerItems i JOIN dbo.Flyers f ON f.FlyerId = i.FlyerId
            WHERE {string.Join(" AND ", where)} {order}
            """;
        var list = new List<FlyerItem>();
        await using (var r = await cmd.ExecuteReaderAsync(ct))
            while (await r.ReadAsync(ct))
                list.Add(new FlyerItem
                {
                    FlyerId = r.GetString(0), Store = r.GetString(1), StoreTitle = r.GetString(2), FlyerUrl = r.GetString(3),
                    ValidFrom = DateOnly.FromDateTime(r.GetDateTime(4)), ValidTo = DateOnly.FromDateTime(r.GetDateTime(5)),
                    Page = r.GetInt32(6), Name = r.GetString(7), Norm = r.GetString(8),
                    Brand = r.IsDBNull(9) ? null : r.GetString(9), Quantity = r.IsDBNull(10) ? null : r.GetString(10),
                    PromoPrice = r.IsDBNull(11) ? null : r.GetDouble(11), RegularPrice = r.IsDBNull(12) ? null : r.GetDouble(12),
                    DiscountPct = r.IsDBNull(13) ? null : r.GetInt32(13), Note = r.IsDBNull(14) ? null : r.GetString(14),
                    PageImage = r.GetString(15), BoxLeft = r.GetDouble(16), BoxTop = r.GetDouble(17),
                    BoxWidth = r.GetDouble(18), BoxHeight = r.GetDouble(19), PageRatio = r.GetDouble(20),
                });
        if (tokens.Length == 0) return list;
        // по съвпадение, а при равни – по-евтиното първо
        return Catalog.Rank(list.OrderBy(i => i.PromoPrice ?? double.MaxValue), i => i.Norm, tokens, limit);
    }
}
