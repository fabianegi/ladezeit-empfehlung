"""
Empfehlungslogik für Ladezeitfenster.

Schnittstelle:

    Eingabe : gewünschte Abfahrtszeit und voraussichtlich benötigte Ladedauer
    Ausgabe : 1. empfohlenes Ladezeitfenster
              2. alternatives Ladezeitfenster
              3. verständliche Begründung
              4. einfache Darstellung der erwarteten erneuerbaren Erzeugung

Der Dienst empfiehlt, er steuert nicht. Eine Steuerung von Fahrzeug oder
Wallbox ist nicht vorgesehen.

Kennzahl
--------

Bewertet wird der Anteil der prognostizierten Wind- und Solarerzeugung an der
prognostizierten Netzlast:

    anteil = (Wind Onshore + Wind Offshore + Photovoltaik) / Netzlast

Die Definition ist aus den SMARD-Reihen ableitbar: SMARD veröffentlicht die
Residuallast, und es gilt nachprüfbar `Residuallast = Netzlast - (Wind + PV)`
(eigener Abruf am 24.08.2026, maximale Abweichung 0,0 MWh je Viertelstunde).
Der Anteil ist also identisch mit `1 - Residuallast / Netzlast`.

Grenzen der Kennzahl:

1. Sie ist kein Ökostromanteil. Biomasse, Wasserkraft und sonstige
   Erneuerbare sind nicht enthalten, weil SMARD sie nicht als Day-Ahead-Prognose
   führt. Der Wert ist deshalb nicht mit dem gesamten EE-Anteil gleichzusetzen.
2. Der Anteil kann über 100 % liegen (am 14.08.2026 bis 111,5 % um 13:45).
   Dann übersteigt die prognostizierte Wind- und Solarerzeugung die
   prognostizierte Netzlast.
3. Sie beruht auf einer Prognose, nicht auf einer Messung. Sie gilt für DE-LU
   beziehungsweise die gewählte Regelzone und beschreibt keinen Strommix am
   Ladepunkt.

Aus diesen Gründen versprechen die Ausgabetexte keine CO2-Einsparung.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from math import isfinite

VIERTELSTUNDEN_JE_STUNDE = 4
RASTER = timedelta(minutes=15)


@dataclass
class Prognosepunkt:
    """Ein Viertelstundenwert der Prognose."""
    beginn: datetime
    wind_pv_mwh: float
    netzlast_mwh: float

    def __post_init__(self) -> None:
        if self.beginn.tzinfo is None or self.beginn.utcoffset() is None:
            raise ValueError("Prognosezeitstempel benötigt eine Zeitzone.")
        if not isfinite(self.wind_pv_mwh) or self.wind_pv_mwh < 0:
            raise ValueError("Wind-/PV-Prognose muss endlich und nicht negativ sein.")
        if not isfinite(self.netzlast_mwh) or self.netzlast_mwh <= 0:
            raise ValueError("Netzlastprognose muss endlich und größer als null sein.")

    @property
    def anteil(self) -> float:
        """Anteil Wind+PV an der Netzlast (1,0 = 100 %)."""
        return self.wind_pv_mwh / self.netzlast_mwh


@dataclass
class Ladefenster:
    """Ein zusammenhängendes Ladezeitfenster mit seiner Bewertung."""
    beginn: datetime
    ende: datetime
    mittlerer_anteil: float
    wind_pv_mwh: float

    @property
    def dauer(self) -> timedelta:
        return self.ende - self.beginn

    def als_text(self) -> str:
        return f"{self.beginn:%H:%M} bis {self.ende:%H:%M} Uhr"

    def anteil_prozent(self) -> int:
        return round(self.mittlerer_anteil * 100)


@dataclass
class Empfehlung:
    """Das vollständige Ergebnis mit allen vier Ausgaben."""
    abfahrtszeit: datetime
    ladedauer: timedelta
    fruehester_start: datetime
    empfohlen: Ladefenster | None
    alternative: Ladefenster | None
    begruendung: str
    hinweise: list[str] = field(default_factory=list)
    verlauf: list[Prognosepunkt] = field(default_factory=list)
    region: str = "DE-LU"
    datenstand: str | None = None


class KeinFensterMoeglich(ValueError):
    """Der verfügbare Zeitraum ist kürzer als die benötigte Ladedauer."""


def validiere_ladedauer(ladedauer: timedelta) -> int:
    """Der PoC akzeptiert positive Dauern in ganzen Viertelstunden, ohne Rundung."""
    if ladedauer <= timedelta(0) or ladedauer % RASTER:
        raise ValueError("Ladedauer muss positiv und ein Vielfaches von 15 Minuten sein.")
    return ladedauer // RASTER


def validiere_punkte(punkte: list[Prognosepunkt]) -> list[Prognosepunkt]:
    """Sortiert nach UTC; weist Duplikate, Datenlücken und ungültige Werte zurück."""
    for punkt in punkte:
        punkt.__post_init__()
    sortiert = sorted(punkte, key=lambda p: p.beginn.astimezone(timezone.utc))
    for vorher, nachher in zip(sortiert, sortiert[1:]):
        if vorher.beginn.utcoffset() != nachher.beginn.utcoffset():
            raise ValueError("Zeiträume mit Zeitumstellung werden im PoC nicht unterstützt.")
        delta = (nachher.beginn.astimezone(timezone.utc)
                 - vorher.beginn.astimezone(timezone.utc))
        if delta != RASTER:
            raise ValueError("Prognoseraster enthält doppelte Zeitstempel oder Datenlücken.")
    return sortiert


# ---------------------------------------------------------------------------
# Fensterbewertung
# ---------------------------------------------------------------------------

def _bewerte_fenster(punkte: list[Prognosepunkt], start_index: int,
                     laenge: int) -> Ladefenster:
    ausschnitt = punkte[start_index:start_index + laenge]
    wind_pv = sum(p.wind_pv_mwh for p in ausschnitt)
    netzlast = sum(p.netzlast_mwh for p in ausschnitt)
    return Ladefenster(
        beginn=ausschnitt[0].beginn,
        ende=ausschnitt[-1].beginn + RASTER,
        mittlerer_anteil=wind_pv / netzlast,
        wind_pv_mwh=wind_pv,
    )


def alle_fenster(punkte: list[Prognosepunkt], ladedauer: timedelta) -> list[Ladefenster]:
    """Bewertet jedes mögliche Fenster der geforderten Länge (gleitendes Fenster)."""
    laenge = validiere_ladedauer(ladedauer)
    punkte = validiere_punkte(punkte)
    if laenge > len(punkte):
        raise KeinFensterMoeglich(
            f"Ladedauer {ladedauer} passt nicht in den verfügbaren Zeitraum "
            f"({len(punkte)} Viertelstunden).")
    return [_bewerte_fenster(punkte, i, laenge)
            for i in range(len(punkte) - laenge + 1)]


def _ueberschneidet(a: Ladefenster, b: Ladefenster) -> bool:
    return a.beginn < b.ende and b.beginn < a.ende


def empfehle(punkte: list[Prognosepunkt], abfahrtszeit: datetime,
             ladedauer: timedelta,
             fruehester_start: datetime | None = None, *,
             region: str = "DE-LU", datenstand: str | None = None) -> Empfehlung:
    """Berechnet Empfehlung und Alternative innerhalb der Nutzerrestriktion.

    Anders als bei einem allgemeinen Grünstromsignal wird nur der Zeitraum
    zwischen `fruehester_start` und `abfahrtszeit` bewertet. Gemeldet wird das
    Optimum innerhalb dessen, was die ladende Person überhaupt nutzen kann.

    Die Alternative ist bewusst überschneidungsfrei zum empfohlenen Fenster.
    Ein Fenster, das nur 15 Minuten verschoben ist, wäre formal zweitbester Wert,
    aber keine echte Wahlmöglichkeit.
    """
    validiere_ladedauer(ladedauer)
    punkte = validiere_punkte(punkte)
    if abfahrtszeit.tzinfo is None or abfahrtszeit.utcoffset() is None:
        raise ValueError("Abfahrtszeit benötigt eine Zeitzone.")
    if fruehester_start is None:
        fruehester_start = punkte[0].beginn if punkte else abfahrtszeit
    if fruehester_start.tzinfo is None or fruehester_start.utcoffset() is None:
        raise ValueError("Frühester Start benötigt eine Zeitzone.")
    if abfahrtszeit <= fruehester_start:
        raise KeinFensterMoeglich("Abfahrtszeit muss nach dem frühesten Start liegen.")
    if punkte and (punkte[0].beginn > fruehester_start
                   or punkte[-1].beginn + RASTER < abfahrtszeit):
        raise KeinFensterMoeglich("Prognose deckt den angefragten Zeitraum nicht vollständig ab.")

    verfuegbar = [p for p in punkte
                  if fruehester_start <= p.beginn and p.beginn + RASTER <= abfahrtszeit]

    hinweise: list[str] = []
    if not verfuegbar:
        raise KeinFensterMoeglich(
            "Zwischen frühestem Start und Abfahrtszeit liegt kein Prognosewert vor.")

    fenster = alle_fenster(verfuegbar, ladedauer)
    sortiert = sorted(fenster, key=lambda f: f.mittlerer_anteil, reverse=True)
    empfohlen = sortiert[0]

    alternative = next((f for f in sortiert[1:] if not _ueberschneidet(f, empfohlen)), None)
    if alternative is None:
        hinweise.append(
            "Es gibt kein überschneidungsfreies zweites Fenster, weil der "
            "verfügbare Zeitraum dafür zu knapp ist.")

    if empfohlen.mittlerer_anteil > 1.0:
        hinweise.append(
            "Der Anteil liegt über 100 %: die prognostizierte Wind- und "
            "Solarerzeugung übersteigt in diesem Fenster die prognostizierte Netzlast.")

    spanne = abfahrtszeit - fruehester_start
    if spanne < ladedauer * 1.25:
        hinweise.append(
            "Der verfügbare Zeitraum ist kaum länger als die Ladedauer; "
            "es stehen nur wenige Startzeiten zur Auswahl.")

    return Empfehlung(
        abfahrtszeit=abfahrtszeit,
        ladedauer=ladedauer,
        fruehester_start=fruehester_start,
        empfohlen=empfohlen,
        alternative=alternative,
        begruendung=begruende(empfohlen, alternative, abfahrtszeit, verfuegbar, region),
        hinweise=hinweise,
        verlauf=verfuegbar,
        region=region,
        datenstand=datenstand,
    )


# ---------------------------------------------------------------------------
# Begründung
# ---------------------------------------------------------------------------
# Die Textbausteine folgen fünf Formulierungsregeln:
#   1. immer mit Nutzerbezug          4. keine Steuerungs-Sprache
#   2. nie günstig/sparen/Tarif       5. Prognose als Prognose kennzeichnen
#   3. keine CO2-Einsparung

def begruende(empfohlen: Ladefenster, alternative: Ladefenster | None,
              abfahrtszeit: datetime, verfuegbar: list[Prognosepunkt],
              region: str = "DE-LU") -> str:
    """Formuliert die verständliche Begründung (Ausgabe 3)."""
    tagesbestwert = max(p.anteil for p in verfuegbar)
    schlechtestes = min(p.anteil for p in verfuegbar)

    zeilen = [
        f"Empfohlenes Ladezeitfenster: {empfohlen.als_text()}.",
        f"Unter den verfügbaren Fenstern deiner Ladedauer erreicht dieses Fenster "
        f"in {region} vor deiner Abfahrt um {abfahrtszeit:%H:%M} Uhr den höchsten "
        f"Wert: Wind und Photovoltaik decken rechnerisch "
        f"{empfohlen.anteil_prozent()} % der prognostizierten Netzlast im Fenster.",
        f"Zum Vergleich: über den gesamten für dich nutzbaren Zeitraum schwankt der "
        f"erwartete Anteil zwischen {round(schlechtestes * 100)} % und "
        f"{round(tagesbestwert * 100)} %.",
    ]
    if alternative is not None:
        zeilen.append(
            f"Alternatives Ladezeitfenster: {alternative.als_text()} mit "
            f"{alternative.anteil_prozent()} % der prognostizierten Netzlast.")
    zeilen.append(
        "Grundlage ist die Day-Ahead-Prognose der Bundesnetzagentur (SMARD). "
        "Der Wert gilt für das gewählte Gebiet und beschreibt keinen "
        "Strommix an deinem Ladepunkt. Die Empfehlung ist unverbindlich; "
        "tatsächliche Erzeugung und Verbrauch können von der Prognose abweichen.")
    return "\n".join(zeilen)


# ---------------------------------------------------------------------------
# Ausgabe 4: einfache Darstellung der erwarteten Erzeugung
# ---------------------------------------------------------------------------

_BLOECKE = "▁▂▃▄▅▆▇█"


def sparkline(punkte: list[Prognosepunkt], breite: int = 24) -> str:
    """Textbalken des erwarteten Anteils, die platzsparende Variante von Ausgabe 4."""
    if not punkte:
        return ""
    if breite < 1:
        raise ValueError("Grafikbreite muss positiv sein.")
    # Mittelt disjunkte Blöcke, die gemeinsam alle Werte einschließlich des
    # letzten abdecken; die Zeitbeschriftung bleibt dadurch zutreffend.
    anzahl = min(breite, len(punkte))
    bloecke = [punkte[i * len(punkte) // anzahl:(i + 1) * len(punkte) // anzahl]
               for i in range(anzahl)]
    werte = [sum(p.anteil for p in block) / len(block) for block in bloecke]
    tief, hoch = min(werte), max(werte)
    spanne = (hoch - tief) or 1.0
    return "".join(_BLOECKE[min(len(_BLOECKE) - 1,
                                int((w - tief) / spanne * (len(_BLOECKE) - 1)))]
                   for w in werte)


def als_karte(empfehlung: Empfehlung) -> str:
    """Kompakte Darstellung aller vier Ausgaben (Konsolenfassung)."""
    e = empfehlung
    stunden = e.ladedauer.total_seconds() / 3600
    zeilen = [
        "Deine Lade-Empfehlung",
        f"Gebiet {e.region}; Abfahrt {e.abfahrtszeit:%d.%m.%Y %H:%M} Uhr, {stunden:g} h Ladezeit",
        "",
    ]
    if e.empfohlen:
        zeilen.append(f"  Empfohlen:   {e.empfohlen.als_text()}, {e.empfohlen.anteil_prozent()} % "
                      f"Wind/PV relativ zur Netzlast")
    if e.alternative:
        zeilen.append(f"  Alternative: {e.alternative.als_text()}, "
                      f"{e.alternative.anteil_prozent()} %")
    zeilen.append(f"Datenabruf: {e.datenstand or 'nicht dokumentiert'}")
    zeilen += ["", "  " + sparkline(e.verlauf) + "  "
               f"{e.verlauf[0].beginn:%H:%M} bis {e.verlauf[-1].beginn + RASTER:%H:%M} Uhr",
               "", e.begruendung]
    for hinweis in e.hinweise:
        zeilen.append(f"\nHinweis: {hinweis}")
    return "\n".join(zeilen)
