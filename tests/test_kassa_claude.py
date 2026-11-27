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
from jaarboek.omzet import (OmzetFout, asperion_weergave, koppeling_uit_json, maak_boeking, naar_fmutac,
                            stel_koppeling_voor)

D = Decimal
NINJA = DATA / "ninja_boekhouding_2026-01.pdf"
KAZOKU = DATA / "kazoku_omzetlijst_2026-10.pdf"


def _opname(klant):
    return json.loads((DATA / f"kassa_claude_{klant}_opname.json").read_text(encoding="utf-8"))


def _opname_pad(naam):
    return json.loads((DATA / naam).read_text(encoding="utf-8"))


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


def test_cafe_bica_zoals_de_boekhouder_het_wil():
    """booq-rapport: omzet per tarief (basisbedrag + btw), cadeaubonnen op 1259, al het andere op 1255.

    Fooi, uitgegeven cadeaukaarten en statiegeld worden niet apart geboekt: ze zitten in het verschil op 1255.
    Het antwoord is met de hand gemaakt volgens de instructie (geen opname van een echte run).
    """
    pdf = DATA / "bica_omzet_2026-10.pdf"
    kassa = lees_met_claude([(pdf.name, pdf.read_bytes())],
                            ClaudeAI(client=NepClaude(kassa=_opname_pad("kassa_claude_bica_voorbeeld.json"))))
    b = _boeking(kassa, "cafe_bica")
    status = {c["controle"]: c["status"] for c in b.controles}
    assert not b.fout and status["Bedragen staan in het rapport"] == "OK"
    assert "Door Claude uitgerekende bedragen" not in status            # incl. = basisbedrag + btw uit het rapport
    assert b.verschil == D("98.45") and status["Niet geboekte posten in het rapport"] == "LET OP"
    weergave = Counter((r["rekening"], r["debet"], r["credit"], r["btw"]) for r in asperion_weergave(b))
    assert weergave == Counter([("8100", D(0), D("55224.61"), D("4970.21")), ("8000", D(0), D("766.67"), D("161.00")),
                                ("1259", D("235.21"), D(0), D(0)), ("1255", D("60887.28"), D(0), D(0))])


def test_v_pay_is_een_pinbetaling():
    from jaarboek.omzet import soort_betaling
    assert {soort_betaling(n) for n in ("V-pay", "V PAY", "vpay", "Visa Debit", "Eurocard Mastercard")} == {"pin"}
    assert soort_betaling("DIsh Giftcard") == "cadeaubonnen" and soort_betaling("Klearly contant") == "contant"


def test_claude_per_categorie_zonder_tarief_is_gelijk_aan_de_boekhouder():
    """Geeft het rapport geen tarief per categorie, dan levert Claude de categorieën zonder tarief; de koppeling
    (vast tarief + restpost) maakt er dezelfde boeking van als de vaste booq-lezer."""
    pdf = DATA / "bica_omzet_2026-10.pdf"
    nep = NepClaude(kassa=_opname_pad("kassa_claude_bica_categorieen.json"))
    kassa = lees_met_claude([(pdf.name, pdf.read_bytes())], ClaudeAI(client=nep))
    schema = nep.verzoeken[0]["output_config"]["format"]["schema"]
    assert {"type": "null"} in schema["properties"]["omzet"]["items"]["properties"]["tarief"]["anyOf"]
    assert all(o.tarief is None for o in kassa.omzet)
    b = _boeking(kassa, "cafe_bica")
    assert not b.fout, [c for c in b.controles if c["status"] == "FOUT"]
    assert naar_fmutac(b) == (DATA / "bica_omzet_2026-10_verwacht.csv").read_bytes()


def _foto(pdf, schaal=3.0, formaat="JPEG") -> bytes:
    """'Foto' van een rapport: de PDF-pagina als afbeelding (groter dan Claude nodig heeft)."""
    import io

    import pypdfium2
    buf = io.BytesIO()
    pypdfium2.PdfDocument(str(pdf))[0].render(scale=schaal).to_pil().convert("RGB").save(buf, formaat)
    return buf.getvalue()


def test_foto_van_een_kassarapport_gaat_als_afbeelding_naar_claude():
    import base64
    import io

    from PIL import Image

    from jaarboek.kassalezers import lees_omzet
    from jaarboek.omzet import OnbekendFormaat
    foto = _foto(DATA / "bica_omzet_2026-10.pdf")
    with pytest.raises(OnbekendFormaat, match="foto"):
        lees_omzet([("bica.jpg", foto)])                  # geen vaste lezer: Claude leest foto's
    nep = NepClaude(kassa=_opname_pad("kassa_claude_bica_categorieen.json"))
    kassa = lees_met_claude([("bica.jpg", foto)], ClaudeAI(client=nep))
    blokken = nep.verzoeken[0]["messages"][0]["content"]
    assert [b["type"] for b in blokken] == ["text", "image", "text"]
    beeld = blokken[1]["source"]
    assert beeld["media_type"] == "image/jpeg"
    assert max(Image.open(io.BytesIO(base64.b64decode(beeld["data"]))).size) <= 1568     # verkleind
    b = _boeking(kassa, "cafe_bica")
    status = {c["controle"]: c["status"] for c in b.controles}
    assert not b.fout and status["Gelezen van een foto"] == "LET OP"
    assert "Bedragen staan in het rapport" not in status            # niet na te lezen in een foto
    assert naar_fmutac(b) == (DATA / "bica_omzet_2026-10_verwacht.csv").read_bytes()


def test_gedraaide_telefoonfoto_wordt_rechtgezet():
    import base64
    import io

    from PIL import Image

    from jaarboek.kassa_claude import foto_blok, is_foto
    buf, exif = io.BytesIO(), Image.Exif()
    exif[0x0112] = 6                                       # 'staand gefotografeerd': 90 graden gedraaid
    Image.new("RGB", (300, 100), "white").save(buf, "JPEG", exif=exif)
    blok = foto_blok(buf.getvalue(), "telefoon.jpg")
    assert Image.open(io.BytesIO(base64.b64decode(blok["source"]["data"]))).size == (100, 300)
    png = _foto(DATA / "bica_omzet_2026-10.pdf", schaal=0.5, formaat="PNG")
    assert is_foto(buf.getvalue()) and is_foto(png) and not is_foto(b"%PDF-1.7")
    with pytest.raises(OmzetFout, match="kan deze foto niet openen"):
        foto_blok(b"\xff\xd8\xff kapot", "kapot.jpg")
