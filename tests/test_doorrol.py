"""Doorrollen van een willekeurig jaarrapport van het kantoor (hier: Van Manen Beheer 2 BV, met
5-jarige meerjarenoverzichten) met de nieuwe Asperion-balans.

De Claude-stap wordt afgespeeld uit een opname van een echte run (tests/data/vanmanen_koppelingen_claude.json),
zodat de test realistisch is en niets kost.
"""
import pytest
from conftest import DATA, DATUM
from nep_claude import NepClaude

from jaarboek.ai import ClaudeAI
from jaarboek.doorrol import lijkt_op, rol_door
from jaarboek.rapportlezer import bedrag, lees_rapport

OUD = DATA / "vanmanen_jaarverslag_2024.pdf"
BALANS = DATA / "vanmanen_balans_2025.pdf"
OPNAME = DATA / "vanmanen_koppelingen_claude.json"

# Handberekening 2025 uit de Asperion-balans (onafhankelijk van de code)
RES = -1982
RESERVE = -32289 + RES
EV = 1 + RESERVE
VERWACHT = {
    "Algemene kosten": 599, "Totaal kosten": 599, "Bedrijfsresultaat": -599, "Financiële baten en lasten": -1383,
    "Resultaat voor belasting": RES, "Resultaat deelnemingen": 0, "Resultaat na belasting": RES,
    "Financiële vaste activa": 10000, "Vaste activa": 10000, "Activa": 10000, "Aandelenkapitaal": 1,
    "Overige reserves": RESERVE, "Onverdeelde winst": 0, "Eigen vermogen": EV, "Langlopende schulden": 29039,
    "Kortlopende schulden": 15230, "Passiva": EV + 29039 + 15230, "Resultaat voor belastingen": RES,
    "Belastbare winst": RES, "Belastbaar bedrag": RES, "Leningen u/g (langlopend)": 10000,
    "Lening Poesiat & Kater Holding B.V.": 10000, "Overige reserve": RESERVE, "Geldlening Eymert van Manen": 29039,
    "Nog te betalen administratiekosten": 1000, "Administratiekosten": 599, "Overige rentelasten": 1383,
}


def test_lezer_herkent_opbouw():
    r = lees_rapport(OUD)
    assert (len(r.paginas), r.verslagjaar, r.bedrijf) == (15, 2024, "E. van Manen Beheer 2 B.V.")
    meerjaren = [t for t in r.tabellen if "meerjaren" in t.kop]
    assert [t.jaren for t in meerjaren] == [[2024, 2023, 2022, 2021, 2020]] * 2
    assert sum(len(t.rijen) for t in r.tabellen) == 60
    assert all(p.afbeeldingen for p in r.paginas)               # logo op elke pagina


def test_lezer_misto():
    r = lees_rapport(DATA / "misto_jaarrekening_2024.pdf")
    assert (len(r.paginas), r.verslagjaar, sum(len(t.rijen) for t in r.tabellen)) == (21, 2024, 110)
    mva = next(t for t in r.tabellen if t.kop == "Materiële vaste activa")
    assert mva.rijen[1].label == "Automobielen en overige transportmiddelen"   # afgebroken omschrijving samengevoegd


def test_omschrijving_lijkt():
    assert lijkt_op("Financiële vaste activa", "Totaal FINANCIELE VASTE AKTIVA (balans)")
    assert lijkt_op("Geldlening Eymert van Manen", "500 geldlening eymert van manen (Onderhandse leningen)")
    assert not lijkt_op("Passiva", "Totaal FINANCIELE VASTE AKTIVA (balans)")


@pytest.fixture(scope="module")
def doorrol(tmp_path_factory, ):
    from jaarboek.dossier import laad_kantoor
    from conftest import BASIS
    nep = NepClaude(opname=OPNAME)
    d = rol_door(OUD, BALANS, DATUM, laad_kantoor(BASIS / "kantoor.json"), ClaudeAI(client=nep),
                 tmp_path_factory.mktemp("vanmanen"), instructies="De lening van de DGA loopt door tot 2030.")
    d._nep = nep
    return d


def test_alle_regels_bewezen_en_controles(doorrol):
    assert all(u.status == "OK" for u in doorrol.uitkomsten.values())
    status = {k["controle"]: k["status"] for k in doorrol.controles}
    assert status["Tabellen tellen op (zoals in het oude rapport)"] == "OK"
    assert status["Dezelfde regel heeft overal hetzelfde bedrag"] == "OK"
    assert status["Elke Asperion-rekening komt in het rapport terug"] == "OK"
    assert status["Balanstotaal activa en passiva"] == "LET OP"       # 10.000 vs 9.999: afronding Asperion
    assert sum(1 for u in doorrol.uitkomsten.values() if u.bron == "code") >= 30
    taken = [kw["output_config"]["format"]["schema"]["properties"] for kw in doorrol._nep.verzoeken]
    assert len(taken) == 2                                          # één koppelverzoek + aandachtspunten


