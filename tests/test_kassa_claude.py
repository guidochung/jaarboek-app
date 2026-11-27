"""Claude als vangnet voor onbekende kassaformaten: echte Claude-antwoorden (26-11-2026) afgespeeld.

De antwoorden komen uit een proef met de echte API op dezelfde PDF's (kassa_claude_*_opname.json).
Getest wordt wat de code ermee doet: omzetten naar het tussenformaat en de harde controles.
"""
import copy
import json
import re
from collections import Counter
from decimal import Decimal

import pytest
from conftest import DATA
from nep_claude import NepClaude

from jaarboek.ai import ClaudeAI
from jaarboek.kassa_claude import lees_met_claude, naar_kassaomzet, staat_in, tekst_van_bestanden
from jaarboek.omzet import OmzetFout, asperion_weergave, koppeling_uit_json, maak_boeking, stel_koppeling_voor

D = Decimal
NINJA = DATA / "ninja_boekhouding_2026-01.pdf"
KAZOKU = DATA / "kazoku_omzetlijst_2026-10.pdf"


def _opname(klant):
    return json.loads((DATA / f"kassa_claude_{klant}_opname.json").read_text(encoding="utf-8"))


def _klant(slug):
    return koppeling_uit_json((DATA / f"omzet_koppeling_{slug}.json").read_text(encoding="utf-8"))


def _boeking(kassa, slug):
    k = _klant(slug)
    return maak_boeking(kassa, stel_koppeling_voor(kassa, k), k)


def test_claude_leest_ninja_zoals_het_kantoor_boekt():
    nep = NepClaude(kassa=_opname("ninja"))
    kassa = lees_met_claude([(NINJA.name, NINJA.read_bytes())], ClaudeAI(client=nep))
    vraag = nep.verzoeken[0]["messages"][0]["content"]
    assert "=== BESTAND: ninja_boekhouding_2026-01.pdf ===" in vraag and re.search(r"Boekhouding\s+overzicht", vraag)
    assert kassa.door_claude and kassa.lezer.startswith("Claude")
    b = _boeking(kassa, "ninja_sushi_almere")
    assert not b.fout
    status = {c["controle"]: c["status"] for c in b.controles}
    assert status["Bedragen staan in het rapport"] == "OK"
    weergave = Counter((r["rekening"], r["debet"], r["credit"], r["btw"]) for r in asperion_weergave(b))
    assert weergave == Counter([("8100", D(0), D("34172.44"), D("3075.52")), ("8000", D(0), D("237.50"), D("49.88")),
                                ("1250", D("575.84"), D(0), D(0)), ("1255", D("36959.50"), D(0), D(0))])


def test_verzonnen_bedrag_blokkeert_de_download():
    antwoord = _opname("ninja")
    antwoord["betalingen"][0]["bedrag"] = 1234.56          # staat nergens in het rapport
    kassa = naar_kassaomzet(antwoord, tekst_van_bestanden([(NINJA.name, NINJA.read_bytes())]), [NINJA.name])
    b = _boeking(kassa, "ninja_sushi_almere")
    fout = {c["controle"]: c["detail"] for c in b.controles if c["status"] == "FOUT"}
    assert b.fout and "€ 1.234,56" in fout["Bedragen staan in het rapport"]


def test_kassarapport_dat_niet_klopt_wordt_niet_geboekt():
    """Kazoku: Claude boekte per categorie; die tellen € 7,34 minder op dan het btw-overzicht -> FOUT."""
    kassa = naar_kassaomzet(_opname("kazoku"), tekst_van_bestanden([(KAZOKU.name, KAZOKU.read_bytes())]),
                            [KAZOKU.name])
    b = _boeking(kassa, "kazoku")
    fout = [c["controle"] for c in b.controles if c["status"] == "FOUT"]
    assert "Omzet 21% gelijk aan btw-overzicht" in fout                  # (en de sushi-categorieën zijn niet gekoppeld)
    assert any(c["controle"] == "Door Claude uitgerekende bedragen" for c in b.controles)


def test_kassadag_tot_de_volgende_middag_is_een_maand():
    antwoord = _opname("blackpepper")
    assert (antwoord["periode_van"], antwoord["periode_tot"]) == ("2026-09-01", "2026-10-01")
    tekst = tekst_van_bestanden([(p.name, p.read_bytes()) for p in sorted(DATA.glob("blackpepper_*.pdf"))])
    kassa = naar_kassaomzet(antwoord, tekst, ["maand", "categorie"])
    assert f"{kassa.eind:%d-%m-%Y}" == "30-09-2026"
    b = _boeking(kassa, "black_pepper_grill_restaurant")
    assert not b.fout and b.verschil == 0


def test_bedrag_moet_als_los_getal_in_het_rapport_staan():
    assert staat_in(D("1234.56"), "totaal € 1.234,56 incl.") and staat_in(D("1234.56"), "TOTAL 1234.56")
    assert not staat_in(D("234.56"), "totaal 1.234,56") and not staat_in(D("1.23"), "1.234,56")


def test_tekst_van_bestanden():
    tekst = tekst_van_bestanden([("x.xlsx", (DATA / "bellini_dagrapport_2026-10.xlsx").read_bytes()),
                                 ("y.csv", "Datum;Omzet\n1-10;12,50\n".encode())])
    assert "=== BESTAND: x.xlsx ===" in tekst and "Day Report | Bar Bellini" in tekst and "1-10;12,50" in tekst
    with pytest.raises(OmzetFout, match="geen tekst"):
        tekst_van_bestanden([("leeg.txt", b"  ")])


def test_ongeldig_antwoord_geeft_nette_fout():
    antwoord = copy.deepcopy(_opname("ninja"))
    antwoord["periode_tot"] = "eind januari"
    with pytest.raises(OmzetFout, match="einddatum"):
        naar_kassaomzet(antwoord, "", ["x"])
