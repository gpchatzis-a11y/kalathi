"""Καθημερινή συλλογή τιμών για το «καλάθι».

Διαβάζει το basket.yaml, επισκέπτεται τη δημόσια σελίδα κάθε προϊόντος
σε κάθε αλυσίδα και γράφει τις τιμές στο docs/data/.

Χρήση:
    python collector/collect.py            # κανονική ημερήσια συλλογή
    python collector/collect.py --probe URL [URL ...]   # δοκιμή μεμονωμένων σελίδων
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import re
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
BASKET = ROOT / "basket.yaml"
DATA = ROOT / "docs" / "data"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
CHAINS = {
    "mymarket": "My Market",
    "ab": "ΑΒ Βασιλόπουλος",
    "masoutis": "Μασούτης",
}
# Ευγενική καθυστέρηση ανάμεσα σε αιτήματα προς την ίδια αλυσίδα (δευτερόλεπτα)
DELAY = (2.0, 4.0)


def chain_of(url: str) -> str:
    if "mymarket.gr" in url:
        return "mymarket"
    if "ab.gr" in url:
        return "ab"
    if "masoutis.gr" in url:
        return "masoutis"
    raise ValueError(f"Άγνωστη αλυσίδα για {url}")


def parse_eur(text: str) -> float | None:
    """'5.44€', '2,95 €', '€2,42' -> float."""
    if not text:
        return None
    m = re.search(r"(\d+)[.,](\d{1,2})", text)
    if m:
        return float(f"{m.group(1)}.{m.group(2)}")
    m = re.search(r"(\d+)", text)
    return float(m.group(1)) if m else None


# ---------------------------------------------------------------- My Market
def fetch_mymarket(session: requests.Session, url: str) -> dict:
    r = session.get(url, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.get_text())
        except json.JSONDecodeError:
            continue
        nodes = data.get("@graph", [data]) if isinstance(data, dict) else data
        for node in nodes:
            if isinstance(node, dict) and node.get("@type") == "Product":
                offer = node.get("offers") or {}
                if isinstance(offer, list):
                    offer = offer[0]
                price = offer.get("price")
                return {
                    "name": node.get("name"),
                    "price": float(price) if price not in (None, "") else None,
                    "available": "InStock" in str(offer.get("availability", "")),
                }
    raise RuntimeError("Δεν βρέθηκαν δεδομένα προϊόντος στη σελίδα")


# ---------------------------------------------------------------- AB (rendered)
AB_JS = r"""
() => {
  const box = document.querySelector('[data-testid="product-properties-price-info"]');
  const p = box && box.querySelector('[data-testid="product-block-price"]');
  const h1 = document.querySelector('h1');
  const unavailable = !!document.querySelector('[data-testid*="out-of-stock" i]');
  return {
    name: h1 ? h1.innerText.trim() : null,
    label: p ? p.getAttribute('aria-label') : null,
    text: p ? p.innerText : null,
    unavailable,
  };
}
"""


def parse_ab_label(label: str | None, text: str | None) -> float | None:
    # π.χ. "Τιμή: 2 ευρώ και 42 λεπτά"
    if label:
        e = re.search(r"(\d+)\s*ευρώ", label)
        c = re.search(r"(\d+)\s*λεπτ", label)
        if e or c:
            return round(int(e.group(1) if e else 0) + int(c.group(1) if c else 0) / 100, 2)
    if text:  # "€\n2\n42"
        nums = re.findall(r"\d+", text)
        if len(nums) >= 2:
            return float(f"{nums[0]}.{nums[1][:2].ljust(2, '0')}")
    return None


def fetch_ab(page, url: str) -> dict:
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_selector(
        '[data-testid="product-properties-price-info"] [data-testid="product-block-price"]',
        timeout=30000,
    )
    d = page.evaluate(AB_JS)
    return {
        "name": d["name"],
        "price": parse_ab_label(d["label"], d["text"]),
        "available": not d["unavailable"],
    }


# ---------------------------------------------------------------- Μασούτης (rendered)
MS_JS = r"""
() => {
  const p = document.querySelector('.item-priceContainer .item-price') || document.querySelector('.item-price');
  const old = document.querySelector('.item-priceContainer .item-oldPrice, .item-priceContainer del, .item-priceContainer s');
  return {
    name: document.title,
    text: p ? p.innerText : null,
    old: old ? old.innerText : null,
  };
}
"""


def fetch_masoutis(page, url: str) -> dict:
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_selector(".item-price", timeout=30000)
    d = page.evaluate(MS_JS)
    name = re.sub(r"\s*\|.*$", "", d["name"] or "").strip()
    return {"name": name, "price": parse_eur(d["text"]), "available": True}


# ---------------------------------------------------------------- orchestration
class Fetcher:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA, "Accept-Language": "el-GR,el;q=0.9"})
        self._pw = None
        self._browser = None
        self._page = None

    def page(self):
        if self._page is None:
            from playwright.sync_api import sync_playwright

            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
            ctx = self._browser.new_context(user_agent=UA, locale="el-GR")
            # Δεν χρειαζόμαστε εικόνες/γραμματοσειρές: λιγότερη κίνηση προς τα sites
            ctx.route(
                re.compile(r".*\.(png|jpe?g|webp|gif|svg|woff2?|ttf|mp4)(\?.*)?$"),
                lambda route: route.abort(),
            )
            self._page = ctx.new_page()
        return self._page

    def fetch(self, url: str) -> dict:
        chain = chain_of(url)
        if chain == "mymarket":
            return fetch_mymarket(self.session, url)
        if chain == "ab":
            return fetch_ab(self.page(), url)
        return fetch_masoutis(self.page(), url)

    def close(self):
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()


def probe(urls: list[str]) -> None:
    f = Fetcher()
    try:
        for u in urls:
            try:
                r = f.fetch(u)
                print(f"OK   {chain_of(u):9} {r['price']!s:>6}  {r['name']}  <- {u}")
            except Exception as e:  # noqa: BLE001
                print(f"FAIL {u}: {type(e).__name__}: {str(e)[:120]}")
            time.sleep(random.uniform(*DELAY))
    finally:
        f.close()


def collect() -> int:
    basket = yaml.safe_load(BASKET.read_text(encoding="utf-8"))
    today = dt.datetime.now(ZoneInfo("Europe/Athens")).date().isoformat()
    DATA.mkdir(parents=True, exist_ok=True)
    hist_path = DATA / "history.json"
    history = json.loads(hist_path.read_text(encoding="utf-8")) if hist_path.exists() else {}

    # Ομαδοποίηση ανά αλυσίδα ώστε να μην «σφυροκοπάμε» μία αλυσίδα συνεχόμενα
    jobs = []
    for p in basket["products"]:
        for chain, url in (p.get("urls") or {}).items():
            if url:
                jobs.append((p["id"], chain, url))
    random.shuffle(jobs)

    f = Fetcher()
    errors = []
    day = {}
    try:
        for pid, chain, url in jobs:
            try:
                r = f.fetch(url)
                if r["price"] is None:
                    raise RuntimeError("χωρίς τιμή")
                day.setdefault(pid, {})[chain] = r["price"]
                print(f"OK   {pid:24} {chain:9} {r['price']:6.2f}  {r['name']}")
            except Exception as e:  # noqa: BLE001
                errors.append({"product": pid, "chain": chain, "url": url, "error": f"{type(e).__name__}: {str(e)[:200]}"})
                print(f"FAIL {pid:24} {chain:9} {type(e).__name__}: {str(e)[:120]}", file=sys.stderr)
            time.sleep(random.uniform(*DELAY))
    finally:
        f.close()

    history[today] = day
    hist_path.write_text(json.dumps(history, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")

    meta = {
        "updated": dt.datetime.now(ZoneInfo("Europe/Athens")).isoformat(timespec="minutes"),
        "chains": CHAINS,
        "products": [
            {k: p.get(k) for k in ("id", "name", "category", "urls")} for p in basket["products"]
        ],
        "errors": errors,
        "ok": len(jobs) - len(errors),
        "total": len(jobs),
    }
    (DATA / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n{meta['ok']}/{meta['total']} τιμές συλλέχθηκαν για {today}.")
    # Αποτυγχάνουμε μόνο αν δεν πήραμε σχεδόν τίποτα (π.χ. μπλόκαρε κάποιο site)
    return 0 if meta["ok"] >= meta["total"] * 0.5 else 1


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", nargs="+", metavar="URL")
    a = ap.parse_args()
    if a.probe:
        probe(a.probe)
        return 0
    return collect()


if __name__ == "__main__":
    raise SystemExit(main())
