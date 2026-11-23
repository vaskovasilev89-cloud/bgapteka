"""Качване на данните от локалната база в публичния сайт (ASP.NET Core на MonsterASP).

Настройки в scraper/config.json (НЕ се качва в git):
    {"remote_url": "https://bgapteka.runasp.net", "api_key": "...", "auto_push": true}
или чрез променливи на средата BGAPTEKA_URL и BGAPTEKA_APIKEY.
"""
import gzip
import json
import os
import time

from curl_cffi import requests

from . import db
from .sites import SITES

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.json")

STATUS = {"state": "idle", "message": ""}


def load_config() -> dict:
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    cfg["remote_url"] = (os.environ.get("BGAPTEKA_URL") or cfg.get("remote_url") or "").rstrip("/")
    cfg["api_key"] = os.environ.get("BGAPTEKA_APIKEY") or cfg.get("api_key") or ""
    cfg.setdefault("auto_push", False)
    return cfg


def site_payload(con, site: str) -> dict:
    rows = con.execute(
        "SELECT url, name, norm, price, old_price, in_stock, ean, brand, image, updated_at "
        "FROM products WHERE site=?", (site,)).fetchall()
    return {
        "site": site,
        "title": SITES[site]["title"],
        "base": SITES[site]["base"],
        "generated": time.time(),
        "products": [dict(r) for r in rows],
    }


def push(sites=None) -> list:
    cfg = load_config()
    if not cfg["remote_url"] or not cfg["api_key"]:
        raise RuntimeError("Липсват remote_url / api_key в scraper/config.json")
    con = db.connect()
    results = []
    for site in (sites or SITES):
        data = site_payload(con, site)
        if not data["products"]:
            results.append((site, "пропуснат – няма продукти в локалната база"))
            continue
        body = gzip.compress(json.dumps(data, ensure_ascii=False).encode("utf-8"))
        STATUS.update(state="running", message=f"качва {SITES[site]['title']} ({len(data['products'])} продукта)")
        r = requests.post(f"{cfg['remote_url']}/api/import/{site}", data=body, timeout=180,
                          headers={"X-Api-Key": cfg["api_key"], "Content-Type": "application/json",
                                   "Content-Encoding": "gzip"})
        if r.status_code != 200:
            raise RuntimeError(f"{site}: HTTP {r.status_code} {r.text[:200]}")
        results.append((site, f"качени {len(data['products'])} продукта"))
    con.close()
    STATUS.update(state="done", message="; ".join(f"{SITES[s]['title']}: {m}" for s, m in results))
    return results


def push_safe(sites=None):
    try:
        push(sites)
    except Exception as e:
        STATUS.update(state="error", message=f"грешка при качване: {e}")
