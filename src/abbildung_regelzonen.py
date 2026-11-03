"""
Abbildung: bundesweites Signal gegen regelzonenscharfe Prognose.

Vergleicht den erwarteten Anteil von Wind und Photovoltaik an der Netzlast für
die Gebotszone DE-LU und die vier deutschen Regelzonen. Die Prognosen liegen
regelzonenscharf vor; offen ist, ob der Unterschied groß genug ist, um die
zusätzliche Komplexität einer regional differenzierten Empfehlung und die damit
verbundene Standortfrage (Datenschutz) zu rechtfertigen.

Aufruf:  python3 src/abbildung_regelzonen.py --live [JJJJ-MM-TT]
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

import poc  # noqa: E402
import smard_client as sc  # noqa: E402

TZ_DE = ZoneInfo("Europe/Berlin")
ZIEL = Path(__file__).resolve().parent.parent / "ausgabe"

FARBEN = {
    "DE-LU": "#1a1a1a",
    "50Hertz": "#1a7a45",
    "Amprion": "#c07020",
    "TenneT": "#4a6f92",
    "TransnetBW": "#a03050",
}


def erzeuge(tag: datetime) -> tuple[Path, dict]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    reihen = {}
    for region in FARBEN:
        punkte = poc.lade_prognose(tag, region)
        # Nur den ersten Kalendertag zeigen; zwei Tage machen fünf Kurven unlesbar.
        reihen[region] = [p for p in punkte if p.beginn.date() == tag.date()]

    fig, ax = plt.subplots(figsize=(10, 4.6))
    for region, punkte in reihen.items():
        ax.plot([p.beginn for p in punkte], [p.anteil * 100 for p in punkte],
                label=("Gebotszone Deutschland/Luxemburg" if region == "DE-LU" else region),
                color=FARBEN[region],
                linewidth=2.4 if region == "DE-LU" else 1.5,
                linestyle="-" if region == "DE-LU" else "--",
                zorder=3 if region == "DE-LU" else 2)

    ax.axhline(100, color="#888888", linewidth=0.9, linestyle=":", zorder=1)
    ax.set_ylabel("erwarteter Anteil Wind + Photovoltaik\nan der Netzlast [%]", fontsize=9)
    ax.set_xlabel("Uhrzeit (deutsche Ortszeit)", fontsize=9)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=TZ_DE))
    ax.xaxis.set_major_locator(mdates.HourLocator(interval=3))
    ax.tick_params(labelsize=8.5)
    ax.grid(alpha=0.22, linewidth=0.6)
    for rand in ("top", "right"):
        ax.spines[rand].set_visible(False)
    ax.legend(fontsize=8, loc="upper left", framealpha=0.92, ncol=2)
    ax.set_title(f"DE-LU und Regelzonen im Vergleich, {tag:%d.%m.%Y}",
                 fontsize=10.5, pad=10)

    fig.text(0.008, 0.015,
             "Datengrundlage: Bundesnetzagentur | SMARD.de, Prognostizierte Erzeugung Day-Ahead "
             "und Prognostizierter Stromverbrauch (Netzlast). Eigene Darstellung.",
             fontsize=6.5, color="#555555", va="bottom")
    fig.tight_layout(rect=(0, 0.055, 1, 1))

    ZIEL.mkdir(parents=True, exist_ok=True)
    pfad = ZIEL / f"regelzonen-vergleich_{tag:%Y%m%d}_{datetime.now():%Y%m%dT%H%M%S}.png"
    fig.savefig(pfad, dpi=200)
    plt.close(fig)

    kennzahlen = {}
    for region, punkte in reihen.items():
        werte = [p.anteil * 100 for p in punkte]
        kennzahlen[region] = {
            "tagesmittel": sum(werte) / len(werte),
            "min": min(werte), "max": max(werte),
        }
    return pfad, kennzahlen


if __name__ == "__main__":
    if "--live" not in sys.argv:
        raise SystemExit("Dieser Generator lädt Live-Daten von SMARD, weil für die Regelzonen keine "
                         "Lastarchive im Repository liegen. Aufruf: "
                         "python3 src/abbildung_regelzonen.py --live [JJJJ-MM-TT]")
    argumente = [a for a in sys.argv[1:] if a != "--live"]
    datum = argumente[0] if argumente else "2026-08-14"
    tag = datetime.strptime(datum, "%Y-%m-%d").replace(tzinfo=TZ_DE)
    pfad, kennzahlen = erzeuge(tag)
    print(f"{'Region':<14}{'Mittel':>9}{'min':>9}{'max':>9}")
    for region, k in kennzahlen.items():
        print(f"{region:<14}{k['tagesmittel']:>8.1f}%{k['min']:>8.1f}%{k['max']:>8.1f}%")
    spanne = (max(k['tagesmittel'] for k in kennzahlen.values())
              - min(k['tagesmittel'] for k in kennzahlen.values()))
    print(f"\nSpannweite der Tagesmittel zwischen den Regelzonen: {spanne:.1f} Prozentpunkte")
    print(f"Abbildung: {pfad}")
