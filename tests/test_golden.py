"""De MISTO-jaarrekening 2025 (goedgekeurde referentie) moet exact gelijk blijven."""
import pytest
from conftest import DATUM, PDF, XLSX, pdf_tekst

from jaarboek.pipeline import maak_concept, stel_vast


def _watermerk(pad) -> bool:
    import pdfplumber
    with pdfplumber.open(pad) as pdf:
        return any(ch["size"] > 50 for pg in pdf.pages for ch in pg.chars)


@pytest.mark.parametrize("invoer", [XLSX, PDF], ids=["excel", "pdf"])
def test_concept_gelijk_aan_referentie(invoer, klant, kantoor, golden_posten, golden_tekst, tmp_path):
    concept = maak_concept(klant, invoer, DATUM, kantoor, ai=None, uitmap=tmp_path)
    afwijkend = {k: (v, concept.c.get(k)) for k, v in golden_posten.items() if concept.c.get(k) != v}
    assert not afwijkend
    tekst = pdf_tekst(concept.pdf)
    assert len(tekst) == 21
    assert tekst == golden_tekst
    assert concept.status_telling == {"OK": 7, "LET OP": 2, "FOUT": 0}


def test_kerncijfers_zoals_asperion(klant, kantoor, tmp_path):
    c = maak_concept(klant, XLSX, DATUM, kantoor, uitmap=tmp_path).c
    assert (c["mva"], c["mva_inventaris"], c["belastingen"], c["afschrijvingen"]) == (22115, 14849, 12929, 13771)
    assert (c["activa"], c["passiva"], c["ev"], c["resultaat"]) == (61296, 61294, 9938, 58839)
    assert (c["brutomarge"], c["bedrijfsresultaat"], c["lt"], c["kt"]) == (97445, 61063, 15916, 35440)


def test_vaststellen(klant, kantoor, tmp_path):
    concept = maak_concept(klant, XLSX, DATUM, kantoor, uitmap=tmp_path)
    definitief = stel_vast(concept, uitmap=tmp_path)
    assert _watermerk(concept.pdf) and not _watermerk(definitief)
    opgeslagen = klant.jaarrekening(2025)
    assert opgeslagen["status"] == "vastgesteld" and opgeslagen["posten"]["resultaat"] == 58839
    assert klant.jaren() == [2024, 2025]
