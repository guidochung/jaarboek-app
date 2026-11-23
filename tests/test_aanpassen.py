"""Aanpassen via de chat na het maken: Claude stelt voor, de code bewijst, controleert en zet opnieuw op."""
import pytest
from conftest import BASIS, DATA, DATUM, pdf_tekst
from nep_claude import NepClaude

from jaarboek.ai import ClaudeAI
from jaarboek.dossier import laad_kantoor
from jaarboek.doorrol import DoorrolFout, pas_aan, rol_door
from jaarboek.rapportlezer import lees_rapport

OUD = DATA / "vanmanen_jaarverslag_2024.pdf"
BALANS = DATA / "vanmanen_balans_2025.pdf"


@pytest.fixture()
def gemaakt(tmp_path):
    nep = NepClaude(opname=DATA / "vanmanen_koppelingen_claude.json")
    ai = ClaudeAI(client=nep)
    d = rol_door(OUD, BALANS, DATUM, laad_kantoor(BASIS / "kantoor.json"), ai, tmp_path)
    return d, ai, nep


def _rij(d, label):
    return next(i for i, u in d.uitkomsten.items() if u.rij.label == label)


def test_tekst_aanpassen_komt_in_het_rapport(gemaakt):
    d, ai, nep = gemaakt
    item = pas_aan(d, 'vervang "Zwanenburg" door "Amsterdam"', ai)
    assert item["doorgevoerd"] == ['p. 4: "Zwanenburg, 16 november 2026" → "Amsterdam, 16 november 2026"']
    for pdf in (d.pdf, d.definitief):
        t = pdf_tekst(pdf)[3]
        assert "Amsterdam, 16 november 2026" in t and "Zwanenburg" not in t
    nieuw = lees_rapport(d.pdf)                               # blijft volgend jaar weer in te lezen
    assert sum(len(t.rijen) for t in nieuw.tabellen) == 60
    md = d.rapport_md.read_text(encoding="utf-8")
    assert "## Aanpassingen via de chat" in md and "Amsterdam" in md
    assert nep.verzoeken[-1]["output_config"]["format"]["schema"]["properties"].keys() >= {"antwoord", "formules"}
    assert d.gebruik[-1].taak == "aanpassen"


def test_formule_die_niet_klopt_wordt_geweigerd(gemaakt):
    d, ai, nep = gemaakt
    adm = _rij(d, "Administratiekosten")
    voor = (d.uitkomsten[adm].termen, d.uitkomsten[adm].nieuw)
    nep.aanpassing = {"antwoord": "Aangepast.", "teksten": [],
                      "formules": [{"rij": adm, "termen": [{"bron": "resultaat", "naam": "", "teken": 1}],
                                    "toelichting": "fout"}]}
    pdf_tijd = d.pdf.stat().st_mtime
    item = pas_aan(d, "zet administratiekosten op het resultaat", ai)
    assert item["doorgevoerd"] == [] and "Administratiekosten (p. 14)" in item["geweigerd"][0]
    assert (d.uitkomsten[adm].termen, d.uitkomsten[adm].nieuw) == voor
    assert d.uitkomsten[adm].status == "OK" and d.pdf.stat().st_mtime == pdf_tijd   # niets opnieuw opgemaakt


def test_formule_die_klopt_wordt_doorgevoerd(gemaakt):
    d, ai, nep = gemaakt
    adm = _rij(d, "Administratiekosten")
    nep.aanpassing = {"antwoord": "Via de groep.", "teksten": [],
                      "formules": [{"rij": adm, "termen": [{"bron": "groep", "naam": "wv|Algemene kosten", "teken": 1}],
                                    "toelichting": "groep algemene kosten"}]}
    item = pas_aan(d, "gebruik de groep algemene kosten", ai)
    u = d.uitkomsten[adm]
    assert (u.bron, u.status, u.nieuw) == ("chat", "OK", 599)
    assert item["doorgevoerd"] == ["Administratiekosten (p. 14): formule aangepast, bedrag gelijk"]


def test_verzonnen_bedrag_of_te_lange_tekst_geweigerd(gemaakt):
    d, ai, nep = gemaakt
    nep.aanpassing = lambda inv: {"antwoord": "x", "formules": [], "teksten": [
        {"regel": next(r for r, p, t in inv["tekstregels"] if t.startswith("Zwanenburg")),
         "tekst": "Het resultaat is 99.999 euro."},
        {"regel": next(r for r, p, t in inv["tekstregels"] if t.startswith("R.A. de Wit")), "tekst": "lang " * 60}]}
    item = pas_aan(d, "test", ai)
    assert item["doorgevoerd"] == []
    assert "99.999 komt niet uit de cijfers" in item["geweigerd"][0] and "past niet" in item["geweigerd"][1]
    assert d.tekstwijzigingen == {}


def test_zonder_claude_duidelijke_melding(gemaakt):
    d, _, _ = gemaakt
    with pytest.raises(DoorrolFout, match="Claude"):
        pas_aan(d, "iets", None)
