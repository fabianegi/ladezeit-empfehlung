"""
Minimaler SMARD-Client.

SMARD (Strommarktdaten der Bundesnetzagentur, smard.de) ist ohne API-Key und
ohne Registrierung nutzbar, im Unterschied zu ENTSO-E (siehe `entsoe_client.py`).
Dieses Modul enthält deshalb keinerlei Zugangsdaten.

Es werden zwei Endpunkte genutzt:

1. Chart-Data-API (`/app/chart_data/...`): liefert Wochenblöcke als JSON im
   Format `[timestamp_ms, wert]`. Schnell, aber ohne Bezeichnungen: Welche
   Größe eine Filter-ID bezeichnet, steht nicht in der Antwort.
2. Download-API (`/nip-download-manager/nip/download/market-data`): liefert
   CSV mit deutscher Kopfzeile. Diese Kopfzeile belegt die Bedeutung der
   Modul-IDs (siehe `MODULE_DAY_AHEAD`).

Endpunkte und Modul-IDs wurden am 24.08.2026 per eigenem Abruf verifiziert.
"""

from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime, timezone

BASE = "https://www.smard.de"
CHART_DATA = BASE + "/app/chart_data"
DOWNLOAD = BASE + "/nip-download-manager/nip/download/market-data"

# ---------------------------------------------------------------------------
# Regionen
# ---------------------------------------------------------------------------
# Bezeichner exakt so, wie sie das SMARD-Download-Center verwendet.
# "DE-LU" ist die Gebotszone (Marktgebiet ab 01.10.2018) und umfasst neben den
# vier deutschen Regelzonen auch Luxemburg (Creos). Das erklärt die Differenz
# zwischen DE-LU und der Summe der Regelzonen (siehe datenanalyse.py).
REGIONS = {
    "DE-LU": "Marktgebiet DE/LU (Gebotszone)",
    "50Hertz": "Regelzone (DE): 50Hertz",
    "Amprion": "Regelzone (DE): Amprion",
    "TenneT": "Regelzone (DE): TenneT",
    "TransnetBW": "Regelzone (DE): TransnetBW",
    "Creos": "Regelzone (LU): Creos",
}

REGELZONEN_DE = ["50Hertz", "Amprion", "TenneT", "TransnetBW"]

# ---------------------------------------------------------------------------
# Modul-/Filter-IDs
# ---------------------------------------------------------------------------
# Die Download-API adressiert Zeitreihen mit siebenstelligen Modul-IDs
# (z. B. 2000125), die Chart-Data-API mit der verkürzten Filter-ID (125).
# Zusammenhang: filter_id = module_id - 2_000_000.
#
# Die Zuordnung wurde am 24.08.2026 einzeln verifiziert: je Modul-ID ein
# eigener CSV-Abruf, die Bezeichnung stammt aus der Kopfzeile der Antwort der
# Bundesnetzagentur, nicht aus einer Community-Dokumentation.
# Reproduzierbar mit `verify_module_labels()` weiter unten.
#
# Diese Prüfung hat eine Fehlannahme korrigiert: 715 bezeichnet "Sonstige",
# Photovoltaik ist 125.
MODULE_DAY_AHEAD = {
    2000122: "Gesamt",
    2005097: "Photovoltaik und Wind",
    2003791: "Wind Offshore",
    2000123: "Wind Onshore",
    2000125: "Photovoltaik",
    2000715: "Sonstige",
}

# Die drei Reihen, die der Empfehlungsdienst tatsächlich braucht.
MODULE_WIND_ONSHORE = 2000123
MODULE_WIND_OFFSHORE = 2003791
MODULE_PHOTOVOLTAIK = 2000125
MODULE_EE_PROGNOSE = [MODULE_WIND_ONSHORE, MODULE_WIND_OFFSHORE, MODULE_PHOTOVOLTAIK]

# SMARD nennt die Kategorie im Download-Center "Prognostizierte Erzeugung
# Day-Ahead"; daneben existiert "Prognostizierte Erzeugung Intraday".
KATEGORIE = "Prognostizierte Erzeugung Day-Ahead"

_MIN_SECONDS_BETWEEN_REQUESTS = 0.4
_last_request = 0.0

