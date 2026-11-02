"""
Wohnungs-Alarm Hamburg
Prüft die Angebotsseiten aus sites.json, erkennt NEUE Anzeigen,
filtert nach Preis/Zimmern und schickt Treffer per Telegram.
"""
import hashlib
import json
import os
import re
from collections import Counter
from datetime import datetime, timedelta
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
SKIP_FRAMES = re.compile(r"(youtube|vimeo|google|maps|facebook|instagram|doubleclick|cookie)", re.I)
 
 
def fetch(site):
    if site.get("js"):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(user_agent=HEADERS["User-Agent"])
            page.goto(site["url"], timeout=60000, wait_until="networkidle")
            page.wait_for_timeout(3000)
            html = page.content()
            for fr in page.frames:  # eingebettete Fenster (z. B. Immomio) mitlesen
                if fr != page.main_frame and not SKIP_FRAMES.search(fr.url or ""):
                    try:
                        html += "\n" + fr.content()
                    except Exception:
                        pass
            browser.close()
            return html
    r = requests.get(site["url"], headers=HEADERS, timeout=30)
    r.raise_for_status()
    html = r.text
    try:
        for fr in BeautifulSoup(html, "html.parser").find_all("iframe", src=True)[:3]:
            src = urljoin(site["url"], fr["src"])
            if src.startswith("http") and not SKIP_FRAMES.search(src):
                html += "\n" + requests.get(src, headers=HEADERS, timeout=20).text
    except Exception:
        pass
    return html
 
 
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
    if re.search(r"Kaufpreis|zum Kauf|Kapitalanlage|Baugrundst", text, re.I):
        return False, ""
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
# ---------- Bewerbungslink, leere Seiten, Tagesbericht ----------
EMPTY_MARKER = re.compile(
    r"(keine\s+(freien\s+)?(wohnungen|angebote|mietangebote|objekte|immobilien)|zurzeit\s+keine|"
    r"derzeit\s+(keine|nichts|sind\s+keine)|aktuell\s+(keine|sind\s+keine)|zur\s+zeit\s+keine|"
    r"momentan\s+keine|leider\s+(keine|nichts)|keine\s+aktuellen|keine\s+mietwohnungen|"
    r"0\s+ergebnisse|keine\s+(treffer|ergebnisse))", re.I)
APPLY_LABEL = re.compile(r"bewerb", re.I)
CONTACT_LABEL = re.compile(r"(anfrage|besichtigung|interesse|kontakt aufnehmen|anmieten|termin)", re.I)
 
 
def find_apply(url, site):
    """Sucht auf der Anzeigenseite den Bewerbungslink.
    Rückgabe: ('link', url) | ('mail', adresse) | (None, None).
    Reihenfolge: apply_url aus sites.json > Immomio-Link > Button 'bewerben' > Anfrage/Besichtigung > E-Mail."""
    if site.get("apply_url"):
        a = site["apply_url"]
        return ("mail", a[7:]) if a.startswith("mailto:") else ("link", a)
    try:
        html = fetch({**site, "url": url})
    except Exception as e:
        print("Bewerbungslink-Suche fehlgeschlagen:", e)
        return (None, None)
    soup = BeautifulSoup(html, "html.parser")
    immomio = bewerb = kontakt = mail = None
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith("mailto:"):
            mail = mail or href[7:].split("?")[0]
            continue
        if href.startswith(("tel:", "javascript:", "#")):
            continue
        full = urljoin(url, href)
        label = a.get_text(" ", strip=True)
        if "immomio" in full.lower():
            immomio = immomio or full
        elif APPLY_LABEL.search(label):
            bewerb = bewerb or full
        elif CONTACT_LABEL.search(label):
            kontakt = kontakt or full
    best = immomio or bewerb or kontakt
    if best:
        return ("link", best)
    if mail:
        return ("mail", mail)
    return (None, None)
 
 
def apply_line(kind, value, listing_url):
    if kind == "link" and value != listing_url:
        return f"📝 Hier bewerben: {value}"
    if kind == "mail":
        return f"✉️ Bewerbung per E-Mail: {value}"
    return "📝 Bewerben: über die Anzeige (Link oben), Bewerbungslink nicht erkannt"
 
 
