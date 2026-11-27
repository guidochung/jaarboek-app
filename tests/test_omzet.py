"""Kassa-omzet -> Asperion-boeking (FMUTAC). Echte rapporten van vier klanten met vier kassasystemen.

Bar Bellini oktober: de CSV moet byte-gelijk zijn aan wat het kantoor in Asperion inleest.
Ninja Sushi, Kazoku en Black Pepper: de boeking moet in Asperion precies zo uitkomen als op de
schermafdrukken van het kantoor (rekening, debet/credit excl. btw, btw-bedrag).
"""
import json
from collections import Counter
from decimal import Decimal

import pytest
from conftest import DATA

from jaarboek.kassalezers import Dagrapport, Productrapport, lees_kassarapport, lees_omzet
from jaarboek.omzet import (OmzetFout, OnbekendFormaat, OnvolledigeInvoer, asperion_weergave, bewaar_koppeling,
                            koppeling_uit_json, laad_koppeling, maak_boeking, naar_fmutac, prefix_uit_naam,
                            stel_betalingen_voor, stel_koppeling_voor)

DAG = DATA / "bellini_dagrapport_2026-10.xlsx"
PRODUCT = DATA / "bellini_productrapport_2026-10.xlsx"
VERWACHT = DATA / "bellini_omzet_2026-10_verwacht.csv"     # zoals het kantoor hem in Asperion inleest
D = Decimal


@pytest.fixture(scope="module")
def bellini():
    return lees_omzet([(PRODUCT.name, PRODUCT), (DAG.name, DAG.read_bytes())])   # volgorde maakt niet uit


def _boeking(kassa, koppeling=None):
    koppeling = koppeling or laad_koppeling()
    return maak_boeking(kassa, stel_koppeling_voor(kassa, koppeling), koppeling)


def _klant(slug):
    return koppeling_uit_json((DATA / f"omzet_koppeling_{slug}.json").read_text(encoding="utf-8"))


def _weergave(b):
    """Asperion-weergave als (rekening, debet, credit, btw), zonder volgorde."""
    return Counter((r["rekening"], r["debet"], r["credit"], r["btw"]) for r in asperion_weergave(b))


def _w(rek, debet="0", credit="0", btw="0"):
    return (rek, D(debet), D(credit), D(btw))


# --------------------------------------------------------------------------
# Bar Bellini (Excel, dag- en productrapport)
# --------------------------------------------------------------------------
def test_bellini_csv_is_gelijk_aan_het_voorbeeld(bellini):
    b = _boeking(bellini)
    assert naar_fmutac(b) == VERWACHT.read_bytes()
    assert b.bestandsnaam == "Bar_Bellini_Omzet_2026-10_Kasboek.csv"
    assert (b.boekstuk, b.dagboek, b.periode) == ("BELL1026", "10", "2026-10")


def test_bellini_rapporten_worden_herkend(bellini):
    assert isinstance(lees_kassarapport(DAG), Dagrapport) and isinstance(lees_kassarapport(PRODUCT), Productrapport)
    assert bellini.bedrijf == "Bar Bellini" and bellini.dagen == 31 and bellini.lezer == "Dag- en productrapport (Excel)"
    assert [o.groep for o in bellini.omzet][:3] == ["Bier", "Bijgerechten", "Borrel"]
    with pytest.raises(OmzetFout, match="Twee rapporten van dezelfde soort"):
        lees_omzet([("a", DAG), ("b", DAG)])
    with pytest.raises(OnvolledigeInvoer, match="het productrapport"):
        lees_omzet([("a", DAG)])
    with pytest.raises(OmzetFout, match="kan dit Excel-bestand niet openen"):
        lees_kassarapport(b"PK geen excel", "kapot.xlsx")
    with pytest.raises(OnbekendFormaat):
        lees_omzet([("x.csv", b"datum;omzet\n")])


def test_bellini_controles(bellini):
    b = _boeking(bellini)
    status = {c["controle"]: c["status"] for c in b.controles}
    assert not b.fout
    assert status["Verschil"] == "LET OP" and b.verschil == D("70.52")
    assert [s for c, s in status.items() if c != "Verschil"] == ["OK"] * (len(status) - 1)
    assert status["Omzet 9% gelijk aan dagrapport"] == status["Omzet 21% gelijk aan dagrapport"] == "OK"
    assert sum(r.bedrag for r in b.regels if r.bedrag is not None) == 0
    # btw per regel = incl. × t/(100+t): 1 cent meer dan het dagrapport (39.651,81), binnen de marge
    assert (b.omzet_bruto, b.omzet_btw) == (D("292723.96"), D("39651.82"))


