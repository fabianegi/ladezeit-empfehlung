"""
Minimaler ENTSO-E-Client.

Kein Token im Quelltext: Der Security Token wird aus der Umgebungsvariable
`ENTSOE_TOKEN` oder aus einer lokalen `.env` im Projektverzeichnis gelesen
(Vorlage: `.env.example`).

Endpunkt und Parameterlogik nach der offiziellen ENTSO-E-Dokumentation
(Restful API integration guide, transparencyplatform.zendesk.com, abgerufen 21.08.2026).
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from datetime import datetime, timedelta
from math import isfinite

# Für das Parsen der XML-Antworten wird defusedxml bevorzugt (härtet gegen
# manipulierte XML-Dokumente ab). Ist es nicht installiert, wird auf die
# Standardbibliothek zurückgefallen; die Antworten kommen ausschließlich vom
# ENTSO-E-Endpunkt über https.
try:
    from defusedxml import ElementTree as ET
except ImportError:  # pragma: no cover
    import xml.etree.ElementTree as ET
from urllib.parse import urlencode
from urllib.request import urlopen
from urllib.error import HTTPError

# Produktionsendpunkt (nur https wird unterstützt).
BASE_URL = "https://web-api.tp.entsoe.eu/api"

# Gebotszone Deutschland/Luxemburg und die vier deutschen Regelzonen.
# Die Bedeutung der Codes ist per Testabruf zu bestätigen, nicht anzunehmen.
AREAS = {
    "DE-LU":       "10Y1001A1001A82H",
    "50Hertz":     "10YDE-VE-------2",
    "Amprion":     "10YDE-RWENET---I",
    "TenneT-DE":   "10YDE-EON------1",
    "TransnetBW":  "10YDE-ENBW-----N",
}

# Dokumenttypen laut Reference Documentation.
DOC_WIND_SOLAR_FORECAST = "A69"   # Erzeugungsprognose Wind/Solar
DOC_ACTUAL_GENERATION = "A75"     # tatsächliche Erzeugung je Erzeugungsart
PROCESS_DAY_AHEAD = "A01"

# ENTSO-E begrenzt auf 400 Anfragen/Minute; der PoC bleibt weit darunter.
_MIN_SECONDS_BETWEEN_REQUESTS = 0.5
_last_request = 0.0


def load_token() -> str:
    """Liest ENTSOE_TOKEN aus der Umgebung oder aus der lokalen .env."""
    token = os.environ.get("ENTSOE_TOKEN")
    if token:
        return token

    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == "ENTSOE_TOKEN":
                return value.strip()

    raise RuntimeError(
        "Kein ENTSO-E-Token gefunden. Erwartet als Umgebungsvariable ENTSOE_TOKEN "
        "oder in .env (Vorlage: .env.example)."
    )


def request(params: dict, token: str | None = None) -> str:
    """Führt einen Abruf aus und gibt die XML-Antwort als Text zurück."""
    global _last_request

    query = dict(params)
    query["securityToken"] = token or load_token()

    elapsed = time.monotonic() - _last_request
    if elapsed < _MIN_SECONDS_BETWEEN_REQUESTS:
        time.sleep(_MIN_SECONDS_BETWEEN_REQUESTS - elapsed)

    url = f"{BASE_URL}?{urlencode(query)}"
    with urlopen(url, timeout=60) as response:
        _last_request = time.monotonic()
        return response.read().decode("utf-8")


def wind_solar_forecast(area: str, period_start: str, period_end: str,
                        token: str | None = None) -> str:
    """Day-Ahead-Erzeugungsprognose Wind/Solar. Zeitformat: yyyyMMddHHmm (UTC)."""
    return request({
        "documentType": DOC_WIND_SOLAR_FORECAST,
        "processType": PROCESS_DAY_AHEAD,
        "in_Domain": AREAS.get(area, area),
        "periodStart": period_start,
        "periodEnd": period_end,
    }, token=token)


class EntsoeDatenfehler(ValueError):
    """Antwort enthält keine eindeutig auswertbaren Prognosereihen."""


def parse_timeseries(xml_text: str) -> list[dict]:
    """Metadaten sämtlicher Perioden; Fehlerantworten sind keine leeren Reihen."""
    return [{**r, "point_count": len(r["punkte"]),
             "first_value": str(r["punkte"][0][1]) if r["punkte"] else None}
            for r in parse_timeseries_punkte(xml_text)]


def describe_error(error: HTTPError) -> str:
    """Macht die Fehlermeldung von ENTSO-E lesbar (steckt im XML-Body)."""
    body = error.read().decode("utf-8", errors="replace")
    try:
        root = ET.fromstring(body)
        for element in root.iter():
            if element.tag.endswith("text") and element.text:
                return f"HTTP {error.code}: {element.text.strip()}"
    except ET.ParseError:
        pass
    return f"HTTP {error.code}: {body[:300]}"


# ---------------------------------------------------------------------------
# A03-Rekonstruktion
# ---------------------------------------------------------------------------
# ENTSO-E liefert die Prognosereihen mit curveType A03 ("variable sized block").
# Dabei werden identische Folgewerte weggelassen: Ein Punkt gilt so lange
# fort, bis der nächste Punkt eine neue Position meldet. Deshalb hat die
# Solarreihe nachts weniger Punkte als das 96er-Raster Positionen.
#
# Wer die Punkte einfach der Reihe nach auf Viertelstunden abbildet, verschiebt
# die halbe Kurve. Beobachtet am 24.08.2026: B16 (Solar) kam mit 64 statt 96 Punkten.
PSR_TYPEN = {
    "B16": "Solar",
    "B18": "Wind Offshore",
    "B19": "Wind Onshore",
}

_RESOLUTION_MINUTEN = {"PT15M": 15, "PT30M": 30, "PT60M": 60, "PT1H": 60}


def parse_timeseries_punkte(xml_text: str) -> list[dict]:
    """Liest sämtliche Perioden mit explizitem Kurventyp, Einheit und Zeitbezug."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise EntsoeDatenfehler("ENTSO-E-Antwort ist kein gültiges XML.") from exc
    # Lokale Namen vermeiden voneinander abhängige Namespace-Pfade.
    for element in root.iter():
        element.tag = element.tag.rsplit("}", 1)[-1]
    if root.tag == "Acknowledgement_MarketDocument":
        codes = [e.text for e in root.findall(".//Reason/code")]
        # Keine vom Server wiederholten Anfrageparameter/Token ausgeben.
        code = ", ".join(c for c in codes if c and c.isdigit()) or "unbekannt"
        raise EntsoeDatenfehler(f"ENTSO-E meldet eine Acknowledgement-Antwort (Code {code}).")
    reihen = []
    for ts in root.findall(".//TimeSeries"):
        periods = ts.findall("Period")
        if not periods:
            raise EntsoeDatenfehler("Zeitreihe enthält keine Perioden.")
        for period in periods:
            punkte = []
            try:
                for point in period.findall("Point"):
                    punkte.append((int(point.findtext("position")), float(point.findtext("quantity"))))
            except (ValueError, TypeError) as exc:
                raise EntsoeDatenfehler("Ungültige Position oder Menge in ENTSO-E-Reihe.") from exc
            reihen.append({
                "psr_type": ts.findtext(".//psrType"),
                "unit": ts.findtext("quantity_Measure_Unit.name"),
                "curve_type": ts.findtext("curveType"),
                "resolution": period.findtext("resolution"),
                "start": period.findtext("timeInterval/start"),
                "end": period.findtext("timeInterval/end"),
                "punkte": punkte,
            })
    if not reihen:
        raise EntsoeDatenfehler("ENTSO-E-Antwort enthält keine Zeitreihen.")
    return reihen