def test_nieuw_rapport_klopt_en_schuift_op(doorrol):
    """Lees het NIEUWE rapport opnieuw in (zoals volgend jaar) en reken alles na."""
    oud, nieuw = lees_rapport(OUD), lees_rapport(doorrol.pdf)
    assert len(nieuw.paginas) == 15 and nieuw.verslagjaar == 2025
    nagerekend = verschoven = 0
    for to, tn in zip(oud.tabellen, nieuw.tabellen):
        assert (to.kop, len(to.cellen), len(to.rijen)) == (tn.kop, len(tn.cellen), len(tn.rijen))
        assert tn.jaren == [j + 1 for j in to.jaren]
        for ro, rn in zip(to.rijen, tn.rijen):
            for cel, tekst in ro.waarden.items():                 # oude kolommen één jaargroep opgeschoven
                if cel + to.groep < len(to.cellen):
                    assert rn.waarden.get(cel + to.groep) == tekst
                    verschoven += 1
            waarde = next(bedrag(rn.waarden[c]) for c in range(to.groep) if c in rn.waarden)
            if ro.label == "Overige schulden":
                verwacht = 15230 if to.kop == "Kortlopende schulden" else 14230
            elif ro.label:
                verwacht = VERWACHT[ro.label]
            else:
                verwacht = waarde          # totaalregels: gedekt door de optelcontrole
            assert waarde == verwacht, (to.kop, ro.label)
            nagerekend += 1
    assert (nagerekend, verschoven) == (60, 108)


def test_teksten_en_datums(doorrol):
    from conftest import pdf_tekst
    t = pdf_tekst(doorrol.definitief)
    assert t[0].splitlines()[0] == "Jaarrapport 2025"
    assert "1 januari 2025 - 31 december 2025" in t[0] and "Gegenereerd op 16 november 2026" in t[0]
    assert "balans per 31 december 2025" in t[3] and "Zwanenburg, 16 november 2026" in t[3]
    assert "2020" not in "".join(t)                               # oudste jaar is weggevallen
    assert "Resultaat deelnemingen 0 0 0 0 0" in t[4]             # het -6.111 uit 2020 is weg


def test_extra_instructies_gaan_mee_naar_claude(doorrol):
    import json
    for kw in doorrol._nep.verzoeken:                              # koppelen + aandachtspunten
        invoer = json.loads(kw["messages"][0]["content"])
        assert invoer["extra_instructies"] == "De lening van de DGA loopt door tot 2030."
        assert "extra_instructies" in str(kw["system"])
    assert "> De lening van de DGA loopt door tot 2030." in doorrol.rapport_md.read_text(encoding="utf-8")


def test_inhoudsopgave_is_klikbaar(doorrol):
    from pypdf import PdfReader
    for pdf in (doorrol.pdf, doorrol.definitief):
        r = PdfReader(str(pdf))
        doelen = {}
        for a in r.pages[1]["/Annots"]:
            a = a.get_object()
            doelen[str(a["/Dest"])] = r.get_destination_page_number(r.named_destinations[str(a["/Dest"])]) + 1
        assert doelen == {f"/p{n}": n for n in (3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 14)}
        assert all("/Annots" not in pg for pg in r.pages[2:])     # alleen de inhoudsopgave heeft links


def test_zonder_claude_duidelijke_melding(tmp_path):
    from jaarboek.doorrol import DoorrolFout
    with pytest.raises(DoorrolFout, match="Claude"):
        rol_door(OUD, BALANS, DATUM, {}, None, tmp_path)


def test_andere_klant_wordt_geweigerd(tmp_path):
    from jaarboek.doorrol import DoorrolFout
    with pytest.raises(DoorrolFout, match="Misto Interiors"):
        rol_door(OUD, DATA / "misto_balans_2025.xlsx", DATUM, {}, None, tmp_path)


def test_misto_via_sjabloon_gelijk_aan_goedgekeurd(tmp_path, golden_posten):
    """MISTO met het oude jaarverslag als sjabloon (echte Claude-formules afgespeeld) = goedgekeurde cijfers."""
    from conftest import BASIS
    from jaarboek.dossier import laad_kantoor
    d = rol_door(DATA / "misto_jaarrekening_2024.pdf", DATA / "misto_balans_2025.xlsx", DATUM,
                 laad_kantoor(BASIS / "kantoor.json"), ClaudeAI(client=NepClaude(opname=DATA / "misto_koppelingen_claude.json")),
                 tmp_path)
    assert sum(1 for u in d.uitkomsten.values() if u.status == "OK") == 107
    assert {u.melding for u in d.uitkomsten.values() if u.status != "OK"} == {"niet te controleren op vorig jaar (aflossing)"}
    per_label = {u.rij.label: u.nieuw for u in d.uitkomsten.values() if u.rij.label}
    assert per_label["Bedrijfsresultaat"] == golden_posten["bedrijfsresultaat"] == 61063
    assert per_label["Langlopende schulden"] == golden_posten["lt"]
    assert per_label["Financial leaseverplichtingen"] == golden_posten["kt_fin_lease"] == 8165
    assert per_label["Mercedes-Benz Financial Services"] == 7124 and per_label["Leasecontract de Lage Landen"] == 8792
    assert d.pdf.name == "MISTO Interiors Jaarrekening 2025 (concept).pdf"
    status = {k["controle"]: k["status"] for k in d.controles}
    assert status["Dezelfde regel heeft overal hetzelfde bedrag"] == "OK"
    assert status["Regels die in alle kolommen nihil zijn"] == "LET OP"
