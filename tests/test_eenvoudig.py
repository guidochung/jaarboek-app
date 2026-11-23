"""Eén stap: oud jaarverslag + nieuwe balans -> nieuw jaarverslag."""
import pytest
from conftest import DATUM, JR2024_PDF, PDF, XLSX, pdf_tekst
from nep_claude import NepClaude

from jaarboek.ai import ClaudeAI
from jaarboek.eenvoudig import JaarverslagFout, maak_jaarverslag


def _watermerk(pad) -> bool:
    import pdfplumber
    with pdfplumber.open(pad) as pdf:
        return any(ch["size"] > 50 for pg in pdf.pages for ch in pg.chars)


@pytest.mark.parametrize("balans", [XLSX, PDF], ids=["excel", "pdf"])
def test_oud_plus_balans_geeft_referentie(balans, kantoor, golden_tekst, tmp_path):
    nep = NepClaude()  # koppelt de leaserekeningen 502/825 zelf op naam
    res = maak_jaarverslag(JR2024_PDF, balans, DATUM, kantoor, ClaudeAI(client=nep), tmp_path)
    assert pdf_tekst(res.concept.pdf) == golden_tekst
    assert _watermerk(res.concept.pdf) and not _watermerk(res.definitief)
    assert sorted(k["nr"] for k in res.concept.ai_koppelingen) == ["502", "825"]
    assert res.concept.status_telling["FOUT"] == 0
    taken = [kw["output_config"]["format"]["schema"]["properties"] for kw in nep.verzoeken]
    assert len(taken) == 3   # uitlezen, koppelvoorstellen, aandachtspunten


def test_hergebruik_uitgelezen_jaarverslag(kantoor, tmp_path):
    nep = NepClaude()
    ai = ClaudeAI(client=nep)
    eerste = maak_jaarverslag(JR2024_PDF, XLSX, DATUM, kantoor, ai, tmp_path / "a")
    n = len(nep.verzoeken)
    maak_jaarverslag(JR2024_PDF, XLSX, DATUM, kantoor, ai, tmp_path / "b", inlezing=eerste.inlezing)
    assert len(nep.verzoeken) - n == 2   # geen nieuw uitleesverzoek


def test_verkeerd_jaar(kantoor, tmp_path):
    from jaarboek.onboarding import lees_vorige_jaarrekening
    inl = lees_vorige_jaarrekening(JR2024_PDF, ClaudeAI(client=NepClaude()))
    inl.jaar = 2023
    with pytest.raises(JaarverslagFout, match="2023"):
        maak_jaarverslag(JR2024_PDF, XLSX, DATUM, kantoor, ClaudeAI(client=NepClaude()), tmp_path, inlezing=inl)
