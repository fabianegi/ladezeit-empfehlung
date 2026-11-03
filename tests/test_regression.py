"""Offline-Regression: archivierte Ergebnisse und zuvor fehlerhafte Eingaben."""
import contextlib
import io
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import empfehlung as e
import poc
import datenanalyse as da
import entsoe_client as ec

DATA = Path(__file__).resolve().parents[1] / 'data'
START = datetime(2026, 8, 14, tzinfo=poc.TZ_DE)


def punkte(n=32):
    return [e.Prognosepunkt(START + i * e.RASTER, 10, 100) for i in range(n)]


def xml(points='<Point><position>1</position><quantity>0</quantity></Point>', curve='A03'):
    return ('<GL_MarketDocument><TimeSeries><MktPSRType><psrType>B16</psrType></MktPSRType>'
            '<quantity_Measure_Unit.name>MAW</quantity_Measure_Unit.name>'
            f'<curveType>{curve}</curveType><Period><timeInterval><start>2026-08-14T00:00Z</start>'
            '<end>2026-08-14T01:00Z</end></timeInterval><resolution>PT15M</resolution>'
            f'{points}</Period></TimeSeries></GL_MarketDocument>')


class Regression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.archiv, cls.stand = poc.lade_archiv(START)
        cls.ee = next(DATA.glob('smard_day-ahead-ee_DE-LU_20260814_*.csv')).read_text(encoding='utf-8-sig')
        cls.last = next(DATA.glob('smard_netzlast*.csv')).read_text(encoding='utf-8-sig')
        cls.last = '\n'.join(cls.last.splitlines()[:97])
        # Das ENTSO-E-XML liegt aus Lizenzgründen nicht im Repository; die beiden
        # davon abhängigen Tests werden dann übersprungen, statt den Lauf abzubrechen.
        xml_datei = next(DATA.glob('entsoe*.xml'), None)
        cls.entsoe = xml_datei.read_text() if xml_datei else None

    def test_drei_archivfaelle(self):
        for hour, duration, begin, score, alternative in [
            (7, 3, 0, 0.4898990299566247, 3),
            (18, 3, 12, 1.094418534232235, 9),
            (7, 5, 0, 0.467736909149137, None),
        ]:
            with self.subTest(hour=hour, duration=duration):
                r = e.empfehle(self.archiv, START.replace(hour=hour), timedelta(hours=duration), START)
                self.assertEqual(r.empfohlen.beginn, START.replace(hour=begin))
                self.assertAlmostEqual(r.empfohlen.mittlerer_anteil, score, places=12)
                self.assertEqual(r.empfohlen.dauer, timedelta(hours=duration))
                self.assertEqual(r.alternative.beginn.hour if r.alternative else None, alternative)
                if alternative is None:
                    self.assertTrue(any('kein überschneidungsfreies' in h for h in r.hinweise))

    def test_unpassende_dauern(self):
        for minutes in [0, -15, 67, 68, 1]:
            with self.subTest(minutes=minutes), self.assertRaises(ValueError):
                e.alle_fenster(punkte(), timedelta(minutes=minutes))

    def test_gueltige_dauern(self):
        for minutes in [15, 60, 75]:
            self.assertEqual(e.alle_fenster(punkte(), timedelta(minutes=minutes))[0].dauer,
                             timedelta(minutes=minutes))

    def test_luecke_wird_nicht_ueberbrueckt(self):
        p = punkte(); del p[2]
        with self.assertRaises(ValueError): e.alle_fenster(p, timedelta(hours=1))

    def test_duplikat(self):
        p = punkte(); p[2] = p[1]
        with self.assertRaises(ValueError): e.validiere_punkte(p)

    def test_unsortiert(self):
        self.assertEqual(e.validiere_punkte(punkte()[::-1]), punkte())

    def test_keine_nan_inf_negativ_null_last(self):
        for generation, load in [(1, 0), (1, -1), (1, float('nan')), (1, float('inf')),
                                 (-1, 100), (float('nan'), 100), (float('inf'), 100)]:
            with self.subTest(generation=generation, load=load), self.assertRaises(ValueError):
                e.Prognosepunkt(START, generation, load)
        self.assertEqual(e.Prognosepunkt(START, 0, 100).anteil, 0)

    def test_zeitraum_nicht_vollstaendig(self):
        with self.assertRaises(ValueError):
            e.empfehle(punkte(4), START.replace(hour=7), timedelta(hours=1), START)

    def test_gleichstand_chronologisch_und_neutrale_alternative(self):
        r = e.empfehle(punkte(), START.replace(hour=8), timedelta(hours=1))
        self.assertEqual(r.empfohlen.beginn, START)
        self.assertEqual(r.alternative.beginn, START.replace(hour=1))
        self.assertNotIn('weniger', r.begruendung)

    def test_lastgewichtung(self):
        p = [e.Prognosepunkt(START, 10, 10), e.Prognosepunkt(START+e.RASTER, 0, 90)]
        self.assertEqual(e.alle_fenster(p, e.RASTER*2)[0].mittlerer_anteil, 0.1)

    def test_sparkline_letzter_punkt(self):
        p=punkte(28);p[-1].wind_pv_mwh=100
        s=e.sparkline(p)
        self.assertEqual(len(s),24);self.assertGreater(s[-1], s[0])

    def test_csv_archiveinlesen(self):
        self.assertEqual(len(poc.prognose_aus_csv(self.ee,self.last,START,START+timedelta(days=1))),96)

    def test_csv_fehlende_erzeugung(self):
        lines = self.ee.splitlines()
        k = next(i for i, name in enumerate(lines[0].split(';')) if name.startswith('Wind Offshore'))
        row = lines[1].split(';'); row[k] = '-'; lines[1] = ';'.join(row)
        with self.assertRaises(ValueError):
            poc.prognose_aus_csv('\n'.join(lines), self.last, START, START+timedelta(days=1), 'TransnetBW')

    def test_csv_manipulierte_zellen(self):
        for which, value in [('ee','-'),('last','-'),('last','0'),('last','NaN')]:
            ee, last = self.ee.splitlines(), self.last.splitlines()
            lines = ee if which=='ee' else last
            row=lines[1].split(';');row[2]=value;lines[1]=';'.join(row)
            with self.subTest(which=which,value=value), self.assertRaises(ValueError):
                poc.prognose_aus_csv('\n'.join(ee),'\n'.join(last),START,START+timedelta(days=1))

    def test_csv_fehlende_spalte(self):
        with self.assertRaises(ValueError):
            poc.prognose_aus_csv(self.ee.replace('Photovoltaik','Unbekannt'),self.last,START,START+timedelta(days=1))

    def test_csv_falscher_tag(self):
        with self.assertRaises(ValueError):
            poc.prognose_aus_csv(self.ee,self.last,START+timedelta(days=1),START+timedelta(days=2))

    def test_csv_fehlender_zeitpunkt(self):
        with self.assertRaises(ValueError):
            poc.prognose_aus_csv('\n'.join(self.ee.splitlines()[:-1]),self.last,START,START+timedelta(days=1))

    def test_offshore_nur_strukturell_unbesetzt(self):
        lines=self.ee.splitlines(); header=lines[0].split(';')
        k=next(i for i,h in enumerate(header) if h.startswith('Wind Offshore'))
        for i in range(1,len(lines)):
            row=lines[i].split(';');row[k]='-';lines[i]=';'.join(row)
        csv='\n'.join(lines)
        self.assertEqual(len(poc.prognose_aus_csv(csv,self.last,START,START+timedelta(days=1),'TransnetBW')),96)
        with self.assertRaises(ValueError):
            poc.prognose_aus_csv(csv,self.last,START,START+timedelta(days=1),'DE-LU')

    def test_dst_mehrdeutige_und_fehlende_ortszeit(self):
        for text in ['25.10.2026 02:15','29.03.2026 02:15']:
            with self.subTest(text=text), self.assertRaises(ValueError): poc.ortszeit(text)

    def test_acknowledgement(self):
        ack='<Acknowledgement_MarketDocument><Reason><code>999</code></Reason></Acknowledgement_MarketDocument>'
        for fn in [ec.parse_timeseries, ec.parse_timeseries_punkte, ec.reihen_auf_raster]:
            with self.subTest(fn=fn.__name__), self.assertRaises(ec.EntsoeDatenfehler): fn(ack)

    def test_xml_ohne_reihen(self):
        with self.assertRaises(ec.EntsoeDatenfehler): ec.parse_timeseries('<GL_MarketDocument/>')

    def test_a03_ungueltige_positionen(self):
        for points in [[],[(2,0)],[(1,0),(1,1)],[(1,0),(5,1)],[(1,float('nan'))]]:
            with self.subTest(points=points), self.assertRaises(ec.EntsoeDatenfehler):
                ec.rekonstruiere_raster(points,4)

    def test_a03_fortschreibung(self):
        self.assertEqual(ec.rekonstruiere_raster([(1,0),(3,10)],4),[0,0,10,10])

    def test_a01_luecke(self):
        with self.assertRaises(ec.EntsoeDatenfehler): ec.reihen_auf_raster(xml(curve='A01'))

    def test_periodenlaenge_88(self):
        if self.entsoe is None: self.skipTest('ENTSO-E-XML nicht vorhanden (aus Lizenzgründen nicht im Repository)')
        self.assertEqual({len(v) for v in ec.reihen_auf_raster(self.entsoe).values()},{88})
        with self.assertRaises(ec.EntsoeDatenfehler): ec.reihen_auf_raster(self.entsoe,96)

    def test_mehrperioden_nicht_stillschweigend_ignoriert(self):
        text=xml();period=text[text.index('<Period>'):text.index('</Period>')+9]
        text=text.replace('</TimeSeries>',period+'</TimeSeries>')
        self.assertEqual(len(ec.parse_timeseries(text)),2)
        with self.assertRaises(ec.EntsoeDatenfehler): ec.reihen_auf_raster(text)

    def test_falscher_quellenvergleichstag(self):
        if self.entsoe is None: self.skipTest('ENTSO-E-XML nicht vorhanden (aus Lizenzgründen nicht im Repository)')
        with self.assertRaises(ec.EntsoeDatenfehler): da.quellenvergleich(xml_text=self.entsoe)

    def test_datumsauswahl(self):
        self.assertIn('_20260814_',da._neuester_snapshot('DE-LU').name)
        self.assertIn('_20260815_',da._neuester_snapshot('DE-LU',START+timedelta(days=1)).name)

    def test_konsistenz_nur_14_august(self):
        with patch('smard_client.download_csv',side_effect=AssertionError('Netzwerk verboten')):
            r=da.analysieren()
        self.assertEqual({v['zeilen'] for v in r['vollstaendigkeit'].values()},{96})
        self.assertEqual(r['interne_konsistenz']['Wind Onshore']['unerklaerter_rest_MWh'],-0.08)
        self.assertEqual(r['interne_konsistenz']['Photovoltaik']['unerklaerter_rest_MWh'],-0.04)
        self.assertTrue(all('_20260814_' in v['datei'] for v in r['eingabedateien'].values()))

    def test_cli_offline_ohne_netzwerk(self):
        for hour,duration in [('07:00','3'),('18:00','3'),('07:00','5')]:
            with patch.object(sys,'argv',['poc','--offline','--tag','2026-08-14','--abfahrt',hour,'--dauer',duration]), patch('smard_client.download_csv',side_effect=AssertionError('Netzwerk verboten')), contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(poc.main(),0)
                self.assertIn('Archivabrufe',out.getvalue())
                self.assertIn('DE-LU',out.getvalue())

    def test_cli_fehler_vor_abruf(self):
        for duration in ['nan','inf','0','-1','1.1']:
            with patch.object(sys,'argv',['poc','--dauer',duration]), patch('smard_client.download_csv',side_effect=AssertionError('Netzwerk verboten')), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(poc.main(),2)


if __name__ == '__main__': unittest.main()