def test_bellini_juli_nederlandse_export():
    """Zelfde kassa, Nederlandse labels, datum '1 jul, 2026' en bedrijf 'Bar Bellini, Bar Bellini'."""
    k = lees_omzet([(p, DATA / p) for p in ("bellini_dagrapport_2026-07_nl.xlsx", "bellini_productrapport_2026-07_nl.xlsx")])
    assert (k.bedrijf, k.start.isoformat(), k.eind.isoformat()) == ("Bar Bellini", "2026-07-01", "2026-07-31")
    b = _boeking(k)
    status = {c["controle"]: c["status"] for c in b.controles}
    assert not b.fout and status["Rapporten horen bij elkaar"] == "OK"
    assert status["Omzet 9% gelijk aan dagrapport"] == status["Omzet 21% gelijk aan dagrapport"] == "OK"
    assert b.verschil == D("-161.01") and status["Niet geboekte posten in het rapport"] == "LET OP"
    assert dict(k.niet_geboekt)["Cadeaubon betalingen"] == D("300.00")


def test_onbekende_categorie_blokkeert(bellini):
    k = laad_koppeling()
    k["categorieen"] = [c for c in k["categorieen"] if c["categorie"] != "Lunch"]
    regels = stel_koppeling_voor(bellini, k)
    lunch = next(r for r in regels if r.categorie == "Lunch")
    assert (lunch.rekening, lunch.bekend, lunch.omschrijving) == ("", False, "Lunch")
    b = maak_boeking(bellini, regels, k)
    assert b.fout and any("Lunch 9%: geen rekening" in c["detail"] for c in b.controles)
    lunch.rekening = "8100"                               # gekozen in de app
    assert not maak_boeking(bellini, regels, k).fout
    opgeslagen = bewaar_koppeling(k, regels)
    assert {"categorie": "Lunch", "tarief": "", "omschrijving": "Lunch", "rekening": "8100"} in opgeslagen["categorieen"]
    assert all(c["categorie"] != "Lunch" for c in k["categorieen"])                    # origineel onaangetast


def test_categorie_met_twee_tarieven_wordt_gesplitst(bellini):
    k = laad_koppeling()
    k["categorieen"] = [c for c in k["categorieen"] if c["categorie"] != "Other"]
    k["categorieen"].append({"categorie": "other", "tarief": "", "omschrijving": "Overig", "rekening": "8080"})
    regels = [r for r in stel_koppeling_voor(bellini, k) if r.categorie == "Other"]
    assert [(r.omschrijving, r.tarief, r.rekening, r.gesplitst) for r in regels] == [
        ("Overig 9%", 9, "8080", True), ("Overig 21%", 21, "8080", True)]
    regels[0].rekening = "8090"
    nieuw = bewaar_koppeling(k, regels)
    other = [c for c in nieuw["categorieen"] if c["categorie"].lower() == "other"]
    assert {"categorie": "Other", "tarief": "9", "omschrijving": "Overig 9%", "rekening": "8090"} in other
    assert len(other) == 2                                # 21% volgt nog uit de algemene regel


def test_prefix_tegenrekeningen_en_upload(bellini):
    assert prefix_uit_naam("Bar Bellini") == "BELL" and prefix_uit_naam("Café de Zwaan B.V.") == "ZWAA"
    k = koppeling_uit_json(json.dumps({"boekstuk_prefix": "BB", "dagboek": "20",
                                       "tegenrekeningen": {"pin": "1260"}, "categorieen": laad_koppeling()["categorieen"]}))
    b = _boeking(bellini, k)
    assert (b.boekstuk, b.dagboek) == ("BB1026", "20")
    pin = b.regels[1]
    assert (pin.rekening, pin.bedrag) == ("1260", D("-267648.07"))
    assert b.regels[2].rekening == "1255"                 # niet meegegeven: standaard
    assert maak_boeking(bellini, stel_koppeling_voor(bellini, k), k, boekstuk="102").boekstuk == "102"
    with pytest.raises(OmzetFout):
        koppeling_uit_json("{kapot")
    with pytest.raises(OmzetFout, match="boekingsstijl"):
        koppeling_uit_json(json.dumps({"stijl": "anders"}))


def test_lege_tegenrekening_blokkeert(bellini):
    k = laad_koppeling()
    k["tegenrekeningen"]["fooi"] = ""
    b = _boeking(bellini, k)
    assert b.fout and any(c["controle"] == "Tegenrekeningen ingevuld" for c in b.controles)


def test_betaalwijze_koppeling_gaat_voor_standaard(bellini):
    k = laad_koppeling()
    betaal = stel_betalingen_voor(bellini, k)
    assert [(b.naam, b.rekening, b.herkomst) for b in betaal][:2] == [("Pinbetalingen", "1255", "standaard"),
                                                                      ("Contant", "1255", "standaard")]
    betaal[1].rekening = "1250"
    nieuw = bewaar_koppeling(k, [], betaal)
    assert nieuw["betaalwijzen"] == [{"betaalwijze": "Contant", "rekening": "1250"}]   # alleen wat afwijkt
    contant = stel_betalingen_voor(bellini, nieuw)[1]
    assert (contant.rekening, contant.herkomst) == ("1250", "koppeling")


