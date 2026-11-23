"""Nieuwe klant: oude jaarrekening uitlezen, controleren, opslaan en direct een concept maken."""
import json

from conftest import DATA, DATUM, JR2024_PDF, XLSX, pdf_tekst
from nep_claude import NepClaude

from jaarboek.ai import ClaudeAI
from jaarboek.onboarding import bewaar_als_klant, hercontroleer, lees_vorige_jaarrekening
from jaarboek.onboarding import pdf_tekst as jr_tekst
from jaarboek.pipeline import maak_concept
from jaarboek.structuur import ALLE_POSTEN


def test_tekst_zonder_watermerk():
    t = jr_tekst(JR2024_PDF)
    assert "Grondslag van materiële vaste activa" in t       # in de ruwe tekst staat 'materiOële' (watermerk)
    assert "toerekenbare kosten" in t and "Pagina 21 van 21" in t
    assert len(t) < 20000                                    # ca. 4-5K tokens i.p.v. een volledige PDF


def test_inlezen_en_controleren(klanten_root):
    nep = NepClaude()
    inl = lees_vorige_jaarrekening(JR2024_PDF, ClaudeAI(client=nep))
    assert nep.verzoeken[0]["output_config"]["effort"] == "medium"
    assert "document" not in json.dumps(nep.verzoeken[0]["messages"])     # tekst gestuurd, geen PDF
    assert (inl.naam, inl.jaar, inl.gegevens["kvk"]) == ("MISTO Interiors", 2024, "82376700")
    assert inl.fouten == 0, inl.controles
    vj = json.loads((klanten_root / "misto_interiors" / "jaarrekening_2024.json").read_text())["posten"]
    assert {k: inl.posten[k] for k in ALLE_POSTEN} == {k: vj.get(k, 0) for k in ALLE_POSTEN}
    # Ook de tweede jaarkolom (2023) is uitgelezen en gecontroleerd
    assert inl.jaren == [2024, 2023] and len(inl.kolommen) == 2
    k23 = inl.kolommen[1]
    assert (k23["activa"], k23["resultaat"], k23["verk_relatiegeschenken"], k23["lt"]) == (88213, 52580, -181, 39731)
    assert any(c["controle"] == "Totalen 2023 tellen op (marge 2 euro)" and c["status"] == "OK" for c in inl.controles)


def test_verzonnen_bedrag_wordt_afgevangen(tmp_path, monkeypatch):
    data = json.loads((DATA / "misto_2024_uitlezing.json").read_text())
    for p in data["posten"]:
        if p["post"] == "omzet":
            p["bedragen"][0] = 580898                             # 1 euro naast de PDF (verslagjaar)
        if p["post"] == "crediteuren":
            p["bedragen"][1] = 16267                              # cijfers verwisseld (vorig jaar)
    nep_bestand = tmp_path / "misto_2024_uitlezing.json"
    nep_bestand.write_text(json.dumps(data))
    import nep_claude
    monkeypatch.setattr(nep_claude, "DATA", tmp_path)
    inl = lees_vorige_jaarrekening(JR2024_PDF, ClaudeAI(client=NepClaude()))
    assert inl.fouten >= 1
    assert "580.898" in inl.controles[0]["detail"] and "Crediteuren 2023 (16.267)" in inl.controles[0]["detail"]
    inl.posten["omzet"] = 580899                                  # handmatig gecorrigeerd in de app
    inl.kolommen[1]["crediteuren"] = 16276
    hercontroleer(inl)
    assert inl.fouten == 0


def test_nieuwe_klant_levert_zelfde_jaarrekening(tmp_path, kantoor, golden_tekst):
    """Volledige route: oude PDF uitlezen -> klant opslaan -> concept 2025 = goedgekeurde referentie."""
    root = tmp_path / "klanten"
    inl = lees_vorige_jaarrekening(JR2024_PDF, ClaudeAI(client=NepClaude()))
    klant = bewaar_als_klant(inl, root=root)
    lease = {spec["label"]: post for post, spec in klant.mapping["leasecontracten"].items()}
    nep = NepClaude(koppel={"502": lease["Mercedes-Benz Financial Services"],
                            "825": lease["Leasecontract de Lage Landen"]})
    concept = maak_concept(klant, XLSX, DATUM, kantoor, ai=ClaudeAI(client=nep), uitmap=tmp_path / "uit")
    assert sorted(k["nr"] for k in concept.ai_koppelingen) == ["502", "825"]
    assert pdf_tekst(concept.pdf) == golden_tekst
    assert concept.status_telling["FOUT"] == 0
