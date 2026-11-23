"""Безплатно разчитане на брошура на Lidl от PDF-а ѝ (без Claude).

PDF-ът на Lidl съдържа истински текст. Всеки артикул се състои от отделни елементи:
    MILBONA / Пушен кашкавал     ← марка и име (шрифт LidlFontCondProBG-Bold)
    От краве мляко / 730 g/…     ← описание и количество (LidlFontCondProBG-Book)
    6.49€                        ← промоционална цена (шрифт LidlFontPrice)
    Акция 7.66 € / -23% 2.59 €   ← стандартна цена (LidlFontProBG)
Елементите се събират по страницата и се свързват по близост. Снимката на артикула е
най-близкото до него изображение.
"""
import re
import unicodedata

import pymupdf

NUM = r"(\d+[.,]\d{2})"
PRICE_RE = re.compile(r"(\d+)(?:[.,](\d{2}|-))?\s*€")   # 6.49€, 199.-€, 5€
OLD_RE = re.compile(r"^(?:Акция|-\s*(\d{1,2})\s*%)?\s*" + NUM + r"\s*€\*?$")
QTY_RE = re.compile(r"\d\s*(g|kg|ml|l|г|кг|мл|л|бр)\b|/\s*\w", re.I)
NOTE_RE = re.compile(r"^(До \d|В асортимента|Редовна цена|С Lidl Plus|Цена за|Само |Валидно|№\s*\d)", re.I)
VALID_RE = re.compile(r"^От\s.+\sдо\s.+", re.I)


def _clean(s):
    # NFKC: „2.⁵⁰€“ → „2.50€“, „﹪“ → „%“ (Lidl понякога пише стотинките като горни индекси)
    s = unicodedata.normalize("NFKC", s)
    return re.sub(r"\s+", " ", s.replace(" ", " ").replace("\xa0", " ").replace("﹪", "%")).strip()


def _f(s):
    return float(s.replace(",", "."))


def _is_brand(text):
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and all(not c.islower() for c in letters)


def _union(a, b):
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _dist(a, b):
    """Разстояние между два правоъгълника (0, ако се застъпват)."""
    dx = max(b[0] - a[2], a[0] - b[2], 0)
    dy = max(b[1] - a[3], a[1] - b[3], 0)
    return (dx * dx + dy * dy) ** 0.5


def _overlap(a0, a1, b0, b1):
    return max(0.0, min(a1, b1) - max(a0, b0))


def _side_dist(img, txt):
    """Колко далеч е снимката от текста, ако е вляво от него или над него; иначе безкрайност."""
    if _overlap(img[0], img[2], txt[0], txt[2]) > 0 and _overlap(img[1], img[3], txt[1], txt[3]) > 0:
        return 0.0
    vo = _overlap(img[1], img[3], txt[1], txt[3])
    if vo > 0.3 * min(img[3] - img[1], txt[3] - txt[1]) and img[2] <= txt[0] + 10:
        return max(txt[0] - img[2], 0.0)
    ho = _overlap(img[0], img[2], txt[0], txt[2])
    if ho > 0.3 * min(img[2] - img[0], txt[2] - txt[0]) and img[3] <= txt[1] + 10 and txt[1] - img[3] < 25:
        return max(txt[1] - img[3], 0.0)
    return float("inf")


def _elements(page):
    """Разделя текста на: групи с име/описание, промоционални цени, стари цени, бележка за дати."""
    groups, promos, olds, plus, page_note = [], [], [], [], None
    H = page.rect.height
    for b in page.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        cur = None
        for line in b["lines"]:
            spans = [s for s in line["spans"] if s["text"].strip()]
            if not spans:
                continue
            bbox = tuple(line["bbox"])
            text = _clean("".join(s["text"] for s in spans))
            price_spans = [s for s in spans if "Price" in s["font"]]
            if price_spans:
                m = PRICE_RE.search(_clean("".join(s["text"] for s in price_spans)).replace(" ", ""))
                if m:
                    cents = m.group(2) if m.group(2) and m.group(2) != "-" else "00"
                    pb = tuple(price_spans[0]["bbox"])
                    for s_ in price_spans[1:]:
                        pb = _union(pb, tuple(s_["bbox"]))
                    promos.append({"price": float(f"{m.group(1)}.{cents}"), "bbox": pb})
                spans = [s for s in spans if "Price" not in s["font"]]
                if not spans:
                    continue
                text = _clean("".join(s["text"] for s in spans))
            fonts = {s["font"] for s in spans}
            if all("CondPro" in f for f in fonts):
                bold = all("Bold" in f for f in fonts)
                if cur is None:
                    cur = {"lines": [], "bbox": bbox}
                    groups.append(cur)
                cur["lines"].append((text, bold))
                cur["bbox"] = _union(cur["bbox"], bbox)
                continue
            cur = None
            if "Lidl Plus" in text:
                plus.append(bbox)
            m = OLD_RE.match(text)
            if m:
                olds.append({"price": _f(m.group(2)), "pct": int(m.group(1)) if m.group(1) else None, "bbox": bbox})
            elif VALID_RE.match(text) and bbox[1] < H * 0.15:
                page_note = text
    # група без удебелено име или само с число/количество („1“, „2“ от „1+1“) не е артикул
    groups = [g for g in groups if any(b for _, b in g["lines"])
              and not QTY_ONLY_RE.match(" ".join(t for t, b in g["lines"] if b))]
    return groups, promos, olds, plus, page_note


