"""
Ladezeit-Empfehlung (Proof of Concept): Einstiegspunkt für die Kommandozeile.

Verbindet die Datenbeschaffung (`smard_client`) mit der Empfehlungslogik
(`empfehlung`) und gibt vier Ergebnisse aus: empfohlenes Fenster, Alternative,
Begründung und eine einfache Darstellung der erwarteten Erzeugung.

Aufruf:

    python3 src/poc.py                                   # Beispiel: Abfahrt 07:00, 3 h
    python3 src/poc.py --abfahrt 07:00 --dauer 3
    python3 src/poc.py --abfahrt 17:30 --dauer 2 --region TransnetBW
    python3 src/poc.py --offline --tag 2026-08-14 --abfahrt 18:00 --dauer 3 --diagramm

Alle Zeitangaben sind deutsche Ortszeit, so wie ladende Personen sie verwenden.
Die Umrechnung auf das UTC-Raster der Datenquellen passiert intern.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from urllib.error import URLError
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

import empfehlung as emp  # noqa: E402
import smard_client as sc  # noqa: E402

# SMARD-CSV enthält Ortszeiten ohne UTC-Offset. Mehrdeutige/nicht existente
# Ortszeiten und Zeiträume mit Zeitumstellung werden im PoC zurückgewiesen.
TZ_DE = ZoneInfo("Europe/Berlin")

MODULE_NETZLAST = 6000411       # Netzlast-Prognose, per Abruf verifiziert (24.08.2026)
MODULE_RESIDUALLAST = 6004362


def ortszeit(text: str) -> datetime:
    naiv = datetime.strptime(text, "%d.%m.%Y %H:%M")
    a = naiv.replace(tzinfo=TZ_DE, fold=0)
    b = naiv.replace(tzinfo=TZ_DE, fold=1)
    if a.utcoffset() != b.utcoffset():
        raise ValueError("Zeitumstellung: Ortszeit ist mehrdeutig oder nicht existent.")
    return a


def validiere_csv_zeitraum(zeilen: list[dict], beginn: datetime, ende: datetime) -> dict:
    if beginn.utcoffset() != ende.utcoffset():
        raise ValueError("Zeiträume mit Zeitumstellung werden im PoC nicht unterstützt.")
    raster = {}
    for zeile in zeilen:
        von, bis = ortszeit(zeile["von"]), ortszeit(zeile["bis"])
        if von in raster or bis - von != emp.RASTER:
            raise ValueError("CSV enthält doppelte Zeitstempel oder ein ungültiges Raster.")
        raster[von] = zeile["werte"]
    soll = set()
    punkt = beginn
    while punkt < ende:
        soll.add(punkt)
        punkt += emp.RASTER
    if set(raster) != soll:
        raise ValueError("CSV deckt den angefragten Zeitraum nicht exakt und lückenlos ab.")
    return raster


def prognose_aus_csv(ee_csv: str, last_csv: str, beginn: datetime,
                     ende: datetime, region: str = "DE-LU") -> list[emp.Prognosepunkt]:
    spalten_ee, zeilen_ee = sc.parse_csv(ee_csv)
    spalten_last, zeilen_last = sc.parse_csv(last_csv)
    erforderlich = {"Wind Onshore", "Photovoltaik", "Wind Offshore"}
    if not erforderlich.issubset(spalten_ee) or "Netzlast" not in spalten_last:
        raise ValueError("Erforderliche Erzeugungs- oder Netzlastspalte fehlt.")
    ee = validiere_csv_zeitraum(zeilen_ee, beginn, ende)
    last = validiere_csv_zeitraum(zeilen_last, beginn, ende)
    # Nur vollständig unbesetzte Offshore-Reihen dieser Binnenregelzonen
    # werden als strukturell nicht geführt behandelt, niemals einzelne Lücken.
    offshore_ungefuehrt = (region in {"Amprion", "TransnetBW", "Creos"}
                           and all(w["Wind Offshore"] is None for w in ee.values()))
    punkte = []
    for zeit in sorted(ee):
        werte = [ee[zeit][s] for s in sorted(erforderlich)
                 if not (s == "Wind Offshore" and offshore_ungefuehrt)]
        if any(w is None or not emp.isfinite(w) or w < 0 for w in werte):
            raise ValueError("Erzeugungsprognose enthält fehlende oder ungültige Werte.")
        netzlast = last[zeit]["Netzlast"]
        if netzlast is None:
            raise ValueError("Netzlastprognose enthält fehlende Werte.")
        punkte.append(emp.Prognosepunkt(zeit, sum(werte), netzlast))
    return emp.validiere_punkte(punkte)


def lade_prognose(tag: datetime, region: str = "DE-LU") -> list[emp.Prognosepunkt]:
    """Lädt zwei Kalendertage ab Mitternacht des Starttags (keinen Vortag)."""
    beginn = tag.replace(hour=0, minute=0, second=0, microsecond=0)
    ende = beginn + timedelta(days=2)

    return prognose_aus_csv(
        sc.download_csv(sc.MODULE_EE_PROGNOSE, region, beginn, ende),
        sc.download_csv([MODULE_NETZLAST], region, beginn, ende), beginn, ende, region)


def lade_archiv(tag: datetime, region: str = "DE-LU", data: Path | None = None):
    """Liest ausschließlich das vorhandene, vollständig belegbare Zweitagesarchiv."""
    if tag.date().isoformat() != "2026-08-14" or region != "DE-LU":
        raise ValueError("Vollständiges Offline-Archiv liegt nur für DE-LU ab 14.08.2026 vor.")
    data = data or Path(__file__).resolve().parent.parent / "data"
    namen = [
        "smard_day-ahead-ee_DE-LU_20260814_abgerufen-20260824T1037Z.csv",
        "smard_day-ahead-ee_DE-LU_20260815_abgerufen-20260914T1108Z.csv",
        "smard_netzlast-prognose_DE-LU_20260814-15_abgerufen-20260914T1108Z.csv",
    ]
    a, b, last = [(data / name).read_text(encoding="utf-8-sig") for name in namen]
    ee = a.rstrip() + "\n" + "\n".join(b.splitlines()[1:])
    beginn = tag.replace(hour=0, minute=0, second=0, microsecond=0)
    punkte = prognose_aus_csv(ee, last, beginn, beginn + timedelta(days=2), region)
    return punkte, "Archivabrufe: EE 14.08.: 24.08.2026 10:37 UTC; EE 15.08. und Last: 14.09.2026 11:08 UTC"


def _naechster_zeitpunkt(basis: datetime, uhrzeit: time) -> datetime:
    """Die nächste Gelegenheit zu dieser Uhrzeit: heute, sonst morgen."""
    kandidat = basis.replace(hour=uhrzeit.hour, minute=uhrzeit.minute,
                             second=0, microsecond=0)
    return kandidat if kandidat > basis else kandidat + timedelta(days=1)


def diagramm(ergebnis: emp.Empfehlung, ziel: Path, region: str,
             alle_punkte: list[emp.Prognosepunkt] | None = None) -> Path:
    """Ausgabe 4 als PNG: erwartete Erzeugung mit markiertem Fenster.

    Gezeigt wird bewusst der gesamte Prognosehorizont, nicht nur der für die
    Person nutzbare Ausschnitt: Erst im Tageskontext wird sichtbar, dass die
    Empfehlung nicht das Tagesmaximum meldet, sondern das Optimum innerhalb der
    Nutzerrestriktion. Das ist der Unterschied zu einem allgemeinen
    Grünstromsignal.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    punkte = alle_punkte or ergebnis.verlauf
    zeiten = [p.beginn for p in punkte]
    anteile = [p.anteil * 100 for p in punkte]

    fig, ax = plt.subplots(figsize=(10, 4.4))

    # Nicht nutzbarer Zeitraum blass hinterlegen (vor Ladebeginn / nach Abfahrt).
    ax.axvspan(zeiten[0], ergebnis.fruehester_start, color="#000000", alpha=0.045, lw=0)
    ax.axvspan(ergebnis.abfahrtszeit, zeiten[-1], color="#000000", alpha=0.045, lw=0)

    ax.plot(zeiten, anteile, linewidth=1.9, color="#1a5c3a", zorder=3)
    ax.fill_between(zeiten, anteile, alpha=0.10, color="#1a5c3a", lw=0)

    hoch = max(anteile)
    kopf = hoch * 1.30

    # Beschriftung gestaffelt: empfohlenes Fenster und Alternative grenzen oft
    # unmittelbar aneinander, dann würden mittige Labels übereinanderfallen.
    def markiere(fenster, farbe, name, hoehe, seite):
        if fenster is None:
            return
        ax.axvspan(fenster.beginn, fenster.ende, color=farbe, alpha=0.22, lw=0, zorder=1)
        anker = fenster.beginn if seite == "links" else fenster.ende
        ax.annotate(f"{name}: {fenster.anteil_prozent()} %",
                    xy=(anker, kopf * hoehe),
                    xytext=(-4 if seite == "links" else 4, 0), textcoords="offset points",
                    ha="right" if seite == "links" else "left", va="center",
                    fontsize=8.5, color=farbe, weight="bold",
                    arrowprops=dict(arrowstyle="-", color=farbe, lw=0.8,
                                    shrinkA=0, shrinkB=0, alpha=0.6))

    markiere(ergebnis.alternative, "#4a6f92", "Alternative", 0.78, "rechts")
    markiere(ergebnis.empfohlen, "#1a7a45", "empfohlen", 0.94, "rechts")

    ax.axvline(ergebnis.abfahrtszeit, color="#b03030", linestyle="--", linewidth=1.4, zorder=4)
    ax.annotate(f"Abfahrt {ergebnis.abfahrtszeit:%H:%M}",
                xy=(ergebnis.abfahrtszeit, kopf * 0.55), xytext=(6, 0),
                textcoords="offset points", ha="left", fontsize=8.5, color="#b03030")

    # Tagesgrenzen markieren: Der Prognosehorizont umfasst zwei Kalendertage,
    # ohne Trennung liest sich "00:00" zweimal wie derselbe Zeitpunkt.
    tag_wechsel = [z for z in zeiten if z.hour == 0 and z.minute == 0][1:]
    for wechsel in tag_wechsel:
        ax.axvline(wechsel, color="#999999", linewidth=0.8, alpha=0.7, zorder=2)
        ax.annotate(f"{wechsel:%d.%m.}", xy=(wechsel, 0), xytext=(4, 4),
                    textcoords="offset points", fontsize=7.5, color="#777777")
    ax.annotate(f"{zeiten[0]:%d.%m.}", xy=(zeiten[0], 0), xytext=(4, 4),
                textcoords="offset points", fontsize=7.5, color="#777777")

    if hoch > 100:
        ax.axhline(100, color="#888888", linewidth=0.9, linestyle=":", zorder=2)
        ax.annotate("100 % der Netzlast", xy=(zeiten[-1], 100), xytext=(-4, 3),
                    textcoords="offset points", ha="right", fontsize=7.5, color="#666666")

    ax.set_ylim(0, kopf)
    ax.set_xlim(zeiten[0], zeiten[-1])
    ax.set_ylabel("erwarteter Anteil Wind + Photovoltaik\nan der Netzlast [%]", fontsize=9)
    ax.set_xlabel("Uhrzeit (deutsche Ortszeit)", fontsize=9)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=TZ_DE))
    ax.xaxis.set_major_locator(mdates.HourLocator(interval=3))
    ax.tick_params(labelsize=8.5)
    ax.grid(alpha=0.22, linewidth=0.6)
    for rand in ("top", "right"):
        ax.spines[rand].set_visible(False)

    ax.set_title(f"Day-Ahead-Prognose Wind und Photovoltaik, {region}, "
                 f"{zeiten[0]:%d.%m.} bis {zeiten[-1]:%d.%m.%Y}", fontsize=10.5, pad=10)

    fig.text(0.008, 0.015,
             "Datengrundlage: Bundesnetzagentur | SMARD.de, Prognostizierte Erzeugung Day-Ahead "
             "und Prognostizierter Stromverbrauch (Netzlast).\nGrau hinterlegt: Zeitraum außerhalb "
             "der Ladegelegenheit. Eigene Darstellung.",
             fontsize=6.5, color="#555555", va="bottom")
    fig.tight_layout(rect=(0, 0.075, 1, 1))
    ziel.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(ziel, dpi=200)
    plt.close(fig)
    return ziel


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ladezeit-Empfehlung auf Basis öffentlicher Prognosedaten (Proof of Concept)")
    parser.add_argument("--abfahrt", default="07:00",
                        help="gewünschte Abfahrtszeit, HH:MM (Ortszeit)")
    parser.add_argument("--dauer", type=float, default=3.0,
                        help="voraussichtlich benötigte Ladedauer in Stunden")
    parser.add_argument("--region", default="DE-LU", choices=sorted(sc.REGIONS),
                        help="Gebotszone oder Regelzone")
    parser.add_argument("--tag", default=None,
                        help="Starttag JJJJ-MM-TT (Vorgabe: heute)")
    parser.add_argument("--diagramm", action="store_true",
                        help="Diagramm als PNG erzeugen")
    parser.add_argument("--offline", action="store_true",
                        help="archivierte CSV verwenden; erfordert --tag 2026-08-14 und DE-LU")
    args = parser.parse_args()

    try:
        basis = (datetime.strptime(args.tag, "%Y-%m-%d").replace(tzinfo=TZ_DE)
                 if args.tag else datetime.now(TZ_DE))
        stunde, minute = (int(t) for t in args.abfahrt.split(":"))
        abfahrt = _naechster_zeitpunkt(basis, time(stunde, minute))
        ladedauer = timedelta(hours=args.dauer)
        emp.validiere_ladedauer(ladedauer)
        if args.offline:
            punkte, datenstand = lade_archiv(basis, args.region)
        else:
            punkte = lade_prognose(basis, args.region)
            datenstand = "Abruf abgeschlossen: " + datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
        ergebnis = emp.empfehle(punkte, abfahrt, ladedauer, basis,
                               region=args.region, datenstand=datenstand)
    except (ValueError, OverflowError, RuntimeError, OSError) as fehler:
        # Netzwerkfehler können URLs enthalten. Keine Anfrage-URL ausgeben.
        if isinstance(fehler, URLError):
            print("Keine Empfehlung möglich: Datenabruf fehlgeschlagen.", file=sys.stderr)
            return 2
        print(f"Keine Empfehlung möglich: {fehler}", file=sys.stderr)
        return 2

    print(emp.als_karte(ergebnis))

    if args.diagramm:
        ziel = (Path(__file__).resolve().parent.parent / "ausgabe" /
                f"ee-prognose_{args.region}_{abfahrt:%Y%m%d-%H%M}_{datetime.now():%Y%m%dT%H%M%S}.png")
        print(f"\nDiagramm: {diagramm(ergebnis, ziel, args.region, punkte)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
