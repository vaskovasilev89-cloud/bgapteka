import asyncio, json, os, sys, threading, tempfile, http.server, socketserver
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ["APTEKI_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
from pharma.extract import extract
from pharma.normalize import norm, valid_ean

# --- extractor: форми, видени на живите сайтове ---
sop = '<html><head><meta property="og:type" content="product"><script type="application/ld+json">[{"@context":"https://schema.org/","@type":"Product","name":"Тусипрес Сироп х200 мл Fortex","sku":"000000000030025699","brand":{"@type":"Brand","name":"FORTEX"},"offers":{"@type":"Offer","priceCurrency":"EUR","price":7.86,"availability":"https://schema.org/InStock"}}]</script></head><body><h1>x</h1></body></html>'
r = extract(sop, "u"); assert r["price"] == 7.86 and r["ean"] is None and r["in_stock"] == 1 and r["brand"] == "FORTEX", r
s366 = '<script type="application/ld+json"> { "@context": "http://schema.org", "@type": "Product", "sku": "4015672108608", "name": "Omron HN289", "brand": "Omron", "offers": { "@type": "Offer", "price": "28.94", "priceCurrency": "EUR", "availability": "http://schema.org/InStock" } }</script>'
r = extract(s366, "u"); assert r["price"] == 28.94 and r["ean"] == "4015672108608" and r["brand"] == "Omron", r
galen = '<head><meta property="og:type" content="product"><meta property="product:price:amount" content="7.75"><meta property="product:price:currency" content="EUR"><meta property="og:title" content="Тетатокс 0.5 мл х 10"></head><script type="application/ld+json">{"@type":"Product","name":"Тетатокс инжекционна суспензия 0.5 мл х 10 ампули ","sku":"15","description":"a\r\nb"}</script>'
r = extract(galen, "u"); assert r["price"] == 7.75 and r["name"].startswith("Тетатокс инжекционна"), r
framar = '<head><meta property="product:price:amount" content="4.55"><meta property="product:price:amount" content="16.50"></head><body><h1>БИЛКА ГЕЛ – ДЕЛИКАТЕН 280 мл</h1></body>'
r = extract(framar, "u"); assert r["price"] == 4.55 and r["name"] == "БИЛКА ГЕЛ – ДЕЛИКАТЕН 280 мл", r
subra = '<script type="application/ld+json">{"@type":"Product","name":"STREPSILS INTENSIVE мед и лимон х 16 таблетки | Subra","offers":{"@type":"Offer","price":"4,55","priceCurrency":"EUR"}}</script>'
r = extract(subra, "u"); assert r["price"] == 4.55 and r["name"].endswith("таблетки"), r
bgn = '<script type="application/ld+json">{"@graph":[{"@type":["Product"],"name":"X","offers":[{"price":"19.56","priceCurrency":"BGN"},{"price":"25","priceCurrency":"BGN"}]}]}</script>'
r = extract(bgn, "u"); assert r["price"] == 10.0 and r["currency"] == "EUR", r
assert extract("<html><h1>Категория</h1></html>", "u") is None
assert valid_ean("4015672108608") and not valid_ean("4015672108609")
assert norm("Нурофен 400мг х12") == norm("NUROFEN 400 mg x 12") == "nurofen 400 mg 12", norm("Нурофен 400мг х12")
print("extract OK")

# --- пълен цикъл срещу локален фалшив сайт ---
PRODUCTS = {f"/p/{i}": (f"Нурофен 400 мг х {12 if i%2 else 24} таблетки", 5 + i) for i in range(30)}
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        base = f"http://127.0.0.1:{PORT}"
        if self.path == "/sitemap.xml":
            body = f'<sitemapindex><sitemap><loc>{base}/sm-products.xml</loc></sitemap><sitemap><loc>{base}/sm-pages.xml</loc></sitemap></sitemapindex>'
        elif self.path == "/sm-products.xml":
            body = "<urlset>" + "".join(f"<url><loc>{base}{p}</loc></url>" for p in PRODUCTS) + f"<url><loc>{base}/category</loc></url></urlset>"
        elif self.path in PRODUCTS:
            n, p = PRODUCTS[self.path]
            body = '<script type="application/ld+json">' + json.dumps({"@type": "Product", "name": n, "offers": {"price": p, "priceCurrency": "EUR"}}) + "</script>"
        elif self.path.startswith("/api"):
            page = int(self.path.split("page=")[-1])
            items = [{"name": "Нурофен 400мг x12 табл.", "sku": "", "permalink": f"{base}/w/{page}", "prices": {"price": "399", "regular_price": "450", "currency_minor_unit": 2, "currency_code": "EUR"}, "is_in_stock": True, "images": [{"src": "i.jpg"}]}]
            self.send_response(200); self.send_header("x-wp-totalpages", "2"); self.send_header("x-wp-total", "2"); self.end_headers()
            self.wfile.write(json.dumps(items).encode()); return
        else:
            self.send_response(404); self.end_headers(); return
        self.send_response(200); self.end_headers(); self.wfile.write(body.encode())
srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H); PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

from pharma import sites, scraper, db
sites.SITES.clear()
sites.SITES.update({
    "a": {"title": "Аптека А", "base": f"http://127.0.0.1:{PORT}", "mode": "sitemap", "sitemaps": [f"http://127.0.0.1:{PORT}/sitemap.xml"], "child_filter": "products", "url_filter": r"/p/\d+$"},
    "w": {"title": "Аптека W", "base": f"http://127.0.0.1:{PORT}", "mode": "woo", "api": f"http://127.0.0.1:{PORT}/api"},
})
scraper.SITES = sites.SITES
asyncio.run(scraper.refresh(None, concurrency=4, delay=0))
print(scraper.STATUS)
assert scraper.STATUS["a"]["saved"] == 30 and scraper.STATUS["w"]["saved"] == 2

from fastapi.testclient import TestClient
from pharma import app as appmod
appmod.SITES = sites.SITES
c = TestClient(appmod.app)
res = c.get("/api/search", params={"q": "nurofen 400 12"}).json()
g0 = res["groups"][0]
print(res["offers"], "offers,", len(res["groups"]), "groups; first:", g0["name"], g0["min_price"], [(o["site_title"], o["price"]) for o in g0["offers"]][:4])
assert g0["offers"][0]["price"] == 3.99 and g0["offers"][0]["site_title"] == "Аптека W"
assert all(o["price"] >= 3.99 for o in g0["offers"])
assert c.get("/api/search", params={"q": "нуроф"}).json()["offers"] >= 30
st = c.get("/api/status").json(); assert st["sites"][0]["products"] == 30
assert c.get("/").status_code == 200
print("ALL OK")