def _match(left, right, key_l, key_r, max_d, dist=None):
    """Свързва елементи едно към едно, като започва от най-близките двойки."""
    dist = dist or _dist
    pairs = sorted((dist(key_l(a), key_r(b)), i, j) for i, a in enumerate(left) for j, b in enumerate(right))
    used_l, used_r, out = set(), set(), {}
    for d, i, j in pairs:
        if d > max_d:
            break
        if i in used_l or j in used_r:
            continue
        used_l.add(i)
        used_r.add(j)
        out[i] = j
    return out


def _item(group):
    bold = [t for t, b in group["lines"] if b]
    book = [t for t, b in group["lines"] if not b]
    brand_lines = []
    while len(bold) > 1 and _is_brand(bold[0]):
        brand_lines.append(bold.pop(0))
    notes = [l for l in book if NOTE_RE.match(l)]
    rest = [l for l in book if l not in notes]
    qty = next((l for l in rest if QTY_RE.search(l)), None)
    desc = [l for l in rest if l != qty][:2]
    brand = " ".join(brand_lines) or None
    name = " ".join(bold) + (", " + " ".join(desc) if desc else "")
    return {
        "name": _clean(((brand + " ") if brand else "") + name),
        "brand": brand,
        "quantity": qty,
        "note": "; ".join(notes) or None,
    }


def parse_page(page):
    W, H = page.rect.width, page.rect.height
    groups, promos, olds, plus, page_note = _elements(page)
    if not groups or not promos:
        return []
    g2p = _match(groups, promos, lambda g: g["bbox"], lambda p: p["bbox"], 0.35 * W, _not_above)
    items = []
    for gi, pi in g2p.items():
        it = _item(groups[gi])
        it.update(promo_price=promos[pi]["price"], regular_price=None, discount_pct=None)
        it["_bbox"] = _union(groups[gi]["bbox"], promos[pi]["bbox"])
        it["_price_bbox"] = promos[pi]["bbox"]
        items.append(it)
    if not items:
        return []

    # стандартна цена: най-близкият блок „Акция/-x%/x.xx €“ до промоционалната цена
    for bb in plus:  # „С Lidl Plus“ → цената е с приложението
        k = min(range(len(items)), key=lambda j: _dist(items[j]["_price_bbox"], bb))
        if _dist(items[k]["_price_bbox"], bb) < 0.15 * W and "с Lidl Plus" not in (items[k]["note"] or ""):
            items[k]["note"] = "; ".join(n for n in (items[k]["note"], "с Lidl Plus") if n)
    for ii, oi in _match(items, olds, lambda i: i["_price_bbox"], lambda o: o["bbox"], 0.12 * W).items():
        it, o = items[ii], olds[oi]
        if o["price"] > it["promo_price"]:
            it["regular_price"], it["discount_pct"] = o["price"], o["pct"]
            it["_bbox"] = _union(it["_bbox"], o["bbox"])

    return _finish(page, items, lambda it: page_note)


def _cell(i, items, W, H):
    """„Клетката“ на артикул: вляво/вдясно до съседите на същия ред, нагоре до артикула над нея.
    Ползва се, когато снимката не е отделно изображение – тя е някъде в клетката над/до текста."""
    x0, y0, x1, y1 = items[i]["_bbox"]
    others = [o["_bbox"] for j, o in enumerate(items) if j != i]
    same_row = [o for o in others if _overlap(y0, y1, o[1], o[3]) > 0.3 * min(y1 - y0, o[3] - o[1])]
    w = x1 - x0
    # встрани – до съседите, но не повече от ~ширината на текста (иначе хваща банери и купони)
    left = max([o[2] for o in same_row if o[2] <= x0 + 5] + [0.0, x0 - 1.6 * w])
    right = min([o[0] for o in same_row if o[0] >= x1 - 5] + [W, x1 + 0.35 * w])
    above = [o[3] for o in others if o[3] <= y0 + 5
             and _overlap(left, right, o[0], o[2]) > 0.25 * min(right - left, o[2] - o[0])]
    reach = min(0.33 * H, max(0.12 * H, 2.2 * (y1 - y0)))  # снимката е ~2 пъти по-висока от текста
    top = min(max(max(above) if above else 0.0, y0 - reach), y0)
    return (left, top, right, y1)