# --------------------------------------------------------------------------
# Nieuwe kassasystemen (PDF): uitkomst in Asperion = schermafdruk van het kantoor
# --------------------------------------------------------------------------
def test_ninja_sushi_boekhouding_overzicht():
    k = lees_omzet([("ninja.pdf", DATA / "ninja_boekhouding_2026-01.pdf")])
    assert (k.bedrijf, k.lezer, f"{k.eind:%d-%m-%Y}") == ("Ninja Sushi, Almere", "Boekhouding overzicht (PDF)", "31-01-2026")
    b = _boeking(k, _klant("ninja_sushi_almere"))
    assert not b.fout and b.dagboek == "1" and b.stijl == "compact"
    assert _weergave(b) == Counter([_w("8100", credit="34172.44", btw="3075.52"), _w("8000", credit="237.50", btw="49.88"),
                                    _w("1250", debet="575.84"), _w("1255", debet="36959.50")])
    assert {r.omschrijving for r in b.regels} == {"Omzet 2026-01"}
    status = {c["controle"]: c["status"] for c in b.controles}
    assert status["Verschil"] == "LET OP" and b.verschil == D("-0.03")           # kassa zelf: 3 cent verschil
    assert status["Omzet 21% gelijk aan btw-overzicht"] == "LET OP"              # kassa-btw 48,62, geboekt 49,88


def test_kazoku_omzetlijst():
    k = lees_omzet([("kazoku.pdf", DATA / "kazoku_omzetlijst_2026-10.pdf")])
    assert (k.bedrijf, k.lezer) == ("Kazoku", "Omzetlijst (PDF)")
    assert [(b.naam, b.bedrag) for b in k.betalingen] == [("Online", D("14297.56")), ("Contant", D("519.74")),
                                                          ("Pin", D("369.90"))]
    b = _boeking(k, _klant("kazoku"))
    assert not b.fout
    assert _weergave(b) == Counter([_w("8100", credit="9145.21", btw="823.07"), _w("8000", credit="4313.17", btw="905.76"),
                                    _w("1250", debet="519.74"), _w("1255", debet="14667.47")])
    status = {c["controle"]: c for c in b.controles}
    assert status["Categorieën tegen btw-overzicht"]["status"] == "LET OP"     # kassa telt € 7,37 extra mee
    assert status["Niet geboekte posten in het rapport"]["status"] == "LET OP"  # fooi € 70,05 los van de betalingen


def test_black_pepper_summary_rapporten():
    maand = ("maand.pdf", DATA / "blackpepper_maandrapport_2026-09.pdf")
    cat = ("categorie.pdf", DATA / "blackpepper_categorierapport_2026-09.pdf")
    k = lees_omzet([cat, maand])
    assert (k.bedrijf, f"{k.start:%d-%m-%Y}", f"{k.eind:%d-%m-%Y}") == ("Black Pepper Grill Restaurant", "01-09-2026",
                                                                         "30-09-2026")      # kassadag 12:00-12:00
    assert [b.naam for b in k.betalingen] == ["Cash", "Quick Cash", "Maestro", "MasterCard", "Visa"]   # geen subtotalen
    b = _boeking(k, _klant("black_pepper_grill_restaurant"))
    assert not b.fout and b.verschil == 0 and b.dagboek == "10"
    assert {r.omschrijving for r in b.regels} == {"omzet 2026-09"}
    assert _weergave(b) == Counter([
        _w("8100", credit="16601.65", btw="1494.15"), _w("8100", credit="4713.94", btw="424.26"),
        _w("8100", credit="4146.15", btw="373.15"), _w("8100", credit="1594.50", btw="143.50"),
        _w("8100", credit="460.09", btw="41.41"), _w("8100", credit="46.33", btw="4.17"),
        _w("8050", credit="393.49", btw="35.41"), _w("8000", credit="1526.74", btw="320.61"),
        _w("8050", credit="1961.56", btw="176.54"), _w("8030", credit="1695.58", btw="356.07"),
        _w("8070", credit="692.25", btw="62.30"), _w("8020", credit="210.74", btw="44.26"),
        _w("8030", credit="764.05", btw="160.45"), _w("8020", credit="560.70", btw="117.75"),
        _w("8050", credit="10.55", btw="0.95"),
        _w("1250", debet="5177.90"), _w("1255", debet="33955.40")])
    alleen_maand = lees_omzet([maand])                     # zonder categorierapport: per btw-tarief
    assert [o.groep for o in alleen_maand.omzet] == ["Omzet 9%", "Omzet 21%"]
    with pytest.raises(OnvolledigeInvoer, match="maandrapport"):
        lees_omzet([cat])


def test_verschillende_kassas_samen_geeft_fout():
    with pytest.raises(OmzetFout, match="verschillende kassasystemen"):
        lees_omzet([("k.pdf", DATA / "kazoku_omzetlijst_2026-10.pdf"), ("n.pdf", DATA / "ninja_boekhouding_2026-01.pdf")])
