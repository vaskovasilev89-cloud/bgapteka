namespace BgApteka;

/// <summary>Копие на scraper/pharma/grouping.py: EAN или (еднакви числа + сходно име).</summary>
public static class Grouping
{
    const double Threshold = 82;

    public sealed class Group
    {
        public string Name = "", Norm = "", Nums = "";
        public string? Image;
        public HashSet<string> Eans = new();
        public List<Offer> Offers = new();
    }

    public static List<Group> Build(IEnumerable<Offer> rows)
    {
        var groups = new List<Group>();
        foreach (var r in rows)
        {
            var nums = TextNorm.NumbersKey(r.Norm);
            Group? target = r.Ean != null ? groups.FirstOrDefault(g => g.Eans.Contains(r.Ean)) : null;
            if (target == null)
            {
                double best = 0;
                foreach (var g in groups)
                {
                    if (g.Nums != nums) continue;
                    if (r.Ean != null && g.Eans.Count > 0 && !g.Eans.Contains(r.Ean)) continue;
                    var score = TokenSetRatio(r.Norm, g.Norm);
                    if (score >= Threshold && score > best) { best = score; target = g; }
                }
            }
            if (target == null)
            {
                target = new Group { Name = r.Name, Norm = r.Norm, Nums = nums };
                groups.Add(target);
            }
            if (r.Ean != null) target.Eans.Add(r.Ean);
            target.Image ??= r.Image;
            target.Offers.Add(r);
        }
        foreach (var g in groups)
            g.Offers = g.Offers.OrderBy(o => o.InStock == 0).ThenBy(o => o.Price).ToList();
        return groups;
    }

    // ---- token_set_ratio като в rapidfuzz ----
    public static double TokenSetRatio(string a, string b)
    {
        var ta = new SortedSet<string>(a.Split(' ', StringSplitOptions.RemoveEmptyEntries), StringComparer.Ordinal);
        var tb = new SortedSet<string>(b.Split(' ', StringSplitOptions.RemoveEmptyEntries), StringComparer.Ordinal);
        if (ta.Count == 0 || tb.Count == 0) return 0;
        var inter = ta.Intersect(tb, StringComparer.Ordinal).ToList();
        var da = ta.Except(tb, StringComparer.Ordinal).ToList();
        var db = tb.Except(ta, StringComparer.Ordinal).ToList();
        if (inter.Count > 0 && (da.Count == 0 || db.Count == 0)) return 100;
        string si = string.Join(' ', inter), sa = string.Join(' ', da), sb = string.Join(' ', db);
        string c1 = si.Length > 0 ? si + " " + sa : sa;
        string c2 = si.Length > 0 ? si + " " + sb : sb;
        double res = Ratio(sa, sb);
        if (si.Length > 0)
        {
            // rapidfuzz смята t0 vs t1/t2 аналитично: разликата е само "дифът"
            res = Math.Max(res, Ratio(si, c1));
            res = Math.Max(res, Ratio(si, c2));
            res = Math.Max(res, Ratio(c1, c2));
        }
        return res;
    }

    public static double Ratio(string s1, string s2)
    {
        int total = s1.Length + s2.Length;
        if (total == 0) return 100;
        return 100.0 * 2 * Lcs(s1, s2) / total;
    }

    static int Lcs(string a, string b)
    {
        var prev = new int[b.Length + 1];
        var cur = new int[b.Length + 1];
        for (int i = 1; i <= a.Length; i++)
        {
            for (int j = 1; j <= b.Length; j++)
                cur[j] = a[i - 1] == b[j - 1] ? prev[j - 1] + 1 : Math.Max(prev[j], cur[j - 1]);
            (prev, cur) = (cur, prev);
        }
        return prev[b.Length];
    }
}
