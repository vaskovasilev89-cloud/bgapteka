"""Качва локалната база в публичния сайт:  python push.py [аптека ...]"""
import sys
from pharma import push

if __name__ == "__main__":
    for site, msg in push.push(sys.argv[1:] or None):
        print(f"{site:<12} {msg}")
