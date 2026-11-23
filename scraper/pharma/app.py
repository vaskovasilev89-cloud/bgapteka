"""Уеб сайтът: търсачка + бутон за обновяване."""
import os
import threading

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import db, push, scraper
from .grouping import group_offers
from .sites import SITES

app = FastAPI(title="Сравни цени в аптеките")
_local = threading.local()


def get_con():
    if not hasattr(_local, "con"):
        _local.con = db.connect()
    return _local.con


# общата страница е в web/wwwroot (същата се ползва и от публичния ASP.NET сайт)
STATIC = os.path.join(os.path.dirname(__file__), "..", "..", "web", "wwwroot")

KEEP = ("site", "name", "price", "old_price", "currency", "url", "in_stock", "ean", "brand", "image", "updated_at")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


@app.get("/api/search")
def search(q: str = Query("", max_length=200), in_stock: bool = False, sites: str = ""):
    rows = db.search(get_con(), q)
    if in_stock:
        rows = [r for r in rows if r["in_stock"] != 0]
    if sites:
        allowed = set(sites.split(","))
        rows = [r for r in rows if r["site"] in allowed]
    groups = group_offers(rows)
    for g in groups:
        g["offers"] = [{**{k: o[k] for k in KEEP}, "site_title": SITES.get(o["site"], {}).get("title", o["site"])}
                       for o in g["offers"]]
    return {"query": q, "offers": len(rows), "groups": groups}


@app.get("/api/status")
def status():
    per_site, runs = db.stats(get_con())
    out = []
    for k, cfg in SITES.items():
        out.append({
            "site": k, "title": cfg["title"], "base": cfg["base"],
            "products": per_site.get(k, {}).get("products", 0),
            "last_update": per_site.get(k, {}).get("last_update"),
            "last_run": runs.get(k),
            "live": scraper.STATUS.get(k),
        })
    cfg = push.load_config()
    return {"running": scraper.is_running(), "sites": out, "canRefresh": True,
            "remote": cfg["remote_url"] if cfg["api_key"] else "", "push": push.STATUS}


class RefreshReq(BaseModel):
    sites: list[str] | None = None
    concurrency: int = 4
    limit: int | None = None


@app.post("/api/refresh")
def refresh(req: RefreshReq):
    ok = scraper.start_background(req.sites or None, concurrency=max(1, min(req.concurrency, 10)), limit=req.limit)
    return {"started": ok, "message": "Обновяването започна" if ok else "Вече тече обновяване"}


@app.post("/api/stop")
def stop():
    scraper.stop()
    return {"stopping": True}


@app.post("/api/push")
def push_now():
    if push.STATUS.get("state") == "running":
        return {"started": False, "message": "Вече се качва"}
    push.STATUS.update(state="running", message="започва качване")
    threading.Thread(target=push.push_safe, daemon=True).start()
    return {"started": True}
