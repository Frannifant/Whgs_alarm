"""
Wohnungs-Alarm Hamburg
Prüft die Angebotsseiten aus sites.json, erkennt NEUE Anzeigen,
filtert nach Preis/Zimmern und schickt Treffer per Telegram.
"""
import hashlib
import json
import os
import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

TOKEN = os.environ.get("TG_TOKEN", "")
CHAT_ID = os.environ.get("TG_CHAT_ID", "")
STATE_FILE = "state.json"
TZ = ZoneInfo("Europe/Berlin")
HEADERS = {"User-Agent": "Mozilla/5.0 (privater Wohnungs-Alarm, max. alle 30 Min.)"}

CFG = json.load(open("sites.json", encoding="utf-8"))
MAX_WARM = CFG["max_warm"]
MAX_KALT = CFG["max_kalt_if_only_kalt"]
MAX_ROOMS = CFG["max_rooms"]

LISTING_HINT = re.compile(r"(\bZimmer\b|\bZi\b\.?|m²|\bqm\b|Wohnfläche)", re.I)
NOT_RENTAL = re.compile(r"(Stellplatz|Tiefgarage|Garage|Gewerbe|Büro|Laden|Lager|Praxis|Kaufpreis|zum Kauf|Eigentumswohnung)", re.I)
WOHNUNG = re.compile(r"(Wohnung|Apartment|Appartement|Zimmer)", re.I)
PRICE = re.compile(r"(\d{1,2}\.\d{3}|\d{3,4})(?:,\d{1,2})?\s*(?:€|EUR|Euro)", re.I)
ROOMS = re.compile(r"(\d(?:[,.]5)?)\s*(?:-\s*)?(?:Zimmer|Zi\b|Zi\.|Räume)", re.I)
SKIP_WORDS = re.compile(r"(kaution|nebenkosten|betriebskosten|heizkosten|stellplatz|garage|\bnk\b|\bbk\b|\bhk\b|provision|anteil|eintrittsgeld)")

TAGS = [
    (re.compile(r"(Erdgeschoss|\bEG\b|Hochparterre)", re.I), "EG?"),
    (re.compile(r"möbliert", re.I), "möbliert"),
    (re.compile(r"(WBS|§\s*5|Wohnberechtigungsschein|gefördert)", re.I), "WBS nötig?"),
    (re.compile(r"(Senior|60\s*\+|ab 60)", re.I), "Senioren?"),
    (re.compile(r"(befristet|Zeitmiet|auf Zeit)", re.I), "befristet?"),
]


# ---------- Telegram ----------
def tg(text):
    if not TOKEN or not CHAT_ID:
        print("[kein Telegram-Token]", text)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text[:4000], "disable_web_page_preview": True},
            timeout=20,
        )
    except Exception as e:
        print("Telegram-Fehler:", e)


