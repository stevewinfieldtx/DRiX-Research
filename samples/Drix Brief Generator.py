"""
Drix pre-meeting brief prototype — WorldMonitor technique, zero paid APIs.

WorldMonitor's 'curated feeds' are parameterized Google News RSS queries plus a
few institutional RSS feeds (Fed, SEC). This script reuses that exact pattern
but adds a TARGET layer the dashboard doesn't have: the prospect itself.
"""
import json
import sys
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) DrixBrief/0.1"}

def gnews(q):
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"

# Feed layers, WorldMonitor-style. TARGET layer is the addition.
FEEDS = {
    "target": [
        ("Target Watch", gnews('"North+Dallas+Bank"+OR+"NDBT"+OR+"NODB"+when:90d')),
    ],
    "sector": [
        ("Community Banks", gnews('("community+bank"+OR+"regional+bank"+OR+"community+banking")+when:14d')),
        ("Central Bank Rates", gnews('("interest+rate"+OR+"rate+decision"+OR+"rate+cut"+OR+"monetary+policy")+Fed+when:7d')),
        ("CRE Exposure", gnews('("commercial+real+estate")+(bank+OR+loans+OR+office)+when:14d')),
        ("Housing Market", gnews('("housing+market"+OR+"mortgage+rates")+when:7d')),
    ],
    "regulatory": [
        ("Federal Reserve", "https://www.federalreserve.gov/feeds/press_all.xml"),
        ("Banking Rules", gnews('(Basel+OR+"capital+requirements"+OR+"banking+regulation"+OR+FDIC+OR+OCC)+when:14d')),
    ],
    "region": [
        ("DFW Economy", gnews('(Dallas+OR+"North+Texas"+OR+Frisco+OR+Plano)+(economy+OR+banking+OR+"real+estate")+when:14d')),
        ("Texas Banking", gnews('Texas+(bank+OR+banking)+(merger+OR+acquisition+OR+earnings+OR+deposits)+when:30d')),
    ],
}

def fetch(name, url, limit=12):
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=20) as r:
            root = ET.fromstring(r.read())
    except Exception as e:
        return {"feed": name, "error": str(e), "items": []}
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub = (it.findtext("pubDate") or "").strip()
        src = it.findtext("source") or ""
        try:
            dt = parsedate_to_datetime(pub).strftime("%Y-%m-%d")
        except Exception:
            dt = pub
        items.append({"title": title, "source": src.strip(), "date": dt, "link": link})
        if len(items) >= limit:
            break
    return {"feed": name, "items": items}

def main():
    out = {}
    for layer, feeds in FEEDS.items():
        out[layer] = [fetch(n, u) for n, u in feeds]
    json.dump(out, sys.stdout, indent=1)

if __name__ == "__main__":
    main()
