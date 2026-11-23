"""Брошури на супермаркети (Kaufland, Lidl) → артикули с цени → публичния сайт.

    python flyers.py                         # брошурите от config.json → "flyers"
    python flyers.py URL [URL ...]           # конкретни брошури
    python flyers.py URL --pages 1-3         # само няколко страници (за проба)
    python flyers.py URL --no-push           # без качване в сайта
    python flyers.py URL --source claude     # разчитане на снимките с Claude (платено)

По подразбиране (--source pdf, безплатно) артикулите се взимат от текста в PDF-а на брошурата
(pharma/flyer_pdf.py): име, количество, промоционална и стандартна цена, отстъпка и къде на
страницата е артикулът (за изрязаната снимка в сайта).

С --source claude всяка страница (снимка) се изпраща на Claude (Anthropic API, нужен е
ANTHROPIC_API_KEY). Резултатът за всяка страница се пази в data/flyers/<брошура>/, така че при
повторно пускане вече разчетените страници не се плащат пак.
"""
import argparse
import base64
import gzip
import json
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import anthropic
from curl_cffi import requests

from pharma import flyer_pdf, push

MODEL = "claude-opus-5-5"
PRICE_IN, PRICE_OUT = 4.0, 20.0          # $ за 1M токена (claude-opus-5-5)
FLYER_API = "https://endpoints.leaflets.schwarz/v4/flyer"
DATA_DIR = os.path.join(os.path.dirname(__file__), "data", "flyers")

STORE_TITLES = {"kaufland": "Kaufland", "lidl": "Lidl"}

SYSTEM = """You extract product offers from one page of a Bulgarian supermarket weekly flyer.
Return every distinct product on the page that has a price. For each product:
- name: the product name as printed, in Bulgarian, with brand and variety (e.g. "Портокали", "Кафе на зърна Lavazza Qualità Rossa"). Do not put the pack size in the name.
- brand: the brand if one is printed, else null.
- quantity: pack size or unit as printed (e.g. "500 г", "2 кг в мрежа", "бр.", "кг", "4 x 1.5 л"), else null.
- promo_price: the current (large) price in EUR. If both EUR and BGN are printed, use EUR. Null only if no price is readable.
- regular_price: the crossed-out or "стара цена" price in EUR if printed, else null.
- discount_pct: the discount percentage shown, as a positive integer (e.g. 57 for "-57%"), else null.
- note: any condition that changes the price or the dates, e.g. "с Kaufland Card", "с Lidl Plus", "от вторник 24.11", "цена за 1 кг", "при покупка на 2 бр.", else null.
- box: bounding box covering the product photo together with its name and price tag, in percent of the page: left and width as percent of the page width, top and height as percent of the page height (0-100).
Skip decorations, QR codes, headers, store logos and anything without a price. A page with no priced products returns an empty list."""

