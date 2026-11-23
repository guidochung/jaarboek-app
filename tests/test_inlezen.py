"""PDF- en Excel-invoer moeten exact hetzelfde opleveren; foute invoer geeft een duidelijke melding."""
import pytest
from conftest import JR2024_PDF, PDF, XLSX

from jaarboek.inlezen import InvoerFout, _verwerk_rijen, boekjaar, lees_asperion


def _zonder_namen(asp):
    return {**asp, "rekeningen": {nr: {k: v for k, v in r.items() if k != "naam"} for nr, r in asp["rekeningen"].items()}}


def test_pdf_en_excel_gelijk():
    x, p = lees_asperion(XLSX), lees_asperion(PDF)
    assert _zonder_namen(x) == _zonder_namen(p)
    assert len(x["rekeningen"]) == 63 and len(x["groepen"]) == 27 and len(x["secties"]) == 11
    assert boekjaar(x) == 2025
    assert x["resultaat"] == {"balans": {"huidig": 58839, "vorig": 56765}, "wv": {"huidig": 58839, "vorig": 56765}}


def test_credit_bedrag_in_pdf_juiste_kolom():
    # 4390 overige algemene kosten staat in de CREDIT-kolom (-7); in de PDF alleen aan de positie te zien
    assert lees_asperion(PDF)["rekeningen"]["4390"]["huidig"] == -7


def test_verlies_krijgt_minteken():
    rijen = [
        [None, None, None, "Periode 0-2025 t/m 12-2025", None, None, "Periode 0-2024 t/m 12-2024", None],
        [None, None, "Resultaat ----- verlies", 1500, None, None, None, 200],   # balans: verlies debet, vorig winst
        ["Winst- en Verliesrekening"] + [None] * 7,
        [None, None, "Resultaat ----- verlies", None, 1500, None, 200, None],   # W&V: verlies credit
    ]
    asp = _verwerk_rijen(rijen)
    assert asp["resultaat"]["balans"] == {"huidig": -1500, "vorig": 200}
    assert asp["resultaat"]["wv"] == {"huidig": -1500, "vorig": 200}


def test_verkeerd_bestand(tmp_path):
    with pytest.raises(InvoerFout):
        lees_asperion(JR2024_PDF)              # een jaarrekening, geen Asperion-balans
    tekst = tmp_path / "x.csv"
    tekst.write_text("a,b")
    with pytest.raises(InvoerFout):
        lees_asperion(tekst)
