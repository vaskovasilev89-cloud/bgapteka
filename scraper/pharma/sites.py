"""Описание на аптеките и как се намират продуктите им.

mode = "sitemap": продуктовите URL-и се взимат от sitemap, после всяка страница се чете
                  и цената се извлича от JSON-LD / meta тагове (виж extract.py).
mode = "woo":     WooCommerce Store API (JSON, 100 продукта на заявка) – много по-бързо.
"""

SITES = {
    "sopharmacy": {
        "title": "SOpharmacy",
        "base": "https://sopharmacy.bg",
        "mode": "sitemap",
        "sitemaps": ["https://sopharmacy.bg/bg/sitemap.xml"],
        "child_filter": r"sitemap-products",          # кои под-sitemap-и да се ползват
        "url_filter": r"/bg/product/",                  # кои URL-и са продукти
    },
    "remedium": {
        "title": "Ремедиум",
        "base": "https://remedium.bg",
        "mode": "sitemap",
        "sitemaps": ["https://remedium.bg/media/sitemap/sitemap.xml", "https://remedium.bg/sitemap.xml"],
        "child_filter": r".*",
        "url_filter": r"^https://remedium\.bg/[^?#]+$",
        "use_robots": True,                              # ако горните не работят – търси в robots.txt
    },
    "galen": {
        "title": "Гален",
        "base": "https://galen.bg",
        "mode": "sitemap",
        "sitemaps": ["https://galen.bg/media/sitemap_products.xml"],
        "child_filter": r".*",
        "url_filter": r"^https://galen\.bg/[^/?#]+$",
    },
    "366": {
        "title": "Аптеки 36.6",
        "base": "https://366.bg",
        "mode": "sitemap",
        "sitemaps": ["https://366.bg/sitemap/sitemap-bg.xml"],
        "child_filter": r"sitemap-products-",
        "url_filter": r"/product/",
    },
    "benu": {
        "title": "BENU",
        "base": "https://benu.bg",
        "mode": "sitemap",
        "sitemaps": ["https://benu.bg/sitemap.products.xml"],
        "child_filter": r"products",
        "url_filter": r"^https://benu\.bg/[^/?#]+$",
    },
    "farmavision": {
        "title": "Farmavision",
        "base": "https://farmavision.bg",
        "mode": "woo",
        "api": "https://farmavision.bg/wp-json/wc/store/v1/products",
    },
    "framar": {
        "title": "Framar",
        "base": "https://apteka.framar.bg",
        "mode": "sitemap",
        "sitemaps": ["https://apteka.framar.bg/sitemap.xml"],
        "child_filter": r"apteka_produkti(\d*)\.xml$",   # без apteka_produkti_arhiv (спрени продукти)
        "url_filter": r"^https://apteka\.framar\.bg/\d+/?",
    },
    "subra": {
        "title": "Subra",
        "base": "https://subra.bg",
        "mode": "sitemap",
        "sitemaps": ["https://subra.bg/bg/sitemap/products"],
        "child_filter": r"products",
        "url_filter": r"subra\.bg/bg/.+/\d+/\d+$",
    },
}
