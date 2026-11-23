"""Извличане на име / цена / баркод от продуктова страница.

Ред на източниците (проверен на живо на сайтовете, ноември 2026):
  1. JSON-LD schema.org Product (SOpharmacy, 36.6, Гален, BENU, Framar, Subra, Farmavision)
  2. <meta property="product:price:amount"> – ВЗИМА СЕ ПЪРВИЯТ (Framar има и мета-тагове
     на "подобни продукти" по-надолу в страницата)
  3. itemprop="price" / Magento data-price-amount
"""
import json
import re
from typing import Optional

from lxml import html as lxml_html

from .normalize import valid_ean

BGN_PER_EUR = 1.95583


def _to_float(v) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("\xa0", "").replace(" ", "")
    s = re.sub(r"[^\d.,]", "", s)
    if not s:
        return None
    if "," in s and "." in s:          # 1.234,56 или 1,234.56
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    else:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _types(node) -> list:
    t = node.get("@type", [])
    return t if isinstance(t, list) else [t]


def _walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _find_ld_product(doc):
    """Първо Product с offers; ако няма – какъвто и да е Product (за името)."""
    fallback = None
    for s in doc.xpath('//script[@type="application/ld+json"]'):
        raw = (s.text or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            try:   # някои сайтове слагат управляващи символи в описанието
                data = json.loads(re.sub(r"[\x00-\x1f]", " ", raw))
            except ValueError:
                continue
        for node in _walk(data):
            if "Product" in _types(node):
                if node.get("offers") is not None:
                    return node
                fallback = fallback or node
    return fallback


def _offer_price(offers):
    """Връща (цена, валута, наличност) от offers (dict, list или AggregateOffer)."""
    if isinstance(offers, list):
        best = None
        for o in offers:
            r = _offer_price(o)
            if r[0] is not None and (best is None or r[0] < best[0]):
                best = r
        return best or (None, None, None)
    if not isinstance(offers, dict):
        return None, None, None
    price = _to_float(offers.get("price"))
    if price is None:
        price = _to_float(offers.get("lowPrice"))
    if price is None:
        spec = offers.get("priceSpecification")
        if isinstance(spec, list) and spec:
            spec = spec[0]
        if isinstance(spec, dict):
            price = _to_float(spec.get("price"))
    cur = offers.get("priceCurrency")
    av = str(offers.get("availability") or "")
    stock = None
    if "InStock" in av or "LimitedAvailability" in av or "OnlineOnly" in av:
        stock = 1
    elif "OutOfStock" in av or "Discontinued" in av or "SoldOut" in av:
        stock = 0
    return price, cur, stock


def _meta(doc, *names):
    for n in names:
        r = doc.xpath(f'//meta[@property="{n}" or @name="{n}" or @itemprop="{n}"]/@content')
        if r and r[0].strip():
            return r[0].strip()
    return None


_SUFFIX = re.compile(r"\s*\|\s*[^|]{1,30}$")


def _clean_name(name: str) -> str:
    name = re.sub(r"\s+", " ", name or "").strip()
    # "Стрепсилс ... | Subra" -> "Стрепсилс ..."
    if " | " in name:
        cleaned = _SUFFIX.sub("", name)
        if len(cleaned) > 8:
            name = cleaned
    return name


def extract(page: str, url: str) -> Optional[dict]:
    try:
        doc = lxml_html.fromstring(page)
    except Exception:
        return None

    name = price = currency = stock = ean = brand = image = None
    ld = _find_ld_product(doc)
    if ld:
        name = ld.get("name")
        price, currency, stock = _offer_price(ld.get("offers"))
        for k in ("gtin13", "gtin", "gtin8", "ean", "gtin12", "sku", "mpn"):
            v = str(ld.get(k) or "").strip()
            if valid_ean(v):
                ean = v
                break
        b = ld.get("brand")
        brand = b.get("name") if isinstance(b, dict) else (b if isinstance(b, str) else None)
        img = ld.get("image")
        if isinstance(img, list):
            img = img[0] if img else None
        if isinstance(img, dict):
            img = img.get("url")
        image = img

    if price is None:
        price = _to_float(_meta(doc, "product:price:amount", "og:price:amount"))
        currency = currency or _meta(doc, "product:price:currency", "og:price:currency")
    if price is None:
        r = doc.xpath('//*[@itemprop="price"]')
        if r:
            price = _to_float(r[0].get("content") or r[0].text_content())
    if price is None:   # Magento
        r = doc.xpath('//*[@data-price-type="finalPrice"]/@data-price-amount')
        if r:
            price = _to_float(r[0])

    is_product = bool(ld) or _meta(doc, "og:type") in ("product", "og:product") \
        or _meta(doc, "product:price:amount") is not None
    if not is_product or not price or price <= 0:
        return None

    if not name:
        name = _meta(doc, "og:title")
    if not name:
        h1 = doc.xpath("//h1")
        name = h1[0].text_content() if h1 else None
    if not name:
        return None
    image = image or _meta(doc, "og:image")
    if not ean:
        for k in ("gtin13", "gtin", "ean"):
            v = (_meta(doc, k) or "").strip()
            if valid_ean(v):
                ean = v
                break

    currency = (currency or "EUR").upper()
    if currency in ("BGN", "ЛВ", "LV"):
        price = round(price / BGN_PER_EUR, 2)
        currency = "EUR"

    return {
        "url": url,
        "name": _clean_name(str(name)),
        "price": round(float(price), 2),
        "currency": currency,
        "in_stock": stock,
        "ean": ean,
        "brand": (brand or None),
        "image": image if isinstance(image, str) else None,
    }
