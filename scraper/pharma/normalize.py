"""Нормализиране на имена: малки букви, кирилица -> латиница, за да се търси
"нурофен" и "nurofen" еднакво, и за групиране на един и същ продукт от различни аптеки."""
import re
import unicodedata

_TR = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh", "з": "z",
    "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p",
    "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "ch",
    "ш": "sh", "щ": "sht", "ъ": "a", "ь": "y", "ю": "yu", "я": "ya", "ы": "y", "э": "e",
    "ё": "yo",
}
# латинско x -> ks, за да съвпадат "Xylo" и "Ксило"
_LAT = {"x": "ks", "w": "v", "q": "k"}

_UNITS = {"мл": "ml", "мг": "mg", "гр": "g", "г": "g", "мкг": "mcg", "бр": "br", "таб": "tab"}


def translit(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    out = []
    for ch in text:
        if ch in _TR:
            out.append(_TR[ch])
        elif ch in _LAT:
            out.append(_LAT[ch])
        else:
            out.append(ch)
    return "".join(out)


def norm(text: str) -> str:
    """Текст за търсене/сравнение: латиница, без пунктуация, 'х20'/'x20' -> '20'."""
    t = (text or "").lower()
    t = re.sub(r"(?<![a-zа-я])[xх×*](?=\s?\d)", " ", t)      # х20, x 30, *6 -> 20, 30, 6
    t = re.sub(r"(\d),(\d)", r"\1.\2", t)                      # 0,5 -> 0.5
    t = re.sub(r"(\d)\s*(мл|мг|мкг|гр|г|бр|таб)\b", lambda m: m.group(1) + " " + _UNITS[m.group(2)], t)
    t = re.sub(r"(\d)([a-zа-я])", r"\1 \2", t)                 # 500mg -> 500 mg
    t = translit(t)
    t = re.sub(r"[^\w.]+", " ", t)
    t = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def numbers(text_norm: str) -> frozenset:
    """Числата в името (доза, количество) – два продукта с различни числа не са един и същ."""
    return frozenset(re.findall(r"\d+(?:\.\d+)?", text_norm))


def valid_ean(code: str) -> bool:
    if not code or not code.isdigit() or len(code) not in (8, 13):
        return False
    digits = [int(c) for c in code]
    check = digits.pop()
    weights = [3, 1] if len(code) == 13 else [3, 1]
    s = sum(d * weights[(len(digits) - 1 - i) % 2] for i, d in enumerate(digits))
    return (10 - s % 10) % 10 == check