NULLABLE_STR = {"anyOf": [{"type": "string"}, {"type": "null"}]}
NULLABLE_NUM = {"anyOf": [{"type": "number"}, {"type": "null"}]}
SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "brand": NULLABLE_STR,
                    "quantity": NULLABLE_STR,
                    "promo_price": NULLABLE_NUM,
                    "regular_price": NULLABLE_NUM,
                    "discount_pct": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                    "note": NULLABLE_STR,
                    "box": {
                        "type": "object",
                        "properties": {k: {"type": "number"} for k in ("left", "top", "width", "height")},
                        "required": ["left", "top", "width", "height"],
                        "additionalProperties": False,
                    },
                },
                "required": ["name", "brand", "quantity", "promo_price", "regular_price", "discount_pct", "note", "box"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

_usage_lock = threading.Lock()
USAGE = {"in": 0, "out": 0, "pages": 0}


def identifier(url: str) -> str:
    """https://leaflets.kaufland.com/bg-BG/BG_bg_KDZ_4200_BG48-LFT/view/flyer/page/1 → BG_bg_KDZ_4200_BG48-LFT
    https://www.lidl.bg/l/bg/broshura/23-11-29-11/view/flyer/page/1         → 23-11-29-11"""
    m = re.search(r"/([^/]+)/(?:view|ar)(?:/|$)", url)
    if not m:
        raise SystemExit(f"Не разпознавам адреса на брошурата: {url}")
    return m.group(1)


def load_flyer(url: str) -> dict:
    r = requests.get(FLYER_API, params={"flyer_identifier": identifier(url)},
                     headers={"Accept": "application/json"}, impersonate="chrome", timeout=60)
    r.raise_for_status()
    return r.json()["flyer"]


def store_of(flyer: dict) -> str:
    key = (flyer.get("clientLocale") or "").split("/")[0].lower()
    return key if re.fullmatch(r"[a-z0-9_-]{1,32}", key) else "store"


def analyze_page(client: anthropic.Anthropic, page: dict) -> list:
    img_url = page.get("zoom") or page["image"]
    img = requests.get(img_url, impersonate="chrome", timeout=60)
    img.raise_for_status()
    media = "image/png" if img.content[:4] == b"\x89PNG" else "image/jpeg"
    resp = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=SYSTEM,
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media,
                                         "data": base64.standard_b64encode(img.content).decode()}},
            {"type": "text", "text": f"Страница {page['number']} от брошурата. Извлечи артикулите."},
        ]}],
    )
    with _usage_lock:
        USAGE["in"] += resp.usage.input_tokens
        USAGE["out"] += resp.usage.output_tokens
        USAGE["pages"] += 1
    if resp.stop_reason == "refusal":
        raise RuntimeError(f"страница {page['number']}: отказ ({getattr(resp.stop_details, 'category', None)})")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError(f"страница {page['number']}: отговорът е отрязан (max_tokens)")
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)["items"]


def parse_pages(spec, total):
    if not spec:
        return list(range(1, total + 1))
    out = set()
    for part in spec.split(","):
        a, _, b = part.partition("-")
        out.update(range(int(a), int(b or a) + 1))
    return sorted(n for n in out if 1 <= n <= total)


def pdf_items(flyer, store, folder):
    """{страница: [артикули]} от PDF-а на брошурата (сваля го веднъж в папката на брошурата)."""
    if store not in flyer_pdf.PARSERS:
        raise SystemExit(f"Няма PDF парсер за „{store}“ – ползвай --source claude")
    path = os.path.join(folder, "flyer.pdf")
    if not os.path.exists(path):
        r = requests.get(flyer.get("hiResPdfUrl") or flyer["pdfUrl"], impersonate="chrome", timeout=600)
        r.raise_for_status()
        with open(path, "wb") as f:
            f.write(r.content)
    parsed = flyer_pdf.parse_pdf(path, store)
    if len(parsed) != len(flyer["pages"]):
        print(f"  внимание: PDF-ът има {len(parsed)} страници, а брошурата {len(flyer['pages'])}", file=sys.stderr)
    return parsed


def process(url: str, pages_spec=None, workers=4, do_push=True, source="pdf"):
    flyer = load_flyer(url)
    store = store_of(flyer)
    title = STORE_TITLES.get(store, flyer.get("name") or store)
    fid = flyer["id"]
    folder = os.path.join(DATA_DIR, fid)
    os.makedirs(folder, exist_ok=True)
    pages = {p["number"]: p for p in flyer["pages"]}
    wanted = parse_pages(pages_spec, len(flyer["pages"]))
    print(f"{title}: {flyer['offerStartDate']} – {flyer['offerEndDate']}, {len(flyer['pages'])} страници "
          f"({len(wanted)} за обработка)")

    def cache(n):
        return os.path.join(folder, f"p{n:03}.json")

    if source == "pdf":
        page_items = {n: v for n, v in pdf_items(flyer, store, folder).items() if n in pages and n in wanted}
        for n, v in sorted(page_items.items()):
            if v:
                print(f"  стр. {n:>3}: {len(v)} артикула")
    else:
        page_items = claude_items(pages, wanted, folder, cache, workers)
    build_and_push(flyer, url, store, title, fid, folder, pages, page_items, do_push)


