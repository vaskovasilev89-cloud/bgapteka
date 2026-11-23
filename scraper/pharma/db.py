"""SQLite база с пълнотекстово търсене (FTS5)."""
import os
import sqlite3
import threading
import time

from .normalize import norm

DB_PATH = os.environ.get("APTEKI_DB", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "apteki.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS products(
    url        TEXT PRIMARY KEY,
    site       TEXT NOT NULL,
    name       TEXT NOT NULL,
    norm       TEXT NOT NULL,
    price      REAL NOT NULL,
    old_price  REAL,
    currency   TEXT,
    ean        TEXT,
    brand      TEXT,
    image      TEXT,
    in_stock   INTEGER,
    updated_at REAL NOT NULL,
    run_id     TEXT
);
CREATE INDEX IF NOT EXISTS ix_products_site ON products(site);
CREATE INDEX IF NOT EXISTS ix_products_ean  ON products(ean);

CREATE VIRTUAL TABLE IF NOT EXISTS products_fts USING fts5(
    norm, content='products', content_rowid='rowid', tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS products_ai AFTER INSERT ON products BEGIN
    INSERT INTO products_fts(rowid, norm) VALUES (new.rowid, new.norm);
END;
CREATE TRIGGER IF NOT EXISTS products_ad AFTER DELETE ON products BEGIN
    INSERT INTO products_fts(products_fts, rowid, norm) VALUES ('delete', old.rowid, old.norm);
END;
CREATE TRIGGER IF NOT EXISTS products_au AFTER UPDATE OF norm ON products BEGIN
    INSERT INTO products_fts(products_fts, rowid, norm) VALUES ('delete', old.rowid, old.norm);
    INSERT INTO products_fts(rowid, norm) VALUES (new.rowid, new.norm);
END;

CREATE TABLE IF NOT EXISTS runs(
    site TEXT PRIMARY KEY, started REAL, finished REAL, found INTEGER, saved INTEGER, errors INTEGER, message TEXT
);
"""

_lock = threading.Lock()


def connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    return con


def save_products(con, site: str, items: list, run_id: str):
    now = time.time()
    rows = [(
        it["url"], site, it["name"], norm(it["name"] + " " + (it.get("brand") or "")),
        it["price"], it.get("old_price"), it.get("currency") or "EUR", it.get("ean"),
        it.get("brand"), it.get("image"), it.get("in_stock"), now, run_id,
    ) for it in items]
    with _lock:
        con.executemany("""
            INSERT INTO products(url, site, name, norm, price, old_price, currency, ean, brand, image, in_stock, updated_at, run_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(url) DO UPDATE SET
                site=excluded.site, name=excluded.name, norm=excluded.norm, price=excluded.price,
                old_price=excluded.old_price, currency=excluded.currency,
                ean=COALESCE(excluded.ean, products.ean), brand=excluded.brand,
                image=COALESCE(excluded.image, products.image), in_stock=excluded.in_stock,
                updated_at=excluded.updated_at, run_id=excluded.run_id
        """, rows)
        con.commit()


def remove_stale(con, site: str, run_id: str, seen: int):
    """След пълно обновяване махаме продуктите, които вече ги няма на сайта.
    Предпазно: само ако сме намерили поне 70% от досегашния брой."""
    with _lock:
        before = con.execute("SELECT COUNT(*) FROM products WHERE site=?", (site,)).fetchone()[0]
        if before and seen >= 0.7 * before:
            con.execute("DELETE FROM products WHERE site=? AND (run_id IS NULL OR run_id<>?)", (site, run_id))
            con.commit()


def set_run(con, site, **kw):
    with _lock:
        con.execute("INSERT OR IGNORE INTO runs(site) VALUES (?)", (site,))
        sets = ", ".join(f"{k}=?" for k in kw)
        con.execute(f"UPDATE runs SET {sets} WHERE site=?", (*kw.values(), site))
        con.commit()


def search(con, q: str, limit: int = 400):
    tokens = [t for t in norm(q).split() if t]
    if not tokens:
        return []
    fts = " ".join('"' + t.replace('"', "") + '"*' for t in tokens)
    sql = """
        SELECT p.*, bm25(products_fts) AS rank
        FROM products_fts JOIN products p ON p.rowid = products_fts.rowid
        WHERE products_fts MATCH ?
        ORDER BY rank LIMIT ?
    """
    rows = con.execute(sql, (fts, limit)).fetchall()
    # ако EAN е въведен директно
    if len(tokens) == 1 and tokens[0].isdigit() and len(tokens[0]) in (8, 13):
        rows = con.execute("SELECT p.*, 0 AS rank FROM products p WHERE ean=?", (tokens[0],)).fetchall() or rows
    return [dict(r) for r in rows]


def stats(con):
    per_site = {r["site"]: dict(r) for r in con.execute(
        "SELECT site, COUNT(*) AS products, MAX(updated_at) AS last_update FROM products GROUP BY site")}
    runs = {r["site"]: dict(r) for r in con.execute("SELECT * FROM runs")}
    return per_site, runs
