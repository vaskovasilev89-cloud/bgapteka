"""Изтегляне на продуктите от всички аптеки.

Ползва curl_cffi (имитира TLS отпечатъка на Chrome), защото част от сайтовете
(SOpharmacy, 36.6, Ремедиум, Farmavision) връщат грешка на "голи" HTTP клиенти.
"""
import asyncio
import gzip
import html
import random
import re
import threading
import time
import uuid

from curl_cffi.requests import AsyncSession

from . import db
from .extract import extract
from .normalize import valid_ean
from .sites import SITES

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "bg-BG,bg;q=0.9,en;q=0.8",
}

# Състояние за UI-то
STATUS: dict = {}
_stop = threading.Event()
_runner_lock = threading.Lock()
_runner_thread = None


def _st(site, **kw):
    STATUS.setdefault(site, {}).update(kw)


class SiteScraper:
    def __init__(self, key, cfg, con, concurrency=4, delay=0.3, limit=None):
        self.key, self.cfg, self.con = key, cfg, con
        self.concurrency, self.delay, self.limit = concurrency, delay, limit
        self.run_id = uuid.uuid4().hex
        self.buffer, self.saved, self.errors, self.done = [], 0, 0, 0
        self.abort = False
        # качване на части в публичния сайт по време на обхождането (config.json: auto_push, push_every)
        from . import push
        cfg_push = push.load_config()
        self.push_every = int(cfg_push.get("push_every", 1000) or 0)             if cfg_push.get("auto_push") and cfg_push.get("remote_url") and cfg_push.get("api_key") and not limit else 0
        self.pushed_at = 0
        self.push_task = None

    # ---------- HTTP ----------
    async def get(self, s, url, *, binary=False, tries=4):
        last = None
        for attempt in range(tries):
            if _stop.is_set():
                raise asyncio.CancelledError()
            try:
                r = await s.get(url, headers=HEADERS, timeout=40, allow_redirects=True)
                if r.status_code == 200:
                    return r.content if binary else r.text
                last = f"HTTP {r.status_code}"
                if r.status_code in (404, 410):
                    return None
                if r.status_code == 403 and attempt >= 1:
                    raise RuntimeError(f"{url}: HTTP 403 (сайтът блокира заявката)")
            except (RuntimeError, asyncio.CancelledError):
                raise
            except Exception as e:  # мрежова грешка
                last = str(e)[:120]
            await asyncio.sleep(2 ** attempt + random.random())
        raise RuntimeError(f"{url}: {last}")

    # ---------- sitemap ----------
    async def sitemap_urls(self, s):
        cfg = self.cfg
        child_re = re.compile(cfg.get("child_filter", ".*"))
        url_re = re.compile(cfg.get("url_filter", ".*"))
        seen_maps, found = set(), []
        queue = list(cfg["sitemaps"])

        async def load(sm):
            data = await self.get(s, sm, binary=True)
            if not data:
                return ""
            if data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
            return data.decode("utf-8", "replace")

        tried_robots = False
        while True:
            while queue:
                sm = queue.pop(0)
                if sm in seen_maps:
                    continue
                seen_maps.add(sm)
                try:
                    text = await load(sm)
                except RuntimeError as e:
                    _st(self.key, message=str(e))
                    continue
                locs = [html.unescape(m) for m in re.findall(r"<loc>\s*(?:<!\[CDATA\[)?\s*([^<\s\]]+)", text)]
                if "<sitemapindex" in text:
                    queue += [l for l in locs if child_re.search(l)]
                else:
                    found += [l for l in locs if url_re.search(l)]
                _st(self.key, message=f"sitemap: {len(found)} продукта намерени")
            if found or tried_robots or not cfg.get("use_robots"):
                break
            tried_robots = True
            robots = await self.get(s, cfg["base"] + "/robots.txt") or ""
            queue += re.findall(r"(?im)^\s*sitemap:\s*(\S+)", robots)

        found = list(dict.fromkeys(found))
        return found[: self.limit] if self.limit else found

    # ---------- запис ----------
    def flush(self, force=False):
        if self.buffer and (force or len(self.buffer) >= 200):
            db.save_products(self.con, self.key, self.buffer, self.run_id)
            self.saved += len(self.buffer)
            self.buffer = []
            _st(self.key, saved=self.saved)
            self.maybe_push()

    def maybe_push(self):
        """На всеки push_every нови продукта качва всичко събрано досега (по едно качване наведнъж)."""
        if not self.push_every or self.saved - self.pushed_at < self.push_every:
            return
        if self.push_task and not self.push_task.done():
            return
        from . import push
        self.pushed_at = self.saved
        _st(self.key, message=f"качва {self.saved} продукта в сайта")
        self.push_task = asyncio.ensure_future(asyncio.to_thread(push.push_safe, [self.key]))

    async def wait_push(self):
        if self.push_task:
            await asyncio.gather(self.push_task, return_exceptions=True)

    # ---------- режими ----------
    async def run_sitemap(self, s):
        urls = await self.sitemap_urls(s)
        if not urls:
            raise RuntimeError("не бяха намерени продуктови URL-и в sitemap")
        _st(self.key, found=len(urls), message="чете продуктовите страници")
        sem = asyncio.Semaphore(self.concurrency)

        async def one(u):
            async with sem:
                if _stop.is_set() or self.abort:
                    return
                # предпазител: ако сайтът ни блокира изцяло, не продължаваме с хиляди грешки
                if self.errors >= 40 and self.saved + len(self.buffer) == 0:
                    self.abort = True
                    return
                try:
                    page = await self.get(s, u)
                    item = extract(page, u) if page else None
                    if item:
                        self.buffer.append(item)
                        self.flush()
                except RuntimeError:
                    self.errors += 1
                finally:
                    self.done += 1
                    if self.done % 20 == 0:
                        _st(self.key, done=self.done, errors=self.errors)
                await asyncio.sleep(self.delay)

        await asyncio.gather(*(one(u) for u in urls), return_exceptions=True)
        if self.abort:
            raise RuntimeError(f"сайтът отказва достъп ({self.errors} поредни грешки) – виж README, раздел „Блокиране“")
        return len(urls)

    async def run_woo(self, s):
        api, page, pages, total = self.cfg["api"], 1, None, 0
        while pages is None or page <= pages:
            if _stop.is_set():
                break
            r = await s.get(f"{api}?per_page=100&page={page}", headers={"Accept": "application/json"}, timeout=60)
            if r.status_code != 200:
                raise RuntimeError(f"WooCommerce API: HTTP {r.status_code}")
            if pages is None:
                pages = int(r.headers.get("x-wp-totalpages") or 1)
                total = int(r.headers.get("x-wp-total") or 0)
                if self.limit:
                    pages = min(pages, max(1, self.limit // 100))
                _st(self.key, found=total)
            for p in r.json():
                pr = p.get("prices") or {}
                mu = int(pr.get("currency_minor_unit") or 2)
                try:
                    price = int(pr.get("price")) / 10 ** mu
                except (TypeError, ValueError):
                    continue
                if price <= 0:
                    continue
                reg = pr.get("regular_price")
                old = int(reg) / 10 ** mu if reg and str(reg).isdigit() else None
                sku = str(p.get("sku") or "")
                brands = p.get("brands") or []
                self.buffer.append({
                    "url": p.get("permalink"),
                    "name": html.unescape(re.sub(r"<[^>]+>", "", p.get("name") or "")),
                    "price": round(price, 2),
                    "old_price": old if old and old > price else None,
                    "currency": pr.get("currency_code") or "EUR",
                    "in_stock": 1 if p.get("is_in_stock") else 0,
                    "ean": sku if valid_ean(sku) else None,
                    "brand": brands[0].get("name") if brands and isinstance(brands[0], dict) else None,
                    "image": (p.get("images") or [{}])[0].get("src"),
                })
                self.done += 1
            self.flush()
            _st(self.key, done=self.done)
            page += 1
            await asyncio.sleep(self.delay)
        return self.done

    async def run(self):
        t0 = time.time()
        _st(self.key, state="running", found=0, done=0, saved=0, errors=0, started=t0, finished=None, message="старт")
        db.set_run(self.con, self.key, started=t0, finished=None, message="running")
        try:
            async with AsyncSession(impersonate="chrome") as s:
                if self.cfg["mode"] == "woo":
                    found = await self.run_woo(s)
                else:
                    found = await self.run_sitemap(s)
            self.flush(force=True)
            await self.wait_push()   # частичното качване не бива да изпревари последното
            if not _stop.is_set() and not self.limit:
                db.remove_stale(self.con, self.key, self.run_id, self.saved)
            state = "stopped" if _stop.is_set() else "done"
            msg = f"готово: {self.saved} продукта" + (f", {self.errors} грешки" if self.errors else "")
        except Exception as e:
            self.flush(force=True)
            await self.wait_push()
            found, state, msg = self.done, "error", f"грешка: {e}"
        t1 = time.time()
        _st(self.key, state=state, done=self.done, saved=self.saved, errors=self.errors, finished=t1, message=msg)
        db.set_run(self.con, self.key, finished=t1, found=found, saved=self.saved, errors=self.errors, message=msg)


async def refresh(sites=None, concurrency=4, delay=0.3, limit=None):
    con = db.connect()
    keys = [k for k in (sites or SITES) if k in SITES]
    for k in keys:
        _st(k, state="queued", message="чака")
    await asyncio.gather(*(SiteScraper(k, SITES[k], con, concurrency, delay, limit).run() for k in keys))
    con.close()
    # автоматично качване в публичния сайт (ако е включено в config.json)
    from . import push
    if push.load_config().get("auto_push") and not limit:
        ok = [k for k in keys if STATUS.get(k, {}).get("state") == "done"]
        if ok:
            await asyncio.to_thread(push.push_safe, ok)


def is_running() -> bool:
    return _runner_thread is not None and _runner_thread.is_alive()


def start_background(sites=None, **kw) -> bool:
    """Пуска обновяването в отделна нишка (за бутона в сайта)."""
    global _runner_thread
    with _runner_lock:
        if is_running():
            return False
        _stop.clear()
        _runner_thread = threading.Thread(target=lambda: asyncio.run(refresh(sites, **kw)), daemon=True)
        _runner_thread.start()
        return True


def stop():
    _stop.set()
