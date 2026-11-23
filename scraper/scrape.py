"""Обновяване от командния ред.

    python scrape.py                     # всички аптеки
    python scrape.py galen benu          # само избрани
    python scrape.py --limit 50 subra    # бърз тест: първите 50 продукта
"""
import argparse
import asyncio
import threading
import time

from pharma import scraper
from pharma.sites import SITES


def printer(stop):
    while not stop.is_set():
        line = " | ".join(f"{k}: {v.get('done', 0)}/{v.get('found', '?')} ({v.get('state')})"
                          for k, v in scraper.STATUS.items())
        print("\r" + line[:220], end="", flush=True)
        time.sleep(2)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("sites", nargs="*", help="ключове: " + ", ".join(SITES))
    ap.add_argument("--concurrency", type=int, default=4, help="паралелни заявки към един сайт (по подразбиране 4)")
    ap.add_argument("--delay", type=float, default=0.3, help="пауза след всяка заявка, сек.")
    ap.add_argument("--limit", type=int, default=None, help="максимум продукти на сайт (за тест)")
    a = ap.parse_args()
    bad = [s for s in a.sites if s not in SITES]
    if bad:
        raise SystemExit(f"Непознат сайт: {bad}. Възможни: {', '.join(SITES)}")
    stop = threading.Event()
    threading.Thread(target=printer, args=(stop,), daemon=True).start()
    try:
        asyncio.run(scraper.refresh(a.sites or None, a.concurrency, a.delay, a.limit))
    except KeyboardInterrupt:
        scraper.stop()
    stop.set()
    print()
    for k, v in scraper.STATUS.items():
        print(f"{SITES[k]['title']:<14} {v.get('message', '')}")