# Ohne Referer/Origin antwortet der Download-Endpunkt mit HTTP 400.
_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "*/*",
    "Origin": BASE,
    "Referer": BASE + "/home/downloadcenter/download-marktdaten/",
    "User-Agent": "ladezeit-empfehlung/1.0 (nicht-kommerzielles Studienprojekt)",
}


def _throttle() -> None:
    global _last_request
    elapsed = time.monotonic() - _last_request
    if elapsed < _MIN_SECONDS_BETWEEN_REQUESTS:
        time.sleep(_MIN_SECONDS_BETWEEN_REQUESTS - elapsed)
    _last_request = time.monotonic()


def _to_millis(moment: datetime) -> int:
    if moment.tzinfo is None:
        raise ValueError("Zeitpunkt braucht eine Zeitzone (naive datetime abgelehnt).")
    return int(moment.timestamp() * 1000)


def download_csv(module_ids: list[int], region: str,
                 start: datetime, end: datetime,
                 resolution: str = "quarterhour") -> str:
    """Ruft Zeitreihen als CSV ab (mit benannter Kopfzeile).

    `start`/`end` sind zeitzonenbehaftete datetime-Objekte. Die Kopfzeile der
    Antwort ist die zitierfähige Fundstelle für die Bedeutung der Modul-IDs.
    """
    if region not in REGIONS:
        raise ValueError(f"Unbekannte Region {region!r}. Bekannt: {sorted(REGIONS)}")

    payload = {"request_form": [{
        "moduleIds": module_ids,
        "region": region,
        "resolution": resolution,
        "format": "CSV",
        "timestamp_from": _to_millis(start),
        "timestamp_to": _to_millis(end),
        "type": "discrete",
        "language": "de",
    }]}

    _throttle()
    request = urllib.request.Request(
        DOWNLOAD, data=json.dumps(payload).encode("utf-8"),
        method="POST", headers=_HEADERS)
    with urllib.request.urlopen(request, timeout=60) as response:
        # utf-8-sig: SMARD stellt der CSV eine Byte-Order-Mark voran.
        return response.read().decode("utf-8-sig", errors="replace")


def parse_csv(text: str) -> tuple[list[str], list[dict]]:
    """Zerlegt eine SMARD-CSV in (Spaltennamen, Zeilen).

    Deutsches Zahlenformat: Punkt als Tausender-, Komma als Dezimaltrennzeichen.
    Fehlende Werte kommen als "-" und werden zu None, nicht zu 0,0.
    0 MWh ist eine Aussage (nachts keine Photovoltaik), ein fehlender Wert nicht.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return [], []

    header = lines[0].split(";")
    spalten = [h.split(" [")[0].strip() for h in header[2:]]

    zeilen = []
    for line in lines[1:]:
        felder = line.split(";")
        if len(felder) < 3:
            continue
        werte = {}
        for name, roh in zip(spalten, felder[2:]):
            roh = roh.strip()
            werte[name] = None if roh in ("", "-") else float(
                roh.replace(".", "").replace(",", "."))
        zeilen.append({
            "von": felder[0].strip(),
            "bis": felder[1].strip(),
            "werte": werte,
        })
    return spalten, zeilen


def chart_data_index(filter_id: int, region: str = "DE") -> list[int]:
    """Liste der verfügbaren Wochenblock-Zeitstempel der Chart-Data-API."""
    url = f"{CHART_DATA}/{filter_id}/{region}/index_quarterhour.json"
    _throttle()
    request = urllib.request.Request(url, headers={"User-Agent": _HEADERS["User-Agent"]})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read()).get("timestamps", [])


def verify_module_labels(region: str = "DE-LU") -> dict[int, str]:
    """Belegt die Bedeutung jeder Modul-ID durch je einen eigenen Abruf.

    Gibt {modul_id: Bezeichnung aus der SMARD-Kopfzeile} zurück. Diese Funktion
    ist der Nachweis hinter `MODULE_DAY_AHEAD` und macht ihn reproduzierbar.
    """
    from datetime import timedelta
    ende = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start = ende - timedelta(days=1)

    ergebnis = {}
    for modul in MODULE_DAY_AHEAD:
        kopf = download_csv([modul], region, start, ende).splitlines()[0]
        ergebnis[modul] = kopf.split(";")[2].split(" [")[0].strip()
    return ergebnis
