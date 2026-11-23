"""Jaarverslagen met meer dan 2 jaarkolommen: het aantal kolommen blijft gelijk,
het nieuwe jaar komt vooraan en de oudste kolom valt weg."""
import json

import pytest
from conftest import BASIS, DATA, DATUM, XLSX, pdf_tekst
from nep_claude import NepClaude

from jaarboek.ai import ClaudeAI
from jaarboek.dossier import laad_kantoor
from jaarboek.eenvoudig import maak_jaarverslag
from jaarboek.onboarding import VRAAG_POSTEN, lees_vorige_jaarrekening
from jaarboek.render import render_pdf
from jaarboek.structuur import labels_voor

JAREN_OUD = [2024, 2023, 2022, 2021, 2020]
LEASE = [("lease_mercedes_lt", "Mercedes-Benz Financial Services"), ("lease_dll_lt", "Leasecontract de Lage Landen")]
# Posten die samen omhoog gaan, zodat alle optellingen blijven kloppen
SCHUIF = ["omzet", "omzet_omzet", "brutomarge", "bedrijfsresultaat", "resultaat",
          "bank_rc", "tegoeden", "liquide", "vlottend", "activa", "kapitaal", "ev", "passiva"]


def _kolommen_oud():
    """Jaarverslag 2024 met 5 jaarkolommen. 2024 en 2023 zijn echt (MISTO); 2022-2020 zijn afgeleid van 2023
    met per jaar een eigen, kloppend verschil, zodat elke kolom herkenbaar is."""
    k24 = json.loads((BASIS / "klanten/misto_interiors/jaarrekening_2024.json").read_text())["posten"]
    uit = json.loads((DATA / "misto_2024_uitlezing.json").read_text())
    k23 = {k: 0 for k in k24}
    for item in uit["posten"]:
        k23[item["post"]] = item["bedragen"][1]
    for kopie, bron in {"debiteuren": "deb_debiteuren", "overige_vorderingen": "ov_vooruitbetalingen",
                        "kruisposten": "krp_stortingen", "ev": "kapitaal", "lt_fin_lease": "lt",
                        "belastingen": "btw_omzetbelasting", "omzet_omzet": "omzet", "inkopen": "kostprijs",
                        "afschr_mva": "afschrijvingen", "rb_bank": "rentebaten", "rl_financieringen": "rentelasten"}.items():
        k23[kopie] = k23[bron]
    k23["lease_mercedes_lt"], k23["lease_dll_lt"] = 15281, 24450
    kolommen = [k24, k23]
    for i in (1, 2, 3):                                   # 2022, 2021, 2020
        k = dict(k23)
        for post in SCHUIF:
            k[post] += i * 1111
        kolommen.append(k)
    return kolommen


@pytest.fixture(scope="module")
def oud_verslag(tmp_path_factory):
    """Rendert het 5-jarige jaarverslag 2024 als PDF + het bijbehorende 'Claude-antwoord'."""
    map = tmp_path_factory.mktemp("vijfjaar")
    kolommen = _kolommen_oud()
    mapping = json.loads((BASIS / "klanten/misto_interiors/mapping.json").read_text())
    klant = json.loads((BASIS / "klanten/misto_interiors/klant.json").read_text())
    meta = {"bedrijf": "MISTO Interiors", "jaar": 2024, "vorig_jaar": 2023, "datum": "16 mei 2025",
            "klant": klant, "kantoor": laad_kantoor(BASIS / "kantoor.json"), "concept": True, "pagina_totaal": 21,
            "lease_lt_posten": [p for p, _ in LEASE], "jaren": JAREN_OUD}
    pdf = render_pdf(kolommen, meta, labels_voor(mapping), map / "jaarverslag_2024_vijf_jaar.pdf")
    antwoord = {
        "bedrijfsnaam": "MISTO Interiors", "jaren": JAREN_OUD, "vestigingsplaats": "Vinkeveen", "kvk": "82376700",
        "posten": [{"post": p, "bedragen": [k.get(p, 0) for k in kolommen]} for p in VRAAG_POSTEN
                   if any(k.get(p, 0) for k in kolommen)],
        "leasecontracten": [{"naam": naam, "langlopend": [k[post] for k in kolommen], "tekst": klant["lease_teksten"][i]}
                            for i, (post, naam) in enumerate(LEASE)],
        "afschrijvingen_tekst": klant["afschrijvingen_tekst"],
    }
    (map / "uitlezing.json").write_text(json.dumps(antwoord))
    return pdf, map / "uitlezing.json", kolommen


def test_vijf_jaar_uitlezen_en_controleren(oud_verslag):
    pdf, uitlezing, kolommen = oud_verslag
    tekst = "".join(pdf_tekst(pdf))
    assert "31-12-2020" in tekst and "2020" in tekst          # 5 kolommen staan echt in de PDF
    inl = lees_vorige_jaarrekening(pdf, ClaudeAI(client=NepClaude(uitlezing=uitlezing)))
    assert inl.jaren == JAREN_OUD
    assert inl.fouten == 0, inl.controles                      # elk bedrag van elke kolom staat in de PDF
    assert sum(1 for c in inl.controles if c["controle"].startswith("Totalen")) == 5


def test_nieuw_verslag_houdt_vijf_kolommen(oud_verslag, kantoor, tmp_path, golden_posten):
    pdf, uitlezing, kolommen = oud_verslag
    res = maak_jaarverslag(pdf, XLSX, DATUM, kantoor, ClaudeAI(client=NepClaude(uitlezing=uitlezing)), tmp_path)
    con = res.concept
    assert con.jaren == [2025, 2024, 2023, 2022, 2021]         # 2020 is weggevallen
    gelijk = {k: v for k, v in golden_posten.items() if not k.startswith("lease_")}   # leaseposten heten hier anders
    assert {k: con.kolommen[0][k] for k in gelijk} == gelijk                       # nieuw jaar = balans 2025
    for i in range(1, 5):                                     # oude kolommen één plek opgeschoven
        assert con.kolommen[i]["omzet"] == kolommen[i - 1]["omzet"]
        assert con.kolommen[i]["activa"] == kolommen[i - 1]["activa"]
    assert con.kolommen[4]["omzet"] == 377097 + 2 * 1111      # 2021
    assert con.status_telling["FOUT"] == 0

    tekst = pdf_tekst(con.pdf)
    alles = "".join(tekst)
    assert "31-12-2021" in alles and "31-12-2020" not in alles and "\n2020" not in alles
    balans = tekst[5]
    assert "31-12-2025 31-12-2024 31-12-2023 31-12-2022 31-12-2021" in balans
    assert "Activa 61.296 113.339 88.213 89.324 90.435" in balans
    wv = tekst[7]
    assert "Omzet 564.686 580.899 377.097 378.208 379.319" in wv

    # Vastgesteld dossier onthoudt 5 kolommen voor volgend jaar
    opgeslagen = res.concept.klant.jaarrekening(2025)
    assert opgeslagen["aantal_jaren"] == 5
    assert [v["jaar"] for v in opgeslagen["vergelijkend"]] == [2024, 2023, 2022, 2021]