def site_groups(state):
    stats = state.get("site_stats", {})
    return {k: [n for n, s in stats.items() if s["status"] == k] for k in ("ok", "empty_ok", "empty", "error")}
 
 
def recent_events(state, now, hours):
    cut = now - timedelta(hours=hours)
    return [e for e in state.get("events", []) if datetime.fromisoformat(e[0]) >= cut]
 
 
def summary_lines(state, now):
    g = site_groups(state)
    total = sum(len(v) for v in g.values())
    e24 = recent_events(state, now, 24)
    reported = sum(e[2] for e in e24)
    lines = [
        f"Geprüft: {total} Seiten",
        f"✅ {len(g['ok'])} mit Anzeigen · 💤 {len(g['empty_ok'])} leer laut Seite · "
        f"⚠️ {len(g['empty'])} nichts erkannt · ❌ {len(g['error'])} Fehler",
        f"Letzte 24 h: {len(e24)} neue Anzeigen erkannt, {reported} gemeldet.",
    ]
    if reported == 0:
        extra = f" {len(e24)} neue Anzeigen passten nicht (zu teuer, zu groß, anderer Ort)." if e24 else ""
        lines.append("Keine neue passende Wohnung in den letzten 24 h." + extra)
    if g["empty"]:
        lines.append("⚠️ Nichts erkannt (Seite leer oder nicht lesbar): " + ", ".join(g["empty"])
                     + "\nDetails: /diag")
    if g["error"]:
        lines.append("❌ Nicht erreichbar: " + ", ".join(g["error"]))
    return lines
 
 
 
KW_LINE = re.compile(r"(Zimmer|m²|\bqm\b|€|Miete|Wohnung|Apartment|verfügbar|frei)", re.I)
NOISE = re.compile(r"(cookie|datenschutz|impressum|newsletter|©|copyright)", re.I)
 
 
def _clean_soup(html):
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        t.decompose()
    return soup
 
 
def page_facts(html):
    """Kurzbefund einer Seite für /diag: Titel, Textmenge, Links, iframes, Textanfang."""
    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.get_text(" ", strip=True) if soup.title else "")[:70]
    iframes = len(soup.find_all("iframe"))
    links = len(soup.find_all("a", href=True))
    text = re.sub(r"\s+", " ", _clean_soup(html).get_text(" ", strip=True))
    return {"title": title, "len": len(text), "links": links, "iframes": iframes, "head": text[:150]}
 
 
def keyword_fingerprint(html):
    """Fingerabdruck aller Textzeilen mit Wohnungs-Stichwörtern. Ändert er sich, hat die Seite neue Inhalte."""
    lines = {re.sub(r"\s+", " ", l).strip().lower() for l in _clean_soup(html).get_text("\n").split("\n")}
    keep = sorted(l for l in lines if 8 < len(l) < 300 and KW_LINE.search(l) and not NOISE.search(l))
    return hashlib.md5("\n".join(keep).encode()).hexdigest() if keep else ""
 
 
def diag_messages(state):
    g = site_groups(state)
    d = state.get("diag", {})
    blocks = []
    for icon, key in (("⚠️", "empty"), ("💤", "empty_ok")):
        for n in g[key]:
            x = d.get(n)
            if not x:
                blocks.append(f"{icon} {n}: noch keine Daten (nächster Lauf)")
                continue
            blocks.append(f"{icon} {n}\n„{x['title']}“ · {x['len']} Zeichen Text · {x['links']} Links · "
                          f"{x['iframes']} iframes\nText: {x['head']}")
    if not blocks:
        return ["🔧 Alle Seiten liefern Anzeigen, nichts zu diagnostizieren."]
    msgs, cur = [], "🔧 Diagnose: Seiten ohne erkannte Anzeigen\n"
    for b in blocks:
        if len(cur) + len(b) > 3500:
            msgs.append(cur)
            cur = ""
        cur += "\n" + b + "\n"
    msgs.append(cur)
    return msgs
 
 