def _finish(page, items, note_for, grow_up=False):
    """Добавя снимките към артикулите и смята box в % от страницата.
    grow_up: снимките не са отделни изображения (Kaufland – цялата страница е една картинка),
    затова всеки артикул получава своята клетка (виж _cell)."""
    W, H = page.rect.width, page.rect.height
    if grow_up:
        cells = [_cell(i, items, W, H) for i in range(len(items))]
        for it, c in zip(items, cells):
            it["_bbox"] = c
    # снимката е вляво от текста или над него; всяка снимка – при един артикул
    boxes = [i["_bbox"] for i in items]
    got = set()
    for im in ([] if grow_up else page.get_image_info()):
        r = im["bbox"]
        r = (max(r[0], 0), max(r[1], 0), min(r[2], W), min(r[3], H))
        if r[2] - r[0] < 8 or r[3] - r[1] < 8 or (r[2] - r[0]) * (r[3] - r[1]) > 0.3 * W * H or r[3] - r[1] > 0.45 * H:
            continue
        best, best_d = None, (0.18 * W, 0.0)
        cx, cy = (r[0] + r[2]) / 2, (r[1] + r[3]) / 2
        for j, it in enumerate(items):
            t = it["_bbox"]
            # при равно разстояние (напр. снимката се застъпва с два текста) – по-близкият център
            d = (_side_dist(r, t), abs(cx - (t[0] + t[2]) / 2) + abs(cy - (t[1] + t[3]) / 2))
            if d[0] < 0.18 * W and (best is None or d < best_d):
                best, best_d = j, d
        if best is not None:
            boxes[best] = _union(boxes[best], r)
            got.add(best)

    # артикул без отделна снимка (снимката е част от голяма обща картинка) → цялата му клетка
    for i, it in enumerate(items):
        if i in got or grow_up:
            continue
        boxes[i] = _cell(i, items, W, H)

    out = []
    for it, (x0, y0, x1, y1) in zip(items, boxes):
        if QTY_ONLY_RE.match(it["name"]):  # „479 г / 900 мл“ не е име на артикул
            continue
        pad = 4
        x0, y0, x1, y1 = max(x0 - pad, 0), max(y0 - pad, 0), min(x1 + pad, W), min(y1 + pad, H)
        extra = note_for(it)
        it.pop("_bbox")
        it.pop("_price_bbox")
        if extra:
            it["note"] = "; ".join(n for n in (it["note"], extra) if n)
        if it["regular_price"]:
            calc = round(100 * (1 - it["promo_price"] / it["regular_price"]))
            if it["discount_pct"] is None or abs(it["discount_pct"] - calc) > 2:
                it["discount_pct"] = calc
        it["box"] = {"left": 100 * x0 / W, "top": 100 * y0 / H,
                     "width": 100 * (x1 - x0) / W, "height": 100 * (y1 - y0) / H}
        out.append(it)
    return out


# ---------------------------------------------------------------- Kaufland
# Цена: Kaufland-Heavy ~20 pt; стара цена: Kaufland-Bold ~10 pt над нея; отстъпка: „-“ „57“ „%“
# (Heavy ~26 pt); име: Kaufland-Bold+ ~9 pt; количество/описание под него: ~7 pt.
K_PRICE_RE = re.compile(r"^" + NUM + r"\*?$")
QTY_ONLY_RE = re.compile(r"^[\d\s.,/x×х+%-]*\d[\d\s.,/x×х+%-]*(г|гр|кг|мл|л|бр|g|kg|ml|l|см|м)?\.?(\s*[/,]\s*[\d.,]+\s*(г|гр|кг|мл|л|бр|g|kg|ml|l)\.?)*$", re.I)
K_NOTE_RE = re.compile(r"^(ОТ|ДО|САМО)\s.*\d{1,2}\.\d{1,2}", re.I)


def _k_lines(page):
    out = []
    for b in page.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        for l in b["lines"]:
            spans = [s for s in l["spans"] if s["text"].strip()]
            if spans:
                out.append({"text": _clean("".join(s["text"] for s in spans)), "font": spans[0]["font"],
                            "size": spans[0]["size"], "bbox": tuple(l["bbox"])})
    return out


def _not_above(name, price):
    """При Lidl цената е под името или до него – никога изцяло над него."""
    if price[3] < name[1] + 5:
        return float("inf")
    return _dist(name, price)