# ---------- Laden ----------
def fetch(site):
    if site.get("js"):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(user_agent=HEADERS["User-Agent"])
            page.goto(site["url"], timeout=60000, wait_until="networkidle")
            page.wait_for_timeout(3000)
            html = page.content()
            browser.close()
            return html
    r = requests.get(site["url"], headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


# ---------- Anzeigen erkennen ----------
def extract_listings(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        tag.decompose()
    items = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        block, text = a, a.get_text(" ", strip=True)
        for _ in range(5):  # nach oben gehen, bis der Block wie eine Anzeige aussieht
            if LISTING_HINT.search(text) and PRICE.search(text):
                break
            parent = block.parent
            if parent is None:
                break
            other_links = {x["href"] for x in parent.find_all("a", href=True)} - {a["href"]}
            if other_links:  # Eltern-Block enthält schon andere Anzeigen -> hier stoppen
                break
            block = parent
            text = block.get_text(" ", strip=True)
        if not LISTING_HINT.search(text) or len(text) > 1500:
            continue
        if NOT_RENTAL.search(text) and not WOHNUNG.search(text):
            continue
        url = urljoin(base_url, href)
        key = url if url.rstrip("/") != base_url.rstrip("/") else hashlib.md5(text[:200].encode()).hexdigest()
        items.setdefault(key, {"url": url, "text": re.sub(r"\s+", " ", text)[:600]})
    return items


# ---------- Preis / Zimmer ----------
def _label(snippet):
    s = snippet.lower()
    if SKIP_WORDS.search(s):
        return "skip"
    if re.search(r"warm|gesamt|inkl", s):
        return "warm"
    if re.search(r"kalt|netto|grundmiete", s):
        return "kalt"
    return None


def parse(text):
    t = text.replace("\xa0", " ")
    warm = kalt = other = None
    for m in PRICE.finditer(t):
        value = float(m.group(1).replace(".", ""))
        if not 200 <= value <= 5000:
            continue
        lab = _label(t[m.end():m.end() + 18]) or _label(t[max(0, m.start() - 30):m.start()])
        if lab == "skip":
            continue
        if lab == "warm":
            warm = value if warm is None else min(warm, value)
        elif lab == "kalt":
            kalt = value if kalt is None else min(kalt, value)
        else:
            other = value if other is None else min(other, value)
    rm = ROOMS.search(t)
    rooms = float(rm.group(1).replace(",", ".")) if rm else None
    return warm, kalt, other, rooms


def verdict(text):
    """Gibt (melden?, Preis-Info) zurück."""
    warm, kalt, other, rooms = parse(text)
    if rooms is not None and rooms > MAX_ROOMS:
        return False, ""
    if warm is not None:
        return warm <= MAX_WARM, f"{warm:.0f} € warm"
    if kalt is not None:
        return kalt <= MAX_KALT, f"{kalt:.0f} € kalt (warm geschätzt +150 €)"
    if other is not None:
        return other <= MAX_WARM, f"{other:.0f} € (kalt/warm unklar)"
    return True, "⚠️ Preis nicht erkannt – selbst prüfen"


# ---------- Hauptlauf ----------
def main():
    state = json.load(open(STATE_FILE, encoding="utf-8")) if os.path.exists(STATE_FILE) else {}
    seen = state.setdefault("seen", {})
    errors = state.setdefault("errors", {})
    first_run = not seen
    status = []
    hits = 0

    for site in CFG["sites"]:
        name = site["name"]
        try:
            listings = extract_listings(fetch(site), site["url"])
            errors[name] = 0
        except Exception as e:
            errors[name] = errors.get(name, 0) + 1
            status.append(f"❌ {name}: Fehler ({type(e).__name__})")
            if errors[name] == 3:
                tg(f"❌ {name} ist seit 3 Läufen nicht erreichbar.\n{site['url']}")
            continue

        known = set(seen.get(name, []))
        new_keys = [k for k in listings if k not in known]
        status.append(("✅" if listings else "⚠️") + f" {name}: {len(listings)} Anzeigen erkannt")

        if not first_run and name in seen:
            for k in new_keys:
                item = listings[k]
                ok, price = verdict(item["text"])
                if not ok:
                    continue
                tags = [label for rx, label in TAGS if rx.search(item["text"])]
                msg = (f"🏠 NEU bei {name}\n{price}"
                       + (f"\nHinweise: {', '.join(tags)}" if tags else "")
                       + f"\n\n{item['text'][:350]}\n\n👉 {item['url']}")
                tg(msg)
                hits += 1

        seen[name] = list(known | set(listings))[-500:]

    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    if first_run:
        tg("🤖 Wohnungs-Alarm gestartet. Bestehende Anzeigen gespeichert – ab jetzt melde ich nur NEUE.\n\n" + "\n".join(status))
    elif state.get("last_status_day") != today and now.hour >= 7:
        tg(f"☀️ Tagesstatus ({now:%d.%m.})\n" + "\n".join(status))
    if now.hour >= 7:
        state["last_status_day"] = today

    json.dump(state, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"Fertig. Treffer gemeldet: {hits}")


if __name__ == "__main__":
    main()