# ---------- Prüfbefehle (/status, /test, /hilfe) ----------
SELFTEST = [
    ("2 Zimmer · 48 m² · 690 € Kaltmiete", True),
    ("2 Zimmer · 55 m² · 820 € warm", True),
    ("2 Zimmer · 55 m² · 1.050 € warm", False),
    ("4 Zimmer · 90 m² · 1.200 € warm", False),
    ("Einfamilienhaus 120 m² Kaufpreis 499.000 €", False),
]
 
 
def poll_commands(state):
    """Liest neue Telegram-Nachrichten (nur von deiner Chat-ID) und gibt die Befehle zurück."""
    if not TOKEN or not CHAT_ID:
        return []
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TOKEN}/getUpdates",
            params={"offset": state.get("tg_offset", 0), "timeout": 0},
            timeout=20,
        ).json()
    except Exception as e:
        print("getUpdates-Fehler:", e)
        return []
    cmds = []
    for u in r.get("result", []):
        state["tg_offset"] = u["update_id"] + 1
        m = u.get("message") or {}
        if str(m.get("chat", {}).get("id")) != str(CHAT_ID):
            continue
        t = (m.get("text") or "").strip().lower()
        if t.startswith("/"):
            cmds.append(t.split()[0].split("@")[0])
    return cmds
 
 
def selftest():
    lines, good = [], 0
    for text, expected in SELFTEST:
        got, _ = verdict(text)
        ok = got == expected
        good += ok
        lines.append(("✅ " if ok else "❌ ") + f"{text} → {'melden' if got else 'ignorieren'}")
    return good, len(SELFTEST), lines
 
 
def build_status(state, now):
    e7 = recent_events(state, now, 24 * 7)
    lines = [f"📊 Status {now:%d.%m. %H:%M}",
             "Der Bot läuft – dieser Lauf hat gerade alle Seiten geprüft.",
             f"Aktiv seit: {state.get('since', 'unbekannt')}"]
    lines += summary_lines(state, now)
    lines.append(f"Letzte 7 Tage: {len(e7)} erkannt, {sum(e[2] for e in e7)} gemeldet")
    top = Counter(e[1] for e in e7).most_common(5)
    if top:
        lines.append("Aktivste Seiten (7 Tage): " + ", ".join(f"{n} {c}" for n, c in top))
    la = state.get("last_alert")
    if la:
        lines.append(f"Letzte Meldung an dich: {datetime.fromisoformat(la['t']):%d.%m. %H:%M} ({la['site']})")
    else:
        lines.append("Letzte Meldung an dich: noch keine")
    return "\n".join(lines)
 
 
def answer_commands(cmds, state, now):
    for c in dict.fromkeys(cmds):  # jeden Befehl nur einmal beantworten
        if c == "/status":
            tg(build_status(state, now))
        elif c == "/test":
            good, total, lines = selftest()
            tg("🧪 Test: So sähe eine echte Meldung aus:\n\n🏠 NEU bei Beispiel-Hausverwaltung\n"
               "690 € kalt (warm geschätzt +150 €)\nHinweise: EG?\n\n"
               "Helle 2-Zimmer-Wohnung, 48 m², Eimsbüttel\n\n👉 https://example.org/\n\n"
               f"Filter-Selbsttest: {good}/{total} korrekt\n" + "\n".join(lines))
        elif c == "/diag":
            for m in diag_messages(state):
                tg(m)
        elif c in ("/hilfe", "/help", "/start"):
            tg("Befehle:\n/status – Gesundheitscheck mit Zahlen der letzten 24 h und 7 Tage\n"
               "/test – Probemeldung plus Filter-Selbsttest\n"
               "/diag – Befund zu Seiten ohne erkannte Anzeigen\n\n"
               "Antwort kommt beim nächsten Lauf (bis ca. 15 Min.). "
               "Sofort: GitHub → Actions → Run workflow.")
 
 