def _right_of(name, price):
    """При Kaufland цената е вдясно от името и/или под него – никога вляво или над него.
    Иначе съседен артикул може да „открадне“ цената (напр. краставици с цената на морковите)."""
    if price[0] < name[0] - 35 or price[3] < name[1] or price[1] - name[3] > 90:
        return float("inf")
    return max(price[0] - name[2], 0.0) + max(price[1] - name[3], 0.0)


def parse_kaufland_page(page):
    W = page.rect.width
    lines = _k_lines(page)
    promos, olds, pcts, names, notes = [], [], [], [], []
    for l in lines:
        t, f, sz, bb = l["text"], l["font"], l["size"], l["bbox"]
        m = K_PRICE_RE.match(t.replace(",", "."))
        if m and "Heavy" in f and sz >= 16:
            promos.append({"price": _f(m.group(1)), "bbox": bb})
        elif m and f.startswith("Kaufland-Bold") and not f.startswith("Kaufland-Bold+") and 8 <= sz <= 30:
            olds.append({"price": _f(m.group(1)), "bbox": bb})
        elif re.fullmatch(r"\d{1,2}", t) and "Heavy" in f and sz >= 20:
            pcts.append({"pct": int(t), "bbox": bb})
        elif f.startswith("Kaufland-Bold+") and sz > 11 and K_NOTE_RE.match(t):
            notes.append({"text": t, "bbox": bb})
        elif (f.startswith("Kaufland-Bold+") or "Medium" in f) and 5.5 <= sz <= 16:  # до 16 – „големи“ страници
            names.append(l)

    # име + редовете точно под него (същото ляво подравняване) = един артикул
    groups = []
    for l in sorted(names, key=lambda x: (x["bbox"][1], x["bbox"][0])):
        big = l["size"] >= 8.5 and l["font"].startswith("Kaufland-Bold+") and not QTY_ONLY_RE.match(l["text"])
        g = next((g for g in groups if abs(g["bbox"][0] - l["bbox"][0]) < 3
                  and -1 <= l["bbox"][1] - g["bbox"][3] < 5 and (not big or g["small"] == [])), None)
        if g is None:
            if not big:
                continue
            g = {"name": [], "small": [], "bbox": l["bbox"]}
            groups.append(g)
        (g["name"] if big else g["small"]).append(l)
        g["bbox"] = _union(g["bbox"], l["bbox"])
    groups = [g for g in groups if not QTY_ONLY_RE.match(" ".join(l["text"] for l in g["name"]))]
    if not groups or not promos:
        return []

    items = []
    for gi, pi in _match(groups, promos, lambda g: g["bbox"], lambda p: p["bbox"], 0.3 * W, _right_of).items():
        g, p = groups[gi], promos[pi]
        name = ""
        for l in g["name"]:
            name += l["text"] if name.endswith("-") else (" " + l["text"])
        qty = " ".join(l["text"] for l in g["small"] if l["font"].startswith("Kaufland-Bold+")) or None
        desc = [l["text"] for l in g["small"] if not l["font"].startswith("Kaufland-Bold+")]
        if desc:
            name += ", " + " ".join(desc[:3])
        items.append({"name": _clean(name), "brand": None, "quantity": qty, "note": None,
                      "promo_price": p["price"], "regular_price": None, "discount_pct": None,
                      "_bbox": _union(g["bbox"], p["bbox"]), "_price_bbox": p["bbox"]})
    if not items:
        return []
    for ii, oi in _match(items, olds, lambda i: i["_price_bbox"], lambda o: o["bbox"], 0.06 * W).items():
        if olds[oi]["price"] > items[ii]["promo_price"]:
            items[ii]["regular_price"] = olds[oi]["price"]
            items[ii]["_bbox"] = _union(items[ii]["_bbox"], olds[oi]["bbox"])
    for ii, ci in _match(items, pcts, lambda i: i["_price_bbox"], lambda c: c["bbox"], 0.08 * W).items():
        items[ii]["discount_pct"] = pcts[ci]["pct"]
        items[ii]["_bbox"] = _union(items[ii]["_bbox"], pcts[ci]["bbox"])

    def note_for(it):  # „ОТ ВТОРНИК 24.11.“ важи само за колоната под надписа
        x0 = it["_bbox"][0]
        hits = [n["text"] for n in notes if n["bbox"][0] - 25 <= x0 and n["bbox"][1] <= it["_bbox"][1]]
        return hits[-1] if hits else None

    return _finish(page, items, note_for, grow_up=True)


PARSERS = {"lidl": parse_page, "kaufland": parse_kaufland_page}


def parse_pdf(path, store="lidl"):
    """{номер на страница: [артикули]} за целия PDF."""
    fn = PARSERS[store]
    with pymupdf.open(path) as doc:
        return {i + 1: fn(p) for i, p in enumerate(doc)}
