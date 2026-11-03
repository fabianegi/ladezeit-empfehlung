"""
Datenqualitäts- und Konsistenzanalyse für SMARD und ENTSO-E.

Leitfrage: Liefern beide Quellen dieselbe Größe für denselben Zeitraum, und wie
groß sind die Abweichungen? Das Skript rechnet die SMARD-interne Analyse aus
archivierten Rohdaten-Snapshots und schreibt die Ergebnisse zusätzlich als
datierte JSON-Datei nach `data/`, damit jede berichtete Zahl eine Fundstelle hat.

Aufruf:

    python3 src/datenanalyse.py            # rechnet und schreibt Ergebnisse
    python3 src/datenanalyse.py --snapshot # zieht vorher frische Rohdaten

Geprüfte Dimensionen: Vollständigkeit, Auflösung, Konsistenz (intern und
quellenübergreifend), Aktualität und technische Nutzbarkeit.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from hashlib import sha256
from math import isfinite

sys.path.insert(0, str(Path(__file__).resolve().parent))

import smard_client as sc  # noqa: E402
import entsoe_client as ec  # noqa: E402
from poc import validiere_csv_zeitraum

DATA = Path(__file__).resolve().parent.parent / "data"
DATA.mkdir(exist_ok=True)

# Analysetag: ein vollständiger Tag, für den die Day-Ahead-Prognose vorliegt.
#
# Zeitzonen-Fallstrick (eigener Test am 24.08.2026): `timestamp_from`/`timestamp_to`
# sind absolute Epoch-Millisekunden, SMARD beschriftet die Zeilen aber in
# deutscher Ortszeit. Wer UTC-Mitternacht anfragt, bekommt eine Reihe, die um
# 02:00 Ortszeit beginnt, also keinen Kalendertag. Für einen deutschen
# Kalendertag muss ab lokaler Mitternacht angefragt werden.
# Gegenstück bei ENTSO-E: dort ist alles UTC (siehe `entsoe_client.py`).
ZEITZONE_DE = ZoneInfo("Europe/Berlin")
ANALYSETAG = datetime(2026, 8, 14, tzinfo=ZEITZONE_DE)


def _abrufstempel() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def snapshot_ziehen(tag: datetime = ANALYSETAG) -> dict[str, Path]:
    """Zieht die Rohdaten und legt sie unverändert mit Abrufzeitpunkt in data/ ab."""
    stempel = _abrufstempel()
    pfade = {}
    for region in ["DE-LU", *sc.REGELZONEN_DE, "Creos"]:
        csv = sc.download_csv(sc.MODULE_EE_PROGNOSE, region, tag, tag + timedelta(days=1))
        name = (f"smard_day-ahead-ee_{region}_"
                f"{tag.strftime('%Y%m%d')}_abgerufen-{stempel}.csv")
        pfad = DATA / name
        with pfad.open("x", encoding="utf-8") as datei:
            datei.write(csv)
        pfade[region] = pfad
    return pfade


def _neuester_snapshot(region: str, tag: datetime = ANALYSETAG) -> Path | None:
    treffer = sorted(DATA.glob(f"smard_day-ahead-ee_{region}_{tag:%Y%m%d}_abgerufen-*.csv"))
    return treffer[-1] if treffer else None


def _laden(region: str, tag: datetime = ANALYSETAG) -> tuple[list[str], list[dict]]:
    pfad = _neuester_snapshot(region, tag)
    if pfad is None:
        raise FileNotFoundError(
            f"Kein Snapshot für {region}. Erst 'python3 src/datenanalyse.py --snapshot' laufen lassen.")
    spalten, zeilen = sc.parse_csv(pfad.read_text(encoding="utf-8-sig"))
    validiere_csv_zeitraum(zeilen, tag, tag + timedelta(days=1))
    if not {"Wind Onshore", "Wind Offshore", "Photovoltaik"}.issubset(spalten):
        raise ValueError(f"Unvollständige Erzeugungsspalten: {pfad.name}")
    return spalten, zeilen


def _summe(zeilen: list[dict], spalte: str) -> float:
    werte = [z["werte"].get(spalte) for z in zeilen]
    if not werte or any(w is None or not isfinite(w) or w < 0 for w in werte):
        raise ValueError(f"Fehlende/ungültige Werte in {spalte}; kein Ersatz durch null.")
    return sum(werte)


def _wird_gefuehrt(zeilen: list[dict], spalte: str) -> bool:
    """Führt die Region diese Reihe überhaupt?

    Amprion und TransnetBW haben keinen Meereszugang und führen deshalb keine
    Wind-Offshore-Reihe. Das ist etwas anderes als eine Reihe, die 0 MWh meldet.
    Ein Bericht, der beides als "0 MWh" ausgibt, behauptet eine Messung, die es
    nicht gibt.
    """
    return any(z["werte"].get(spalte) is not None for z in zeilen)


def _fehlend(zeilen: list[dict], spalte: str) -> int:
    return sum(1 for z in zeilen if z["werte"].get(spalte) is None)


def analysieren(tag: datetime = ANALYSETAG) -> dict:
    ergebnis: dict = {
        "analysetag": tag.strftime("%d.%m.%Y"),
        "kategorie": sc.KATEGORIE,
        "gerechnet_am": datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC"),
        "quelle": "Bundesnetzagentur | SMARD.de",
    }

    regionen = ["DE-LU", *sc.REGELZONEN_DE, "Creos"]
    daten = {}
    for region in regionen:
        spalten, zeilen = _laden(region, tag)
        daten[region] = (spalten, zeilen)

    ergebnis["eingabedateien"] = {
        r: {"datei": _neuester_snapshot(r, tag).name,
            "sha256": sha256(_neuester_snapshot(r, tag).read_bytes()).hexdigest()}
        for r in regionen}

    def gebietssumme(region, spalte):
        zeilen = daten[region][1]
        if (spalte == "Wind Offshore" and region in {"Amprion", "TransnetBW", "Creos"}
                and all(z["werte"].get(spalte) is None for z in zeilen)):
            return 0.0  # strukturell unbesetzte Komponente, keine Nullmessung
        return _summe(zeilen, spalte)

    # --- Vollständigkeit und Auflösung -------------------------------------
    ergebnis["vollstaendigkeit"] = {}
    for region in regionen:
        spalten, zeilen = daten[region]
        ergebnis["vollstaendigkeit"][region] = {
            "zeilen": len(zeilen),
            "erwartet": 96,
            "spalten": spalten,
            "fehlende_werte": {s: _fehlend(zeilen, s) for s in spalten},
        }

    # --- Verfügbarkeit je Regelzone ----------------------------------------
    # Kernfrage: Liegen die Prognosen regelzonenscharf vor?
    ergebnis["regelzonen_verfuegbarkeit"] = {}
    for region in sc.REGELZONEN_DE:
        spalten, zeilen = daten[region]
        ergebnis["regelzonen_verfuegbarkeit"][region] = {
            s: (round(_summe(zeilen, s), 1) if _wird_gefuehrt(zeilen, s) else None)
            for s in spalten
        }

    # --- Interne Konsistenz: DE-LU == Summe(4 Regelzonen) + Creos? ----------
    konsistenz = {}
    for spalte in ["Wind Onshore", "Photovoltaik", "Wind Offshore"]:
        gebotszone = gebietssumme("DE-LU", spalte)
        regelzonen = sum(gebietssumme(r, spalte) for r in sc.REGELZONEN_DE)
        luxemburg = gebietssumme("Creos", spalte)
        rest = gebotszone - regelzonen - luxemburg
        konsistenz[spalte] = {
            "gebotszone_DE-LU": round(gebotszone, 1),
            "summe_4_regelzonen": round(regelzonen, 1),
            "luxemburg_creos": round(luxemburg, 1),
            "unerklaerter_rest_MWh": round(rest, 3),
            "unerklaerter_rest_prozent": round(rest / gebotszone * 100, 4) if gebotszone else None,
        }
    ergebnis["interne_konsistenz"] = konsistenz

    return ergebnis


# ---------------------------------------------------------------------------
# Quellenübergreifende Konsistenz
# ---------------------------------------------------------------------------

def quellenvergleich(tag: datetime = ANALYSETAG, *, xml_text: str) -> dict:
    """Vergleicht SMARD und ENTSO-E für dieselbe Größe und denselben Zeitraum.

    Vergleichsbasis: Day-Ahead-Prognose Wind Onshore / Wind Offshore / Solar für
    die Gebotszone DE-LU, 96 Viertelstunden. SMARD liefert Arbeit (MWh je
    Viertelstunde), ENTSO-E Leistung (MW); umgerechnet wird über MWh = MW / 4.

    Voraussetzung auf der ENTSO-E-Seite ist die A03-Rekonstruktion; ohne sie
    ist der Vergleich wertlos (siehe `a03_gegenprobe`).
    """
    ende = tag + timedelta(days=1)
    spalten, zeilen = _laden("DE-LU", tag)
    smard = {s: [z["werte"][s] for z in zeilen] for s in spalten}
    for spalte in spalten:
        _summe(zeilen, spalte)
    entso = ec.reihen_auf_raster(xml_text, 96, expected_start=tag, expected_end=ende)

    paare = [("Photovoltaik", "Solar"), ("Wind Onshore", "Wind Onshore"),
             ("Wind Offshore", "Wind Offshore")]
    ergebnis = {}
    for smard_name, entso_name in paare:
        a = smard[smard_name]
        b = [w / 4 for w in entso[entso_name]]
        diff = [abs(x - y) for x, y in zip(a, b)]
        ergebnis[smard_name] = {
            "viertelstunden": len(diff),
            "tagessumme_smard_MWh": round(sum(a), 1),
            "tagessumme_entsoe_MWh": round(sum(b), 1),
            "max_abweichung_MWh": round(max(diff), 4),
            "mittlere_abweichung_MWh": round(sum(diff) / len(diff), 4),
            "identisch_im_rundungsrahmen": sum(1 for d in diff if d <= 0.005),
        }
    return ergebnis


def a03_gegenprobe(tag: datetime = ANALYSETAG, *, xml_text: str) -> dict:
    """Beziffert, welchen Fehler die A03-Rekonstruktion verhindert.

    Ohne Rekonstruktion entsteht ein Fehler in der Größenordnung der
    Mittagsspitze. Die Rekonstruktion ist also notwendig, nicht kosmetisch.
    """
    ende = tag + timedelta(days=1)
    spalten, zeilen = _laden("DE-LU", tag)
    _summe(zeilen, "Photovoltaik")
    smard_pv = [z["werte"]["Photovoltaik"] for z in zeilen]
    raster = ec.reihen_auf_raster(xml_text, 96, expected_start=tag, expected_end=ende)
    reihe = next(r for r in ec.parse_timeseries_punkte(xml_text) if r["psr_type"] == "B16")
    if reihe["curve_type"] != "A03":
        raise ValueError("Gegenprobe benötigt eine A03-Solarreihe.")
    punkte = reihe["punkte"]

    naiv = [q / 4 for _, q in punkte] + [0.0] * (96 - len(punkte))
    korrekt = [w / 4 for w in raster["Solar"]]
    fehler_naiv = [abs(x - y) for x, y in zip(smard_pv, naiv)]
    fehler_korrekt = [abs(x - y) for x, y in zip(smard_pv, korrekt)]
    schlimmste = fehler_naiv.index(max(fehler_naiv))

    return {
        "gemeldete_punkte": len(punkte),
        "soll_punkte": 96,
        "max_fehler_ohne_rekonstruktion_MWh": round(max(fehler_naiv), 1),
        "max_fehler_mit_rekonstruktion_MWh": round(max(fehler_korrekt), 4),
        "schlimmste_viertelstunde": zeilen[schlimmste]["von"],
    }


def bericht(ergebnis: dict) -> str:
    zeilen = []
    add = zeilen.append
    add(f"Datenqualitäts- und Konsistenzanalyse SMARD: {ergebnis['kategorie']}")
    add(f"Analysetag {ergebnis['analysetag']}, gerechnet {ergebnis['gerechnet_am']}")
    add(f"Quelle: {ergebnis['quelle']}")
    add("")
    add("1) Vollständigkeit und Auflösung")
    for region, v in ergebnis["vollstaendigkeit"].items():
        fehl = sum(v["fehlende_werte"].values())
        add(f"   {region:<12} {v['zeilen']:>3}/{v['erwartet']} Viertelstunden, "
            f"{len(v['spalten'])} Reihen, fehlende Werte: {fehl}")
    add("")
    add("2) Liegen die Day-Ahead-Prognosen regelzonenscharf vor?")
    for region, werte in ergebnis["regelzonen_verfuegbarkeit"].items():
        teile = "; ".join(
            f"{k} {v:,.0f} MWh" if v is not None else f"{k} nicht geführt"
            for k, v in werte.items())
        add(f"   {region:<12} {teile}")
    add("")
    add("3) Interne Konsistenz: Gebotszone = Summe der Regelzonen + Luxemburg?")
    for spalte, k in ergebnis["interne_konsistenz"].items():
        add(f"   {spalte}")
        add(f"      DE-LU {k['gebotszone_DE-LU']:>12,.1f} MWh")
        add(f"      Summe Regelzonen {k['summe_4_regelzonen']:>10,.1f} MWh")
        add(f"      Luxemburg (Creos) {k['luxemburg_creos']:>9,.1f} MWh")
        add(f"      unerklärter Rest {k['unerklaerter_rest_MWh']:>10,.3f} MWh "
            f"({k['unerklaerter_rest_prozent']} %)")
    if "quellenvergleich" in ergebnis:
        add("")
        add("4) Quellenvergleich SMARD und ENTSO-E (gleiche Größe, gleicher Zeitraum)")
        for name, v in ergebnis["quellenvergleich"].items():
            add(f"   {name}")
            add(f"      Tagessumme SMARD {v['tagessumme_smard_MWh']:>12,.1f} MWh, "
                f"ENTSO-E {v['tagessumme_entsoe_MWh']:>12,.1f} MWh")
            add(f"      max. Abweichung je Viertelstunde {v['max_abweichung_MWh']:.4f} MWh, "
                f"deckungsgleich {v['identisch_im_rundungsrahmen']}/{v['viertelstunden']}")
    if "a03_gegenprobe" in ergebnis:
        g = ergebnis["a03_gegenprobe"]
        add("")
        add("5) Gegenprobe: was die A03-Rekonstruktion verhindert (Photovoltaik)")
        add(f"   ENTSO-E meldet {g['gemeldete_punkte']} statt {g['soll_punkte']} Punkte")
        add(f"   ohne Rekonstruktion max. Fehler {g['max_fehler_ohne_rekonstruktion_MWh']:>10,.1f} MWh/Viertelstunde")
        add(f"   mit  Rekonstruktion max. Fehler {g['max_fehler_mit_rekonstruktion_MWh']:>10,.4f} MWh/Viertelstunde")
        add(f"   größter Fehler bei {g['schlimmste_viertelstunde']}")
    return "\n".join(zeilen)


if __name__ == "__main__":
    if "--snapshot" in sys.argv:
        pfade = snapshot_ziehen()
        print("Snapshots abgelegt:")
        for region, pfad in pfade.items():
            print(f"   {region:<12} {pfad.name}")
        print()

    ergebnis = analysieren()
    ergebnis["quellenvergleich_status"] = (
        "Nicht erneut berechnet: passendes ENTSO-E-Roharchiv für den 14.08.2026 fehlt. "
        "Die Funktionen erfordern ausdrücklich übergebenes XML mit passenden Periodengrenzen.")
    ziel = DATA / f"analyse_datenqualitaet_{_abrufstempel()}.json"
    with ziel.open("x", encoding="utf-8") as datei:
        json.dump(ergebnis, datei, ensure_ascii=False, indent=2)
    print(bericht(ergebnis))
    print(ergebnis["quellenvergleich_status"])
    print(f"\nErgebnisse als JSON: {ziel.relative_to(ziel.parent.parent)}")
