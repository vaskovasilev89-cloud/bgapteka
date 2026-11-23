using System.Text;
using System.Text.RegularExpressions;

namespace BgApteka;

/// <summary>
/// Точно копие на scraper/pharma/normalize.py → norm().
/// Python изпраща готовото "norm" поле за всеки продукт; тук нормализираме само заявката,
/// затова двете реализации трябва да дават еднакъв резултат (проверява се в тестовете).
/// </summary>
public static partial class TextNorm
{
    static readonly Dictionary<char, string> Tr = new()
    {
        ['а'] = "a", ['б'] = "b", ['в'] = "v", ['г'] = "g", ['д'] = "d", ['е'] = "e", ['ж'] = "zh", ['з'] = "z",
        ['и'] = "i", ['й'] = "y", ['к'] = "k", ['л'] = "l", ['м'] = "m", ['н'] = "n", ['о'] = "o", ['п'] = "p",
        ['р'] = "r", ['с'] = "s", ['т'] = "t", ['у'] = "u", ['ф'] = "f", ['х'] = "h", ['ц'] = "c", ['ч'] = "ch",
        ['ш'] = "sh", ['щ'] = "sht", ['ъ'] = "a", ['ь'] = "y", ['ю'] = "yu", ['я'] = "ya", ['ы'] = "y", ['э'] = "e",
        ['ё'] = "yo",
        ['x'] = "ks", ['w'] = "v", ['q'] = "k",
    };

    static readonly Dictionary<string, string> Units = new()
    {
        ["мл"] = "ml", ["мг"] = "mg", ["гр"] = "g", ["г"] = "g", ["мкг"] = "mcg", ["бр"] = "br", ["таб"] = "tab",
    };

    [GeneratedRegex(@"(?<![a-zа-я])[xх×*](?=\s?\d)")] private static partial Regex RxTimes();
    [GeneratedRegex(@"(\d),(\d)")] private static partial Regex RxComma();
    [GeneratedRegex(@"(\d)\s*(мл|мг|мкг|гр|г|бр|таб)\b")] private static partial Regex RxUnit();
    [GeneratedRegex(@"(\d)([a-zа-я])")] private static partial Regex RxSplit();
    [GeneratedRegex(@"[^\w.]+")] private static partial Regex RxNonWord();
    [GeneratedRegex(@"(?<!\d)\.|\.(?!\d)")] private static partial Regex RxDot();
    [GeneratedRegex(@"\s+")] private static partial Regex RxSpace();
    [GeneratedRegex(@"\d+(?:\.\d+)?")] private static partial Regex RxNum();

    public static string Translit(string text)
    {
        text = (text ?? "").Normalize(NormalizationForm.FormKC).ToLowerInvariant();
        var sb = new StringBuilder(text.Length + 8);
        foreach (var ch in text)
            if (Tr.TryGetValue(ch, out var s)) sb.Append(s); else sb.Append(ch);
        return sb.ToString();
    }

    public static string Norm(string? text)
    {
        var t = (text ?? "").ToLowerInvariant();
        t = RxTimes().Replace(t, " ");
        t = RxComma().Replace(t, "$1.$2");
        t = RxUnit().Replace(t, m => m.Groups[1].Value + " " + Units[m.Groups[2].Value]);
        t = RxSplit().Replace(t, "$1 $2");
        t = Translit(t);
        t = RxNonWord().Replace(t, " ");
        t = RxDot().Replace(t, " ");
        return RxSpace().Replace(t, " ").Trim();
    }

    public static string NumbersKey(string norm) =>
        string.Join(",", RxNum().Matches(norm).Select(m => m.Value).Distinct().Order(StringComparer.Ordinal));

    public static bool ValidEan(string? code)
    {
        if (string.IsNullOrEmpty(code) || (code.Length != 8 && code.Length != 13) || !code.All(char.IsAsciiDigit)) return false;
        int sum = 0, n = code.Length - 1;
        for (int i = 0; i < n; i++) sum += (code[i] - '0') * (((n - 1 - i) % 2 == 0) ? 3 : 1);
        return (10 - sum % 10) % 10 == code[n] - '0';
    }
}