def rekonstruiere_raster(punkte: list[tuple[int, float]], positionen: int) -> list[float]:
    """A03: Ein gemeldeter Wert gilt bis zur nächsten Position fort; kein Nullersatz."""
    if not isinstance(positionen, int) or positionen < 1 or not punkte:
        raise EntsoeDatenfehler("Leeres oder ungültiges A03-Raster.")
    positionen_ist = [p for p, _ in punkte]
    if (len(set(positionen_ist)) != len(punkte) or min(positionen_ist) != 1
            or any(not isinstance(p, int) or p < 1 or p > positionen for p in positionen_ist)
            or any(not isfinite(q) or q < 0 for _, q in punkte)):
        raise EntsoeDatenfehler("A03 benötigt eindeutige gültige Positionen ab 1 und endliche Werte.")
    nach_position = dict(punkte)
    raster, aktuell = [], nach_position[1]
    for pos in range(1, positionen + 1):
        aktuell = nach_position.get(pos, aktuell)
        raster.append(aktuell)
    return raster


def reihen_auf_raster(xml_text: str, positionen: int | None = None, *,
                     expected_start: datetime | None = None,
                     expected_end: datetime | None = None) -> dict[str, list[float]]:
    """Rasterlänge aus Periodengrenzen; der PoC weist mehrteilige Reihen zurück.

    A03 wird fortgeschrieben, A01 muss jede Position liefern. Gemeinsamer
    Zeitraum und MW-Einheit sind Pflicht; Aufrufer können Sollgrenzen angeben.
    """
    ergebnis, intervall = {}, None
    for reihe in parse_timeseries_punkte(xml_text):
        try:
            start = datetime.fromisoformat(reihe["start"].replace("Z", "+00:00"))
            ende = datetime.fromisoformat(reihe["end"].replace("Z", "+00:00"))
            raster = timedelta(minutes=_RESOLUTION_MINUTEN[reihe["resolution"]])
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise EntsoeDatenfehler("Ungültiger Zeitraum oder unbekannte Auflösung.") from exc
        if start.tzinfo is None or ende.tzinfo is None or ende <= start or (ende - start) % raster:
            raise EntsoeDatenfehler("Ungültige ENTSO-E-Periodengrenzen.")
        anzahl = (ende - start) // raster
        if ((positionen is not None and anzahl != positionen)
                or (expected_start is not None and start != expected_start)
                or (expected_end is not None and ende != expected_end)):
            raise EntsoeDatenfehler("ENTSO-E-Periode weicht vom angefragten Zeitraum ab.")
        if reihe["unit"] != "MAW":
            raise EntsoeDatenfehler("ENTSO-E-Reihe hat nicht die erwartete Einheit MW (MAW).")
        aktuell = (start, ende, raster)
        if intervall is not None and intervall != aktuell:
            raise EntsoeDatenfehler("ENTSO-E-Reihen haben unterschiedliche Zeitraster.")
        intervall = aktuell
        name = PSR_TYPEN.get(reihe["psr_type"], reihe["psr_type"] or "unbekannt")
        if name in ergebnis:
            raise EntsoeDatenfehler("Mehrere Perioden/Reihen derselben Erzeugungsart werden nicht unterstützt.")
        if reihe["curve_type"] not in {"A01", "A03"}:
            raise EntsoeDatenfehler("Nicht unterstützter ENTSO-E-Kurventyp.")
        if reihe["curve_type"] == "A01" and len(reihe["punkte"]) != anzahl:
            raise EntsoeDatenfehler("A01-Reihe ist unvollständig.")
        ergebnis[name] = rekonstruiere_raster(reihe["punkte"], anzahl)
    return ergebnis
