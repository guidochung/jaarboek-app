"""Kassa-omzet -> Asperion-boeking (FMUTAC). Echte rapporten van Bar Bellini, oktober 2026."""
import json
from decimal import Decimal

import pytest
from conftest import DATA

from jaarboek.omzet import (Dagrapport, OmzetFout, Productrapport, bewaar_koppeling, koppeling_uit_json,
                            laad_koppeling, lees_kassarapport, lees_kassarapporten, maak_boeking, naar_fmutac,
                            prefix_uit_naam, stel_koppeling_voor)

DAG = DATA / "bellini_dagrapport_2026-10.xlsx"
PRODUCT = DATA / "bellini_productrapport_2026-10.xlsx"
VERWACHT = DATA / "bellini_omzet_2026-10_verwacht.csv"     # zoals het kantoor hem in Asperion inleest


@pytest.fixture(scope="module")
def rapporten():
    return lees_kassarapporten([(PRODUCT.name, PRODUCT), (DAG.name, DAG.read_bytes())])   # volgorde maakt niet uit


def _boeking(rapporten, koppeling=None):
    dag, prod = rapporten
    koppeling = koppeling or laad_koppeling()
    return maak_boeking(dag, prod, stel_koppeling_voor(prod, koppeling), koppeling)


def test_csv_is_gelijk_aan_het_voorbeeld(rapporten):
    b = _boeking(rapporten)
    assert naar_fmutac(b) == VERWACHT.read_bytes()
    assert b.bestandsnaam == "Bar_Bellini_Omzet_2026-10_Kasboek.csv"
    assert (b.boekstuk, b.dagboek, b.periode) == ("BELL1026", "10", "2026-10")


def test_rapporten_worden_herkend(rapporten):
    dag, prod = rapporten
    assert isinstance(dag, Dagrapport) and isinstance(prod, Productrapport)
    assert dag.bedrijf == prod.bedrijf == "Bar Bellini" and dag.dagen == 31
    assert [c.naam for c in prod.categorieen][:3] == ["Bier", "Bijgerechten", "Borrel"]
    with pytest.raises(OmzetFout, match="precies één dagrapport"):
        lees_kassarapporten([("a", DAG), ("b", DAG)])
    with pytest.raises(OmzetFout, match="kan dit Excel-bestand niet openen"):
        lees_kassarapport(b"geen excel", "kapot.xlsx")


def test_controles(rapporten):
    b = _boeking(rapporten)
    status = {c["controle"]: c["status"] for c in b.controles}
    assert not b.fout
    assert status["Verschil"] == "LET OP" and b.verschil == Decimal("70.52")
    assert [s for c, s in status.items() if c != "Verschil"] == ["OK"] * (len(status) - 1)
    assert status["Omzet 9% gelijk aan dagrapport"] == status["Omzet 21% gelijk aan dagrapport"] == "OK"
    assert sum(r.bedrag for r in b.regels if r.bedrag is not None) == 0
    # btw-regels per categorie afgerond: 1 cent meer dan het dagrapport (39.651,81), binnen de marge
    assert (b.omzet_bruto, b.omzet_btw) == (Decimal("292723.96"), Decimal("39651.82"))


def test_onbekende_categorie_blokkeert(rapporten):
    k = laad_koppeling()
    k["categorieen"] = [c for c in k["categorieen"] if c["categorie"] != "Lunch"]
    regels = stel_koppeling_voor(rapporten[1], k)
    lunch = next(r for r in regels if r.categorie == "Lunch")
    assert (lunch.rekening, lunch.bekend, lunch.omschrijving) == ("", False, "Lunch")
    b = maak_boeking(*rapporten, regels, k)
    assert b.fout and any("Lunch 9%: geen rekening" in c["detail"] for c in b.controles)
    lunch.rekening = "8100"                               # gekozen in de app
    assert not maak_boeking(*rapporten, regels, k).fout
    opgeslagen = bewaar_koppeling(k, regels)
    assert {"categorie": "Lunch", "tarief": "", "omschrijving": "Lunch", "rekening": "8100"} in opgeslagen["categorieen"]
    assert k == {**k} and all(c["categorie"] != "Lunch" for c in k["categorieen"])    # origineel onaangetast


def test_categorie_met_twee_tarieven_wordt_gesplitst(rapporten):
    k = laad_koppeling()
    k["categorieen"] = [c for c in k["categorieen"] if c["categorie"] != "Other"]
    k["categorieen"].append({"categorie": "other", "tarief": "", "omschrijving": "Overig", "rekening": "8080"})
    regels = [r for r in stel_koppeling_voor(rapporten[1], k) if r.categorie == "Other"]
    assert [(r.omschrijving, r.tarief, r.rekening, r.gesplitst) for r in regels] == [
        ("Overig 9%", 9, "8080", True), ("Overig 21%", 21, "8080", True)]
    regels[0].rekening = "8090"
    nieuw = bewaar_koppeling(k, regels)
    other = [c for c in nieuw["categorieen"] if c["categorie"].lower() == "other"]
    assert {"categorie": "Other", "tarief": "9", "omschrijving": "Overig 9%", "rekening": "8090"} in other
    assert len(other) == 2                                # 21% volgt nog uit de algemene regel


def test_prefix_tegenrekeningen_en_upload(rapporten):
    assert prefix_uit_naam("Bar Bellini") == "BELL" and prefix_uit_naam("Café de Zwaan B.V.") == "ZWAA"
    k = koppeling_uit_json(json.dumps({"boekstuk_prefix": "BB", "dagboek": "20",
                                       "tegenrekeningen": {"pin": "1260"}, "categorieen": laad_koppeling()["categorieen"]}))
    b = _boeking(rapporten, k)
    assert (b.boekstuk, b.dagboek) == ("BB1026", "20")
    pin = b.regels[1]
    assert (pin.rekening, pin.bedrag) == ("1260", Decimal("-267648.07"))
    assert b.regels[2].rekening == "1255"                 # niet meegegeven: standaard
    with pytest.raises(OmzetFout):
        koppeling_uit_json("{kapot")


def test_lege_tegenrekening_blokkeert(rapporten):
    k = laad_koppeling()
    k["tegenrekeningen"]["fooi"] = ""
    b = _boeking(rapporten, k)
    assert b.fout and any(c["controle"] == "Tegenrekeningen ingevuld" for c in b.controles)