def main():
    state = json.load(open(STATE_FILE, encoding="utf-8")) if os.path.exists(STATE_FILE) else {}
    seen = state.setdefault("seen", {})
    errors = state.setdefault("errors", {})
    first_run = not seen
    status = []
    hits = 0
    now = datetime.now(TZ)
    state.setdefault("since", f"{now:%d.%m.%Y}")
    cmds = poll_commands(state)
    events = state.setdefault("events", [])
    site_stats = state.setdefault("site_stats", {})
 
    for site in CFG["sites"]:
        name = site["name"]
        try:
            listings, saw_empty_msg, all_html = {}, False, ""
            for u in [site["url"]] + site.get("extra_urls", []):
                html = fetch({**site, "url": u})
                all_html += "\n" + html
                listings.update(extract_listings(html, u))
                if EMPTY_MARKER.search(html):
                    saw_empty_msg = True
            errors[name] = 0
        except Exception as e:
            errors[name] = errors.get(name, 0) + 1
            site_stats[name] = {"count": site_stats.get(name, {}).get("count", 0), "status": "error"}
            status.append(f"❌ {name}: Fehler ({type(e).__name__})")
            if errors[name] == 3:
                tg(f"❌ {name} ist seit 3 Läufen nicht erreichbar.\n{site['url']}")
            continue
 
        known = set(seen.get(name, []))
        new_keys = [k for k in listings if k not in known]
        if listings:
            state_name, icon = "ok", "✅"
        elif saw_empty_msg:
            state_name, icon = "empty_ok", "💤"
        else:
            state_name, icon = "empty", "⚠️"
        status.append(f"{icon} {name}: {len(listings)} Anzeigen erkannt")
        site_stats[name] = {"count": len(listings), "status": state_name}
 
        sig = hashlib.md5(json.dumps([site["url"]] + site.get("extra_urls", [])).encode()).hexdigest()
        sigs = state.setdefault("sig", {})
        silent = first_run or name not in seen or sigs.get(name) != sig
        sigs[name] = sig
        diag = state.setdefault("diag", {})
        if state_name == "ok":
            diag.pop(name, None)
        else:
            diag[name] = page_facts(all_html)
            fp = keyword_fingerprint(all_html)
            fps = state.setdefault("page_fp", {})
            notices = state.setdefault("fp_notice", {})
            day = now.strftime("%Y-%m-%d")
            if (not silent and site.get("watch", True) and fps.get(name) and fp and fp != fps[name]
                    and notices.get(name) != day):
                tg(f"🔔 {name}: Die Seite hat sich geändert, aber ich erkenne keine Anzeige. "
                   f"Bitte kurz ansehen:\n{site['url']}")
                notices[name] = day
            if fp:
                fps[name] = fp
        req = re.compile(site["require"], re.I) if site.get("require") else None
        exc = re.compile(site["exclude"], re.I) if site.get("exclude") else None
 
        if not silent:
            for k in new_keys:
                item = listings[k]
                if req and not req.search(item["text"]):
                    continue
                if exc and exc.search(item["text"]):
                    continue
                ok, price = verdict(item["text"])
                events.append([now.isoformat(timespec="seconds"), name, 1 if ok else 0])
                if not ok:
                    continue
                tags = [label for rx, label in TAGS if rx.search(item["text"])]
                kind, value = find_apply(item["url"], site)
                msg = (f"🏠 NEU bei {name}\n{price}"
                       + (f"\nHinweise: {', '.join(tags)}" if tags else "")
                       + f"\n\n{item['text'][:350]}\n\n🔎 Anzeige: {item['url']}\n"
                       + apply_line(kind, value, item["url"]))
                tg(msg)
                hits += 1
                state["last_alert"] = {"t": now.isoformat(timespec="seconds"), "site": name}
 
        seen[name] = list(known | set(listings))[-500:]
 
    cut = now - timedelta(days=7)
    state["events"] = [e for e in events if datetime.fromisoformat(e[0]) >= cut][-2000:]
    today = now.strftime("%Y-%m-%d")
    if first_run:
        tg("🤖 Wohnungs-Alarm gestartet. Bestehende Anzeigen gespeichert – ab jetzt melde ich nur NEUE.\n\n"
           + "\n".join(status))
        state["last_status_day"] = today
    elif state.get("last_status_day") != today and now.hour >= 8:
        # Täglicher Kontrolllauf: kommt jeden Morgen, auch wenn es nichts Neues gibt.
        tg(f"☀️ Kontrolllauf {now:%d.%m.}: Der Bot läuft.\n" + "\n".join(summary_lines(state, now)))
        state["last_status_day"] = today
 
    answer_commands(cmds, state, now)
    json.dump(state, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"Fertig. Treffer gemeldet: {hits}")
 
 
if __name__ == "__main__":
    main()
