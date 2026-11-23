using System.Data;
using System.Text.Json;
using Microsoft.Data.SqlClient;
using Microsoft.Extensions.Logging;

namespace BgApteka;

/// <summary>
/// Продуктите в MSSQL (напр. безплатната база на MonsterASP).
/// Таблиците се създават сами при първа заявка. Ако базата е празна, а в App_Data/sites има
/// стари *.json.gz файлове, те се пренасят автоматично.
/// </summary>
public sealed class SqlCatalog : ICatalog
{
    const int MaxCandidates = 5000;   // колко реда да се вземат от базата преди класирането

    readonly string _cs;
    readonly string _legacyDir;
    readonly ILogger _log;
    readonly SemaphoreSlim _init = new(1, 1);
    volatile bool _ready;

    public string Kind => "mssql";

    public SqlCatalog(string connectionString, string legacyDir, ILogger<SqlCatalog> log)
    {
        _cs = connectionString;
        _legacyDir = legacyDir;
        _log = log;
    }

    const string Schema = """
        IF OBJECT_ID(N'dbo.Sites', N'U') IS NULL
        CREATE TABLE dbo.Sites (
            Site        varchar(32)   NOT NULL CONSTRAINT PK_Sites PRIMARY KEY,
            Title       nvarchar(200) NOT NULL,
            Base        nvarchar(400) NOT NULL,
            Generated   float         NOT NULL,
            Products    int           NOT NULL,
            ImportedAt  datetime2(0)  NOT NULL CONSTRAINT DF_Sites_ImportedAt DEFAULT SYSUTCDATETIME()
        );

        IF OBJECT_ID(N'dbo.Products', N'U') IS NULL
        BEGIN
            CREATE TABLE dbo.Products (
                Id         int IDENTITY(1,1) NOT NULL CONSTRAINT PK_Products PRIMARY KEY,
                Site       varchar(32)    NOT NULL,
                Url        nvarchar(1000) NOT NULL,
                Name       nvarchar(500)  NOT NULL,
                Norm       nvarchar(1000) COLLATE Latin1_General_100_BIN2 NOT NULL,
                Price      float          NOT NULL,
                OldPrice   float          NULL,
                InStock    int            NULL,
                Ean        varchar(13)    NULL,
                Brand      nvarchar(200)  NULL,
                Image      nvarchar(1000) NULL,
                UpdatedAt  float          NOT NULL
            );
            CREATE INDEX IX_Products_Site ON dbo.Products (Site);
            CREATE INDEX IX_Products_Ean  ON dbo.Products (Ean) WHERE Ean IS NOT NULL;
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
            await using (var cmd = new SqlCommand(Schema, con)) await cmd.ExecuteNonQueryAsync(ct);
            _ready = true;

            await using var cnt = new SqlCommand("SELECT COUNT(*) FROM dbo.Sites", con);
            if ((int)(await cnt.ExecuteScalarAsync(ct))! == 0)
                foreach (var sd in FileCatalog.LoadAll(_legacyDir, _log))
                {
                    var n = await SaveAsync(sd, ct);
                    _log.LogInformation("Пренесени {Count} продукта на {Site} от файл в MSSQL", n, sd.Site);
                }
        }
        finally { _init.Release(); }
    }

    public async Task<IReadOnlyList<SiteInfo>> GetSitesAsync(CancellationToken ct = default)
    {
        await EnsureAsync(ct);
        await using var con = await OpenAsync(ct);
        await using var cmd = new SqlCommand("SELECT Site, Title, Base, Generated, Products FROM dbo.Sites", con);
        await using var r = await cmd.ExecuteReaderAsync(ct);
        var list = new List<SiteInfo>();
        while (await r.ReadAsync(ct))
            list.Add(new SiteInfo(r.GetString(0), r.GetString(1), r.GetString(2), r.GetDouble(3), r.GetInt32(4)));
        return list;
    }

    public async Task<int> ImportAsync(string site, Stream body, CancellationToken ct)
    {
        var sd = await JsonSerializer.DeserializeAsync<SiteData>(body, Catalog.Json, ct)
                 ?? throw new InvalidDataException("празни данни");
        sd.Site = site;
        Catalog.Prepare(sd);
        await EnsureAsync(ct);
        return await SaveAsync(sd, ct);
    }

    static string? Cut(string? s, int max) => s == null || s.Length <= max ? s : s[..max];

    /// <summary>Заменя всички продукти на аптеката в една транзакция.</summary>
    async Task<int> SaveAsync(SiteData sd, CancellationToken ct)
    {
        var table = new DataTable();
        table.Columns.Add("Site", typeof(string));
        table.Columns.Add("Url", typeof(string));
        table.Columns.Add("Name", typeof(string));
        table.Columns.Add("Norm", typeof(string));
        table.Columns.Add("Price", typeof(double));
        table.Columns.Add("OldPrice", typeof(double));
        table.Columns.Add("InStock", typeof(int));
        table.Columns.Add("Ean", typeof(string));
        table.Columns.Add("Brand", typeof(string));
        table.Columns.Add("Image", typeof(string));
        table.Columns.Add("UpdatedAt", typeof(double));
        foreach (var p in sd.Products)
            table.Rows.Add(sd.Site, Cut(p.Url, 1000), Cut(p.Name, 500), Cut(p.Norm, 1000), p.Price,
                (object?)p.OldPrice ?? DBNull.Value, (object?)p.InStock ?? DBNull.Value, (object?)p.Ean ?? DBNull.Value,
                (object?)Cut(p.Brand, 200) ?? DBNull.Value, (object?)Cut(p.Image, 1000) ?? DBNull.Value, p.UpdatedAt);

        await using var con = await OpenAsync(ct);
        await using var tx = (SqlTransaction)await con.BeginTransactionAsync(ct);

        await using (var del = new SqlCommand("DELETE FROM dbo.Products WHERE Site = @s", con, tx))
        {
            del.Parameters.Add("@s", SqlDbType.VarChar, 32).Value = sd.Site;
            del.CommandTimeout = 300;
            await del.ExecuteNonQueryAsync(ct);
        }

        using (var bulk = new SqlBulkCopy(con, SqlBulkCopyOptions.Default, tx)
               { DestinationTableName = "dbo.Products", BatchSize = 2000, BulkCopyTimeout = 600 })
        {
            foreach (DataColumn c in table.Columns) bulk.ColumnMappings.Add(c.ColumnName, c.ColumnName);
            await bulk.WriteToServerAsync(table, ct);
        }

        const string upsert = """
            UPDATE dbo.Sites SET Title = @t, Base = @b, Generated = @g, Products = @n, ImportedAt = SYSUTCDATETIME() WHERE Site = @s;
            IF @@ROWCOUNT = 0
                INSERT dbo.Sites (Site, Title, Base, Generated, Products) VALUES (@s, @t, @b, @g, @n);
            """;
        await using (var up = new SqlCommand(upsert, con, tx))
        {
            up.Parameters.Add("@s", SqlDbType.VarChar, 32).Value = sd.Site;
            up.Parameters.Add("@t", SqlDbType.NVarChar, 200).Value = Cut(sd.Title, 200) ?? sd.Site;
            up.Parameters.Add("@b", SqlDbType.NVarChar, 400).Value = Cut(sd.Base, 400) ?? "";
            up.Parameters.Add("@g", SqlDbType.Float).Value = sd.Generated;
            up.Parameters.Add("@n", SqlDbType.Int).Value = sd.Products.Count;
            await up.ExecuteNonQueryAsync(ct);
        }

        await tx.CommitAsync(ct);
        return sd.Products.Count;
    }

    static string LikeEscape(string s) => s.Replace(@"\", @"\\").Replace("%", @"\%").Replace("_", @"\_").Replace("[", @"\[");

    const string Columns = "Site, Url, Name, Norm, Price, OldPrice, InStock, Ean, Brand, Image, UpdatedAt";

    public async Task<List<Offer>> SearchAsync(string q, bool inStock, HashSet<string>? sites, int limit = 400, CancellationToken ct = default)
    {
        var tokens = Catalog.Tokens(q);
        if (tokens.Length == 0) return new();
        await EnsureAsync(ct);
        await using var con = await OpenAsync(ct);

        if (Catalog.AsEan(tokens) is string ean)
        {
            await using var ec = new SqlCommand($"SELECT {Columns} FROM dbo.Products WHERE Ean = @e", con);
            ec.Parameters.Add("@e", SqlDbType.VarChar, 13).Value = ean;
            var byEan = await ReadAsync(ec, ct);
            if (byEan.Count > 0) return byEan;
        }

        await using var cmd = new SqlCommand { Connection = con, CommandTimeout = 30 };
        var where = new List<string>();
        for (int i = 0; i < tokens.Length; i++)
        {
            where.Add($"(N' ' + Norm) LIKE @t{i} ESCAPE '\\'");
            cmd.Parameters.Add($"@t{i}", SqlDbType.NVarChar, 1100).Value = "% " + LikeEscape(tokens[i]) + "%";
        }
        if (inStock) where.Add("(InStock IS NULL OR InStock <> 0)");
        if (sites is { Count: > 0 })
        {
            var names = new List<string>();
            int j = 0;
            foreach (var s in sites.Where(s => Catalog.SiteKey().IsMatch(s)))
            {
                names.Add($"@s{j}");
                cmd.Parameters.Add($"@s{j++}", SqlDbType.VarChar, 32).Value = s;
            }
            if (names.Count == 0) return new();
            where.Add($"Site IN ({string.Join(",", names)})");
        }
        cmd.CommandText = $"SELECT TOP ({MaxCandidates}) {Columns} FROM dbo.Products WHERE {string.Join(" AND ", where)}";
        return Catalog.Rank(await ReadAsync(cmd, ct), tokens, limit);
    }

    static async Task<List<Offer>> ReadAsync(SqlCommand cmd, CancellationToken ct)
    {
        var list = new List<Offer>();
        await using var r = await cmd.ExecuteReaderAsync(ct);
        while (await r.ReadAsync(ct))
        {
            var o = new Offer
            {
                Site = r.GetString(0), Url = r.GetString(1), Name = r.GetString(2), Norm = r.GetString(3),
                Price = r.GetDouble(4),
                OldPrice = r.IsDBNull(5) ? null : r.GetDouble(5),
                InStock = r.IsDBNull(6) ? null : r.GetInt32(6),
                Ean = r.IsDBNull(7) ? null : r.GetString(7),
                Brand = r.IsDBNull(8) ? null : r.GetString(8),
                Image = r.IsDBNull(9) ? null : r.GetString(9),
                UpdatedAt = r.GetDouble(10),
            };
            o.Padded = " " + o.Norm;
            list.Add(o);
        }
        return list;
    }
}
