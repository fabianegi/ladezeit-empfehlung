"""
Abbildung: Wirkung der A03-Rasterrekonstruktion (ENTSO-E).

Zeigt am Beispiel der Solarprognose für den 14.08.2026 (Gebotszone DE-LU), was
passiert, wenn man die von ENTSO-E mit `curveType=A03` gelieferten Punkte naiv als
lückenloses Viertelstundenraster liest (identische Folgewerte werden von der
Plattform weggelassen), gegenüber der rekonstruierten Reihe und der SMARD-Reihe
derselben Größe. Grundlage: `datenanalyse.a03_gegenprobe()`.

Aufruf:  python3 src/abbildung_a03.py PFAD_ZUM_ENTSOE_XML
Ausgabe: ausgabe/a03-rekonstruktion_20260814_<Zeitstempel>.png (+ .svg)

Datengrundlage: ENTSO-E Transparency Platform (Day-Ahead Generation Forecast for
Wind and Solar, Art. 14.1.d) und Bundesnetzagentur | SMARD.de. Die ENTSO-E-Rohdaten
liegen nicht im Repository, weil ihre Weitergabe nicht freigegeben ist. Das XML
lässt sich mit eigenem Token über `entsoe_client.wind_solar_forecast()` abrufen.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

import entsoe_client as ec  # noqa: E402
import smard_client as sc  # noqa: E402
import datenanalyse as da

TZ_DE = ZoneInfo("Europe/Berlin")
ZIEL = Path(__file__).resolve().parent.parent / "ausgabe"


def erzeuge(tag: datetime = datetime(2026, 8, 14, tzinfo=TZ_DE), *, xml_path: Path) -> tuple[list[Path], dict]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    ende = tag + timedelta(days=1)
    _, zeilen = da._laden("DE-LU", tag)
    da._summe(zeilen, "Photovoltaik")
    smard = [z["werte"]["Photovoltaik"] for z in zeilen]
    zeiten = [datetime.strptime(z["von"], "%d.%m.%Y %H:%M").replace(tzinfo=TZ_DE) for z in zeilen]

    xml = xml_path.read_text(encoding="utf-8")
    raster = ec.reihen_auf_raster(xml, 96, expected_start=tag, expected_end=ende)
    reihe = next(r for r in ec.parse_timeseries_punkte(xml) if r["psr_type"] == "B16")
    if reihe["curve_type"] != "A03":
        raise ValueError("Gegenprobe benötigt eine A03-Solarreihe.")
    punkte = reihe["punkte"]

    # naiv: gemeldete Punkte der Reihe nach als Viertelstunden gelesen (MW in MWh je 15 min)
    naiv = [q / 4 for _, q in punkte]
    korrekt = [w / 4 for w in raster["Solar"]]
    fehler_naiv = [abs(a - b) for a, b in zip(smard, naiv + [0.0] * (96 - len(naiv)))]
    fehler_korrekt = [abs(a - b) for a, b in zip(smard, korrekt)]
    i_max = fehler_naiv.index(max(fehler_naiv))
    kennzahlen = {
        "gemeldete_punkte": len(punkte),
        "max_fehler_ohne_rekonstruktion_MWh": round(max(fehler_naiv), 1),
        "max_fehler_mit_rekonstruktion_MWh": round(max(fehler_korrekt), 4),
        "schlimmste_viertelstunde": zeilen[i_max]["von"],
    }

    fig, ax = plt.subplots(figsize=(10, 4.6))
    ax.plot(zeiten, smard, color="#1a5c3a", linewidth=2.2, label="SMARD (Bundesnetzagentur), 96 Viertelstunden", zorder=3)
    ax.plot(zeiten, korrekt, color="#4a6f92", linewidth=1.6, linestyle="--",
            label=f"ENTSO-E, auf das 96er-Raster rekonstruiert ({len(punkte)} gemeldete Punkte)", zorder=4)
    ax.plot(zeiten[:len(naiv)], naiv, color="#a03050", linewidth=1.4, linestyle=":",
            label="ENTSO-E, gemeldete Punkte naiv als lückenlose Viertelstunden gelesen", zorder=2)
    ax.axvline(zeiten[i_max], color="#a03050", linewidth=0.9, alpha=0.6)
    # Deutsches Zahlenformat nur auf die Zahl anwenden, sonst wird aus "max." ein "max,".
    wert = (f"{kennzahlen['max_fehler_ohne_rekonstruktion_MWh']:,.1f}"
            .replace(",", "X").replace(".", ",").replace("X", "."))
    ax.annotate(f"max. Abweichung ohne Rekonstruktion:\n{wert} MWh um {zeiten[i_max]:%H:%M} Uhr",
                xy=(zeiten[i_max], max(smard) * 0.30), xytext=(10, 0), textcoords="offset points",
                fontsize=8.3, color="#a03050", va="center",
                bbox=dict(boxstyle="round,pad=0.25", facecolor="white", edgecolor="none", alpha=0.9))

    ax.set_ylabel("prognostizierte Solarerzeugung [MWh je Viertelstunde]", fontsize=9)
    ax.set_xlabel("Uhrzeit (deutsche Ortszeit)", fontsize=9)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=TZ_DE))
    ax.xaxis.set_major_locator(mdates.HourLocator(interval=3))
    ax.set_xlim(zeiten[0], zeiten[-1])
    ax.set_ylim(0, max(smard) * 1.18)
    ax.tick_params(labelsize=8.5)
    ax.grid(alpha=0.22, linewidth=0.6)
    for rand in ("top", "right"):
        ax.spines[rand].set_visible(False)
    ax.legend(fontsize=8, loc="upper left", frameon=False)
    ax.set_title(f"Solarprognose Day-Ahead, DE-LU, {tag:%d.%m.%Y}: Wirkung der Rasterrekonstruktion (curveType A03)",
                 fontsize=10.5, pad=10)
    fig.text(0.008, 0.015,
             "Datengrundlage: ENTSO-E Transparency Platform (Generation Forecasts for Wind and Solar, Art. 14.1.d) "
             "und Bundesnetzagentur | SMARD.de (Prognostizierte Erzeugung Day-Ahead, Photovoltaik).\n"
             "Vergleich desselben Zeitraums; Abweichungen werden aus den übergebenen Daten berechnet. Eigene Darstellung.",
             fontsize=6.5, color="#555555", va="bottom")
    fig.tight_layout(rect=(0, 0.075, 1, 1))

    ZIEL.mkdir(parents=True, exist_ok=True)
    pfade = []
    stempel = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    for endung, dpi in (("png", 300), ("svg", None)):
        pfad = ZIEL / f"a03-rekonstruktion_{tag:%Y%m%d}_{stempel}.{endung}"
        fig.savefig(pfad, dpi=dpi, facecolor="white")
        pfade.append(pfad)
    plt.close(fig)
    return pfade, kennzahlen


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Aufruf: python3 src/abbildung_a03.py PFAD_ZUM_ENTSOE_XML "
                         "(A69-Prognose für DE-LU am 14.08.2026; liegt nicht im Repository).")
    pfade, kennzahlen = erzeuge(xml_path=Path(sys.argv[1]))
    for p in pfade:
        print("geschrieben:", p)
    for k, v in kennzahlen.items():
        print(f"  {k}: {v}")
