# Wohnungs-Alarm Hamburg

Prüft alle 15 Minuten (ca. 6–23 Uhr) die Seiten in `sites.json` und meldet per Telegram
nur NEUE Anzeigen mit max. 850 € warm (bzw. 700 € kalt) und max. 2,5 Zimmern.
Anzeigen ohne erkennbaren Preis werden mit ⚠️ trotzdem gemeldet, damit nichts verloren geht.

## Einstellungen ändern
In `sites.json`: `max_warm`, `max_kalt_if_only_kalt`, `max_rooms`.
Neue Seite: eine Zeile `{"name": "...", "url": "..."}` ergänzen.
Seiten, die ihre Angebote erst per Skript nachladen: `"js": true` ergänzen.

## Tagesstatus
Jeden Morgen kommt eine Übersicht: ✅ = Seite liefert Anzeigen, ⚠️ = 0 erkannt
(Seite leer ODER Erkennung klappt dort nicht), ❌ = Seite nicht erreichbar.
Bleibt eine Seite dauerhaft bei ⚠️, obwohl dort Wohnungen stehen: URL prüfen oder `"js": true` setzen.
