"""Групиране на едни и същи продукти от различни аптеки.

1) Еднакъв баркод (EAN) -> сигурно един и същ продукт.
2) Иначе: еднакви числа в името (доза/количество) + много сходно име (rapidfuzz).
"""
from rapidfuzz import fuzz

from .normalize import numbers

THRESHOLD = 82


def group_offers(rows: list) -> list:
    groups = []
    for r in rows:
        nums = numbers(r["norm"])
        target = None
        for g in groups:
            if r.get("ean") and r["ean"] in g["eans"]:
                target = g
                break
        if target is None:
            best = 0
            for g in groups:
                if g["nums"] != nums:
                    continue
                if r.get("ean") and g["eans"] and r["ean"] not in g["eans"]:
                    continue           # различни баркодове -> различни продукти
                score = fuzz.token_set_ratio(r["norm"], g["norm"])
                if score >= THRESHOLD and score > best:
                    best, target = score, g
        if target is None:
            target = {"name": r["name"], "norm": r["norm"], "nums": nums, "eans": set(),
                      "image": None, "offers": []}
            groups.append(target)
        if r.get("ean"):
            target["eans"].add(r["ean"])
        target["image"] = target["image"] or r.get("image")
        target["offers"].append(r)

    out = []
    for g in groups:
        offers = sorted(g["offers"], key=lambda o: (o["in_stock"] == 0, o["price"]))
        prices = [o["price"] for o in offers if o["in_stock"] != 0] or [o["price"] for o in offers]
        out.append({
            "name": g["name"],
            "image": g["image"],
            "eans": sorted(g["eans"]),
            "min_price": min(prices),
            "max_price": max(prices),
            "sites": len({o["site"] for o in offers}),
            "offers": offers,
        })
    return out