def claude_items(pages, wanted, folder, cache, workers):
    todo = [n for n in wanted if not os.path.exists(cache(n))]
    client = anthropic.Anthropic() if todo else None
    errors = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(analyze_page, client, pages[n]): n for n in todo}
        for fut in as_completed(futs):
            n = futs[fut]
            try:
                items = fut.result()
                with open(cache(n), "w", encoding="utf-8") as f:
                    json.dump(items, f, ensure_ascii=False, indent=1)
                print(f"  стр. {n:>3}: {len(items)} артикула")
            except Exception as e:  # една лоша страница не спира останалите
                errors.append(n)
                print(f"  стр. {n:>3}: ГРЕШКА {e}", file=sys.stderr)

    if errors:
        print(f"  грешки на страници {sorted(errors)}", file=sys.stderr)
    # всички разчетени страници (и от предишни пускания) отиват в сайта
    out = {}
    for n in sorted(pages):
        if os.path.exists(cache(n)):
            with open(cache(n), encoding="utf-8") as f:
                out[n] = json.load(f)
    return out


def build_and_push(flyer, url, store, title, fid, folder, pages, page_items, do_push):
    items = []
    for n in sorted(page_items):
        p = pages[n]
        for it in page_items[n]:
            box = it.get("box") or {}
            items.append({
                "page": n, "name": it["name"], "brand": it.get("brand"), "quantity": it.get("quantity"),
                "promo_price": it.get("promo_price"), "regular_price": it.get("regular_price"),
                "discount_pct": it.get("discount_pct"), "note": it.get("note"),
                "page_image": p["image"], "page_ratio": p["height"] / p["width"],
                "box_left": box.get("left", 0), "box_top": box.get("top", 0),
                "box_width": box.get("width", 100), "box_height": box.get("height", 100),
            })
    payload = {
        "flyer_id": fid, "store": store, "store_title": title,
        "title": flyer.get("title") or flyer.get("name") or "", "url": url,
        "valid_from": flyer["offerStartDate"], "valid_to": flyer["offerEndDate"],
        "pages": len(flyer["pages"]), "items": items,
    }
    with open(os.path.join(folder, "flyer.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"  общо {len(items)} артикула")

    if do_push and items:
        cfg = push.load_config()
        if not cfg["remote_url"] or not cfg["api_key"]:
            print("  качването не е настроено (config.json: remote_url, api_key)")
            return
        body = gzip.compress(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        r = requests.post(f"{cfg['remote_url']}/api/flyers/import", data=body, timeout=180,
                          headers={"X-Api-Key": cfg["api_key"], "Content-Type": "application/json",
                                   "Content-Encoding": "gzip"})
        print(f"  качване в сайта: HTTP {r.status_code} {r.text[:200]}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("urls", nargs="*", help="адреси на брошури (по подразбиране: config.json → flyers)")
    ap.add_argument("--pages", help="само тези страници, напр. 1-3,7")
    ap.add_argument("--workers", type=int, default=4, help="паралелни заявки към Claude")
    ap.add_argument("--no-push", action="store_true", help="не качва в сайта")
    ap.add_argument("--source", choices=["pdf", "claude"], default="pdf",
                    help="pdf: от текста на PDF-а (безплатно, по подразбиране); claude: от снимките (платено)")
    a = ap.parse_args()
    urls = a.urls or push.load_config().get("flyers") or []
    if not urls:
        raise SystemExit("Няма брошури: подай адреси или добави \"flyers\": [...] в config.json")
    try:
        for u in urls:
            process(u, a.pages, a.workers, not a.no_push, a.source)
    finally:
        if USAGE["pages"]:
            cost = USAGE["in"] / 1e6 * PRICE_IN + USAGE["out"] / 1e6 * PRICE_OUT
            print(f"Claude: {USAGE['pages']} страници, {USAGE['in']:,} входни и {USAGE['out']:,} изходни токена "
                  f"≈ ${cost:.2f}")
